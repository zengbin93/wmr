# 心跳拆表设计稿(方案 A)

- **日期**: 2026-04-29
- **状态**: 设计已确认,待写实施 plan
- **目标版本**: 0.2.0a1(破坏性升级)
- **影响模块**: `wmr/online.py`、`wmr/local.py`、双端测试

## 1. 背景与动机

`OnlineManager.heartbeat`(ClickHouse 端)当前实现走 `ALTER TABLE metas UPDATE heartbeat_time = ...`,这是 ClickHouse 的 mutation 而非 OLTP UPDATE:

1. 每次 mutation 重写整个 data part,即便 metas 表只有 N 行,匹配的 part 也会整体重写到磁盘。
2. 每次 mutation 在 `system.mutations` 队列累积一条记录,长期不可清理。
3. 未完成 mutation 拖慢 metas 表的 `SELECT FINAL`(`get_meta` / `get_all_metas` / 视图 JOIN 等所有读路径都用 FINAL)。
4. 写放大:同一 part 被反复重写。

`publish_weights` 当前 begin/end 各调一次 heartbeat → 一次发布 = 2 次 mutation。`publish_returns` 漏调(parity 缺陷),将一并修复。

ClickHouse 官方明确 mutations 适合**低频批量修正**,不适合当作高频 UPDATE。`docs/code-review-2026-04-28.md §2.3` 已记录该问题并提出"长期方案:把 heartbeat_time 拆到独立表",本设计稿落实该方案。

## 2. 总体架构

把心跳从"元数据更新"重构为"事件流写入":

```
publish_weights / publish_returns
    │
    │  end-only INSERT
    ▼
heartbeats 表 (Online: ReplacingMergeTree(heartbeat_time) / Local: PRIMARY KEY upsert)
    │
    │  LEFT JOIN strategy
    ▼
get_meta() / get_all_metas() / get_strategies_by_status() — 注入 heartbeat_time 字段
get_heartbeat() / list_heartbeats() — 直读 heartbeats
    ▲
    │
metas 表(无 heartbeat_time 列)
```

### 核心变化

1. **新增** `heartbeats` 表(双端),仅两列 `(strategy, heartbeat_time)`。
2. **修改** `metas` 表(双端):移除 `heartbeat_time` 列。
3. **重写** `heartbeat()`:Online 改 INSERT,Local 改 INSERT OR REPLACE/UPSERT,均不再用 ALTER UPDATE。
4. **简化** `publish_weights` / `publish_returns`:都只在 end 调一次心跳(去掉 begin 心跳;补齐 `publish_returns` 漏调)。
5. **保持** `get_meta()` 返回字典/DataFrame 仍含 `heartbeat_time` 字段,通过 LEFT JOIN heartbeats 注入——**外部 API 表面零破坏**。
6. **新增** 双端 `get_heartbeat(strategy)` 与 `list_heartbeats()`。
7. **破坏性升级**:不写迁移代码,release notes 要求清库重建。

### 硬约束:Online/Local Parity

依据项目硬规则(`tests/unit/test_signature_alignment.py` + `tests/parity/test_parity.py`),OnlineManager 与 LocalManager 必须在**表结构、视图、API 签名、返回结构**完全一致。即便 DuckDB 没有 mutation 性能问题,Local 端也按相同结构改造。

## 3. Schema(双端 DDL)

### 3.1 ClickHouse(`wmr/online.py::initialize`)

新增表:

```sql
CREATE TABLE IF NOT EXISTS {db}.heartbeats (
    strategy        String NOT NULL,
    heartbeat_time  DateTime('Asia/Shanghai')
)
ENGINE = ReplacingMergeTree(heartbeat_time)
ORDER BY strategy;
```

修改 metas(移除 `heartbeat_time` 列):

```sql
CREATE TABLE IF NOT EXISTS {db}.metas (
    strategy       String NOT NULL,
    base_freq      String,
    description    String,
    author         String,
    outsample_sdt  DateTime('Asia/Shanghai'),
    create_time    DateTime('Asia/Shanghai'),
    update_time    DateTime('Asia/Shanghai'),
    weight_type    String,
    status         String DEFAULT '实盘',
    memo           String
)
ENGINE = ReplacingMergeTree()
ORDER BY strategy;
```

### 3.2 DuckDB(`wmr/local.py::initialize`)

新增表:

```sql
CREATE TABLE IF NOT EXISTS heartbeats (
    strategy       VARCHAR PRIMARY KEY,
    heartbeat_time TIMESTAMP
);
```

修改 metas(移除 `heartbeat_time` 列):

