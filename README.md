# wmr

策略持仓权重管理系统(ClickHouse & DuckDB 双后端)。

> ⚠️ **0.2.0a1 是破坏性升级**:`heartbeat_time` 已从 `metas` 表拆到独立 `heartbeats` 表,
> 不支持就地升级。详见 [CHANGELOG.md](CHANGELOG.md#020a1--2026-04-29--心跳拆表破坏性变更)。

## 核心特性

- **双后端**:`LocalManager`(DuckDB,本地/单机/CI)与 `OnlineManager`(ClickHouse,生产)语义等价。
- **接口对齐 `czsc.traders.cwc`**:函数级 API 转方法封装,参数顺序与默认值完全一致。
- **追加 vs 覆盖语义**:`publish_weights` 仅追加 `dt > latest_dt`;`publish_returns` 允许覆盖同日。
- **标签**:在 cwc.py 三表基础上新增 `tags` 表与 4 个标签接口。
- **DSN 连接**:`OnlineManager` 默认从 `WMR_CLICKHOUSE_DSN` 环境变量加载 DSN,密码自动脱敏。

## 安装

```bash
uv add wmr           # 包含 DuckDB + ClickHouse 双后端
uv sync              # 项目内开发,等价 uv pip install -e .
```

## Quickstart(LocalManager)

```python
import pandas as pd
from wmr import LocalManager

with LocalManager(db_path="./weights.duckdb") as mgr:
    mgr.initialize()
    mgr.set_meta("alpha", "1d", "demo", "alice", "2024-01-01", weight_type="ts")

    df = pd.DataFrame({
        "dt": ["2024-01-01", "2024-01-02"],
        "symbol": ["BTC", "BTC"],
        "weight": [0.5, 0.6],
    })
    mgr.publish_weights("alpha", df)

    print(mgr.get_latest_weights("alpha"))
    print(mgr.summary())
```

完整示例见 [`examples/quickstart.py`](examples/quickstart.py)。

## OnlineManager(ClickHouse)

```python
import os
from wmr import OnlineManager

os.environ["WMR_CLICKHOUSE_DSN"] = "clickhouse://user:pass@host:9000/czsc_strategy"

with OnlineManager() as mgr:        # 自动从 env 读 DSN
    mgr.initialize()
    mgr.set_meta("alpha", "1d", "", "u", "2024-01-01")
```

签名:`OnlineManager(dsn=None, database=None, client_kwargs=None)`。`dsn=None`
时回退 `WMR_CLICKHOUSE_DSN`,两者均缺失则抛 `ValueError`。

## 双后端选型

| 场景 | 推荐 | 原因 |
|------|------|------|
| 本地开发 / 单机分析 | LocalManager | 零依赖,启动 < 1 秒 |
| CI 测试 | LocalManager | 无需起容器 |
| 生产 / 多用户 / 高并发 | OnlineManager | ReplacingMergeTree + FINAL,横向扩展 |
| 临时验证流水线 | LocalManager → OnlineManager parity | 用 LocalManager 跑通后再上线 |

## 文档

- 系统设计文档(飞书):[wmr 系统设计文档](https://s0cqcxuy3p.feishu.cn/wiki/X6iZwf0QFigd5gkkCGDcwM6rn6q)
- 代码质量方案:[`docs/code-quality.md`](docs/code-quality.md)

## 测试

```bash
# 仅单元 + 本地集成
uv run pytest -m "unit or integration" -n auto

# 全量(含 ClickHouse,需 docker)
uv run pytest -m "unit or integration or online or parity" -n auto

# 性能基准
uv run pytest -m perf --run-perf
```

覆盖率门槛:整体 ≥ 90%,核心模块 ≥ 95%(由 `--cov-fail-under=90` 强制)。
