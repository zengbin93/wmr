"""``BaseManager`` 抽象基类。

签名严格对齐 ``czsc.traders.cwc`` 函数式接口(去 ``db`` / ``database`` 后),
对应飞书设计文档"三、API 接口定义"。

::

    BaseManager <|-- LocalManager  : DuckDB 后端
    BaseManager <|-- OnlineManager : ClickHouse 后端

子类需要实现的接口分为 6 组:
- 生命周期(connect / close / initialize)
- metas(get_meta / get_all_metas / set_meta / update_strategy_status / get_strategies_by_status)
- weights(publish_weights / get_strategy_weights / get_latest_weights)
- returns(publish_returns / get_strategy_returns)
- tags(add_tag / add_tags / list_tags / remove_tag)
- 心跳与运维(heartbeat / clear_strategy / summary)
"""

from __future__ import annotations

import operator
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import pandas as pd

from wmr.utils import _ensure_series_tz

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

_BaseManagerT = TypeVar("_BaseManagerT", bound="BaseManager")

VALID_STATUSES: frozenset[str] = frozenset({"实盘", "废弃"})
"""策略合法状态枚举,对齐 cwc.update_strategy_status。``frozenset`` 防止运行时被 mutate。"""

VALID_WEIGHT_TYPES: frozenset[str] = frozenset({"ts", "cs"})
"""策略权重类型枚举:``ts`` 时序、``cs`` 截面。"""


