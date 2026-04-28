"""``OnlineManager`` —— ClickHouse 后端实现。

使用 DSN 进行连接配置,默认从环境变量 ``WMR_CLICKHOUSE_DSN`` 加载。
对应飞书设计文档"五、连接配置 5.2 OnlineManager"。

::

    DSN 解析流程:
        ┌─────────────────────────┐
        │ dsn 参数(显式 / None)  │
        └────────────┬────────────┘
                     ↓
            ┌────────────────┐
            │ 落空? 读 env   │ ← WMR_CLICKHOUSE_DSN
            └────────┬───────┘
                     ↓
                依然空 → ValueError
                     ↓
        ┌──────────────────────────┐
        │ urlparse → host/port/... │
        └──────────────────────────┘

写入路径:``ReplacingMergeTree()`` ;读取路径所有 SELECT 必须含 ``FINAL`` 关键字
(对齐 cwc.py),否则在 part 合并完成前会读到重复行。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import loguru
import pandas as pd

from wmr.base import VALID_STATUSES, BaseManager
from wmr.utils import (
    DEFAULT_TZ,
    _ensure_series_tz,
    _ensure_timestamp,
    _format_for_db,
    _localize_dataframe_columns,
    mask_dsn_password,
)

if TYPE_CHECKING:
    from clickhouse_connect.driver.client import Client

DEFAULT_DATABASE: str = os.environ.get("WMR_DATABASE", "czsc_strategy")
"""OnlineManager 默认 database 名,可通过 ``WMR_DATABASE`` 覆盖。"""


def _parse_dsn(dsn: str) -> dict[str, Any]:
    """解析 ``clickhouse://user:password@host:port/database`` 形式的 DSN。

    Args:
        dsn: DSN 字符串,scheme 必须为 ``clickhouse`` 或 ``clickhouse+http`` /
            ``clickhouse+https``,否则抛 ``ValueError``。

    Returns:
        dict,字段含:``host`` / ``port`` / ``user`` / ``password`` / ``database``
        (database 可能为空串,表示由调用方决定默认值)。

    Raises:
        ValueError: DSN 格式不合法或 host/port 缺失。
    """
    parsed = urlparse(dsn)
    if parsed.scheme not in {"clickhouse", "clickhouse+http", "clickhouse+https"}:
        raise ValueError(f"DSN scheme 必须是 clickhouse / clickhouse+http / clickhouse+https,当前: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError(f"DSN 缺少 host: {mask_dsn_password(dsn)!r}")
    if not parsed.port:
        raise ValueError(f"DSN 缺少 port: {mask_dsn_password(dsn)!r}")

    database = (parsed.path or "").lstrip("/")
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "user": parsed.username or "default",
        "password": parsed.password or "",
        "database": database,
    }


class OnlineManager(BaseManager):
    """基于 ClickHouse 的在线策略持仓权重管理器。

    适用场景:生产环境、多用户、高并发。

    Attributes:
        _dsn: 原始 DSN(带密码)。脱敏版本通过 ``__repr__`` 暴露。
        _client: ``clickhouse_connect`` Client 对象,首次 ``connect()`` 后建立。
        _database: 实际使用的 database 名,优先级:``database`` 参数 > DSN
            path 段 > ``WMR_DATABASE`` env > ``czsc_strategy``。
    """

    def __init__(
        self,
        dsn: str | None = None,
        database: str | None = None,
        client_kwargs: dict[str, Any] | None = None,
        logger: Any = loguru.logger,
        tz: ZoneInfo = DEFAULT_TZ,
    ) -> None:
        """初始化 OnlineManager。

        Args:
            dsn: ClickHouse DSN,格式 ``clickhouse://user:pass@host:port/database``。
                ``None`` 时从环境变量 ``WMR_CLICKHOUSE_DSN`` 加载。两者均缺失则
                抛 ``ValueError``。
            database: 显式指定 database 名;``None`` 时按"DSN path > env > 默认"
                顺序解析。
            client_kwargs: 透传给 ``clickhouse_connect.get_client`` 的额外参数,
                如 ``{"connect_timeout": 30, "send_receive_timeout": 60}``。
            logger: 日志器,默认 loguru.logger。
            tz: 时区,默认 ``Asia/Shanghai``。

        Raises:
            ValueError: DSN 缺失或格式非法。
        """
        if dsn is None:
            dsn = os.environ.get("WMR_CLICKHOUSE_DSN")
        if not dsn:
            raise ValueError("OnlineManager 必须提供 dsn 参数,或设置环境变量 WMR_CLICKHOUSE_DSN")

        parsed = _parse_dsn(dsn)
        self._dsn: str = dsn
        self._dsn_parts: dict[str, Any] = parsed
        self._client_kwargs: dict[str, Any] = client_kwargs or {}
        self._logger = logger
        self._tz: ZoneInfo = tz
        self._database: str = database or parsed["database"] or DEFAULT_DATABASE
        self._client: Client | None = None

    # ---------- 生命周期 ----------
    def connect(self) -> Client:
        """建立或复用 clickhouse_connect Client。"""
        if self._client is None:
            import clickhouse_connect

            p = self._dsn_parts
            self._client = clickhouse_connect.get_client(
                host=p["host"],
                port=p["port"],
                user=p["user"],
                password=p["password"],
                **self._client_kwargs,
            )
        return self._client

    def close(self) -> None:
        """关闭 Client。重复调用安全。"""
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    @property
    def client(self) -> Client:
        """惰性建立的 ClickHouse Client。"""
        return self.connect()

    def initialize(self) -> None:
        """创建数据库 / 4 张表 / 3 个视图。允许重复调用。"""
        c = self.client
        db = self._database
        c.command(f"CREATE DATABASE IF NOT EXISTS {db}")

        c.command(
            f"""
            CREATE TABLE IF NOT EXISTS {db}.metas (
                strategy String NOT NULL,
                base_freq String,
                description String,
                author String,
                outsample_sdt DateTime('Asia/Shanghai'),
                create_time DateTime('Asia/Shanghai'),
                update_time DateTime('Asia/Shanghai'),
                heartbeat_time DateTime('Asia/Shanghai'),
                weight_type String,
                status String DEFAULT '实盘',
                memo String
            )
            ENGINE = ReplacingMergeTree()
            ORDER BY strategy
            """
        )
        c.command(
            f"""
            CREATE TABLE IF NOT EXISTS {db}.weights (
                dt DateTime('Asia/Shanghai'),
                symbol String,
                weight Float64,
                strategy String,
                update_time DateTime('Asia/Shanghai')
            )
            ENGINE = ReplacingMergeTree()
            ORDER BY (strategy, dt, symbol)
            """
        )
        c.command(
            f"""
            CREATE TABLE IF NOT EXISTS {db}.returns (
                dt DateTime('Asia/Shanghai'),
                symbol String,
                returns Float64,
                strategy String,
                update_time DateTime('Asia/Shanghai')
            )
            ENGINE = ReplacingMergeTree()
            ORDER BY (strategy, dt, symbol)
            """
        )
        c.command(
            f"""
            CREATE TABLE IF NOT EXISTS {db}.tags (
                strategy String,
                tag String,
                creator String DEFAULT 'system',
                create_time DateTime('Asia/Shanghai')
            )
            ENGINE = ReplacingMergeTree()
            ORDER BY (strategy, tag)
            """
        )
        c.command(
            f"""
            CREATE VIEW IF NOT EXISTS {db}.cs_latest_weights AS
            WITH latest_dates AS (
                SELECT strategy, MAX(dt) AS latest_dt
                FROM {db}.weights GROUP BY strategy
            )
            SELECT w.dt as dt, w.symbol as symbol, w.weight as weight,
                   w.strategy as strategy, w.update_time as update_time
            FROM {db}.weights w
            JOIN latest_dates ld ON w.strategy = ld.strategy AND w.dt = ld.latest_dt
            JOIN {db}.metas m ON w.strategy = m.strategy
            WHERE m.weight_type = 'cs'
            """
        )
        c.command(
            f"""
            CREATE VIEW IF NOT EXISTS {db}.ts_latest_weights AS
            WITH latest_records AS (
                SELECT strategy, symbol, MAX(dt) AS latest_dt
                FROM {db}.weights GROUP BY strategy, symbol
            )
            SELECT w.dt as dt, w.symbol as symbol, w.weight as weight,
                   w.strategy as strategy, w.update_time as update_time
            FROM {db}.weights w
            JOIN latest_records lr
              ON w.strategy = lr.strategy AND w.symbol = lr.symbol AND w.dt = lr.latest_dt
            JOIN {db}.metas m ON w.strategy = m.strategy
            WHERE m.weight_type = 'ts'
            """
        )
        c.command(
            f"""
            CREATE VIEW IF NOT EXISTS {db}.latest_weights AS
            SELECT * FROM {db}.ts_latest_weights
            UNION ALL
            SELECT * FROM {db}.cs_latest_weights
            """
        )
        self._logger.info(f"OnlineManager initialize 完成,database={db}")

    # ---------- metas ----------
    def get_meta(self, strategy: str) -> dict:
        c = self.client
        df = c.query_df(
            f"SELECT * FROM {self._database}.metas FINAL WHERE strategy = %(strategy)s",
            parameters={"strategy": strategy},
        )
        if df.empty:
            self._logger.warning(f"策略 {strategy} 不存在元数据")
            return {}
        df = _localize_dataframe_columns(
            df,
            ["outsample_sdt", "create_time", "update_time", "heartbeat_time"],
            tz=self._tz,
        )
        return df.iloc[0].to_dict()

    def get_all_metas(self) -> pd.DataFrame:
        df = self.client.query_df(f"SELECT * FROM {self._database}.metas FINAL")
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

        outsample_ts = _ensure_timestamp(outsample_sdt, tz=self._tz)
        current_time = pd.Timestamp.now(tz=self._tz)
        create_time = current_time if not meta else _ensure_timestamp(meta.get("create_time"), tz=self._tz)
        df = pd.DataFrame(
            [
                {
                    "strategy": strategy,
                    "base_freq": base_freq,
                    "description": description,
                    "author": author,
                    "outsample_sdt": outsample_ts,
                    "create_time": create_time,
                    "update_time": current_time,
                    "heartbeat_time": current_time,
                    "weight_type": weight_type,
                    "status": status,
                    "memo": memo,
                }
            ]
        )
        self.client.insert_df(f"{self._database}.metas", df)
        self._logger.info(f"{strategy} set_meta: ok")

    def update_strategy_status(self, strategy: str, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"无效的策略状态: {status},有效状态为: {VALID_STATUSES}")
        meta = self.get_meta(strategy)
        if not meta:
            self._logger.warning(f"策略 {strategy} 不存在,无法更新状态")
            return
        current_time = _format_for_db(pd.Timestamp.now(tz=self._tz), tz=self._tz)
        self.client.command(
            f"ALTER TABLE {self._database}.metas "
            "UPDATE status = %(status)s, update_time = %(t)s "
            "WHERE strategy = %(strategy)s",
            parameters={"status": status, "t": current_time, "strategy": strategy},
        )
        self._logger.info(f"策略 {strategy} 状态已更新为: {status}")

    def get_strategies_by_status(self, status: str | None = None) -> pd.DataFrame:
        sql = f"SELECT * FROM {self._database}.metas FINAL"
        params: dict[str, Any] = {}
        if status:
            sql += " WHERE status = %(status)s"
            params["status"] = status
        df = self.client.query_df(sql, parameters=params)
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

        # ---------- 3. 排序 + 去重 ----------
        df = df.sort_values(["dt", "symbol"]).reset_index(drop=True)
        df["update_time"] = pd.Timestamp.now(tz=self._tz)
        df = df[["strategy", "symbol", "dt", "weight", "update_time"]].copy()
        df = df.drop_duplicates(["symbol", "dt", "strategy"], keep="last").reset_index(drop=True)
        df["weight"] = df["weight"].astype(float)

        # ---------- 4. 分批写入 + heartbeat ----------
        for i in range(0, len(df), batch_size):
            batch = df.iloc[i : i + batch_size]
            self.client.insert_df(f"{self._database}.weights", batch)
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
        sql = f"SELECT * FROM {self._database}.weights FINAL WHERE strategy = %(strategy)s"
        params: dict[str, Any] = {"strategy": strategy}
        if sdt is not None:
            s = _format_for_db(sdt, tz=self._tz)
            if s:
                sql += " AND dt >= %(sdt)s"
                params["sdt"] = s
        if edt is not None:
            e = _format_for_db(edt, tz=self._tz)
            if e:
                sql += " AND dt <= %(edt)s"
                params["edt"] = e
        if symbols:
            if isinstance(symbols, str):
                symbols = [symbols]
            sql += " AND symbol IN %(symbols)s"
            params["symbols"] = tuple(symbols)
        df = self.client.query_df(sql, parameters=params)
        if not df.empty:
            df = _localize_dataframe_columns(df, ["dt", "update_time"], tz=self._tz)
            df = df.sort_values(["dt", "symbol"]).reset_index(drop=True)
        return df

    def get_latest_weights(self, strategy: str | None = None) -> pd.DataFrame:
        sql = f"SELECT * FROM {self._database}.latest_weights FINAL"
        params: dict[str, Any] = {}
        if strategy:
            sql += " WHERE strategy = %(strategy)s"
            params["strategy"] = strategy
        df = self.client.query_df(sql, parameters=params)
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
        dfl = self.client.query_df(
            f"SELECT symbol, max(dt) AS dt FROM {self._database}.returns FINAL "
            "WHERE strategy = %(strategy)s GROUP BY symbol",
            parameters={"strategy": strategy},
        )
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

        # ---------- 3. 排序 + 去重 ----------
        df = df.sort_values(["dt", "symbol"]).reset_index(drop=True)
        df["update_time"] = pd.Timestamp.now(tz=self._tz)
        df = df[["strategy", "symbol", "dt", "returns", "update_time"]].copy()
        df = df.drop_duplicates(["symbol", "dt", "strategy"], keep="last").reset_index(drop=True)
        df["returns"] = df["returns"].astype(float)

        # ---------- 4. 分批写入 ----------
        for i in range(0, len(df), batch_size):
            batch = df.iloc[i : i + batch_size]
            self.client.insert_df(f"{self._database}.returns", batch)
            self._logger.info(f"完成批次 {i // batch_size + 1},发布 {len(batch)} 条日收益")
        self._logger.info(f"完成所有日收益发布,共 {len(df)} 条")

    def get_strategy_returns(
        self,
        strategy: str,
        sdt: Any = None,
        edt: Any = None,
        symbols: str | list[str] | None = None,
    ) -> pd.DataFrame:
        sql = f"SELECT * FROM {self._database}.returns FINAL WHERE strategy = %(strategy)s"
        params: dict[str, Any] = {"strategy": strategy}
        if sdt is not None:
            sdt_ts = _ensure_timestamp(sdt, tz=self._tz)
            if not pd.isna(sdt_ts):
                sdt_ts = sdt_ts.replace(hour=0, minute=0, second=0, microsecond=0)
                sql += " AND dt >= %(sdt)s"
                params["sdt"] = _format_for_db(sdt_ts, tz=self._tz)
        if edt is not None:
            edt_ts = _ensure_timestamp(edt, tz=self._tz)
            if not pd.isna(edt_ts):
                edt_ts = edt_ts.replace(hour=23, minute=59, second=59, microsecond=0)
                sql += " AND dt <= %(edt)s"
                params["edt"] = _format_for_db(edt_ts, tz=self._tz)
        if symbols:
            if isinstance(symbols, str):
                symbols = [symbols]
            sql += " AND symbol IN %(symbols)s"
            params["symbols"] = tuple(symbols)
        df = self.client.query_df(sql, parameters=params)
        if not df.empty:
            df = _localize_dataframe_columns(df, ["dt", "update_time"], tz=self._tz)
            df = df.sort_values(["dt", "symbol"]).reset_index(drop=True)
        return df

    # ---------- tags ----------
    def add_tag(self, strategy: str, tag: str, creator: str = "system") -> None:
        now_ts = pd.Timestamp.now(tz=self._tz)
        df = pd.DataFrame([{"strategy": strategy, "tag": tag, "creator": creator, "create_time": now_ts}])
        self.client.insert_df(f"{self._database}.tags", df)

    def add_tags(self, items: Iterable[tuple[str, str]], batch_size: int = 500) -> int:
        rows = list(items)
        if not rows:
            return 0
        now_ts = pd.Timestamp.now(tz=self._tz)
        n = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            df = pd.DataFrame(
                [
                    {
                        "strategy": s,
                        "tag": t,
                        "creator": "system",
                        "create_time": now_ts,
                    }
                    for s, t in batch
                ]
            )
            self.client.insert_df(f"{self._database}.tags", df)
            n += len(batch)
        return n

    def list_tags(self, strategy: str | None = None, tag: str | None = None) -> pd.DataFrame:
        sql = f"SELECT * FROM {self._database}.tags FINAL"
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if strategy:
            clauses.append("strategy = %(strategy)s")
            params["strategy"] = strategy
        if tag:
            clauses.append("tag = %(tag)s")
            params["tag"] = tag
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        df = self.client.query_df(sql, parameters=params)
        if not df.empty:
            df = _localize_dataframe_columns(df, ["create_time"], tz=self._tz)
            df = df.sort_values(["strategy", "tag"]).reset_index(drop=True)
        return df

    def remove_tag(self, strategy: str, tag: str) -> None:
        self.client.command(
            f"DELETE FROM {self._database}.tags WHERE strategy = %(strategy)s AND tag = %(tag)s",
            parameters={"strategy": strategy, "tag": tag},
        )

    # ---------- 心跳与运维 ----------
    def heartbeat(self, strategy: str) -> None:
        meta = self.get_meta(strategy)
        if not meta:
            self._logger.warning(f"策略 {strategy} 不存在元数据,无法发送心跳")
            return
        current_time = _format_for_db(pd.Timestamp.now(tz=self._tz), tz=self._tz)
        try:
            self.client.command(
                f"ALTER TABLE {self._database}.metas UPDATE heartbeat_time = %(t)s WHERE strategy = %(strategy)s",
                parameters={"t": current_time, "strategy": strategy},
            )
        except Exception as e:
            self._logger.error(f"发送心跳失败: {e}")
            raise

    def clear_strategy(self, strategy: str, human_confirm: bool = True) -> None:
        meta = self.get_meta(strategy)
        if not meta:
            self._logger.warning(f"策略 {strategy} 不存在,无需清空")
            return

        c = self.client
        db = self._database

        try:
            weights_count = c.query_df(
                f"SELECT count(*) as count FROM {db}.weights FINAL WHERE strategy = %(strategy)s",
                parameters={"strategy": strategy},
            ).iloc[0]["count"]
            returns_count = c.query_df(
                f"SELECT count(*) as count FROM {db}.returns FINAL WHERE strategy = %(strategy)s",
                parameters={"strategy": strategy},
            ).iloc[0]["count"]
            tags_count = c.query_df(
                f"SELECT count(*) as count FROM {db}.tags FINAL WHERE strategy = %(strategy)s",
                parameters={"strategy": strategy},
            ).iloc[0]["count"]
            self._logger.info(f"策略 {strategy} 数据概况:")
            self._logger.info(f"  - 策略状态: {meta.get('status', '未知')}")
            self._logger.info(f"  - 创建时间: {meta.get('create_time', '未知')}")
            self._logger.info(f"  - 最后更新: {meta.get('update_time', '未知')}")
            self._logger.info(f"  - 权重数据: {weights_count:,} 条")
            self._logger.info(f"  - 收益数据: {returns_count:,} 条")
            self._logger.info(f"  - 标签数据: {tags_count:,} 条")
        except Exception as e:
            self._logger.error(f"查询策略 {strategy} 数据概况失败: {e}")
            self._logger.info("将继续执行删除操作...")

        if human_confirm:
            self._logger.info("=" * 60)
            self._logger.info(f"⚠️  警告:即将删除策略 {strategy} 的所有数据")
            self._logger.info("=" * 60)
            confirm = input("请仔细确认上述信息,确认删除请输入 'DELETE' (大小写敏感): ")
            if confirm != "DELETE":
                self._logger.warning(f"取消清空策略 {strategy} 的所有数据")
                return
            self._logger.info("开始执行删除操作...")

        for table in ("metas", "weights", "returns", "tags"):
            c.command(
                f"DELETE FROM {db}.{table} WHERE strategy = %(strategy)s",
                parameters={"strategy": strategy},
            )
        self._logger.warning(f"策略 {strategy} 清空完成")

    def summary(self) -> dict:
        c = self.client
        db = self._database
        return {
            "metas": c.query_df(f"SELECT count(*) as c FROM {db}.metas FINAL").iloc[0]["c"],
            "weights": c.query_df(f"SELECT count(*) as c FROM {db}.weights FINAL").iloc[0]["c"],
            "returns": c.query_df(f"SELECT count(*) as c FROM {db}.returns FINAL").iloc[0]["c"],
            "tags": c.query_df(f"SELECT count(*) as c FROM {db}.tags FINAL").iloc[0]["c"],
            "strategies": c.query_df(f"SELECT count(DISTINCT strategy) as c FROM {db}.metas FINAL").iloc[0]["c"],
        }

    # ---------- repr ----------
    def __repr__(self) -> str:
        return f"OnlineManager(dsn={mask_dsn_password(self._dsn)!r}, database={self._database!r})"
