# wmr 统一 verbose 模式 + 日志节奏重排 — 设计文档

> 状态:**设计稿(待实现)**
> 范围:`wmr.LocalManager` / `wmr.OnlineManager` 两个 Manager 的日志层全面重排
> 关联文档:`docs/code-quality.md` §2.4 日志规范、`docs/code-review-2026-04-28.md` §2.7
> 关联诉求:仓库根 `human.md` —— *"提供统一的 verbose 模式,默认 False;开启后 loguru 充分展示执行细节"*

---

## 1. 目标

### 1.1 用户视角的两个 SLA

| 场景 | 期望 |
|---|---|
| **生产 / CI 默认**(`verbose=False`) | 只看见**事件级**信息(策略创建、状态变更、清空、错误、警告),不被批次进度刷屏。**少而不缺**:用户能确认"做了什么 + 结果如何",但不会被进度噪音淹没 |
| **本地开发 / 排障 / 数据回灌**(`verbose=True`) | 全链路可观测:**连接 → 初始化 → 输入摘要 → 过滤 → 批次进度 → 总计 → 出口**。用户能复现"发生了什么 + 在哪一步 + 用了多久",**不抓瞎** |

### 1.2 非目标

- 不改变 loguru 全局 sink / format / 颜色等配置(那是调用方的事)。
- 不引入新的依赖或日志库。
- 不引入"按方法 / 按表"的细粒度开关,保持单一 `verbose` 布尔。

---

## 2. 当前日志节奏诊断

### 2.1 调用全图(2026-04-28,基于本次 PR 后代码)

`grep -n "_logger\." wmr/{base,local,online}.py` 共 ~50 条。逐方法盘点后,**节奏问题集中在三类**:

#### 🔴 问题 A — **关键路径完全静默**(用户抓瞎的主因)

| 方法 / 路径 | 现状 | 用户感受 |
|---|---|---|
| `connect()` 双后端 | 无任何日志 | 调用 `mgr.publish_*` 时若网络超时,堆栈直接抛出,中间没有"正在连接 ClickHouse host=..."的信号 |
| `initialize()` 步骤 | 仅一条总结 `initialize 完成,database=...` | 不知道 4 张表 / 3 个视图各自是新建还是复用;失败时不知道卡在哪条 DDL |
| `get_meta` / `get_all_metas` / `get_strategies_by_status` | 无日志(`get_meta` 不存在路径仅 DEBUG) | 查询返回 0 行 vs 没查到差异不明 |
| `get_strategy_weights` / `get_latest_weights` / `get_strategy_returns` | 无日志 | 不知道 SQL 用了什么过滤、命中视图还是直查、返回多少行 |
| `list_tags` / `add_tag` / `add_tags` / `remove_tag` | **全部无日志** | `add_tags` 批量写 1000 条后零反馈,用户必须自己 `list_tags` 验证 |
| `heartbeat` 成功路径 | 仅失败时 ERROR | 调用 `publish_weights` 时实际触发了几次心跳,用户看不见 |
| `publish_weights/returns` 入口 | 无入口日志 | 输入 N 行 / 哪些 symbol / dt 范围都看不见;若 df 被全部过滤,只看见"共 0 条",分不清是输入空还是被过滤 |

#### 🟡 问题 B — **节奏过密 / 应被压制**

| 方法 | 现状 | 问题 |
|---|---|---|
| `_publish_dataframe` | 单次 publish 输出 `N+2` 条 INFO(过滤摘要 + 总计 + N 个批次) | 100w 行 / 10w batch_size = 12 条 INFO,生产日志被刷屏 |
| `clear_strategy.数据概况` | 6 行 INFO + 3 行 `"="*60` 装饰 = 9 行 | 装饰行无信息量,可压缩为 1 行 dict / 表格 |

#### 🟡 问题 C — **入口 / 出口不对称、异常信息不完整**

