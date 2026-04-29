# 心跳拆表实施 Plan(0.2.0a1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `heartbeat_time` 从 `metas` 表拆到独立 `heartbeats` 表,解除 ClickHouse 高频 mutation 写放大;OnlineManager / LocalManager 双端同步改造,外部 API 表面不变。

**Architecture:** 新增 `heartbeats(strategy, heartbeat_time)`(Online: `ReplacingMergeTree(heartbeat_time)`;Local: `PRIMARY KEY` upsert);`metas` 表移除 `heartbeat_time` 列;`heartbeat()` 改成 INSERT/UPSERT(原本 ClickHouse 端走 `ALTER UPDATE` mutation);`get_meta()` 系列 LEFT JOIN heartbeats 注入 `heartbeat_time`;新增 `get_heartbeat / list_heartbeats` 双端 API;`publish_weights/returns` 统一只在 end 调一次心跳。Alpha 阶段不写迁移代码,要求清库重建。

**Tech Stack:** Python 3.10–3.12、DuckDB(LocalManager)、ClickHouse + clickhouse-connect(OnlineManager)、pytest、testcontainers、ruff、basedpyright、uv。

**关键参考:**
- 设计稿:`docs/superpowers/specs/2026-04-29-heartbeat-table-design.md`
- parity 硬约束:`tests/unit/test_signature_alignment.py` + `tests/parity/test_parity.py`
- 测试环境:本机 macOS + Colima 跑 ClickHouse testcontainers 需要导出三个环境变量(见 §0)

---

## §0 公共测试命令(每次跑测试前都要导出)

本机 macOS + Colima 环境下,跑 `pytest tests/integration/test_online_*.py` / `tests/parity/` 必须先导出三个变量。**每个 Task 中"Run pytest"步骤都要带上这组前缀**,否则 docker socket 找不到或 Ryuk 容器创建失败。

```bash
export DOCKER_HOST="unix:///Users/lifanngzhou/.colima/default/docker.sock"
export TESTCONTAINERS_RYUK_DISABLED=true
export TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock
```

下文的 `pytest` 命令省略这段 export 前缀,但每个新 shell session 都要先 export。CI 环境无此问题。

---

## Task 1: BaseManager 契约 + signature_alignment 期望签名

**目的:** 把"新增 `get_heartbeat` / `list_heartbeats`、`heartbeat` 文档措辞调整、`summary` 文档加 heartbeats 计数说明"这些**契约层**改动先落地。BaseManager 的 abstract 方法决定后续 Local/Online 必须实现什么。signature_alignment 是契约的回归网。

**Files:**
- Modify: `wmr/base.py`(+ ~30 行 abstract 声明)
- Modify: `tests/unit/test_signature_alignment.py:33-101`(EXPECTED_SIGNATURES 加两条)
- Modify: `wmr/base.py:182-195`(`publish_weights` 文档措辞调整,去掉"调用前后均触发 heartbeat"那句)

- [ ] **Step 1: 在 `tests/unit/test_signature_alignment.py:94-96` `heartbeat` 条目之后,`clear_strategy` 之前,插入两条新方法的期望签名**

```python
    "heartbeat": [
        ("strategy", _NO_DEFAULT),
    ],
    "get_heartbeat": [
        ("strategy", _NO_DEFAULT),
    ],
    "list_heartbeats": [],
    "clear_strategy": [
        ("strategy", _NO_DEFAULT),
        ("human_confirm", True),
    ],
```

- [ ] **Step 2: 跑测试,确认两个新用例 FAIL**

```bash
uv run pytest tests/unit/test_signature_alignment.py -v --no-cov 2>&1 | tail -20
```

期望:`test_base_manager_signature_matches_expected[get_heartbeat]` 与 `[list_heartbeats]` FAIL,报 `AttributeError: type object 'BaseManager' has no attribute 'get_heartbeat'/'list_heartbeats'`。

- [ ] **Step 3: 修改 `wmr/base.py:311-315` `heartbeat` 文档措辞**

把原来的 docstring "更新 ``metas.heartbeat_time`` 为当前时间" 改成"记录策略心跳到 ``heartbeats`` 表(写入 / upsert 一行)"。

```python
    @abstractmethod
    def heartbeat(self, strategy: str) -> None:
        """记录策略心跳到 ``heartbeats`` 表(写入 / upsert 一行)。

        策略不存在时仅 warning,不抛异常(对齐 cwc 行为)。
        """
```

- [ ] **Step 4: 在 `wmr/base.py` `heartbeat` 之后追加 `get_heartbeat` 与 `list_heartbeats` abstract 方法**

紧接 `def heartbeat(...)` 后插入(在 `clear_strategy` 之前):

```python
    @abstractmethod
    def get_heartbeat(self, strategy: str) -> pd.Timestamp | None:
        """获取指定策略的最新心跳时间。

        Args:
            strategy: 策略名。

        Returns:
            最新心跳时间(``Asia/Shanghai`` 时区);策略无心跳记录时返回 ``None``。
        """

    @abstractmethod
    def list_heartbeats(self) -> pd.DataFrame:
        """列出所有策略的最新心跳时间。

        Returns:
            DataFrame,列 ``strategy``、``heartbeat_time``;按 ``heartbeat_time`` 倒序排序。
            无任何心跳记录时返回空 DataFrame。
        """
```

- [ ] **Step 5: 修改 `wmr/base.py:182-195` `publish_weights` docstring,去掉 "1. 调用前后均触发 heartbeat(每个 batch 之间也调用一次)" 那一行,改成 "1. publish 完成后触发 ``heartbeat``(成功路径)"**

```python
    @abstractmethod
    def publish_weights(self, strategy: str, df: pd.DataFrame, batch_size: int = 100000) -> None:
        """发布策略持仓权重(**仅追加** ``dt > latest_dt``)。

        对齐 ``cwc.publish_weights``:
        1. publish 完成后触发 ``heartbeat``(成功路径)
        2. 输入 DataFrame 须含 ``dt`` / ``symbol`` / ``weight`` 三列
        3. 按 ``get_latest_weights(strategy)`` 查询每个 symbol 的最新 dt,过滤 ``dt > latest_dt``
        4. 按 ``(symbol, dt, strategy)`` 去重后按 ``batch_size`` 分批写入

        Args:
            strategy: 策略名,必须先 ``set_meta`` 注册。
            df: 持仓权重 DataFrame,必含 ``dt`` / ``symbol`` / ``weight``。
            batch_size: 分批大小,默认 10 万。
        """
```

- [ ] **Step 6: 跑测试,确认两个新用例 PASS**

```bash
uv run pytest tests/unit/test_signature_alignment.py -v --no-cov 2>&1 | tail -20
```

期望:`test_base_manager_signature_matches_expected[get_heartbeat]` 与 `[list_heartbeats]` PASS,既有用例继续 PASS。

- [ ] **Step 7: 跑全套 unit 测试,确认无回归**

```bash
uv run pytest tests/unit/ --no-cov -q 2>&1 | tail -10
```

