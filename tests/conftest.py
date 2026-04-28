"""测试全局 fixtures。

对应 docs/code-quality.md §4.4 fixture 设计原则:
- ``local_mgr`` 每用例一个临时 DuckDB 文件
- ``clickhouse_dsn`` session 级容器,所有 online 用例共享
- ``online_mgr`` 每用例独立 database,执行结束 DROP DATABASE
- ``both_mgr`` 双后端等价测试,parity 测试一次写两后端跑
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import pytest

from wmr import LocalManager


# ---------- LocalManager ----------
@pytest.fixture
def local_mgr(tmp_path) -> Iterator[LocalManager]:
    """每用例独立的 LocalManager(临时 DuckDB 文件)。"""
    db_path = str(tmp_path / "wmr_test.duckdb")
    with LocalManager(db_path=db_path) as mgr:
        mgr.initialize()
        yield mgr


# ---------- OnlineManager(testcontainers) ----------
@pytest.fixture(scope="session")
def clickhouse_dsn() -> Iterator[str]:
    """Session 级容器,所有 online 用例共享一个 ClickHouse 实例。

    优先复用外部 ``WMR_TEST_CLICKHOUSE_DSN`` 提供的实例(CI 中通过 service 提供),
    否则用 testcontainers 启动临时容器。
    """
    if dsn := os.getenv("WMR_TEST_CLICKHOUSE_DSN"):
        yield dsn
        return

    try:
        from testcontainers.clickhouse import ClickHouseContainer
    except ImportError:
        pytest.skip("testcontainers[clickhouse] 未安装,无法启动临时 ClickHouse 容器")

    image = os.getenv("WMR_TEST_CLICKHOUSE_IMAGE", "clickhouse/clickhouse-server:24.8")
    with ClickHouseContainer(image) as ch:
        host = ch.get_container_host_ip()
        # clickhouse-connect 走 HTTP 协议(8123),9000 是 native client 端口
        port = ch.get_exposed_port(8123)
        user = ch.username
        password = ch.password
        dsn = f"clickhouse://{user}:{password}@{host}:{port}"
        yield dsn


@pytest.fixture
def online_mgr(clickhouse_dsn: str, request: pytest.FixtureRequest):
    """每用例独立 database。结束后 DROP DATABASE 防污染。"""
    from wmr import OnlineManager

    safe_name = request.node.name.lower().replace("[", "_").replace("]", "_")
    safe_name = "".join(c if c.isalnum() or c == "_" else "_" for c in safe_name)
    db = f"wmr_test_{safe_name}"[:60]

    base_dsn = clickhouse_dsn.rstrip("/")
    if "/" in base_dsn.split("@", 1)[-1]:
        base_dsn = base_dsn.rsplit("/", 1)[0]
    full_dsn = f"{base_dsn}/{db}"

    with OnlineManager(dsn=full_dsn, database=db) as mgr:
        mgr.initialize()
        yield mgr
        with contextlib.suppress(Exception):
            mgr.client.command(f"DROP DATABASE IF EXISTS {db}")


@pytest.fixture(params=["local", "online"])
def both_mgr(request: pytest.FixtureRequest, local_mgr, online_mgr):
    """双后端参数化 fixture。parity 测试用。"""
    if request.param == "local":
        return local_mgr
    return online_mgr


# ---------- 辅助 fixture / 选项 ----------
def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-perf",
        action="store_true",
        default=False,
        help="运行 @pytest.mark.perf 标记的性能基准测试",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-perf"):
        skip_perf = pytest.mark.skip(reason="需要 --run-perf")
        for item in items:
            if "perf" in item.keywords:
                item.add_marker(skip_perf)