| 方法 | 现状 | 期望 |
|---|---|---|
| `set_meta` | 仅出口 `set_meta: ok` | verbose 应有入口:`set_meta(strategy=X, weight_type=ts, status=实盘, overwrite=False)` |
| `publish_weights/returns` | 仅 `_publish_dataframe` 内的"共 N 条" | 入口需明确"输入 M 行 / K 个 symbol / dt ∈ [sdt, edt]";出口需"耗时 T,实际写入 N 条" |
| `clear_strategy` | 出口 `策略 X 清空完成` 无耗时无明细 | 应输出"删除 weights/returns/tags/metas 各 N 条,耗时 Ts" |
| `online.heartbeat` 失败 | 仅 `发送心跳失败(已忽略): {e}` | 默认 INFO 不变;**verbose 应附 traceback**(`logger.exception` 等价) |
| `clear_strategy` 概况查询失败 | `查询策略 X 数据概况失败: {e}` 后 `继续删除` | 同上,verbose 加 traceback |

### 2.2 节奏问题的根因

**单一 `_logger.info` 把"用户事件"和"过程细节"混在同一档**。本设计目标是把混档的日志按职责分清,并补齐**完全缺失的关键节点**。

---

## 3. 日志三档分类

为了同时解决"过密"和"缺失",引入**三档**(而非简单二档):

| 档位 | 语义 | 典型例子 | 默认(`verbose=False`) | `verbose=True` |
|---|---|---|---|---|
| **silent** | 内部冗余信息,任何模式都不该走 INFO | `_insert_publish_batch` 内部 SQL 拼装 | DEBUG | DEBUG |
| **event** | 用户主动操作的入口/出口、状态变更、错误、警告 | `set_meta: ok`、`策略 X 状态已更新为: 实盘`、`心跳失败(已忽略)` | INFO ✅ | INFO ✅ |
| **detail** | 过程细节、进度、SQL 摘要、连接细节、过滤前后行数 | 批次进度、`策略 X 输入 100000 行 / 5 个 symbol`、`连接 ClickHouse host=...` | DEBUG | INFO ✅ |

实现层只引入**两个**新内部方法,放在 `BaseManager`:

```python
def _vlog(self, msg: str, *, level: str = "INFO") -> None:
    """detail 档:verbose=True 时按指定 level 输出,否则降级到 DEBUG。"""
    if self._verbose:
        self._logger.log(level, msg)
    else:
        self._logger.debug(msg)

def _vexc(self, msg: str) -> None:
    """detail 档异常:verbose=True 附 traceback,否则只 ERROR 一行。"""
    if self._verbose:
        self._logger.exception(msg)
    else:
        self._logger.error(msg)
```

> loguru 的 `logger.log(level_name, msg)` / `logger.exception` 在两条路径下都受用户 sink 的 level 控制。Manager 不修改 sink,仅决定调用哪个方法。

---

## 4. 逐方法日志重排清单

下表为**改造目标**,左列是当前实现,右列是改造后:

### 4.1 生命周期

| 方法 | 当前 | 改造后(默认 / verbose) |
|---|---|---|
| `__init__` | 无 | `_vlog(f"LocalManager 创建: db_path={self._db_path}, read_only={self._read_only}, tz={tz}")` —— 默认 DEBUG / verbose INFO |
| `connect()` 首次连接 | 无 | `_vlog(f"连接 ClickHouse: host={p['host']}:{p['port']}, user={p['user']}, db={self._database}")`(密码已 mask)|
| `connect()` 复用现有 | 无 | 不打日志(避免每次 `mgr.client` 访问都打) |
| `close()` | 无 | `_vlog("关闭连接")` |
| `initialize()` 总入口 | `initialize 完成,database=...` | 保持 INFO `event`(出口) |
| `initialize()` 各 DDL 步骤 | 无 | **新增** `_vlog(f"创建/复用表: {db}.metas")` × 4 + `_vlog(f"创建/复用视图: {db}.cs_latest_weights")` × 3 |

### 4.2 metas

| 方法 | 当前 | 改造后 |
|---|---|---|
| `get_meta(strategy)` 命中 | 无 | `_vlog(f"get_meta({strategy}) → 命中")` |
| `get_meta(strategy)` 未命中 | DEBUG | 保持 DEBUG(已对) |
| `get_all_metas()` | 无 | `_vlog(f"get_all_metas → {len(df)} 条")` |
| `set_meta` 入口 | 无 | `_vlog(f"set_meta(strategy={strategy}, weight_type={weight_type}, status={status}, overwrite={overwrite})")` |
| `set_meta` 已存在拒写 | WARNING | 保持 |
| `set_meta` 出口 | INFO `{strategy} set_meta: ok` | 保持 INFO |
| `update_strategy_status` 异常 status | `raise ValueError` | 保持 |
| `update_strategy_status` 不存在 | WARNING | 保持 |
| `update_strategy_status` 出口 | INFO | 保持 |
| `get_strategies_by_status` | 无 | `_vlog(f"get_strategies_by_status(status={status}) → {len(df)} 条")` |

