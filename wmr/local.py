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
    _ensure_timestamp,
    _localize_dataframe_columns,
)

DEFAULT_LOCAL_DB_PATH: str = os.environ.get("WMR_LOCAL_DB_PATH", str(Path.home() / ".wmr" / "weights.duckdb"))
"""LocalManager 默认 db 路径,可通过 ``WMR_LOCAL_DB_PATH`` 环境变量覆盖。"""


def _to_naive(ts: Any) -> pd.Timestamp | None:
    """将带时区的时间对象转为 naive(去掉 tz),供 DuckDB ``TIMESTAMP`` 列存储。

    Args:
        ts: 任意可被 ``pd.to_datetime`` 解析的时间值。

    Returns:
        naive Timestamp;无效输入返回 ``None``。
    """
    t = _ensure_timestamp(ts)
    if not isinstance(t, pd.Timestamp):
        return None
    return t.tz_convert(DEFAULT_TZ).tz_localize(None)


def _series_to_naive(series: pd.Series) -> pd.Series:
    """Series 版的 ``_to_naive``。"""
    s = _ensure_series_tz(series)
    return s.dt.tz_convert(DEFAULT_TZ).dt.tz_localize(None)


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
    ) -> None:
        """初始化 LocalManager。

        Args:
            db_path: DuckDB 文件路径。``None`` 时回落到 ``WMR_LOCAL_DB_PATH``
                env 或 ``~/.wmr/weights.duckdb``;``:memory:`` 表示内存模式。
            read_only: 是否以只读模式打开,默认 False。
            logger: 日志器,默认 loguru.logger。
            tz: 时区,默认 ``Asia/Shanghai``。
        """
        if db_path is None:
            db_path = DEFAULT_LOCAL_DB_PATH
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path: str = db_path
        self._read_only: bool = read_only
        self._logger = logger
        self._tz: ZoneInfo = tz
        self._conn: duckdb.DuckDBPyConnection | None = None

    # ---------- 生命周期 ----------
    def connect(self) -> duckdb.DuckDBPyConnection:
        """建立或复用 DuckDB 连接。"""
        if self._conn is None:
            self._conn = duckdb.connect(self._db_path, read_only=self._read_only)
        return self._conn

    def close(self) -> None:
        """关闭 DuckDB 连接。重复调用安全。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """惰性建立的 DuckDB 连接。"""
        return self.connect()

    def initialize(self) -> None:
        """创建 4 张表与 3 个视图。允许重复调用。"""
        c = self.conn
        c.execute(
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
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS weights (
                dt TIMESTAMP,
                symbol VARCHAR,
                weight DOUBLE,
                strategy VARCHAR,
                update_time TIMESTAMP,
                PRIMARY KEY (strategy, dt, symbol)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS returns (
                dt TIMESTAMP,
                symbol VARCHAR,
                returns DOUBLE,
                strategy VARCHAR,
                update_time TIMESTAMP,
                PRIMARY KEY (strategy, dt, symbol)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS tags (
                strategy VARCHAR,
                tag VARCHAR,
                creator VARCHAR DEFAULT 'system',
                create_time TIMESTAMP,
                PRIMARY KEY (strategy, tag)
            )
            """
        )
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
        c.execute(
            """
            CREATE OR REPLACE VIEW latest_weights AS
            SELECT * FROM ts_latest_weights
            UNION ALL
            SELECT * FROM cs_latest_weights
            """
        )
        self._logger.info(f"LocalManager initialize 完成,db_path={self._db_path}")

    # ---------- metas ----------
    def get_meta(self, strategy: str) -> dict:
        c = self.conn
        df = c.execute("SELECT * FROM metas WHERE strategy = ?", [strategy]).df()
        if df.empty:
            self._logger.warning(f"策略 {strategy} 不存在元数据")
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
            raise ValueError(f"无效的策略状态: {status},有效状态为: {VALID_STATUSES}")
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
        return df

    # ---------- weights ----------
    def publish_weights(self, strategy: str, df: pd.DataFrame, batch_size: int = 100000) -> None:
        self.heartbeat(strategy)

        # ---------- 1. 标准化输入 ----------
        df = df[["dt", "symbol", "weight"]].copy()
        df["strategy"] = strategy
        df["dt"] = _ensure_series_tz(df["dt"], tz=self._tz)

        # ---------- 2. 过滤 dt > latest_dt(仅追加) ----------
        dfl = self.get_latest_weights(strategy)
        if not dfl.empty:
            dfl = _localize_dataframe_columns(dfl, ["dt"], tz=self._tz)
            symbol_dt = dfl.set_index("symbol")["dt"].to_dict()
            self._logger.info(f"策略 {strategy} 最新时间:{dfl['dt'].max()}")
            rows: list[pd.DataFrame] = []
            for symbol, dfg in df.groupby("symbol"):
                if symbol in symbol_dt:
                    dfg = dfg[dfg["dt"] > symbol_dt[symbol]].copy().reset_index(drop=True)
                rows.append(dfg)
            if rows:
                df = pd.concat(rows, ignore_index=True)
            self._logger.info(f"策略 {strategy} 共 {len(df)} 条新信号")

        # ---------- 3. 排序 + 去重 + 转 naive ----------
        df = df.sort_values(["dt", "symbol"]).reset_index(drop=True)
        df["update_time"] = pd.Timestamp.now(tz=self._tz)
        df = df[["strategy", "symbol", "dt", "weight", "update_time"]].copy()
        df = df.drop_duplicates(["symbol", "dt", "strategy"], keep="last").reset_index(drop=True)
        df["weight"] = df["weight"].astype(float)
        df["dt"] = _series_to_naive(df["dt"])
        df["update_time"] = _series_to_naive(df["update_time"])

        # ---------- 4. 分批写入 + heartbeat ----------
        for i in range(0, len(df), batch_size):
            batch = df.iloc[i : i + batch_size]
            self._insert_or_replace("weights", batch, key_cols=["strategy", "dt", "symbol"])
            self.heartbeat(strategy)
            self._logger.info(f"完成批次 {i // batch_size + 1},发布 {len(batch)} 条信号")
        self._logger.info(f"完成所有信号发布,共 {len(df)} 条")
        self.heartbeat(strategy)

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
        return df

    def get_latest_weights(self, strategy: str | None = None) -> pd.DataFrame:
        if strategy:
            df = self.conn.execute("SELECT * FROM latest_weights WHERE strategy = ?", [strategy]).df()
        else:
            df = self.conn.execute("SELECT * FROM latest_weights").df()
        if not df.empty:
            df = _localize_dataframe_columns(df, ["dt", "update_time"], tz=self._tz)
            df = df.sort_values(["strategy", "dt", "symbol"]).reset_index(drop=True)
        return df

    # ---------- returns ----------
    def publish_returns(self, strategy: str, df: pd.DataFrame, batch_size: int = 100000) -> None:
        # ---------- 1. 标准化输入 ----------
        df = df[["dt", "symbol", "returns"]].copy()
        df["strategy"] = strategy
        df["dt"] = _ensure_series_tz(df["dt"], tz=self._tz)

        # ---------- 2. 过滤 dt >= latest_dt(允许覆盖同日) ----------
        dfl = self.conn.execute(
            "SELECT symbol, max(dt) AS dt FROM returns WHERE strategy = ? GROUP BY symbol",
            [strategy],
        ).df()
        if not dfl.empty:
            dfl["dt"] = _ensure_series_tz(dfl["dt"], tz=self._tz)
            symbol_dt = dfl.set_index("symbol")["dt"].to_dict()
            self._logger.info(f"策略 {strategy} 最新时间:{dfl['dt'].max()}")
            rows: list[pd.DataFrame] = []
            for symbol, dfg in df.groupby("symbol"):
                if symbol in symbol_dt:
                    dfg = dfg[dfg["dt"] >= symbol_dt[symbol]].copy()
                rows.append(dfg)
            if rows:
                df = pd.concat(rows, ignore_index=True)
            self._logger.info(f"策略 {strategy} 共 {len(df)} 条新日收益")

        # ---------- 3. 排序 + 去重 + 转 naive ----------
        df = df.sort_values(["dt", "symbol"]).reset_index(drop=True)
        df["update_time"] = pd.Timestamp.now(tz=self._tz)
        df = df[["strategy", "symbol", "dt", "returns", "update_time"]].copy()
        df = df.drop_duplicates(["symbol", "dt", "strategy"], keep="last").reset_index(drop=True)
        df["returns"] = df["returns"].astype(float)
        df["dt"] = _series_to_naive(df["dt"])
        df["update_time"] = _series_to_naive(df["update_time"])

        # ---------- 4. 分批写入 ----------
        for i in range(0, len(df), batch_size):
            batch = df.iloc[i : i + batch_size]
            self._insert_or_replace("returns", batch, key_cols=["strategy", "dt", "symbol"])
            self._logger.info(f"完成批次 {i // batch_size + 1},发布 {len(batch)} 条日收益")
        self._logger.info(f"完成所有日收益发布,共 {len(df)} 条")

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
        return df

    # ---------- tags ----------
    def add_tag(self, strategy: str, tag: str, creator: str = "system") -> None:
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
        return df

    def remove_tag(self, strategy: str, tag: str) -> None:
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

    def clear_strategy(self, strategy: str, human_confirm: bool = True) -> None:
        meta = self.get_meta(strategy)
        if not meta:
            self._logger.warning(f"策略 {strategy} 不存在,无需清空")
            return

        c = self.conn
        weights_count = self._scalar("SELECT count(*) FROM weights WHERE strategy = ?", [strategy])
        returns_count = self._scalar("SELECT count(*) FROM returns WHERE strategy = ?", [strategy])
        tags_count = self._scalar("SELECT count(*) FROM tags WHERE strategy = ?", [strategy])
        self._logger.info(f"策略 {strategy} 数据概况:")
        self._logger.info(f"  - 策略状态: {meta.get('status', '未知')}")
        self._logger.info(f"  - 创建时间: {meta.get('create_time', '未知')}")
        self._logger.info(f"  - 最后更新: {meta.get('update_time', '未知')}")
        self._logger.info(f"  - 权重数据: {weights_count:,} 条")
        self._logger.info(f"  - 收益数据: {returns_count:,} 条")
        self._logger.info(f"  - 标签数据: {tags_count:,} 条")

        if human_confirm:
            self._logger.info("=" * 60)
            self._logger.info(f"⚠️  警告:即将删除策略 {strategy} 的所有数据")
            self._logger.info("=" * 60)
            confirm = input("请仔细确认上述信息,确认删除请输入 'DELETE' (大小写敏感): ")
            if confirm != "DELETE":
                self._logger.warning(f"取消清空策略 {strategy} 的所有数据")
                return
            self._logger.info("开始执行删除操作...")

        c.execute("DELETE FROM metas WHERE strategy = ?", [strategy])
        c.execute("DELETE FROM weights WHERE strategy = ?", [strategy])
        c.execute("DELETE FROM returns WHERE strategy = ?", [strategy])
        c.execute("DELETE FROM tags WHERE strategy = ?", [strategy])
        self._logger.warning(f"策略 {strategy} 清空完成")

    def summary(self) -> dict:
        return {
            "metas": self._scalar("SELECT count(*) FROM metas"),
            "weights": self._scalar("SELECT count(*) FROM weights"),
            "returns": self._scalar("SELECT count(*) FROM returns"),
            "tags": self._scalar("SELECT count(*) FROM tags"),
            "strategies": self._scalar("SELECT count(DISTINCT strategy) FROM metas"),
        }

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
        return f"LocalManager(db_path={self._db_path!r}, read_only={self._read_only})"