期望:全部 PASS(unit 测试不实例化 LocalManager/OnlineManager,abstract 方法变化不影响)。

- [ ] **Step 8: 提交**

```bash
git add wmr/base.py tests/unit/test_signature_alignment.py
git commit -m "feat(base): 契约层加 get_heartbeat / list_heartbeats abstract + 调整 heartbeat/publish_weights 文档"
```

---

## Task 2: LocalManager — schema + heartbeat + set_meta + get_meta 系列 + 新 API

**目的:** Local 端一次性把 schema、heartbeat 写入路径、set_meta 联动、get_meta/get_all_metas/get_strategies_by_status 注入、新 API 全部落地。这是一个原子 commit:中间任何一步落下都会让 Local 测试断。OnlineManager 在 Task 3 之前会因没实现 abstract 方法而无法实例化,所以 **Task 2 的 commit 之后跑 online 测试会炸,Task 3 完成后才恢复**。这是已知中间态。

**Files:**
- Modify: `wmr/local.py`(initialize DDL、heartbeat、set_meta、get_meta、get_all_metas、get_strategies_by_status,新增 get_heartbeat、list_heartbeats)
- Modify: `tests/integration/test_local_metas.py`(schema 断言 + get_meta 仍含 heartbeat_time)
- Create: `tests/integration/test_local_heartbeats.py`(新 API 集中验收)

- [ ] **Step 1: 写新测试文件 `tests/integration/test_local_heartbeats.py`**

```python
"""LocalManager 心跳拆表后的集中验收测试。

覆盖 0.2.0a1 之后 heartbeats 表的写入路径、读取路径、与 metas 的 LEFT JOIN
注入是否正确。
"""

from __future__ import annotations

import time

import pandas as pd
import pytest

pytestmark = pytest.mark.integration


def _seed_meta(mgr, strategy: str = "S1") -> None:
    mgr.set_meta(
        strategy=strategy,
        base_freq="日线",
        description="测试",
        author="tester",
        outsample_sdt="2024-01-01",
    )


def test_heartbeats_table_exists_and_metas_no_heartbeat_time(local_mgr):
    """schema:metas 不含 heartbeat_time 列,heartbeats 表存在且只有两列。"""
    metas_cols = {row[1] for row in local_mgr.conn.execute("PRAGMA table_info('metas')").fetchall()}
    assert "heartbeat_time" not in metas_cols, f"metas 仍含 heartbeat_time: {metas_cols}"

    hb_cols = {row[1] for row in local_mgr.conn.execute("PRAGMA table_info('heartbeats')").fetchall()}
    assert hb_cols == {"strategy", "heartbeat_time"}, f"heartbeats 列异常: {hb_cols}"


def test_set_meta_writes_initial_heartbeat(local_mgr):
    """set_meta 后 get_heartbeat 立即返回非 None 时间戳。"""
    _seed_meta(local_mgr, "S1")
    hb = local_mgr.get_heartbeat("S1")
    assert hb is not None
    assert isinstance(hb, pd.Timestamp)
    assert hb.tzinfo is not None  # tz-aware


def test_get_heartbeat_missing_returns_none(local_mgr):
    """从未 set_meta 过的策略,get_heartbeat 返回 None。"""
    assert local_mgr.get_heartbeat("ghost") is None


def test_heartbeat_upserts_latest_value(local_mgr):
    """多次 heartbeat 同一 strategy:get_heartbeat 取最新,list_heartbeats 行数不变。"""
    _seed_meta(local_mgr, "S1")
    first = local_mgr.get_heartbeat("S1")
    time.sleep(0.05)  # 保证时间戳不同
    local_mgr.heartbeat("S1")
    second = local_mgr.get_heartbeat("S1")
    assert second > first
    df = local_mgr.list_heartbeats()
    assert (df["strategy"] == "S1").sum() == 1, "同 strategy 多次心跳后行数不应增加"


def test_list_heartbeats_orders_desc_by_time(local_mgr):
    """list_heartbeats 按 heartbeat_time 倒序。"""
    _seed_meta(local_mgr, "A")
    time.sleep(0.05)
    _seed_meta(local_mgr, "B")
    df = local_mgr.list_heartbeats()
    assert list(df["strategy"]) == ["B", "A"]


def test_get_meta_includes_heartbeat_time(local_mgr):
    """get_meta 返回字典仍含 heartbeat_time 键(LEFT JOIN 注入)。"""
    _seed_meta(local_mgr, "S1")
    meta = local_mgr.get_meta("S1")
    assert "heartbeat_time" in meta
    assert meta["heartbeat_time"] is not None


def test_get_all_metas_includes_heartbeat_time_column(local_mgr):
    """get_all_metas 返回 DataFrame 仍含 heartbeat_time 列。"""
    _seed_meta(local_mgr, "S1")
    df = local_mgr.get_all_metas()
    assert "heartbeat_time" in df.columns
    assert df.loc[df["strategy"] == "S1", "heartbeat_time"].notna().all()
```

- [ ] **Step 2: 跑新测试,期望全部 FAIL(schema 还没改、新 API 还没实现)**

```bash
uv run pytest tests/integration/test_local_heartbeats.py -v --no-cov 2>&1 | tail -25
```

期望:7 个用例全 FAIL/ERROR(`heartbeats` 表不存在 / `get_heartbeat` 不是 LocalManager 属性等)。

- [ ] **Step 3: 修改 `wmr/local.py::initialize` 的 DDL**

定位 `wmr/local.py` 的 `initialize` 方法(第 123 行起)。在 `for table_name, ddl in (...)` 元组里:

(a) 把 metas 块的 `heartbeat_time TIMESTAMP,` 一行**删除**。

(b) 在 `tags` 之后(注意保持 INSERT 顺序与表名顺序一致)追加新元组:

```python
            (
                "heartbeats",
                """
                CREATE TABLE IF NOT EXISTS heartbeats (
                    strategy       VARCHAR PRIMARY KEY,
                    heartbeat_time TIMESTAMP
                )
                """,
            ),
```

- [ ] **Step 4: 修改 `wmr/local.py::heartbeat`(第 536 行起)**

把原来的 `UPDATE metas SET heartbeat_time = ? WHERE strategy = ?` 改成 UPSERT 到 heartbeats 表。完整新方法:

```python
    def heartbeat(self, strategy: str) -> None:
        meta = self.get_meta(strategy)
        if not meta:
            self._logger.warning(f"策略 {strategy} 不存在元数据,无法发送心跳")
            return
        c = self.conn
        current_time = pd.Timestamp.now(tz=self._tz).to_pydatetime().replace(tzinfo=None)
        c.execute(
            "INSERT OR REPLACE INTO heartbeats (strategy, heartbeat_time) VALUES (?, ?)",
            [strategy, current_time],
        )
        self._vlog(f"heartbeat({strategy}) ok")
```

> **DuckDB 时区处理说明:** `LocalManager` 既有路径都是把 tz-aware Timestamp 转成 naive datetime(local time)再写入 DuckDB,读出后再 `_localize_dataframe_columns` 重附时区。保持同一约定。