### 4.3 weights / returns publish 流水线

| 步骤 | 当前 | 改造后 |
|---|---|---|
| `publish_weights/returns` **入口** | 无 | **新增** INFO `event`:`f"开始 publish_{table}(strategy={strategy}, 输入 {len(df)} 行, {df['symbol'].nunique()} 个 symbol, dt ∈ [{df['dt'].min()}, {df['dt'].max()}])"` |
| `_publish_dataframe` 最新时间摘要 | INFO | `_vlog` |
| `_publish_dataframe` 过滤后行数 | INFO | `_vlog` |
| `_publish_dataframe` 单批进度 | INFO(N 条) | `_vlog`(N 条) |
| `_publish_dataframe` **总计** | INFO | 保持 INFO `event`(出口) |
| `publish_weights/returns` **出口** | 无 | **新增** INFO `event`:`f"完成 publish_{table}(strategy={strategy}, 实际写入 {n_written} 条, 耗时 {elapsed:.2f}s)"` |

> **入口 / 出口都保留为 `event` 档**,即使非 verbose 也能看到"我开始了一次发布,实际写入 N 条"。中间过程(批次、过滤摘要)走 `_vlog`。

### 4.4 weights / returns 读路径

| 方法 | 当前 | 改造后 |
|---|---|---|
| `get_strategy_weights` | 无 | `_vlog(f"get_strategy_weights(strategy={strategy}, sdt={sdt}, edt={edt}, symbols={_truncate(symbols, 5)}) → {len(df)} 行")` |
| `get_latest_weights` | 无 | `_vlog(f"get_latest_weights(strategy={strategy or 'ALL'}) → {len(df)} 行")` |
| `get_strategy_returns` | 无 | 同 weights |

### 4.5 tags

| 方法 | 当前 | 改造后 |
|---|---|---|
| `add_tag(strategy, tag, creator)` | 无 | `_vlog(f"add_tag({strategy}, {tag}, creator={creator})")` |
| `add_tags(items, batch_size)` | 无 | **入口** `_vlog(f"add_tags: 输入 {len(rows)} 条, batch_size={batch_size}")`;**每批** `_vlog(f"add_tags 批次 {k}: {len(batch)} 条")`;**出口** `_vlog(f"add_tags 完成: 处理 {n} 条")` |
| `remove_tag(strategy, tag)` | 无 | `_vlog(f"remove_tag({strategy}, {tag})")` |
| `list_tags(strategy, tag)` | 无 | `_vlog(f"list_tags(strategy={strategy}, tag={tag}) → {len(df)} 行")` |

### 4.6 heartbeat / 运维

| 方法 | 当前 | 改造后 |
|---|---|---|
| `heartbeat` 不存在策略 | WARNING | 保持 |
| `heartbeat` 成功 | 无 | `_vlog(f"heartbeat({strategy}) ok")` |
| `online.heartbeat` 失败 | ERROR 一行 | `_vexc(f"心跳失败(已忽略): {e}")` |
| `clear_strategy` 数据概况 | 6 + 3 = 9 行 INFO | **压缩为 2 行 INFO** + 内部细节走 `_vlog`:<br>① `策略 X 即将清空: status=实盘, weights=12345, returns=12345, tags=8` 单行 dict<br>② `_vlog` 列出 create_time / update_time(细节) |
| `clear_strategy` 概况查询失败 | ERROR 一行 + INFO `继续删除` | `_vexc` + INFO 保持 |
| `clear_strategy` 装饰行 `"="*60 × 3` | INFO | 删除装饰,改为单行 WARNING `⚠️ 即将删除策略 X 的所有数据,输入 'DELETE' 确认:` |
| `clear_strategy` 出口 | WARNING `清空完成` | INFO `event`:`f"策略 X 清空完成: weights={w_deleted}, returns={r_deleted}, tags={t_deleted}, metas=1, 耗时 {t:.2f}s"` |
| `summary()` | 无 | `_vlog(f"summary → {dict}")` |

### 4.7 双后端 parity 校验