```sql
CREATE TABLE IF NOT EXISTS metas (
    strategy       VARCHAR PRIMARY KEY,
    base_freq      VARCHAR,
    description    VARCHAR,
    author         VARCHAR,
    outsample_sdt  TIMESTAMP,
    create_time    TIMESTAMP,
    update_time    TIMESTAMP,
    weight_type    VARCHAR,
    status         VARCHAR DEFAULT '实盘',
    memo           VARCHAR
);
```

### 3.3 视图

`cs_latest_weights` / `ts_latest_weights` / `latest_weights` 不引用 `heartbeat_time`,**视图 DDL 不变**。

### 3.4 引擎选择说明

ClickHouse 端选 `ReplacingMergeTree(heartbeat_time)` 而非裸 `ReplacingMergeTree()`:**显式声明 `heartbeat_time` 为 version 列**,FINAL 严格取 `max(heartbeat_time)`,不依赖 part 写入顺序。`heartbeat_time` 同时充当数据列与 version 列,不增加列数,不违背"极简两列"约束。

DuckDB 端用 `PRIMARY KEY` + `INSERT OR REPLACE`,自然取最新值。

## 4. API 变更

### 4.1 修改既有方法(双端同步)

| 方法 | 修改点 |
|---|---|
| `set_meta(...)` | DataFrame 不再含 `heartbeat_time` 字段(列已不存在);末尾追加调用 `self.heartbeat(strategy)` 写一行到 heartbeats 表,保留"创建即活跃"语义 |
| `get_meta(strategy) -> dict` | SQL 改为 `metas LEFT JOIN heartbeats ON strategy`,返回字典仍含 `heartbeat_time` 键(无心跳时为 `None`/`NaT`) |
| `get_all_metas() -> pd.DataFrame` | 同上,DataFrame 仍含 `heartbeat_time` 列 |
| `get_strategies_by_status(status) -> pd.DataFrame` | 同上,LEFT JOIN 注入 `heartbeat_time` 列 |
| `heartbeat(strategy) -> None` | **实现切换**:Online 改 `INSERT INTO heartbeats VALUES (strategy, now())`;Local 改 `INSERT OR REPLACE INTO heartbeats VALUES (?, ?)`。签名、对外行为不变(策略不存在时仍 WARNING 不抛、Online 端失败仍非阻断) |
| `clear_strategy(strategy)` | 删 metas/weights/returns/tags 同时**删 heartbeats**,保持"完全清除痕迹" |
| `summary()` | 返回字典新增 `heartbeats` 计数列 |

### 4.2 新增方法(双端,签名一致)

```python
def get_heartbeat(self, strategy: str) -> pd.Timestamp | None:
    """返回指定策略的最新心跳时间;不存在则返回 None。"""

def list_heartbeats(self) -> pd.DataFrame:
    """返回所有策略的最新心跳。

    列:strategy、heartbeat_time;按 heartbeat_time DESC 排序。
    """
```

- Online:`SELECT * FROM heartbeats FINAL ORDER BY heartbeat_time DESC`
- Local:`SELECT * FROM heartbeats ORDER BY heartbeat_time DESC`

### 4.3 publish 流程变化(双端同步)

`publish_weights`:

```python
# 改前
self.heartbeat(strategy)
n = self._publish_dataframe(...)
self.heartbeat(strategy)

# 改后:仅 end
n = self._publish_dataframe(...)
self.heartbeat(strategy)
```

`publish_returns`(双端原本都漏调,本次补齐 parity):

```python
# 改前:双端均无 heartbeat 调用(parity 缺陷)
# 改后:双端统一在 end 调一次
n = self._publish_dataframe(...)
self.heartbeat(strategy)  # 新增
```

### 4.4 对外兼容性

- 字段层:`heartbeat_time` 仍出现在所有原本含它的返回结构(get_meta/get_all_metas/get_strategies_by_status)。
- 方法层:既有方法签名不变,新增两个方法(向前兼容)。
- 业务代码:**零修改**。

## 5. 迁移与版本

### 5.1 版本号

- 当前:`0.1.0a1`
- 升至:`0.2.0a1`(minor 跨度,显式破坏性变更)
- 同步更新 `pyproject.toml` 与 `wmr/__init__.py`

### 5.2 迁移策略

不写自动迁移代码。`initialize()` 仅做幂等的 `CREATE TABLE IF NOT EXISTS`。如果用户在带旧 schema 的库上直接升级,新代码写入会因列不匹配而报错——这是预期行为,文档警告。

### 5.3 release notes(写到 `CHANGELOG.md`)