- [ ] **Step 5: 修改 `wmr/local.py::set_meta`(第 252 行 INSERT 块周围)**

定位 `INSERT INTO metas` 那段:

```python
            INSERT INTO metas
            (strategy, base_freq, description, author, outsample_sdt, create_time,
             update_time, heartbeat_time, weight_type, status, memo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

改成(去掉 `heartbeat_time` 列与对应占位符):

```python
            INSERT INTO metas
            (strategy, base_freq, description, author, outsample_sdt, create_time,
             update_time, weight_type, status, memo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

VALUES 数组同步去掉对应那个时间戳元素(原代码会把 `current_time` 用两次,一次 update_time、一次 heartbeat_time;新代码只保留一次 update_time)。

在 `set_meta` **方法末尾**(`self._logger.info(f"{strategy} set_meta: ok")` **之后**)追加:

```python
        self.heartbeat(strategy)
```

> **理由(对齐 spec § 4.1):** 保持"创建即活跃"语义 —— set_meta 后立即写一行心跳。

- [ ] **Step 6: 修改 `wmr/local.py::get_meta`(第 227 行起)的 SQL**

改前:`SELECT * FROM metas WHERE strategy = ?`。改后用 LEFT JOIN heartbeats:

```python
    def get_meta(self, strategy: str) -> dict:
        c = self.conn
        df = c.execute(
            """
            SELECT m.*, h.heartbeat_time
            FROM metas m
            LEFT JOIN heartbeats h ON m.strategy = h.strategy
            WHERE m.strategy = ?
            """,
            [strategy],
        ).df()
        if df.empty:
            self._logger.debug(f"策略 {strategy} 不存在元数据")
            return {}
        df = _localize_dataframe_columns(
            df, ["outsample_sdt", "create_time", "update_time", "heartbeat_time"], tz=self._tz
        )
        return df.iloc[0].to_dict()
```

- [ ] **Step 7: 修改 `wmr/local.py::get_all_metas`(第 241 行)的 SQL**

```python
    def get_all_metas(self) -> pd.DataFrame:
        df = self.conn.execute(
            """
            SELECT m.*, h.heartbeat_time
            FROM metas m
            LEFT JOIN heartbeats h ON m.strategy = h.strategy
            """
        ).df()
        if not df.empty:
            df = _localize_dataframe_columns(
                df,
                ["outsample_sdt", "create_time", "update_time", "heartbeat_time"],
                tz=self._tz,
            )
        self._vlog(f"get_all_metas → {len(df)} 条")
        return df
```

- [ ] **Step 8: 修改 `wmr/local.py::get_strategies_by_status`(第 313 行附近)**

```python
    def get_strategies_by_status(self, status: str | None = None) -> pd.DataFrame:
        sql = """
            SELECT m.*, h.heartbeat_time
            FROM metas m
            LEFT JOIN heartbeats h ON m.strategy = h.strategy
        """
        params: list = []
        if status is not None:
            sql += " WHERE m.status = ?"
            params.append(status)
        df = self.conn.execute(sql, params).df()
        if not df.empty:
            df = _localize_dataframe_columns(
                df,
                ["outsample_sdt", "create_time", "update_time", "heartbeat_time"],
                tz=self._tz,
            )
        self._vlog(f"get_strategies_by_status(status={status!r}) → {len(df)} 条")
        return df
```

- [ ] **Step 9: 在 `wmr/local.py` 紧接 `heartbeat` 方法之后加 `get_heartbeat` / `list_heartbeats`**

```python
    def get_heartbeat(self, strategy: str) -> pd.Timestamp | None:
        df = self.conn.execute(
            "SELECT heartbeat_time FROM heartbeats WHERE strategy = ?",
            [strategy],
        ).df()
        if df.empty:
            self._vlog(f"get_heartbeat({strategy}) → None")
            return None
        df = _localize_dataframe_columns(df, ["heartbeat_time"], tz=self._tz)
        ts = df.iloc[0]["heartbeat_time"]
        self._vlog(f"get_heartbeat({strategy}) → {ts}")
        return ts

    def list_heartbeats(self) -> pd.DataFrame:
        df = self.conn.execute(
            "SELECT strategy, heartbeat_time FROM heartbeats ORDER BY heartbeat_time DESC"
        ).df()
        if not df.empty:
            df = _localize_dataframe_columns(df, ["heartbeat_time"], tz=self._tz)
        self._vlog(f"list_heartbeats → {len(df)} 行")
        return df
```

- [ ] **Step 10: 跑 Local 心跳新测试,期望全 PASS**

```bash
uv run pytest tests/integration/test_local_heartbeats.py -v --no-cov 2>&1 | tail -15
```

期望:7 passed。

- [ ] **Step 11: 跑既有 Local 集成测试 + unit 测试,确认无回归**

```bash
uv run pytest tests/unit/ tests/integration/test_local_*.py --no-cov -q 2>&1 | tail -10
```

期望:全部 PASS。

> **注意:** `tests/integration/test_local_metas.py` 中如果有 `assert sorted(meta.keys()) == [...]` 这类硬编码列名集合断言,会因为新增 `heartbeat_time` 在末尾、原本就有 `heartbeat_time` 列被注入而 PASS。如果列名顺序断言失败,在 Step 11 中 inline 修正,做法:用 `set` 包裹比较或更新列名清单,**不要** 改 `wmr/local.py`。

- [ ] **Step 12: 提交**

```bash
git add wmr/local.py tests/integration/test_local_heartbeats.py tests/integration/test_local_metas.py
git commit -m "feat(local): heartbeat 拆到独立 heartbeats 表 + 新增 get_heartbeat/list_heartbeats"
```

---

## Task 3: OnlineManager — schema + heartbeat + set_meta + get_meta 系列 + 新 API

**目的:** Online 端镜像 Task 2 的所有改动。完成后 abstract 方法都被双端实现,所有既有测试恢复绿。

**Files:**
- Modify: `wmr/online.py`
- Create: `tests/integration/test_online_heartbeats.py`(镜像 Task 2 Step 1 的测试,改为用 ClickHouse fixture)

- [ ] **Step 1: 创建 `tests/integration/test_online_heartbeats.py`**

```python
"""OnlineManager 心跳拆表后的集中验收测试,镜像 test_local_heartbeats.py。"""

from __future__ import annotations

import time

import pandas as pd
import pytest

pytestmark = pytest.mark.integration


def _seed_meta(mgr, strategy: str = "S1") -> None:
    mgr.set_meta(
        strategy=strategy,
        base_freq="日线",
        description="测试",
        author="tester",
        outsample_sdt="2024-01-01",
    )


def test_heartbeats_table_exists_and_metas_no_heartbeat_time(online_mgr):
    """schema:metas 不含 heartbeat_time 列,heartbeats 表存在且只有两列。"""
    db = online_mgr._database
    metas_df = online_mgr.client.query_df(f"DESCRIBE TABLE {db}.metas")
    assert "heartbeat_time" not in metas_df["name"].tolist()

    hb_df = online_mgr.client.query_df(f"DESCRIBE TABLE {db}.heartbeats")
    assert set(hb_df["name"].tolist()) == {"strategy", "heartbeat_time"}


def test_set_meta_writes_initial_heartbeat(online_mgr):
    _seed_meta(online_mgr, "S1")
    hb = online_mgr.get_heartbeat("S1")
    assert hb is not None
    assert isinstance(hb, pd.Timestamp)
    assert hb.tzinfo is not None


def test_get_heartbeat_missing_returns_none(online_mgr):
    assert online_mgr.get_heartbeat("ghost") is None


def test_heartbeat_upserts_latest_value(online_mgr):
    _seed_meta(online_mgr, "S1")
    first = online_mgr.get_heartbeat("S1")
    time.sleep(1.1)  # ClickHouse DateTime 秒级精度,确保差值能被取到
    online_mgr.heartbeat("S1")
    second = online_mgr.get_heartbeat("S1")
    assert second > first
    df = online_mgr.list_heartbeats()
    assert (df["strategy"] == "S1").sum() == 1


def test_list_heartbeats_orders_desc_by_time(online_mgr):
    _seed_meta(online_mgr, "A")
    time.sleep(1.1)
    _seed_meta(online_mgr, "B")
    df = online_mgr.list_heartbeats()
    assert list(df["strategy"]) == ["B", "A"]


def test_get_meta_includes_heartbeat_time(online_mgr):
    _seed_meta(online_mgr, "S1")
    meta = online_mgr.get_meta("S1")
    assert "heartbeat_time" in meta
    assert meta["heartbeat_time"] is not None


def test_get_all_metas_includes_heartbeat_time_column(online_mgr):
    _seed_meta(online_mgr, "S1")
    df = online_mgr.get_all_metas()
    assert "heartbeat_time" in df.columns
    assert df.loc[df["strategy"] == "S1", "heartbeat_time"].notna().all()
```

- [ ] **Step 2: 跑新测试,期望全 FAIL**

```bash
uv run pytest tests/integration/test_online_heartbeats.py -v --no-cov 2>&1 | tail -25
```

(确保已 export §0 那三个环境变量)期望 7 个用例全 FAIL/ERROR。

- [ ] **Step 3: 修改 `wmr/online.py::initialize` 的 DDL**

定位 `wmr/online.py:189-208` 的 metas DDL 与 `wmr/online.py:209-249` 的其他表 DDL。

(a) **删除** metas DDL 中 `heartbeat_time DateTime('Asia/Shanghai'),` 一行(原第 199 行)。

(b) 在 tags DDL(第 237-249 行)**之后**,视图创建之前(第 250 行之前),追加:

```python
        c.command(
            f"""
            CREATE TABLE IF NOT EXISTS {db}.heartbeats (
                strategy       String NOT NULL,
                heartbeat_time DateTime('Asia/Shanghai')
            )
            ENGINE = ReplacingMergeTree(heartbeat_time)
            ORDER BY strategy
            """
        )
        self._vlog(f"创建/复用表: {db}.heartbeats")
```

- [ ] **Step 4: 修改 `wmr/online.py::heartbeat`(第 612 行起)**

```python
    def heartbeat(self, strategy: str) -> None:
        meta = self.get_meta(strategy)
        if not meta:
            self._logger.warning(f"策略 {strategy} 不存在元数据,无法发送心跳")
            return
        current_time = _format_for_db(pd.Timestamp.now(tz=self._tz), tz=self._tz)
        # heartbeat 是观测信号,失败不应阻断业务写入(对齐 LocalManager 非阻断行为)。
        try:
            self.client.command(
                f"INSERT INTO {self._database}.heartbeats (strategy, heartbeat_time) VALUES (%(strategy)s, %(t)s)",
                parameters={"strategy": strategy, "t": current_time},
            )
        except Exception as e:
            self._vexc(f"发送心跳失败(已忽略): {e}")
            return
        self._vlog(f"heartbeat({strategy}) ok")
```

- [ ] **Step 5: 修改 `wmr/online.py::set_meta`(第 326 行起)**

定位 DataFrame 构造(第 347-363 行),从 dict 中**删除** `"heartbeat_time": current_time,` 一行。

在方法末尾(`self._logger.info(f"{strategy} set_meta: ok")` **之后**)追加:

```python
        self.heartbeat(strategy)
```

修改后的 set_meta DataFrame 构造段如下(仅展示改动相关部分):

```python
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
                    "weight_type": weight_type,
                    "status": status,
                    "memo": memo,
                }
            ]
        )
        self.client.insert_df(f"{self._database}.metas", df)
        self._logger.info(f"{strategy} set_meta: ok")
        self.heartbeat(strategy)
```

- [ ] **Step 6: 修改 `wmr/online.py::get_meta`(第 297 行)**

```python
    def get_meta(self, strategy: str) -> dict:
        c = self.client
        df = c.query_df(
            f"""
            SELECT m.*, h.heartbeat_time
            FROM {self._database}.metas AS m FINAL
            LEFT JOIN {self._database}.heartbeats AS h FINAL ON m.strategy = h.strategy
            WHERE m.strategy = %(strategy)s
            """,
            parameters={"strategy": strategy},
        )
        if df.empty:
            self._logger.debug(f"策略 {strategy} 不存在元数据")
            return {}
        df = _localize_dataframe_columns(
            df,
            ["outsample_sdt", "create_time", "update_time", "heartbeat_time"],
            tz=self._tz,
        )
        return df.iloc[0].to_dict()
```

- [ ] **Step 7: 修改 `wmr/online.py::get_all_metas`(第 315 行)**

```python
    def get_all_metas(self) -> pd.DataFrame:
        df = self.client.query_df(
            f"""
            SELECT m.*, h.heartbeat_time
            FROM {self._database}.metas AS m FINAL
            LEFT JOIN {self._database}.heartbeats AS h FINAL ON m.strategy = h.strategy
            """
        )
        if not df.empty:
            df = _localize_dataframe_columns(
                df,
                ["outsample_sdt", "create_time", "update_time", "heartbeat_time"],
                tz=self._tz,
            )
        self._vlog(f"get_all_metas → {len(df)} 条")
        return df
```

- [ ] **Step 8: 修改 `wmr/online.py::get_strategies_by_status`(第 383 行)**

```python
    def get_strategies_by_status(self, status: str | None = None) -> pd.DataFrame:
        sql = f"""
            SELECT m.*, h.heartbeat_time
            FROM {self._database}.metas AS m FINAL
            LEFT JOIN {self._database}.heartbeats AS h FINAL ON m.strategy = h.strategy
        """
        params: dict[str, Any] = {}
        if status is not None:
            sql += " WHERE m.status = %(status)s"
            params["status"] = status
        df = self.client.query_df(sql, parameters=params)
        if not df.empty:
            df = _localize_dataframe_columns(
                df,
                ["outsample_sdt", "create_time", "update_time", "heartbeat_time"],
                tz=self._tz,
            )
        self._vlog(f"get_strategies_by_status(status={status!r}) → {len(df)} 条")
        return df
```

- [ ] **Step 9: 在 `wmr/online.py` 紧接 `heartbeat` 之后追加 `get_heartbeat` / `list_heartbeats`**

```python
    def get_heartbeat(self, strategy: str) -> pd.Timestamp | None:
        df = self.client.query_df(
            f"SELECT heartbeat_time FROM {self._database}.heartbeats FINAL "
            "WHERE strategy = %(strategy)s",
            parameters={"strategy": strategy},
        )
        if df.empty:
            self._vlog(f"get_heartbeat({strategy}) → None")
            return None
        df = _localize_dataframe_columns(df, ["heartbeat_time"], tz=self._tz)
        ts = df.iloc[0]["heartbeat_time"]
        self._vlog(f"get_heartbeat({strategy}) → {ts}")
        return ts

    def list_heartbeats(self) -> pd.DataFrame:
        df = self.client.query_df(
            f"SELECT strategy, heartbeat_time FROM {self._database}.heartbeats FINAL "
            "ORDER BY heartbeat_time DESC"
        )
        if not df.empty:
            df = _localize_dataframe_columns(df, ["heartbeat_time"], tz=self._tz)
        self._vlog(f"list_heartbeats → {len(df)} 行")
        return df
```

- [ ] **Step 10: 跑 Online 心跳新测试,期望全 PASS**

```bash
uv run pytest tests/integration/test_online_heartbeats.py -v --no-cov 2>&1 | tail -15
```

期望:7 passed。

- [ ] **Step 11: 跑既有 Online 集成测试,确认无回归**

```bash
uv run pytest tests/integration/test_online_basic.py tests/integration/test_online_full.py --no-cov -q 2>&1 | tail -10
```

期望:全部 PASS。如果某个用例硬断言列名集合,inline 修测试(列名集合需含 `heartbeat_time`)。

- [ ] **Step 12: 提交**

```bash
git add wmr/online.py tests/integration/test_online_heartbeats.py tests/integration/test_online_basic.py tests/integration/test_online_full.py
git commit -m "feat(online): heartbeat 拆到独立 heartbeats 表 + 新增 get_heartbeat/list_heartbeats"
```

---

## Task 4: 双端 clear_strategy 删 heartbeats + summary 加计数

**目的:** 把"清空策略"与"汇总"两个运维 API 配合到新表上。

**Files:**
- Modify: `wmr/local.py::clear_strategy`、`wmr/local.py::summary`
- Modify: `wmr/online.py::clear_strategy`、`wmr/online.py::summary`
- Modify: `tests/integration/test_local_heartbeats.py`(加 clear/summary 测试)
- Modify: `tests/integration/test_online_heartbeats.py`(同上)

- [ ] **Step 1: 在 `tests/integration/test_local_heartbeats.py` 末尾追加 4 个测试**

```python
def test_clear_strategy_removes_heartbeat(local_mgr):
    """clear_strategy 同时删 heartbeats 表中该策略的行。"""
    _seed_meta(local_mgr, "S1")
    assert local_mgr.get_heartbeat("S1") is not None
    local_mgr.clear_strategy("S1", human_confirm=False)
    assert local_mgr.get_heartbeat("S1") is None


def test_clear_strategy_other_heartbeats_untouched(local_mgr):
    """clear_strategy 只删指定策略,其它策略心跳不动。"""
    _seed_meta(local_mgr, "S1")
    _seed_meta(local_mgr, "S2")
    local_mgr.clear_strategy("S1", human_confirm=False)
    assert local_mgr.get_heartbeat("S2") is not None


def test_summary_includes_heartbeats_count(local_mgr):
    """summary() 返回字典含 heartbeats 字段,值为 heartbeats 表行数。"""
    _seed_meta(local_mgr, "A")
    _seed_meta(local_mgr, "B")
    s = local_mgr.summary()
    assert "heartbeats" in s
    assert s["heartbeats"] == 2


def test_summary_heartbeats_zero_initially(local_mgr):
    """空库 summary 的 heartbeats 字段为 0。"""
    s = local_mgr.summary()
    assert s["heartbeats"] == 0
```

- [ ] **Step 2: 在 `tests/integration/test_online_heartbeats.py` 末尾追加同样的 4 个测试**(把 `local_mgr` 全部替换成 `online_mgr`)

```python
def test_clear_strategy_removes_heartbeat(online_mgr):
    _seed_meta(online_mgr, "S1")
    assert online_mgr.get_heartbeat("S1") is not None
    online_mgr.clear_strategy("S1", human_confirm=False)
    assert online_mgr.get_heartbeat("S1") is None


def test_clear_strategy_other_heartbeats_untouched(online_mgr):
    _seed_meta(online_mgr, "S1")
    _seed_meta(online_mgr, "S2")
    online_mgr.clear_strategy("S1", human_confirm=False)
    assert online_mgr.get_heartbeat("S2") is not None


def test_summary_includes_heartbeats_count(online_mgr):
    _seed_meta(online_mgr, "A")
    _seed_meta(online_mgr, "B")
    s = online_mgr.summary()
    assert "heartbeats" in s
    assert s["heartbeats"] == 2


def test_summary_heartbeats_zero_initially(online_mgr):
    s = online_mgr.summary()
    assert s["heartbeats"] == 0
```

- [ ] **Step 3: 跑新测试,期望 8 个全 FAIL**

```bash
uv run pytest tests/integration/test_local_heartbeats.py tests/integration/test_online_heartbeats.py -v --no-cov -k "clear_strategy_removes_heartbeat or clear_strategy_other_heartbeats_untouched or summary_includes_heartbeats_count or summary_heartbeats_zero_initially" 2>&1 | tail -20
```

期望:8 个 FAIL/ERROR(`clear_strategy` 还没删 heartbeats、`summary` 没含 heartbeats)。

- [ ] **Step 4: 修改 `wmr/local.py::clear_strategy`(第 580 行起)**

定位现有 4 张表的 DELETE 顺序(weights / returns / tags / metas),在 metas 之前**插入**对 heartbeats 的清理:

```python
        c.execute("DELETE FROM heartbeats WHERE strategy = ?", [strategy])
```

紧贴现有 `c.execute("DELETE FROM metas WHERE strategy = ?", [strategy])` 之上一行(同级缩进)。

并把 `_logger.info` 那行的"`tags=..., metas=1`"扩展为 `"tags=..., heartbeats=1, metas=1"`(实际写就是按现有格式串在末尾):

```python
        self._logger.info(
            f"清空 {strategy}: weights={weights_count:,}, returns={returns_count:,}, "
            f"tags={tags_count:,}, heartbeats=1, metas=1, 耗时 {time.perf_counter() - t0:.2f}s"
        )
```

- [ ] **Step 5: 修改 `wmr/local.py::summary`(第 593 行起)**

修改 SQL,把 heartbeats 计数加到查询里:

```python
    def summary(self) -> dict:
        c = self.conn
        row = c.execute(
            """
            SELECT
                (SELECT count(*) FROM metas) AS metas,
                (SELECT count(*) FROM weights) AS weights,
                (SELECT count(*) FROM returns) AS returns,
                (SELECT count(*) FROM tags) AS tags,
                (SELECT count(*) FROM heartbeats) AS heartbeats,
                (SELECT count(DISTINCT strategy) FROM metas) AS strategies
            """
        ).fetchone()
        result = {
            "metas": int(row[0]),
            "weights": int(row[1]),
            "returns": int(row[2]),
            "tags": int(row[3]),
            "heartbeats": int(row[4]),
            "strategies": int(row[5]),
        }
        self._vlog(f"summary → {result}")
        return result
```

> **注意:** 上面 `summary` 的具体行号与字段顺序需对照 `wmr/local.py` 实际代码核对。如果原本 row 用的是命名访问而非位置访问,保持原风格,只新增 heartbeats 那一项。

- [ ] **Step 6: 修改 `wmr/online.py::clear_strategy`(第 629 行起)**

定位 `for table in ("metas", "weights", "returns", "tags"):` 这一行(第 672 行附近)。改成:

```python
        for table in ("metas", "weights", "returns", "tags", "heartbeats"):
```

并把 `_logger.info` 那行的 `"tags={tags_count:,}, metas=1"` 改成 `"tags={tags_count:,}, heartbeats=1, metas=1"`(对齐 LocalManager 文案):

```python
            f"tags={tags_count:,}, heartbeats=1, metas=1, 耗时 {time.perf_counter() - t0:.2f}s"
```

- [ ] **Step 7: 修改 `wmr/online.py::summary`(第 682 行起)**

```python
    def summary(self) -> dict:
        db = self._database
        row = self.client.query_df(
            f"""
            SELECT
                (SELECT count() FROM {db}.metas FINAL) AS metas,
                (SELECT count() FROM {db}.weights FINAL) AS weights,
                (SELECT count() FROM {db}.returns FINAL) AS returns,
                (SELECT count() FROM {db}.tags FINAL) AS tags,
                (SELECT count() FROM {db}.heartbeats FINAL) AS heartbeats,
                (SELECT count(DISTINCT strategy) FROM {db}.metas FINAL) AS strategies
            """
        ).iloc[0]
        result = {
            "metas": int(row["metas"]),
            "weights": int(row["weights"]),
            "returns": int(row["returns"]),
            "tags": int(row["tags"]),
            "heartbeats": int(row["heartbeats"]),
            "strategies": int(row["strategies"]),
        }
        self._vlog(f"summary → {result}")
        return result
```

> **注意:** 上面是基于 `wmr/online.py:682-704` 现有形态的等价改写,核对实际行号/写法,保持原风格只新增 heartbeats。

- [ ] **Step 8: 跑新测试,期望全 PASS**

```bash
uv run pytest tests/integration/test_local_heartbeats.py tests/integration/test_online_heartbeats.py -v --no-cov 2>&1 | tail -15
```

期望:11 + 4 = 11 + 4(每端 11 个),双端共 22 passed。

- [ ] **Step 9: 跑既有 clear_strategy / summary 测试,确认无回归**

```bash
uv run pytest tests/ --no-cov -q -k "clear_strategy or summary" 2>&1 | tail -15
```

期望:全部 PASS;若有用例断言 summary dict 的 key 集合,可能会断,inline 修(把 `heartbeats` 加进期望集合)。

- [ ] **Step 10: 提交**

```bash
git add wmr/local.py wmr/online.py tests/integration/test_local_heartbeats.py tests/integration/test_online_heartbeats.py
git commit -m "feat(local,online): clear_strategy 删 heartbeats + summary 含 heartbeats 计数"
```

---

## Task 5: 双端 publish 流程改造(去 begin 心跳 + 补 publish_returns 心跳)

**目的:** 落实 spec § 4.3 的 publish 流程统一:`publish_weights` 去掉 begin 心跳,`publish_returns` 双端原本都漏调,本次补齐 end 心跳。

**Files:**
- Modify: `wmr/local.py::publish_weights`、`wmr/local.py::publish_returns`
- Modify: `wmr/online.py::publish_weights`、`wmr/online.py::publish_returns`
- Modify: `tests/integration/test_local_weights.py`(扩展现有 `test_heartbeat_updates_strictly_increasing`)
- Modify: `tests/integration/test_local_heartbeats.py`(加 publish_returns 心跳验证)
- Modify: `tests/integration/test_online_heartbeats.py`(同上)

- [ ] **Step 1: 修改 `tests/integration/test_local_weights.py:106-116` 中的 `test_heartbeat_updates_strictly_increasing`**

把测试改成同时验证"严格递增"和"只在 end 调一次"。完整新方法:

```python
def test_heartbeat_updates_strictly_increasing(local_mgr):
    """F6:publish_weights 完成后 heartbeat_time 严格递增,且仅在 end 调一次心跳。"""
    import time as _time
    local_mgr.set_meta(
        strategy="ts1", base_freq="日线", description="d", author="a",
        outsample_sdt="2024-01-01",
    )
    before = local_mgr.get_heartbeat("ts1")
    _time.sleep(0.05)

    df = pd.DataFrame({
        "dt": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "symbol": ["AAA", "AAA"],
        "weight": [0.5, 0.6],
    })
    local_mgr.publish_weights("ts1", df)

    after = local_mgr.get_heartbeat("ts1")
    assert after > before, "publish 后 heartbeat 应严格大于 publish 前"
    # parity: 同一 strategy 多次心跳 UPSERT,行数不变
    assert (local_mgr.list_heartbeats()["strategy"] == "ts1").sum() == 1
```

> **注意:** 原测试用 `local_mgr.get_meta("ts1")["heartbeat_time"]` 读心跳。新版改用 `get_heartbeat("ts1")` 直接读,语义更清晰且对外 API 形成回归网。原 import 中如果只 `import pandas as pd`,保留;否则按需补 `import time as _time`(或顶部已有 import)。

- [ ] **Step 2: 在 `tests/integration/test_local_heartbeats.py` 末尾追加 publish_returns 心跳测试**

```python
def test_publish_returns_triggers_heartbeat(local_mgr):
    """publish_returns 完成后心跳更新(原本两端漏调,本次补齐)。"""
    import time as _time
    _seed_meta(local_mgr, "S1")
    before = local_mgr.get_heartbeat("S1")
    _time.sleep(0.05)

    df = pd.DataFrame({
        "dt": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "symbol": ["AAA", "AAA"],
        "returns": [0.01, 0.02],
    })
    local_mgr.publish_returns("S1", df)
    after = local_mgr.get_heartbeat("S1")
    assert after > before
```

- [ ] **Step 3: 在 `tests/integration/test_online_heartbeats.py` 末尾追加同样测试**(用 `online_mgr`,sleep 改 1.1s 适配 ClickHouse 秒级精度)

```python
def test_publish_returns_triggers_heartbeat(online_mgr):
    import time as _time
    _seed_meta(online_mgr, "S1")
    before = online_mgr.get_heartbeat("S1")
    _time.sleep(1.1)

    df = pd.DataFrame({
        "dt": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "symbol": ["AAA", "AAA"],
        "returns": [0.01, 0.02],
    })
    online_mgr.publish_returns("S1", df)
    after = online_mgr.get_heartbeat("S1")
    assert after > before
```

- [ ] **Step 4: 跑这三个新测试,期望 publish_returns 那两个 FAIL**

```bash
uv run pytest tests/integration/test_local_weights.py::test_heartbeat_updates_strictly_increasing tests/integration/test_local_heartbeats.py::test_publish_returns_triggers_heartbeat tests/integration/test_online_heartbeats.py::test_publish_returns_triggers_heartbeat -v --no-cov 2>&1 | tail -20
```

期望:`test_heartbeat_updates_strictly_increasing` 可能 PASS(begin 心跳还在,但严格递增条件仍满足);`test_publish_returns_triggers_heartbeat` 双端 FAIL(publish_returns 没调心跳)。

- [ ] **Step 5: 修改 `wmr/local.py::publish_weights`(第 328-343 行)**

把第 331 行 `self.heartbeat(strategy)` 删除(begin 心跳)。完整新方法:

```python
    def publish_weights(self, strategy: str, df: pd.DataFrame, batch_size: int = 100000) -> None:
        self._log_publish_entry(strategy, df, table="weights")
        t0 = time.perf_counter()
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
```

- [ ] **Step 6: 修改 `wmr/local.py::publish_returns`(第 392-405 行)**

在 `_publish_dataframe` 之后、`_logger.info` 之前**追加** `self.heartbeat(strategy)`:

```python
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
        self.heartbeat(strategy)
        self._logger.info(
            f"完成 publish_returns(strategy={strategy}, 实际写入 {n} 条, 耗时 {time.perf_counter() - t0:.2f}s)"
        )
```

- [ ] **Step 7: 修改 `wmr/online.py::publish_weights`(第 402-417 行)**

删除第 405 行 `self.heartbeat(strategy)`(begin 心跳),保留第 414 行的 end 心跳:

```python
    def publish_weights(self, strategy: str, df: pd.DataFrame, batch_size: int = 100000) -> None:
        self._log_publish_entry(strategy, df, table="weights")
        t0 = time.perf_counter()
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
```

- [ ] **Step 8: 修改 `wmr/online.py::publish_returns`(第 484-497 行)**

在 `_publish_dataframe` 之后、`_logger.info` 之前**追加** `self.heartbeat(strategy)`:

```python
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
        self.heartbeat(strategy)
        self._logger.info(
            f"完成 publish_returns(strategy={strategy}, 实际写入 {n} 条, 耗时 {time.perf_counter() - t0:.2f}s)"
        )
```

- [ ] **Step 9: 跑全套 publish 与心跳测试**

```bash
uv run pytest tests/integration/ -k "publish or heartbeat" --no-cov -q 2>&1 | tail -15
```

期望:全 PASS。

- [ ] **Step 10: 跑全套 unit + integration**

```bash
uv run pytest tests/unit/ tests/integration/ --no-cov -q 2>&1 | tail -10
```

期望:全 PASS;若有 verbose-mode 相关日志计数断言因 begin 心跳被去掉而断,inline 修(把"两次 heartbeat ok"调整为"一次 heartbeat ok")。

- [ ] **Step 11: 提交**

```bash
git add wmr/local.py wmr/online.py tests/integration/test_local_weights.py tests/integration/test_local_heartbeats.py tests/integration/test_online_heartbeats.py
git commit -m "feat(local,online): publish_weights 去 begin 心跳 + publish_returns 补齐 end 心跳(parity)"
```

---

## Task 6: parity 测试扩展

**目的:** 把心跳新功能加入 `tests/parity/test_parity.py`,锁住"双端结构与返回值一致"。

**Files:**
- Modify: `tests/parity/test_parity.py`(末尾加 parity 用例)

- [ ] **Step 1: 在 `tests/parity/test_parity.py` 末尾追加 3 个 parity 用例**

```python
def test_parity_get_heartbeat(both_mgr):
    """get_heartbeat 双端返回类型一致(tz-aware Timestamp 或 None)。"""
    both_mgr.set_meta(
        strategy="X", base_freq="日线", description="", author="a",
        outsample_sdt="2024-01-01",
    )
    hb = both_mgr.get_heartbeat("X")
    assert hb is not None
    assert hasattr(hb, "tzinfo") and hb.tzinfo is not None
    assert both_mgr.get_heartbeat("ghost") is None


def test_parity_list_heartbeats_columns(both_mgr):
    """list_heartbeats 双端列名与排序一致。"""
    import time as _time
    both_mgr.set_meta(strategy="A", base_freq="d", description="", author="a", outsample_sdt="2024-01-01")
    _time.sleep(1.1)
    both_mgr.set_meta(strategy="B", base_freq="d", description="", author="a", outsample_sdt="2024-01-01")
    df = both_mgr.list_heartbeats()
    assert list(df.columns) == ["strategy", "heartbeat_time"]
    assert list(df["strategy"]) == ["B", "A"]


def test_parity_summary_keys(both_mgr):
    """summary 双端字典 key 集合一致(含 heartbeats)。"""
    s = both_mgr.summary()
    assert set(s.keys()) == {"metas", "weights", "returns", "tags", "heartbeats", "strategies"}
```

- [ ] **Step 2: 跑 parity 测试**

```bash
uv run pytest tests/parity/ --no-cov -q 2>&1 | tail -15
```

期望:全 PASS(每个用例 2 次,local + online,共 6 个新用例 + 既有用例)。

- [ ] **Step 3: 提交**

```bash
git add tests/parity/test_parity.py
git commit -m "test(parity): 加 get_heartbeat / list_heartbeats / summary 双端 parity"
```

---

## Task 7: 版本号 bump + CHANGELOG + README 提示

**目的:** 0.1.0a1 → 0.2.0a1 破坏性发布,文档化升级路径。

**Files:**
- Modify: `pyproject.toml:3`(版本号)
- Create: `CHANGELOG.md`
- Modify: `README.md`(顶部加 0.2 升级提示链接)

- [ ] **Step 1: 修改 `pyproject.toml:3`**

```toml
version = "0.2.0a1"
```

- [ ] **Step 2: 创建 `CHANGELOG.md`**

```markdown
# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范,
并使用 [PEP 440](https://peps.python.org/pep-0440/) 版本号(alpha 阶段使用 `aN` 后缀)。

## 0.2.0a1 — 2026-04-29 — 心跳拆表(破坏性变更)

为彻底解除 ClickHouse 高频 mutation 带来的写放大与 system.mutations 队列堆积,
本版本将 `heartbeat_time` 从 `metas` 表拆出,写入新独立 `heartbeats` 表。

### 兼容性
- 公开 Python API 表面**不变**:`get_meta()` / `get_all_metas()` / `get_strategies_by_status()`
  返回结构仍含 `heartbeat_time` 字段,业务代码无需修改
- 新增 API:`get_heartbeat(strategy)`、`list_heartbeats()`
- 表结构变化:`metas` 删除 `heartbeat_time` 列,新增 `heartbeats` 表
- `summary()` 返回字典新增 `heartbeats` 计数键
- `publish_weights` / `publish_returns` 统一只在 publish 完成后调一次心跳
  (publish_returns 此前两端均漏调,本版本补齐)

### 升级步骤
不支持就地升级。Alpha 阶段不提供数据迁移工具,请按以下步骤操作:

1. 备份必要数据(可选,通过 `get_all_metas` / `get_strategy_weights` 等导出 DataFrame)
2. 升级 wmr 包:`pip install -U wmr==0.2.0a1`
3. 在原数据库执行:
   - ClickHouse:`DROP DATABASE <db>` 后重新调用 `OnlineManager.initialize()`
   - DuckDB:删除原 `.duckdb` 文件后重新调用 `LocalManager.initialize()`
4. 通过 `set_meta` + `publish_weights` + `publish_returns` 重建数据

### 内部实现要点
- ClickHouse 端 `heartbeats` 表使用 `ReplacingMergeTree(heartbeat_time)` 引擎,
  显式声明 `heartbeat_time` 为 version 列,FINAL 严格取最新心跳。
- DuckDB 端 `heartbeats` 表使用 `PRIMARY KEY (strategy)` + `INSERT OR REPLACE`,
  自然取最新心跳。
- `get_meta` / `get_all_metas` / `get_strategies_by_status` 内部通过 `LEFT JOIN`
  注入 `heartbeat_time` 字段,外部 API 无感知。

## 0.1.0a1 — 2026-04-28 — 首个 alpha 预发布

详见 git history。
```

- [ ] **Step 3: 在 `README.md` 顶部(项目标题之后,正文开始之前)加一行升级提示**

定位 README.md 顶部第一段后,插入一行(若已有徽章/简介,放在它们之后):

```markdown
> ⚠️ **0.2.0a1 是破坏性升级**:`heartbeat_time` 已从 `metas` 表拆到独立 `heartbeats` 表,
> 不支持就地升级。详见 [CHANGELOG.md](CHANGELOG.md#020a1--2026-04-29--心跳拆表破坏性变更)。
```

- [ ] **Step 4: 验证 `wmr.__version__` 反映新版本**

```bash
uv sync --frozen 2>&1 | tail -3 && uv run python -c "import wmr; print(wmr.__version__)"
```

期望:打印 `0.2.0a1`。

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml CHANGELOG.md README.md uv.lock
git commit -m "release: bump version to 0.2.0a1(心跳拆表破坏性升级)"
```

> 如果 `uv sync` 没修改 `uv.lock`,只 `git add pyproject.toml CHANGELOG.md README.md`。

---

## Task 8: 最终全套 verification

**目的:** 一次跑完整 pytest,确认所有用例 PASS、coverage 达标、签名锁稳。

- [ ] **Step 1: 全量跑测试**

```bash
uv run pytest tests/ -q 2>&1 | tail -20
```

期望:
- 所有用例 PASS(含 parity)
- coverage ≥ 90%(`pyproject.toml` 配的 fail-under 门槛)
- 仅 perf 套件 skipped(需 `--run-perf`)与 czsc 缺失 skipped 1 个

- [ ] **Step 2: 代码风格 + 类型检查**

```bash
uv run ruff check wmr/ tests/
uv run ruff format --check wmr/ tests/
uv run basedpyright wmr/ 2>&1 | tail -10
```

期望:全 OK / 无新增类型错误。如有报错 inline 修,**不要新加 `# type: ignore`**(除非 Online 测试 fixture 已经在用)。

- [ ] **Step 3: 检查 git 状态**

```bash
git status
git log --oneline main..HEAD
```

期望:工作树 clean;本分支领先 main 7 个 commit(Task 1–7 各一个)。

- [ ] **Step 4: 手动跑一遍快速冒烟**(0.2 升级路径自检)

```bash
uv run python -c "
import tempfile, pathlib, pandas as pd
from wmr import LocalManager
with tempfile.TemporaryDirectory() as d:
    db = pathlib.Path(d) / 'smoke.duckdb'
    with LocalManager(db_path=str(db)) as m:
        m.initialize()
        m.set_meta(strategy='alpha', base_freq='1d', description='smoke', author='ci', outsample_sdt='2024-01-01')
        df = pd.DataFrame({'dt': pd.to_datetime(['2024-01-02']), 'symbol': ['AAA'], 'weight': [1.0]})
        m.publish_weights('alpha', df)
        m.publish_returns('alpha', df.rename(columns={'weight': 'returns'}))
        meta = m.get_meta('alpha')
        assert 'heartbeat_time' in meta and meta['heartbeat_time'] is not None
        assert m.get_heartbeat('alpha') is not None
        s = m.summary()
        assert s['heartbeats'] == 1
        print('SMOKE OK', m.__version__ if hasattr(m, \"__version__\") else 'wmr', s)
"
```

期望:打印 `SMOKE OK ... {'metas': 1, 'weights': 1, 'returns': 1, 'tags': 0, 'heartbeats': 1, 'strategies': 1}`。

- [ ] **Step 5: 调用 finishing-a-development-branch**

完成后:**REQUIRED SUB-SKILL:** Use `superpowers:finishing-a-development-branch` 决定 merge / PR / 清理路径。

---

## 任务依赖图

```
Task 1 (BaseManager 契约)
   │
   ├──> Task 2 (Local 实现) ─┐
   │                         │
   ├──> Task 3 (Online 实现) ─┴──> Task 4 (clear_strategy + summary 双端)
   │                                           │
   │                                           ▼
   │                              Task 5 (publish 流程双端)
   │                                           │
   │                                           ▼
   │                              Task 6 (parity 扩展)
   │                                           │
   │                                           ▼
   └──────────────────────────────> Task 7 (版本号 + CHANGELOG)
                                               │
                                               ▼
                                  Task 8 (最终 verification)
```

> **subagent 并行机会:** Task 2 与 Task 3 之间没有强依赖(契约已在 Task 1 锁定),可并行 dispatch 给两个 subagent。但建议串行,因为 Task 3 在 Task 2 之后跑 `tests/integration/test_online_*.py` 才能闭合双端绿灯;并行可能导致中间状态判定混乱。

## 不在范围

- 不写自动迁移代码(Alpha 阶段)
- 不引入 KeeperMap / 外部 Redis 心跳存储
- 不增加 heartbeat 来源标记 / 计数列
- 不改 `cs/ts/latest_weights` 视图(它们不引用 heartbeat_time)
- 不引入心跳节流或聚合写入