改造后,**LocalManager 和 OnlineManager 的同名方法日志条数与档位必须一致**。在 `tests/parity/test_parity.py` 中加 `test_log_parity_under_verbose` 用例,断言:同输入下,两后端打出的 INFO 行数相等(允许内容差异,但条数一致)。

---

## 5. API 变更

### 5.1 构造函数

```python
class LocalManager(BaseManager):
    def __init__(
        self,
        db_path: str | None = None,
        read_only: bool = False,
        logger: Any = loguru.logger,
        tz: ZoneInfo = DEFAULT_TZ,
        *,
        verbose: bool | None = None,   # ← 新增 keyword-only;None 时读环境变量
    ) -> None: ...
```

`verbose` 解析顺序:**显式参数 > 环境变量 `WMR_VERBOSE` > 默认 `False`**。
环境变量识别 `1` / `true` / `True` / `yes` 为真,其他为假。

### 5.2 BaseManager 增量

```python
class BaseManager(ABC):
    _logger: Any
    _tz: ZoneInfo
    _verbose: bool   # ← 新增

    def _vlog(self, msg: str, *, level: str = "INFO") -> None: ...
    def _vexc(self, msg: str) -> None: ...
```

### 5.3 `__repr__`

追加 `verbose=...` 字段,便于调用方诊断当前实例的日志档位。

---

## 6. 全链路 verbose 输出示例

### 6.1 默认模式(`verbose=False`)

```
$ python publish_demo.py
[INFO] LocalManager initialize 完成,db_path=/Users/x/.wmr/weights.duckdb
[INFO] alpha_001 set_meta: ok
[INFO] 开始 publish_weights(strategy=alpha_001, 输入 100000 行, 5 个 symbol, dt ∈ [2026-01-02, 2026-04-28])
[INFO] 完成所有 weights 发布,共 100000 条
[INFO] 完成 publish_weights(strategy=alpha_001, 实际写入 100000 条, 耗时 1.83s)
```

5 行 INFO,**用户能确认**:策略已建、发布开始、发布结束、实际写入 100000 条、耗时 1.83s。

### 6.2 verbose 模式(`verbose=True`)

```
$ WMR_VERBOSE=1 python publish_demo.py
[INFO] LocalManager 创建: db_path=..., read_only=False, tz=Asia/Shanghai
[INFO] 创建/复用表: metas
[INFO] 创建/复用表: weights
[INFO] 创建/复用表: returns
[INFO] 创建/复用表: tags
[INFO] 创建/复用视图: cs_latest_weights
[INFO] 创建/复用视图: ts_latest_weights
[INFO] 创建/复用视图: latest_weights
[INFO] LocalManager initialize 完成,db_path=...
[INFO] get_meta(alpha_001) → 未命中
[INFO] set_meta(strategy=alpha_001, weight_type=ts, status=实盘, overwrite=False)
[INFO] alpha_001 set_meta: ok
[INFO] heartbeat(alpha_001) ok
[INFO] 开始 publish_weights(strategy=alpha_001, 输入 100000 行, 5 个 symbol, dt ∈ [2026-01-02, 2026-04-28])
[INFO] 策略 alpha_001 最新时间:无历史数据
[INFO] 策略 alpha_001 共 100000 条新数据
[INFO] 完成批次 1,发布 100000 条 weights
[INFO] 完成所有 weights 发布,共 100000 条
[INFO] heartbeat(alpha_001) ok
[INFO] 完成 publish_weights(strategy=alpha_001, 实际写入 100000 条, 耗时 1.83s)
```

> set_meta 与 publish_weights 完成后各触发一次 heartbeat ok(共 2 行,分别属于不同事件;publish 自身只在 end 调一次)。

19 行 INFO,**用户能复现**:每张表 / 视图的创建顺序、心跳触发时机、过滤前后行数、批次进度、总耗时。

---

## 7. 实现拆解

### 7.1 改动文件清单

