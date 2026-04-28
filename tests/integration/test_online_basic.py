"""OnlineManager 基础集成测试(testcontainers ClickHouse)。

复用 ``online_mgr`` fixture,每用例独立 database。
"""

from __future__ import annotations

import time

import pandas as pd
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.online]


def test_initialize_idempotent(online_mgr):
    online_mgr.initialize()
    online_mgr.initialize()
    summary = online_mgr.summary()
    assert summary["metas"] == 0


def test_set_get_meta_roundtrip(online_mgr):
    online_mgr.set_meta("alpha", "1m", "测试", "alice", "2024-01-01", weight_type="ts")
    meta = online_mgr.get_meta("alpha")
    assert meta["strategy"] == "alpha"
    assert meta["weight_type"] == "ts"


def test_publish_weights_appends_only(online_mgr):
    online_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = pd.DataFrame(
        {
            "dt": ["2024-01-01", "2024-01-02"],
            "symbol": ["X", "X"],
            "weight": [0.1, 0.2],
        }
    )
    online_mgr.publish_weights("ts1", df)
    online_mgr.publish_weights("ts1", df)
    out = online_mgr.get_strategy_weights("ts1")
    assert len(out) == 2


def test_update_strategy_status_invalid_raises(online_mgr):
    online_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    with pytest.raises(ValueError, match="无效的策略状态"):
        online_mgr.update_strategy_status("a", "x")


def test_clickhouse_final_required(online_mgr):
    """A5:禁用 FINAL 时(直接读底层表)可能读到重复行;启用 FINAL 后唯一。

    通过两次写入相同主键的数据,验证未加 FINAL 与加 FINAL 的区别。
    """
    online_mgr.set_meta("a", "1d", "", "u", "2024-01-01")
    df = pd.DataFrame({"dt": ["2024-01-01"], "symbol": ["X"], "weight": [0.1]})

    # 写一次基线
    online_mgr.publish_weights("a", df)
    # 直接 INSERT 第二份相同主键(模拟 ReplacingMergeTree 重复 part)
    online_mgr.client.command(
        f"INSERT INTO {online_mgr._database}.weights "
        "VALUES ('2024-01-01 00:00:00', 'X', 0.99, 'a', now('Asia/Shanghai'))"
    )

    # 不加 FINAL 直读底层表,期望 >= 2 行(重复未合并)
    raw = online_mgr.client.query_df(
        f"SELECT * FROM {online_mgr._database}.weights WHERE strategy = 'a' AND symbol = 'X'"
    )
    # 加 FINAL 应去重(或至少不多于 raw)
    final = online_mgr.client.query_df(
        f"SELECT * FROM {online_mgr._database}.weights FINAL WHERE strategy = 'a' AND symbol = 'X'"
    )
    assert len(final) <= len(raw)
    assert len(final) == 1


def test_alter_delete_sync(online_mgr):
    """clear_strategy 后立即 SELECT 应返回 0 行。"""
    online_mgr.set_meta("a", "1d", "", "u", "2024-01-01")
    online_mgr.publish_weights(
        "a",
        pd.DataFrame({"dt": ["2024-01-01"], "symbol": ["X"], "weight": [0.1]}),
    )
    online_mgr.clear_strategy("a", human_confirm=False)

    # ClickHouse DELETE 是异步的,等一小会
    deadline = time.time() + 5
    while time.time() < deadline:
        if online_mgr.get_meta("a") == {}:
            break
        time.sleep(0.2)
    assert online_mgr.get_meta("a") == {}
