# wmr 代码质量控制方案

> 本方案严格遵循团队的 `python-code-quality` / `python-pytest` / `python-best-practices` / `python-code-review` skill 规范,并与 wmr 系统设计文档(飞书 wiki `X6iZwf0QFigd5gkkCGDcwM6rn6q`)的"九、测试方案"与"十、验收标准"保持一致。

## 1. 目标

| 维度 | 准入门槛 | 工具 |
|------|---------|------|
| 代码风格 | `ruff format --check` 与 `ruff check` 必须 0 报错 | ruff |
| 静态类型 | `basedpyright` 在 `standard` 模式下 0 错误 0 警告 | basedpyright |
| 单元测试 | `tests/unit/` 全绿,耗时 < 10s | pytest |
| 集成测试(local) | `tests/integration/` 全绿,耗时 < 60s | pytest + duckdb |
| 集成测试(online) | `tests/integration/` 含 `online` 标记的用例全绿,耗时 < 5min | pytest + testcontainers |
| 双后端等价性 | `tests/parity/` 零失败 | pytest |
| 行 + 分支覆盖率 | **整体 ≥ 90%**,核心模块 (`base.py` / `local.py` / `online.py`) ≥ **95%** | pytest-cov |
| 性能基准 | 选跑(`--run-perf`),CI nightly 跑一次 | pytest-benchmark |

> 任何 PR 合入主干必须同时满足上表全部硬指标。`coverage < 90%` 直接由 `--cov-fail-under=90` 在 pytest 层熔断。

---

## 2. 工具选型与基础原则

### 2.1 ruff:格式 + lint 一体化

替代 black、isort、flake8、pyupgrade。配置集中在 `pyproject.toml` 中:

```toml
[tool.ruff]
line-length = 120
target-version = "py310"
fix = true

[tool.ruff.lint]
select = [
    "E", "W",   # pycodestyle
    "F",        # pyflakes
    "I",        # isort
    "B",        # flake8-bugbear (含可变默认参数等陷阱)
    "C4",       # flake8-comprehensions
    "UP",       # pyupgrade (强制 PEP 604 等新语法)
    "SIM",      # flake8-simplify
    "RET",      # flake8-return
    "N",        # pep8-naming
]
ignore = ["E501"]  # 行长度交给 formatter

[tool.ruff.lint.isort]
known-first-party = ["wmr"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
```

### 2.2 basedpyright:静态类型检查

- 起点:`typeCheckingMode = "standard"` 必须 0 错误 0 警告
- 长期目标:逐步把 `wmr/base.py`、`wmr/utils.py` 等核心模块切到 `strict`

```toml
[tool.basedpyright]
pythonVersion = "3.10"
typeCheckingMode = "standard"
include = ["wmr", "tests"]
exclude = [".venv", "build", "dist"]
reportMissingImports = "error"
reportMissingTypeStubs = false
reportUnusedImport = "warning"
reportPrivateImportUsage = "warning"
```

### 2.3 pytest 与覆盖率

`pyproject.toml` 中的 pytest 配置必须显式声明 markers 与覆盖率门槛:

```toml
[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["tests"]
addopts = "-ra --strict-markers --cov=wmr --cov-report=term-missing --cov-report=xml --cov-fail-under=90"
markers = [
    "unit: pure-function tests, no I/O",
    "integration: requires real backend (DuckDB / ClickHouse)",
    "online: requires ClickHouse (testcontainers)",
    "parity: cross-backend equivalence",
    "perf: performance benchmarks (skipped by default)",
]

[tool.coverage.run]
branch = true
source = ["wmr"]
omit = ["wmr/__init__.py"]

[tool.coverage.report]
fail_under = 90
exclude_lines = [
    "pragma: no cover",
    "raise NotImplementedError",
    "if TYPE_CHECKING:",
    "if __name__ == .__main__.:",
]
```

---

## 3. 编码规范

### 3.1 模块头部约定

所有 `wmr/**.py` 与 `tests/**.py` 文件统一以下两行开头(便于使用 PEP 604 union 与延迟类型求值):

```python
"""<模块说明>。"""
from __future__ import annotations
```

### 3.2 类型注解