| 文件 | 改动 |
|---|---|
| `wmr/base.py` | 1) 类属性新增 `_verbose: bool`;2) 新增 `_vlog` / `_vexc`;3) `_publish_dataframe` 中 3 条中间 INFO → `_vlog`;**新增**入口 / 出口 INFO + 耗时统计(用 `time.perf_counter`)|
| `wmr/local.py` | 构造函数加 `verbose` keyword-only + env 兜底;`__repr__` 追加 `verbose=`;按 §4 表逐方法补 `_vlog`(metas/weights/returns/tags/heartbeat 读写路径);`initialize` 拆出每张表/视图的 `_vlog`;`clear_strategy` 概况压缩为 1 行 + 出口附实际删除条数 |
| `wmr/online.py` | 同 local;`connect()` 补 `_vlog` 描述;`heartbeat` 失败改 `_vexc` |
| `wmr/utils.py` | 新增私有 `_truncate(seq, n=5)` —— 列表过长时输出 `[a,b,c,...,共 N 个]`,用于 `symbols` 参数日志预览 |
| `tests/unit/test_verbose_mode.py` | **新增**:覆盖三档路由、env 兜底、显式覆盖 env、`__repr__`、入口/出口 INFO 计数(默认/ verbose 行数差) |
| `tests/parity/test_parity.py` | **新增** `test_log_parity_under_verbose`:同输入下双后端 INFO 行数一致 |
| `README.md` | "Quick Start" 加 verbose 用法段 |
| `docs/code-quality.md` | §2.4 引用本文件 |
| `human.md` | 标记需求为 ✅ 已实现,链接本文件 |

### 7.2 测试设计

```python
# tests/unit/test_verbose_mode.py
import logging
import pytest
from wmr import LocalManager


# ---------- 三档路由 ----------
def test_vlog_default_goes_to_debug(caplog):
    mgr = LocalManager(db_path=":memory:")
    with caplog.at_level(logging.DEBUG):
        mgr._vlog("detail msg")
    levels = [r.levelname for r in caplog.records if "detail msg" in r.message]
    assert levels == ["DEBUG"]


def test_vlog_verbose_goes_to_info(caplog):
    mgr = LocalManager(db_path=":memory:", verbose=True)
    with caplog.at_level(logging.DEBUG):
        mgr._vlog("detail msg")
    levels = [r.levelname for r in caplog.records if "detail msg" in r.message]
    assert levels == ["INFO"]


def test_vexc_verbose_attaches_traceback(caplog):
    mgr = LocalManager(db_path=":memory:", verbose=True)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        mgr._vexc("expected boom")
    rec = next(r for r in caplog.records if "expected boom" in r.message)
    assert rec.exc_info is not None  # exception() 才会带 exc_info


# ---------- env 兜底 ----------
@pytest.mark.parametrize("env_val,expected", [("1", True), ("true", True), ("yes", True),
                                              ("0", False), ("", False), ("no", False)])
def test_env_var_resolution(monkeypatch, env_val, expected):
    monkeypatch.setenv("WMR_VERBOSE", env_val)
    mgr = LocalManager(db_path=":memory:")
    assert mgr._verbose is expected


def test_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("WMR_VERBOSE", "1")
    assert LocalManager(db_path=":memory:", verbose=False)._verbose is False


# ---------- 入口 / 出口 event 默认可见 ----------
def test_publish_entry_exit_visible_by_default(local_mgr_with_strategy, sample_weights_df, caplog):
    with caplog.at_level(logging.INFO):
        local_mgr_with_strategy.publish_weights("S1", sample_weights_df)
    msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert any("开始 publish_weights" in m for m in msgs)
    assert any("完成 publish_weights" in m for m in msgs)
    assert any("耗时" in m for m in msgs)


# ---------- 中间细节默认不见 ----------
def test_publish_progress_silent_by_default(local_mgr_with_strategy, sample_weights_df, caplog):
    with caplog.at_level(logging.INFO):
        local_mgr_with_strategy.publish_weights("S1", sample_weights_df)
    msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert not any("完成批次" in m for m in msgs)
    assert not any("最新时间" in m for m in msgs)


# ---------- verbose 全链路打开 ----------
def test_publish_full_chain_visible_in_verbose(local_mgr_verbose, sample_weights_df, caplog):
    with caplog.at_level(logging.INFO):
        local_mgr_verbose.publish_weights("S1", sample_weights_df)
    msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert any("开始 publish_weights" in m for m in msgs)
    assert any("最新时间" in m for m in msgs) or any("无历史数据" in m for m in msgs)
    assert any("完成批次 1" in m for m in msgs)
    assert any("完成所有 weights 发布" in m for m in msgs)
    assert any("heartbeat(S1) ok" in m for m in msgs)
    assert any("完成 publish_weights" in m for m in msgs)


# ---------- 读路径 verbose 可见 ----------
def test_read_path_visible_in_verbose(local_mgr_verbose, caplog):
    with caplog.at_level(logging.INFO):
        local_mgr_verbose.get_strategy_weights("S1", sdt="2026-01-01")
    msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert any("get_strategy_weights" in m and "→" in m for m in msgs)


# ---------- repr ----------
def test_repr_contains_verbose():
    assert "verbose=True" in repr(LocalManager(db_path=":memory:", verbose=True))
    assert "verbose=False" in repr(LocalManager(db_path=":memory:"))
```