class BaseManager(ABC):
    """策略持仓权重管理器抽象基类。

    定义 4 张表(``metas`` / ``weights`` / ``returns`` / ``tags``)与 3 个视图
    (``cs_latest_weights`` / ``ts_latest_weights`` / ``latest_weights``)的统一
    操作接口。子类实现需保证:**双后端等价**——同样输入下,``LocalManager``
    与 ``OnlineManager`` 的所有公共 API 必须返回语义一致的结果。
    """

    # 子类构造函数必须设置以下属性,模板方法 ``_publish_dataframe`` 会读取它们。
    _logger: Any
    _tz: ZoneInfo

    # ---------- 生命周期 ----------
    @abstractmethod
    def connect(self) -> Any:
        """建立后端连接,幂等。

        Returns:
            后端 driver 的 client 对象(DuckDB connection 或 clickhouse_connect Client)。
        """

    @abstractmethod
    def close(self) -> None:
        """关闭后端连接。重复调用安全。"""

    @abstractmethod
    def initialize(self) -> None:
        """初始化数据库:创建 4 张表与 3 个视图。

        对齐 ``cwc.initialize`` + ``cwc.init_tables`` + ``cwc.init_latest_weights_view``。
        允许重复调用(幂等)。
        """

    def __enter__(self: _BaseManagerT) -> _BaseManagerT:
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # ---------- metas ----------
    @abstractmethod
    def get_meta(self, strategy: str) -> dict:
        """获取策略元数据。

        Args:
            strategy: 策略名。

        Returns:
            元数据 dict;不存在返回空 dict。datetime 字段带 ``Asia/Shanghai`` 时区。
        """

    @abstractmethod
    def get_all_metas(self) -> pd.DataFrame:
        """获取所有策略元数据。

        Returns:
            DataFrame,datetime 列带 ``Asia/Shanghai`` 时区;无策略返回空 DataFrame。
        """

    @abstractmethod
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
        """设置策略元数据。

        参数顺序与默认值严格对齐 ``cwc.set_meta``。

        Args:
            strategy: 策略名,唯一不可空。
            base_freq: 周期。
            description: 描述。
            author: 作者。
            outsample_sdt: 样本外起始时间,可为 str / datetime / pd.Timestamp。
            weight_type: 权重类型,``ts`` 或 ``cs``,默认 ``ts``。
            status: 策略状态,``实盘`` 或 ``废弃``,默认 ``实盘``。
            memo: 备忘信息,默认空串。
            overwrite: 已存在时是否覆盖,默认 False(已存在时不写入,仅打 warning)。
        """

    @abstractmethod
    def update_strategy_status(self, strategy: str, status: str) -> None:
        """更新策略状态。

        Args:
            strategy: 策略名。
            status: 新状态,必须是 ``实盘`` 或 ``废弃``。

        Raises:
            ValueError: ``status`` 不在合法集合中。
        """

    @abstractmethod
    def get_strategies_by_status(self, status: str | None = None) -> pd.DataFrame:
        """按状态筛选策略。

        Args:
            status: 状态过滤;``None`` 表示返回全部。

        Returns:
            匹配的策略元数据 DataFrame。
        """

    # ---------- weights ----------
    @abstractmethod
    def publish_weights(self, strategy: str, df: pd.DataFrame, batch_size: int = 100000) -> None:
        """发布策略持仓权重(**仅追加** ``dt > latest_dt``)。

        对齐 ``cwc.publish_weights``:
        1. 调用前后均触发 ``heartbeat``(每个 batch 之间也调用一次)
        2. 输入 DataFrame 须含 ``dt`` / ``symbol`` / ``weight`` 三列
        3. 按 ``get_latest_weights(strategy)`` 查询每个 symbol 的最新 dt,过滤 ``dt > latest_dt``
        4. 按 ``(symbol, dt, strategy)`` 去重后按 ``batch_size`` 分批写入

        Args:
            strategy: 策略名,必须先 ``set_meta`` 注册。
            df: 持仓权重 DataFrame,必含 ``dt`` / ``symbol`` / ``weight``。
            batch_size: 分批大小,默认 10 万。
        """

    @abstractmethod
    def get_strategy_weights(
        self,
        strategy: str,
        sdt: Any = None,
        edt: Any = None,
        symbols: str | list[str] | None = None,
    ) -> pd.DataFrame:
        """获取策略持仓权重。

        Args:
            strategy: 策略名。
            sdt: 开始时间(含),``None`` 表示不限。
            edt: 结束时间(含),``None`` 表示不限。
            symbols: 标的过滤,可为单个 symbol 字符串或列表。

        Returns:
            按 ``(dt, symbol)`` 排序的 DataFrame。
        """

    @abstractmethod
    def get_latest_weights(self, strategy: str | None = None) -> pd.DataFrame:
        """从 ``latest_weights`` 视图查最新持仓。

        Args:
            strategy: 策略过滤;``None`` 返回全部策略的最新持仓。

        Returns:
            按 ``(strategy, dt, symbol)`` 排序的 DataFrame。
        """

    # ---------- returns ----------
    @abstractmethod
    def publish_returns(self, strategy: str, df: pd.DataFrame, batch_size: int = 100000) -> None:
        """发布策略日收益(允许覆盖同日 ``dt >= latest_dt``)。

        与 ``publish_weights`` 类似,但允许覆盖同日数据(过滤条件用
        ``dt >= latest_dt``),支持当日收益修正。

        Args:
            strategy: 策略名。
            df: 日收益 DataFrame,必含 ``dt`` / ``symbol`` / ``returns``。
            batch_size: 分批大小,默认 10 万。
        """

    @abstractmethod
    def get_strategy_returns(
        self,
        strategy: str,
        sdt: Any = None,
        edt: Any = None,
        symbols: str | list[str] | None = None,
    ) -> pd.DataFrame:
        """获取策略日收益。

        Args:
            strategy: 策略名。
            sdt: 开始日(自动 truncate 到 00:00:00,含)。
            edt: 结束日(自动 truncate 到 23:59:59,含)。
            symbols: 标的过滤。

        Returns:
            按 ``(dt, symbol)`` 排序的 DataFrame。
        """

    # ---------- tags ----------
    @abstractmethod
    def add_tag(self, strategy: str, tag: str, creator: str = "system") -> None:
        """添加单条标签。

        同一 ``(strategy, tag)`` 重复 add 应**幂等**(保留最后一条)。

        Args:
            strategy: 策略名。
            tag: 标签值。
            creator: 创建者,默认 ``system``。
        """

    @abstractmethod
    def add_tags(self, items: Iterable[tuple[str, str]], batch_size: int = 500) -> int:
        """批量添加标签。

        ⚠️ 与 ``add_tag`` 不同,本方法**不接受 creator 字段**——批量写入时
        ``creator`` 固定写为 ``"system"``,这是为了对齐 cwc.py 的入参签名
        (``Iterable[tuple[str, str]]``)。如需为标签指定具体 creator,请逐条
        调用 ``add_tag(strategy, tag, creator=...)``。

        Args:
            items: ``(strategy, tag)`` 二元组迭代器。
            batch_size: 分批大小,默认 500。

        Returns:
            处理的输入条数(注意:不区分 "新增" 与 "覆盖" — 同一
            ``(strategy, tag)`` 重复出现也计入)。
        """

    @abstractmethod
    def list_tags(self, strategy: str | None = None, tag: str | None = None) -> pd.DataFrame:
        """列出标签,支持按 strategy / tag 过滤。

        Args:
            strategy: 策略名过滤,``None`` 不过滤。
            tag: 标签值过滤,``None`` 不过滤。

        Returns:
            按 ``(strategy, tag)`` 排序的 DataFrame。
        """

    @abstractmethod
    def remove_tag(self, strategy: str, tag: str) -> None:
        """删除单条 ``(strategy, tag)``。不存在时 silent。"""

    # ---------- 心跳与运维 ----------
    @abstractmethod
    def heartbeat(self, strategy: str) -> None:
        """更新 ``metas.heartbeat_time`` 为当前时间。

        策略不存在时仅 warning,不抛异常(对齐 cwc 行为)。
        """

    @abstractmethod
    def clear_strategy(self, strategy: str, human_confirm: bool = True) -> None:
        """级联清空 ``metas`` / ``weights`` / ``returns`` / ``tags`` 中策略相关数据。

        Args:
            strategy: 策略名。
            human_confirm: 是否需要人工确认,默认 True。
                True 时打印数据概况后通过 ``input("DELETE")`` 等待用户输入字面量
                ``"DELETE"`` 确认;非交互场景下传 False 直接删除。
        """

    @abstractmethod
    def summary(self) -> dict:
        """返回各表行数与策略数汇总信息。

        Returns:
            dict,至少包含 ``metas`` / ``weights`` / ``returns`` / ``tags`` /
            ``strategies`` 五个键。
        """

    # ---------- publish 流水线模板(双后端共享) ----------
    @abstractmethod
    def _query_symbol_latest_dt(self, strategy: str, table: str) -> dict[str, pd.Timestamp]:
        """返回 ``{symbol: latest_dt}``。

        ``publish_*`` 流水线在过滤"仅追加 / 允许覆盖"时调用此钩子。表为空或策略
        无历史数据时返回空 dict。

        Args:
            strategy: 策略名。
            table: ``"weights"`` 或 ``"returns"``。

        Returns:
            带时区(``self._tz``)的 ``pd.Timestamp`` 字典。
        """

    @abstractmethod
    def _insert_publish_batch(self, table: str, batch: pd.DataFrame) -> None:
        """把一批已规范化的 DataFrame 写入指定表(子类负责时间戳格式适配)。

        Args:
            table: 目标表名(``"weights"`` / ``"returns"``)。
            batch: 含 ``strategy / symbol / dt / <value_col> / update_time`` 的
                DataFrame,``dt`` / ``update_time`` 带时区。
        """

    def _publish_dataframe(
        self,
        strategy: str,
        df: pd.DataFrame,
        *,
        table: Literal["weights", "returns"],
        value_col: str,
        mode: Literal["append", "upsert"],
        batch_size: int,
    ) -> None:
        """`publish_weights` / `publish_returns` 共享流水线。

        4 步标准化流程:

        1. 抽列 + 附 strategy + 时区归一
        2. 按 ``mode`` 过滤:``append`` 仅 ``dt > latest``,``upsert`` 允许 ``dt >= latest``
        3. 排序、去重、float 化、加 ``update_time``
        4. ``batch_size`` 分批调用 ``_insert_publish_batch``

        Args:
            strategy: 策略名。
            df: 输入 DataFrame,必含 ``dt`` / ``symbol`` / ``<value_col>`` 三列。
            table: 目标表(决定 latest_dt 查询源)。
            value_col: 业务值列名(``weight`` / ``returns``)。
            mode: ``append`` = 仅追加;``upsert`` = 允许覆盖同日。
            batch_size: 分批大小。
        """
        # ---------- 1. 标准化输入 ----------
        df = df[["dt", "symbol", value_col]].copy()
        df["strategy"] = strategy
        df["dt"] = _ensure_series_tz(df["dt"], tz=self._tz)

        # ---------- 2. 过滤 latest_dt ----------
        symbol_dt = self._query_symbol_latest_dt(strategy, table)
        if symbol_dt:
            cmp = operator.gt if mode == "append" else operator.ge
            self._logger.info(f"策略 {strategy} 最新时间:{max(symbol_dt.values())}")
            rows: list[pd.DataFrame] = []
            for symbol, dfg in df.groupby("symbol"):
                latest_dt = symbol_dt.get(str(symbol))
                if latest_dt is not None:
                    dfg = dfg[cmp(dfg["dt"], latest_dt)].copy().reset_index(drop=True)
                rows.append(dfg)
            if rows:
                df = pd.concat(rows, ignore_index=True)
            self._logger.info(f"策略 {strategy} 共 {len(df)} 条新数据")

        # ---------- 3. 排序 + 去重 + 加 update_time ----------
        df = df.sort_values(["dt", "symbol"]).reset_index(drop=True)
        df["update_time"] = pd.Timestamp.now(tz=self._tz)
        df = df[["strategy", "symbol", "dt", value_col, "update_time"]].copy()
        df = df.drop_duplicates(["symbol", "dt", "strategy"], keep="last").reset_index(drop=True)
        df[value_col] = df[value_col].astype(float)

        # ---------- 4. 分批写入 ----------
        for i in range(0, len(df), batch_size):
            batch = df.iloc[i : i + batch_size]
            self._insert_publish_batch(table, batch)
            self._logger.info(f"完成批次 {i // batch_size + 1},发布 {len(batch)} 条 {table}")
        self._logger.info(f"完成所有 {table} 发布,共 {len(df)} 条")