- 公共方法/函数的参数、返回值必须有类型注解
- 用 `pd.DataFrame`、`pd.Series`、`pd.Timestamp` 等具名类型而非 `Any`
- 使用 `X | None` 替代 `Optional[X]`,`list[X]` 替代 `typing.List[X]`
- 时间相关参数对外暴露 `pd.Timestamp | str | None` 联合类型(支持 cwc.py 历史调用风格)

### 3.3 命名规范

| 类型 | 风格 | 示例 |
|------|------|------|
| 包 / 模块 | snake_case | `wmr.local` |
| 类 | PascalCase | `LocalManager` |
| 公共方法 | snake_case | `publish_weights` |
| 内部方法 / 属性 | `_` 前缀 | `_insert_or_replace` |
| 模块级常量 | UPPER_SNAKE_CASE | `DEFAULT_TZ` |
| 表名 / 视图名 | 全小写 + 下划线 | `metas` / `latest_weights` |

### 3.4 文档字符串

- 所有公共类、抽象方法、`set_meta` / `publish_weights` 等用户面向 API 必须有 Google 风格 docstring,标注 `Args` / `Returns` / `Raises`
- 内部纯函数(`_to_naive`、`_series_to_naive`)允许只写一行说明
- 涉及行为约束(如"publish_weights 仅追加 dt > latest_dt")**必须**在 docstring 中显式说明,与设计文档一致

### 3.5 异常处理

- 不允许裸 `except:` 与 `except Exception:` 后吞掉异常
- 业务异常使用 Python 内置 `ValueError` / `TypeError`(已对齐 cwc.py),不引入自定义异常体系
- DSN 解析失败、`status` 不在 `{实盘, 废弃}` 时必须 `raise ValueError`,信息含字段名

### 3.6 公共 API 内部流程

`publish_weights` / `publish_returns` / `clear_strategy` 这类带多步流水线的公共 API,在模块或类 docstring 中以 ASCII 流程图给出阶段切分,并在源码中用 `# ---------- ${section} ----------` 分节注释呼应。

### 3.7 数据结构与性能

- 成员检测优先 `set` / `frozenset`,如 `VALID_STATUSES` 使用 list 仅是为了对齐 cwc.py 的错误信息
- 大表批量写入必须分批,默认 `batch_size=100000`(weights / returns)、`500`(tags),与 cwc.py 一致
- 时间戳格式化避免在循环里调用 `pd.Timestamp.now()` ; 单批次写入只取一次"now"

### 3.8 安全规则

- DSN 中的密码必须经过 `mask_dsn_password()` 脱敏后再写入日志或 `__repr__`
- `clear_strategy(human_confirm=True)` 必须使用 `input("...")` 等待用户输入字面量 `"DELETE"`
- 不直接拼接 SQL 字符串处理用户参数,必须使用 driver 占位符(DuckDB 的 `?` / clickhouse-connect 的 `parameters=`)

---

## 4. 测试规范

### 4.1 目录与标记

```
tests/
├── conftest.py         # 跨文件 fixture
├── unit/               # @pytest.mark.unit,无 I/O
├── integration/        # @pytest.mark.integration / @pytest.mark.online
├── parity/             # @pytest.mark.parity,双后端等价
└── perf/               # @pytest.mark.perf,默认 skip,需 --run-perf
```

### 4.2 命名

- 测试文件:`test_<被测模块>.py`
- 测试函数:`test_<功能>_<场景>_<预期>`,如 `test_publish_weights_filters_old_dt`

### 4.3 AAA 结构

每个测试严格按 `Arrange / Act / Assert` 顺序,中间用空行分块:

```python
def test_publish_weights_appends_only_new_dt(local_mgr):
    # Arrange
    local_mgr.set_meta("s1", "1m", "", "alice", "2024-01-01")
    df = make_weights_df(["s1"], dts=["2024-01-01", "2024-01-02"])
    local_mgr.publish_weights("s1", df)

    # Act
    local_mgr.publish_weights("s1", df)  # 重复发布同一区间

    # Assert
    out = local_mgr.get_strategy_weights("s1")
    assert len(out) == 2
```

### 4.4 fixture 设计原则

- `local_mgr`:每个用例一个临时 DuckDB 文件(`tmp_path / "test.duckdb"`)
- `clickhouse_dsn`:session 级,`ClickHouseContainer` 起一次容器,期间所有 online 用例共享
- `online_mgr`:每用例独立 database 名,执行结束 `DROP DATABASE` 收尾,避免污染
- `both_mgr`:`params=["local", "online"]`,parity 测试一次写两后端跑
- 不允许把 fixture 状态跨用例传递