### 7.3 验收标准

- ✅ `ruff check` / `ruff format --check` / `basedpyright` 全绿
- ✅ `tests/unit/test_verbose_mode.py` 全绿(覆盖三档 + env + 入口/出口 + 读路径)
- ✅ `tests/parity/test_parity.py::test_log_parity_under_verbose` 双后端日志条数一致
- ✅ 既有 LocalManager 99% / utils 98% / base 99% 覆盖率不退化
- ✅ 既有 INFO 日志(`set_meta: ok`、`状态已更新`、`清空完成`、`完成所有 weights 发布,共 N 条`)文案保持向后兼容(下游可能 grep)
- ✅ README、`human.md`、`docs/code-quality.md` 同步引用

---

## 8. 风险与权衡

| 项 | 选择 | 理由 |
|---|---|---|
| 单一 `verbose` vs 细粒度开关 | **单一布尔** | API 简洁;细粒度通过用户 sink 端按 module 过滤可达 |
| detail 降级到 DEBUG vs 完全静默 | **降级到 DEBUG** | 用户即使不开 verbose,仍可临时把 sink level 调到 DEBUG 拿全量;完全静默会丢失排障入口 |
| `WMR_VERBOSE` env 名 | **`WMR_VERBOSE`** | 二档语义足够覆盖;未来要更细粒度再扩展 `WMR_LOG_LEVEL` 不冲突 |
| 不动用户 sink | **不动** | 严格保持"调用方掌控日志路由" |
| 读路径加 detail 日志的代价 | **`_vlog` 默认走 DEBUG → 用户 sink 默认 INFO 时无开销** | loguru 的 lazy 字符串化在 level 不达标时不会执行 |
| `clear_strategy` 装饰行删除 | **删除 `"="*60`** | 装饰行无信息量,信息密度低;改单行 WARNING 同样醒目 |
| **既有 INFO 文案兼容** | **保留** | 下游脚本可能 grep `set_meta: ok` / `清空完成`,改文案会破坏向后兼容 |
| 入口/出口 `event` 即使非 verbose 也加新 INFO | **加** | 缺失"输入 N 行 / 实际写 M 条 / 耗时 T"是当前最大盲点;非 verbose 也应可见 |

---

## 9. 后续演进留口

1. **进度回调**:keyword-only 槽位预留 `progress_callback: Callable[[int, int], None] | None`,verbose 控可视性,精确进度供 UI 接入
2. **结构化日志**:把 `_vlog` 升级为 `_vlog(event: str, **fields)`,内部 `logger.bind(**fields).log(...)`,方便 ELK / OpenSearch 检索
3. **per-call verbose**:`mgr.publish_weights(..., verbose=True)` 用 `contextvars` 临时 override 实例 `_verbose`,适用"只想看这一次发布"的场景

以上三项**不在本期实现范围**,API 设计预留扩展位。

---

## 10. 实施顺序(建议拆 PR)

1. **PR-A(基础)**:`_vlog` / `_vexc` + 构造函数 `verbose` 参数 + env 兜底 + `__repr__` + 单元测试三档路由
2. **PR-B(publish 重排)**:`_publish_dataframe` 中间日志降级 + 入口/出口 event 日志 + 耗时统计 + parity 测试
3. **PR-C(读路径 + 运维)**:metas / weights / returns / tags 读路径补 `_vlog`;`initialize` 步骤展开;`clear_strategy` 压缩 + 出口耗时与删除条数
4. **PR-D(收口)**:README + `human.md` + `docs/code-quality.md` 同步;double 后端 parity 校验;benchmark 跑一次确认 detail 日志 0 开销

每步独立可验收,不破坏既有 INFO 兼容文案。