```markdown
## 0.2.0a1 — 心跳表拆分(破坏性变更)

为彻底解除 ClickHouse 高频 mutation 带来的写放大与 system.mutations 队列堆积,
本版本将 heartbeat_time 从 metas 表拆出,写入新独立 heartbeats 表。

### 兼容性
- 公开 Python API 表面不变:get_meta() / get_all_metas() / get_strategies_by_status()
  返回结构仍含 heartbeat_time 字段,业务代码无需修改
- 新增 API:get_heartbeat(strategy)、list_heartbeats()
- 表结构变化:metas 删除 heartbeat_time 列,新增 heartbeats 表

### 升级步骤
不支持就地升级。Alpha 阶段不提供数据迁移工具,请按以下步骤操作:

1. 备份必要数据(可选,通过 get_all_metas / get_strategy_weights 等导出 DataFrame)
2. 升级 wmr 包:pip install -U wmr==0.2.0a1
3. 在原数据库执行 DROP DATABASE(ClickHouse)/ 删除 .duckdb 文件(Local)
4. 重新调用 mgr.initialize() 创建新表结构
5. 通过 set_meta + publish_weights + publish_returns 重建数据
```

README 顶部置顶提示一行链向 `CHANGELOG.md` 的 0.2.0a1 段。

### 5.4 降级路径

不提供。alpha 阶段用户应能接受。

## 6. 测试策略

双端覆盖,parity 是硬约束。

### 6.1 单元测试(`tests/unit/`)

| 文件 | 改动 |
|---|---|
| `test_signature_alignment.py` | 双端方法对照清单**新增** `get_heartbeat`、`list_heartbeats`;**保留** `heartbeat`(签名不变) |
| `test_verbose_mode.py` | 已有断言 `heartbeat(S1) ok` 保留;核查"begin 心跳被去掉"是否影响 _vlog 计数断言并相应调整 |

### 6.2 集成测试(`tests/integration/`)

| 文件 | 改动 |
|---|---|
| `test_local_metas.py` | 新增:`metas` schema 不再含 `heartbeat_time` 列(列名集合断言);`get_meta()` / `get_all_metas()` 返回**仍含** `heartbeat_time` key/列,值为最新心跳;无心跳时为 `None`/`NaT` |
| `test_local_weights.py` | 既有 `test_heartbeat_updates_strictly_increasing` **保留**:断言 `get_heartbeat(strategy)` 在 publish 前后严格递增。同 strategy 多次 INSERT 是 UPSERT 语义,`list_heartbeats` 行数不变。**新增**:同一次 publish_weights 内,验证仅写 1 行心跳(begin 心跳已去掉),做法是在 publish 前后比较 `get_heartbeat` 是否只有 1 次新值 |
| `test_local_extra_branches.py` | 既有 `test_heartbeat_missing_strategy_warns_only` 保留;新增:`set_meta` 后 `get_heartbeat(strategy)` 立即返回非 None |
| `test_online_basic.py` / `test_online_full.py` | 镜像上述断言到 ClickHouse 端 |
| **新增** `test_local_heartbeats.py` / `test_online_heartbeats.py` | 集中覆盖新表:多次 INSERT 同 strategy 后 `get_heartbeat` 取最新值;`list_heartbeats` 排序;`clear_strategy` 后 heartbeats 行被删;`summary()` 含 `heartbeats` 计数 |

### 6.3 Parity 测试(`tests/parity/test_parity.py`)

- 同一组操作序列在 Online/Local 两端,`get_heartbeat` / `list_heartbeats` 结果**结构一致**(列名、dtype、排序)。
- `get_meta()["heartbeat_time"]` 双端类型一致(都是 tz-aware Timestamp,或都是 None/NaT)。

### 6.4 性能测试(`tests/perf/`,可选)

在 `test_perf.py` 增一组对照:**改造前 vs 改造后** 1000 次 publish_weights 的 elapsed 时间。预期 ClickHouse 端有显著下降。**作为 release notes 数据点,不进 CI 门槛。**

### 6.5 验收 checklist

- [ ] 双端 `initialize()` 创建出的 `metas` schema 不含 `heartbeat_time`
- [ ] 双端 `initialize()` 创建出的 `heartbeats` schema 与设计稿一致
- [ ] `set_meta` 后 `get_heartbeat` 立即可读
- [ ] `publish_weights` / `publish_returns` 各只触发 1 次 INSERT 到 heartbeats
- [ ] `clear_strategy` 删除该策略在 heartbeats 表的行
- [ ] `summary()` 双端均含 `heartbeats` 字段
- [ ] `signature_alignment` 测试通过(新增方法已登记)
- [ ] `tests/parity/test_parity.py` 全绿
- [ ] 包内全套 pytest 全绿

## 7. 不在范围内(Out of Scope)

- 不实现 `mutation → INSERT` 之外的存储层重构(例如 KeeperMap、外部 Redis)。
- 不增加 heartbeat 的来源标记/计数等观测元信息。
- 不引入心跳节流或聚合写入,Alpha 期相信 ClickHouse 的 part 合并能力。
- 不写就地数据迁移脚本。
- 不改 `cs/ts/latest_weights` 视图。
