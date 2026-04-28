"""``LocalManager`` —— DuckDB 后端实现。

::

    publish_weights 流水线:
        ┌────────────────┐
        │ heartbeat(pre) │
        └───────┬────────┘
                ↓
        ┌──────────────────────┐
        │ filter dt > latest   │   ── 仅追加(对齐 cwc.publish_weights)
        └───────┬──────────────┘
                ↓
        ┌──────────────────────────┐
        │ drop_duplicates + sort   │
        └───────┬──────────────────┘
                ↓
        ┌──────────────────────────┐
        │ batch insert + heartbeat │ × N batches
        └──────────────────────────┘

设计文档"五、连接配置 5.1 LocalManager"。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import loguru
import pandas as pd

from wmr.base import VALID_STATUSES, BaseManager
from wmr.utils import (
    DEFAULT_TZ,
    _ensure_series_tz,
    _localize_dataframe_columns,
    _resolve_verbose,
    _series_to_naive,
    _to_naive,
    _truncate_seq,
)

# `_to_naive` / `_series_to_naive` 已移至 ``wmr.utils``,此处仅以 re-export 形式
# 保留 ``from wmr.local import _to_naive`` 的内部使用路径(下划线开头属于私有 API,
# 不作公开兼容承诺;不列入 ``__all__``,``from wmr.local import *`` 不会导出它们)。
__all__ = ["DEFAULT_LOCAL_DB_PATH", "LocalManager"]

DEFAULT_LOCAL_DB_PATH: str = os.environ.get("WMR_LOCAL_DB_PATH", str(Path.home() / ".wmr" / "weights.duckdb"))
"""LocalManager 默认 db 路径,可通过 ``WMR_LOCAL_DB_PATH`` 环境变量覆盖。"""


class LocalManager(BaseManager):
    """基于 DuckDB 的本地策略持仓权重管理器。

    适用场景:本地开发、单机分析、CI 测试。

    Attributes:
        _db_path: DuckDB 文件路径(支持 ``:memory:`` 与磁盘文件)。
        _read_only: 是否以只读模式打开。
        _conn: DuckDB 连接对象,首次 ``connect()`` 后建立。
    """

    def __init__(
        self,
        db_path: str | None = None,
        read_only: bool = False,
        logger: Any = loguru.logger,
        tz: ZoneInfo = DEFAULT_TZ,
        *,
        verbose: bool | None = None,
    ) -> None:
        """初始化 LocalManager。

        Args:
            db_path: DuckDB 文件路径。``None`` 时回落到 ``WMR_LOCAL_DB_PATH``
                env 或 ``~/.wmr/weights.duckdb``;``:memory:`` 表示内存模式。
            read_only: 是否以只读模式打开,默认 False。
            logger: 日志器,默认 loguru.logger。
            tz: 时区,默认 ``Asia/Shanghai``。
            verbose: 是否输出详细执行过程日志(连接、SQL 摘要、批次进度等)。
                ``None``(默认)时读取环境变量 ``WMR_VERBOSE``,识别
                ``1`` / ``true`` / ``yes`` / ``on`` 为真,其它一律 ``False``。
                详见 ``docs/verbose-mode.md``。
        """
        if db_path is None:
            db_path = DEFAULT_LOCAL_DB_PATH
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path: str = db_path
        self._read_only: bool = read_only
        self._logger = logger
        self._tz: ZoneInfo = tz
        self._verbose: bool = _resolve_verbose(verbose)
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._vlog(f"LocalManager 创建: db_path={self._db_path}, read_only={self._read_only}, tz={tz}")

    # ---------- 生命周期 ----------
    def connect(self) -> duckdb.DuckDBPyConnection:
        """建立或复用 DuckDB 连接。"""
        if self._conn is None:
            self._vlog(f"打开 DuckDB 连接: {self._db_path} (read_only={self._read_only})")
            self._conn = duckdb.connect(self._db_path, read_only=self._read_only)
        return self._conn

    def close(self) -> None:
        """关闭 DuckDB 连接。重复调用安全。"""
        if self._conn is not None:
            self._vlog(f"关闭 DuckDB 连接: {self._db_path}")
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """惰性建立的 DuckDB 连接。"""
        return self.connect()

    def initialize(self) -> None:
        """创建 4 张表与 3 个视图。允许重复调用。"""
        c = self.conn
        for table_name, ddl in (
            (
                "metas",
                """
                CREATE TABLE IF NOT EXISTS metas (
                    strategy VARCHAR PRIMARY KEY,
                    base_freq VARCHAR,
                    description VARCHAR,
                    author VARCHAR,
                    outsample_sdt TIMESTAMP,
                    create_time TIMESTAMP,
                    update_time TIMESTAMP,
                    heartbeat_time TIMESTAMP,
                    weight_type VARCHAR,
                    status VARCHAR DEFAULT '实盘',
                    memo VARCHAR
                )
                """,
            ),
            (
                "weights",
                """
                CREATE TABLE IF NOT EXISTS weights (
                    dt TIMESTAMP,
                    symbol VARCHAR,
                    weight DOUBLE,
                    strategy VARCHAR,
                    update_time TIMESTAMP,
                    PRIMARY KEY (strategy, dt, symbol)
                )
                """,
            ),
            (
                "returns",
                """
                CREATE TABLE IF NOT EXISTS returns (
                    dt TIMESTAMP,
                    symbol VARCHAR,
                    returns DOUBLE,
                    strategy VARCHAR,
                    update_time TIMESTAMP,
                    PRIMARY KEY (strategy, dt, symbol)
                )
                """,
            ),
            (
                "tags",
                """
                CREATE TABLE IF NOT EXISTS tags (
                    strategy VARCHAR,
                    tag VARCHAR,
                    creator VARCHAR DEFAULT 'system',
                    create_time TIMESTAMP,
                    PRIMARY KEY (strategy, tag)
                )
                """,
            ),
        ):
            c.execute(ddl)
            self._vlog(f"创建/复用表: {table_name}")
        c.execute(
            """
            CREATE OR REPLACE VIEW cs_latest_weights AS
            WITH latest_dates AS (
                SELECT strategy, MAX(dt) AS latest_dt FROM weights GROUP BY strategy
            )
            SELECT w.dt, w.symbol, w.weight, w.strategy, w.update_time
            FROM weights w
            JOIN latest_dates ld ON w.strategy = ld.strategy AND w.dt = ld.latest_dt
            JOIN metas m ON w.strategy = m.strategy
            WHERE m.weight_type = 'cs'
            """
        )
        self._vlog("创建/复用视图: cs_latest_weights")
        c.execute(
            """
            CREATE OR REPLACE VIEW ts_latest_weights AS
            WITH latest_records AS (
                SELECT strategy, symbol, MAX(dt) AS latest_dt
                FROM weights GROUP BY strategy, symbol
            )
            SELECT w.dt, w.symbol, w.weight, w.strategy, w.update_time
            FROM weights w
            JOIN latest_records lr
              ON w.strategy = lr.strategy AND w.symbol = lr.symbol AND w.dt = lr.latest_dt
            JOIN metas m ON w.strategy = m.strategy
            WHERE m.weight_type = 'ts'
            """
        )
        self._vlog("创建/复用视图: ts_latest_weights")
        c.execute(
            """
            CREATE OR REPLACE VIEW latest_weights AS
            SELECT * FROM ts_latest_weights
            UNION ALL
            SELECT * FROM cs_latest_weights
            """
        )
        self._vlog("创建/复用视图: latest_weights")
        self._logger.info(f"LocalManager initialize 完成,db_path={self._db_path}")

    # ---------- metas ----------
    def get_meta(self, strategy: str) -> dict:
        c = self.conn
        df = c.execute("SELECT * FROM metas WHERE strategy = ?", [strategy]).df()
        if df.empty:
            # get_meta 是底层工具方法,被 set_meta / heartbeat / clear_strategy 等多处调用,
            # 不存在路径走 DEBUG,避免与外层 warning 重复打印。
            self._logger.debug(f"策略 {strategy} 不存在元数据")
            return {}
        df = _localize_dataframe_columns(
            df, ["outsample_sdt", "create_time", "update_time", "heartbeat_time"], tz=self._tz
        )
        return df.iloc[0].to_dict()

    def get_all_metas(self) -> pd.DataFrame:
        df = self.conn.execute("SELECT * FROM metas").df()
        if not df.empty:
            df = _localize_dataframe_columns(
                df,
                ["outsample_sdt", "create_time", "update_time", "heartbeat_time"],
                tz=self._tz,
            )
        self._vlog(f"get_all_metas → {len(df)} 条")
        return df

    def set_meta(
        self,
        strategy: str,
        base_freq: str,
        description: str,
        author: str,
        outsample_sdt: Any,
        weight_type: str = "ts",
        status: str = "实盘",
        memo: str = "",
        overwrite: bool = False,
    ) -> None:
        self._vlog(f"set_meta(strategy={strategy}, weight_type={weight_type}, status={status}, overwrite={overwrite})")
        meta = self.get_meta(strategy)
        if meta and not overwrite:
            self._logger.warning(f"策略 {strategy} 已存在元数据,如需更新请设置 overwrite=True")
            return

        outsample_naive = _to_naive(outsample_sdt)
        now_naive = _to_naive(pd.Timestamp.now(tz=self._tz))
        create_naive = _to_naive(meta.get("create_time")) if meta else now_naive

        c = self.conn
        c.execute("DELETE FROM metas WHERE strategy = ?", [strategy])
        c.execute(
            """
            INSERT INTO metas
            (strategy, base_freq, description, author, outsample_sdt, create_time,
             update_time, heartbeat_time, weight_type, status, memo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                strategy,
                base_freq,
                description,
                author,
                outsample_naive,
                create_naive,
                now_naive,
                now_naive,
                weight_type,
                status,
                memo,
            ],
        )
        self._logger.info(f"{strategy} set_meta: ok")

    def update_strategy_status(self, strategy: str, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"无效的策略状态: {status},有效状态为: {sorted(VALID_STATUSES)}")
        meta = self.get_meta(strategy)
        if not meta:
            self._logger.warning(f"策略 {strategy} 不存在,无法更新状态")
            return
        now_naive = _to_naive(pd.Timestamp.now(tz=self._tz))
        self.conn.execute(
            "UPDATE metas SET status = ?, update_time = ? WHERE strategy = ?",
            [status, now_naive, strategy],
        )
        self._logger.info(f"策略 {strategy} 状态已更新为: {status}")

    def get_strategies_by_status(self, status: str | None = None) -> pd.DataFrame:
        if status is None:
            df = self.conn.execute("SELECT * FROM metas").df()
        else:
            df = self.conn.execute("SELECT * FROM metas WHERE status = ?", [status]).df()
        if not df.empty:
            df = _localize_dataframe_columns(
                df,
                ["outsample_sdt", "create_time", "update_time", "heartbeat_time"],
                tz=self._tz,
            )
        self._vlog(f"get_strategies_by_status(status={status!r}) → {len(df)} 条")
        return df

    # ---------- weights ----------
    def publish_weights(self, strategy: str, df: pd.DataFrame, batch_size: int = 100000) -> None:
        self._log_publish_entry(strategy, df, table="weights")
        t0 = time.perf_counter()
        self.heartbeat(strategy)
        n = self._publish_dataframe(
            strategy,
            df,
            table="weights",
            value_col="weight",
            mode="append",
            batch_size=batch_size,
        )
        self.heartbeat(strategy)
        self._logger.info(
            f"完成 publish_weights(strategy={strategy}, 实际写入 {n} 条, 耗时 {time.perf_counter() - t0:.2f}s)"
        )

    def get_strategy_weights(
        self,
        strategy: str,
        sdt: Any = None,
        edt: Any = None,
        symbols: str | list[str] | None = None,
    ) -> pd.DataFrame:
        sql = "SELECT * FROM weights WHERE strategy = ?"
        params: list[Any] = [strategy]
        if sdt is not None:
            naive = _to_naive(sdt)
            if naive is not None:
                sql += " AND dt >= ?"
                params.append(naive)
        if edt is not None:
            naive = _to_naive(edt)
            if naive is not None:
                sql += " AND dt <= ?"
                params.append(naive)
        if symbols:
            if isinstance(symbols, str):
                symbols = [symbols]
            placeholders = ",".join("?" * len(symbols))
            sql += f" AND symbol IN ({placeholders})"
            params.extend(symbols)
        df = self.conn.execute(sql, params).df()
        if not df.empty:
            df = _localize_dataframe_columns(df, ["dt", "update_time"], tz=self._tz)
            df = df.sort_values(["dt", "symbol"]).reset_index(drop=True)
        self._vlog(
            f"get_strategy_weights(strategy={strategy}, sdt={sdt}, edt={edt}, "
            f"symbols={_truncate_seq(symbols)}) → {len(df)} 行"
        )
        return df

    def get_latest_weights(self, strategy: str | None = None) -> pd.DataFrame:
        if strategy:
            df = self.conn.execute("SELECT * FROM latest_weights WHERE strategy = ?", [strategy]).df()
        else:
            df = self.conn.execute("SELECT * FROM latest_weights").df()
        if not df.empty:
            df = _localize_dataframe_columns(df, ["dt", "update_time"], tz=self._tz)
            df = df.sort_values(["strategy", "dt", "symbol"]).reset_index(drop=True)
        self._vlog(f"get_latest_weights(strategy={strategy or 'ALL'}) → {len(df)} 行")
        return df

    # ---------- returns ----------
    def publish_returns(self, strategy: str, df: pd.DataFrame, batch_size: int = 100000) -> None:
        self._log_publish_entry(strategy, df, table="returns")
        t0 = time.perf_counter()
        n = self._publish_dataframe(
            strategy,
            df,
            table="returns",
            value_col="returns",
            mode="upsert",
            batch_size=batch_size,
        )
        self._logger.info(
            f"完成 publish_returns(strategy={strategy}, 实际写入 {n} 条, 耗时 {time.perf_counter() - t0:.2f}s)"
        )

    # ---------- 公共出入口辅助 ----------
    def _log_publish_entry(self, strategy: str, df: pd.DataFrame, *, table: str) -> None:
        """publish_weights / publish_returns 共享的入口 INFO 日志(event 档)。

        缺列时不在此处兜底 —— 后续 ``_publish_dataframe`` 会以更清晰的 ``KeyError``
        定位具体缺哪一列。
        """
        if df.empty:
            n_symbols, dt_lo, dt_hi = 0, "N/A", "N/A"
        else:
            n_symbols = int(df["symbol"].nunique())
            dt_lo, dt_hi = df["dt"].min(), df["dt"].max()
        self._logger.info(
            f"开始 publish_{table}(strategy={strategy}, 输入 {len(df)} 行, "
            f"{n_symbols} 个 symbol, dt ∈ [{dt_lo}, {dt_hi}])"
        )

    # ---------- publish 流水线钩子 ----------
    def _query_symbol_latest_dt(self, strategy: str, table: str) -> dict[str, pd.Timestamp]:
        """直查目标表获取每个 symbol 的 latest_dt(比走 latest_weights 视图便宜)。"""
        df = self.conn.execute(
            f"SELECT symbol, MAX(dt) AS dt FROM {table} WHERE strategy = ? GROUP BY symbol",
            [strategy],
        ).df()
        if df.empty:
            return {}
        df["dt"] = _ensure_series_tz(df["dt"], tz=self._tz)
        return df.set_index("symbol")["dt"].to_dict()

    def _insert_publish_batch(self, table: str, batch: pd.DataFrame) -> None:
        """DuckDB 写入前把带时区列转 naive(对应 ``TIMESTAMP`` 列定义)。"""
        batch = batch.copy()
        batch["dt"] = _series_to_naive(batch["dt"])
        batch["update_time"] = _series_to_naive(batch["update_time"])
        self._insert_or_replace(table, batch, key_cols=["strategy", "dt", "symbol"])

    def get_strategy_returns(
        self,
        strategy: str,
        sdt: Any = None,
        edt: Any = None,
        symbols: str | list[str] | None = None,
    ) -> pd.DataFrame:
        sql = "SELECT * FROM returns WHERE strategy = ?"
        params: list[Any] = [strategy]
        if sdt is not None:
            naive = _to_naive(sdt)
            if naive is not None:
                naive = naive.replace(hour=0, minute=0, second=0, microsecond=0)
                sql += " AND dt >= ?"
                params.append(naive)
        if edt is not None:
            naive = _to_naive(edt)
            if naive is not None:
                naive = naive.replace(hour=23, minute=59, second=59, microsecond=0)
                sql += " AND dt <= ?"
                params.append(naive)
        if symbols:
            if isinstance(symbols, str):
                symbols = [symbols]
            placeholders = ",".join("?" * len(symbols))
            sql += f" AND symbol IN ({placeholders})"
            params.extend(symbols)
        df = self.conn.execute(sql, params).df()
        if not df.empty:
            df = _localize_dataframe_columns(df, ["dt", "update_time"], tz=self._tz)
            df = df.sort_values(["dt", "symbol"]).reset_index(drop=True)
        self._vlog(
            f"get_strategy_returns(strategy={strategy}, sdt={sdt}, edt={edt}, "
            f"symbols={_truncate_seq(symbols)}) → {len(df)} 行"
        )
        return df

    # ---------- tags ----------
    def add_tag(self, strategy: str, tag: str, creator: str = "system") -> None:
        self._vlog(f"add_tag(strategy={strategy}, tag={tag}, creator={creator})")
        now_naive = _to_naive(pd.Timestamp.now(tz=self._tz))
        c = self.conn
        c.execute("DELETE FROM tags WHERE strategy = ? AND tag = ?", [strategy, tag])
        c.execute(
            "INSERT INTO tags (strategy, tag, creator, create_time) VALUES (?, ?, ?, ?)",
            [strategy, tag, creator, now_naive],
        )

    def add_tags(self, items: Iterable[tuple[str, str]], batch_size: int = 500) -> int:
        rows = list(items)
        if not rows:
            return 0
        self._vlog(f"add_tags: 输入 {len(rows)} 条, batch_size={batch_size}")
        now_naive = _to_naive(pd.Timestamp.now(tz=self._tz))
        c = self.conn
        n = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            for strategy, tag in batch:
                c.execute("DELETE FROM tags WHERE strategy = ? AND tag = ?", [strategy, tag])
                c.execute(
                    "INSERT INTO tags (strategy, tag, creator, create_time) VALUES (?, ?, ?, ?)",
                    [strategy, tag, "system", now_naive],
                )
                n += 1
            self._vlog(f"add_tags 批次 {i // batch_size + 1}: 写入 {len(batch)} 条")
        self._vlog(f"add_tags 完成: 处理 {n} 条")
        return n

    def list_tags(self, strategy: str | None = None, tag: str | None = None) -> pd.DataFrame:
        sql = "SELECT * FROM tags"
        clauses: list[str] = []
        params: list[Any] = []
        if strategy:
            clauses.append("strategy = ?")
            params.append(strategy)
        if tag:
            clauses.append("tag = ?")
            params.append(tag)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        df = self.conn.execute(sql, params).df()
        if not df.empty:
            df = _localize_dataframe_columns(df, ["create_time"], tz=self._tz)
            df = df.sort_values(["strategy", "tag"]).reset_index(drop=True)
        self._vlog(f"list_tags(strategy={strategy}, tag={tag}) → {len(df)} 行")
        return df

    def remove_tag(self, strategy: str, tag: str) -> None:
        self._vlog(f"remove_tag(strategy={strategy}, tag={tag})")
        self.conn.execute("DELETE FROM tags WHERE strategy = ? AND tag = ?", [strategy, tag])

    # ---------- 心跳与运维 ----------
    def heartbeat(self, strategy: str) -> None:
        meta = self.get_meta(strategy)
        if not meta:
            self._logger.warning(f"策略 {strategy} 不存在元数据,无法发送心跳")
            return
        now_naive = _to_naive(pd.Timestamp.now(tz=self._tz))
        self.conn.execute(
            "UPDATE metas SET heartbeat_time = ? WHERE strategy = ?",
            [now_naive, strategy],
        )
        self._vlog(f"heartbeat({strategy}) ok")

    def clear_strategy(self, strategy: str, human_confirm: bool = True) -> None:
        meta = self.get_meta(strategy)
        if not meta:
            self._logger.warning(f"策略 {strategy} 不存在,无需清空")
            return

        c = self.conn
        # 一条 SQL 拿三表的 count,与 OnlineManager.clear_strategy 对称
        row = c.execute(
            """
            SELECT
                (SELECT count(*) FROM weights WHERE strategy = ?) AS weights,
                (SELECT count(*) FROM returns WHERE strategy = ?) AS returns,
                (SELECT count(*) FROM tags WHERE strategy = ?) AS tags
            """,
            [strategy, strategy, strategy],
        ).fetchone()
        weights_count = int(row[0]) if row else 0
        returns_count = int(row[1]) if row else 0
        tags_count = int(row[2]) if row else 0
        self._logger.info(
            f"策略 {strategy} 即将清空: status={meta.get('status', '未知')}, "
            f"weights={weights_count:,}, returns={returns_count:,}, tags={tags_count:,}"
        )
        self._vlog(
            f"  详情: create_time={meta.get('create_time', '未知')}, update_time={meta.get('update_time', '未知')}"
        )

        if human_confirm:
            self._logger.warning(f"⚠️  即将删除策略 {strategy} 的所有数据,输入 'DELETE' 确认:")
            confirm = input("> ")
            if confirm != "DELETE":
                self._logger.warning(f"取消清空策略 {strategy} 的所有数据")
                return

        t0 = time.perf_counter()
        c.execute("DELETE FROM metas WHERE strategy = ?", [strategy])
        c.execute("DELETE FROM weights WHERE strategy = ?", [strategy])
        c.execute("DELETE FROM returns WHERE strategy = ?", [strategy])
        c.execute("DELETE FROM tags WHERE strategy = ?", [strategy])
        self._logger.info(
            f"策略 {strategy} 清空完成: weights={weights_count:,}, returns={returns_count:,}, "
            f"tags={tags_count:,}, metas=1, 耗时 {time.perf_counter() - t0:.2f}s"
        )

    def summary(self) -> dict:
        # 一条 SQL 拿全部 5 个计数,与 OnlineManager.summary 保持对称
        row = self.conn.execute(
            """
            SELECT
                (SELECT count(*) FROM metas) AS metas,
                (SELECT count(*) FROM weights) AS weights,
                (SELECT count(*) FROM returns) AS returns,
                (SELECT count(*) FROM tags) AS tags,
                (SELECT count(DISTINCT strategy) FROM metas) AS strategies
            """
        ).fetchone()
        # 多个 (SELECT count) 子查询恒返回一行,row 不会是 None;assert 给 typecheck 收窄。
        assert row is not None
        result = {
            "metas": int(row[0]),
            "weights": int(row[1]),
            "returns": int(row[2]),
            "tags": int(row[3]),
            "strategies": int(row[4]),
        }
        self._vlog(f"summary → {result}")
        return result

    def _scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        """执行返回单值的 SQL,空结果返回 0。"""
        row = self.conn.execute(sql, params or []).fetchone()
        return row[0] if row is not None else 0

    # ---------- 内部工具 ----------
    def _insert_or_replace(self, table: str, df: pd.DataFrame, key_cols: list[str]) -> None:
        """DuckDB 没有 INSERT OR REPLACE,模拟为先按主键删除再插入。

        Args:
            table: 目标表名。
            df: 待写入 DataFrame,字段顺序必须与表定义完全一致。
            key_cols: 主键列名,用于定位需要删除的旧行。
        """
        if df.empty:
            return
        c = self.conn
        c.register("__tmp_df", df)
        try:
            where = " AND ".join(f"{table}.{k} = __tmp_df.{k}" for k in key_cols)
            c.execute(f"DELETE FROM {table} WHERE EXISTS (SELECT 1 FROM __tmp_df WHERE {where})")
            cols = ", ".join(df.columns)
            c.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM __tmp_df")
        finally:
            c.unregister("__tmp_df")

    def __repr__(self) -> str:
        return f"LocalManager(db_path={self._db_path!r}, read_only={self._read_only}, verbose={self._verbose})"
