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