### 4.5 Mock 策略

- 单元测试只 mock **外部边界**(input、time、loguru.warning 调用计数)
- 集成测试**不**用 mock,跑真实 DuckDB / ClickHouse
- `clear_strategy` 中的 `input("DELETE")` 用 `monkeypatch.setattr("builtins.input", lambda _: "DELETE")` 替换

### 4.6 必备测试用例

对照设计文档"9.5 关键测试用例清单"逐条实现。本质要覆盖:

- A1 — 用 `inspect.signature` 比对 `BaseManager` 与 `czsc.traders.cwc` 函数签名(参数顺序与默认值,去 `db` / `database`)
- A4 — `publish_weights` 仅追加;`publish_returns` 允许覆盖同日
- A5 — `online.py` 静态文本扫描:**所有** `query_df` / `command` 中针对 `weights` / `metas` / `returns` / `tags` 的 SELECT 都包含 `FINAL` 关键字
- F2 — `update_strategy_status("?")` 必须 `pytest.raises(ValueError)`
- F5 — `add_tag` 同 `(strategy, tag)` 重复调用幂等(行数不增加,`create_time` 可更新)
- F7 — `clear_strategy(True)` 在 monkeypatch input 输入 `"DELETE"` / `"x"` 两种场景下均验证

---

## 5. 提交流程与 CI 集成

### 5.1 本地提交前

```bash
uv run ruff format .
uv run ruff check . --fix
uv run basedpyright
uv run pytest -m "unit or integration or parity"
```

任何一步失败都不应 git push。

### 5.2 GitHub Actions 流水线

CI(`.github/workflows/ci.yml`)分四个 job:

1. **lint** — `ruff format --check` + `ruff check`
2. **typecheck** — `basedpyright`
3. **test** —— matrix `python: [3.10, 3.11, 3.12]`,先跑 `unit` + `integration`(LocalManager 全 OK 已经过 90% 门槛);上传 `coverage.xml` 到 Codecov
4. **online-test** — 仅在 `ubuntu-latest` 上跑,启动 ClickHouse service,跑 `online` + `parity`,失败不阻断 lint/typecheck/test 等其他 job
5. **perf**(可选) — `workflow_dispatch` 手动触发,跑 `--run-perf`

任一 job 失败即视为 PR 不可合入。

### 5.3 版本与变更

- 每个 PR 必须更新一行 `CHANGELOG.md` 描述对外可见的行为变更
- 涉及 `BaseManager` 公共方法签名变更必须同步:
  - 更新飞书设计文档"三、API 接口定义"对应表格
  - 在 PR 描述中显式标注 `BREAKING CHANGE`

---

## 6. 验收对照表

| 设计文档验收项 | 本文档对应章节 | 工具 / 命令 |
|----------------|----------------|-------------|
| A1 接口签名对齐 | §4.6 + unit `test_signature_alignment` | pytest |
| A2 4 张表创建 | §4.6 integration `test_initialize_idempotent` | pytest |
| A3 3 个视图创建 | §4.6 integration 视图查询断言 | pytest |
| A4 追加 vs 覆盖语义 | §4.6 | pytest |
| A5 ClickHouse FINAL | §4.6 + 静态扫描 | pytest + grep |
| C4 覆盖率 ≥ 90% / 95% | §1 + §2.3 `--cov-fail-under=90` | pytest-cov |
| C5 ruff 无 error | §2.1 + §5.1 + §5.2 lint job | ruff |
| C6 README + examples | 仓库根 README.md + `examples/quickstart.py` | 人工 review + CI 跑 examples |

---

## 7. 例外与豁免

- 任何引入 `# pragma: no cover` 必须在同一 PR 中:
  - 在 PR 描述中说明无法覆盖的原因
  - 在 review 中获得至少 1 位 maintainer 同意
- 任何引入 `# type: ignore` 必须带具体规则,如 `# type: ignore[call-arg]`,并加 1 行注释说明原因
- 不允许在主仓库分支跳过 `ruff check` / `basedpyright`(包括 `--no-verify` push)
