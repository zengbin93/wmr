"""LocalManager metas 接口集成测试。

覆盖验收点 F1 / F2 / F3 / A2。
"""

from __future__ import annotations

import pandas as pd
import pytest

pytestmark = pytest.mark.integration


def test_initialize_idempotent(local_mgr):
    """A2:连续 initialize 3 次,表与视图无重复创建错误。"""
    local_mgr.initialize()
    local_mgr.initialize()
    local_mgr.initialize()

    summary = local_mgr.summary()
    assert summary["metas"] == 0
    assert summary["weights"] == 0
    assert summary["returns"] == 0
    assert summary["tags"] == 0


def test_set_get_meta_roundtrip(local_mgr):
    """F1:set_meta + get_meta 闭环,字段值与类型一致。"""
    local_mgr.set_meta(
        strategy="alpha",
        base_freq="1m",
        description="测试策略",
        author="alice",
        outsample_sdt="2024-01-01",
        weight_type="ts",
        status="实盘",
        memo="hello",
    )

    meta = local_mgr.get_meta("alpha")
    assert meta["strategy"] == "alpha"
    assert meta["base_freq"] == "1m"
    assert meta["description"] == "测试策略"
    assert meta["author"] == "alice"
    assert meta["weight_type"] == "ts"
    assert meta["status"] == "实盘"
    assert meta["memo"] == "hello"
    assert isinstance(meta["create_time"], pd.Timestamp)
    assert meta["create_time"].tzinfo is not None


def test_get_meta_missing_returns_empty(local_mgr):
    assert local_mgr.get_meta("nonexistent") == {}


def test_set_meta_overwrite_false_skips(local_mgr):
    """F1 + overwrite 语义:未指定 overwrite 重复 set 不写入。"""
    local_mgr.set_meta("alpha", "1m", "v1", "alice", "2024-01-01")
    first = local_mgr.get_meta("alpha")

    local_mgr.set_meta("alpha", "1m", "v2_NEW", "bob", "2024-02-01")
    second = local_mgr.get_meta("alpha")

    assert second["description"] == first["description"]
    assert second["description"] == "v1"


def test_set_meta_overwrite_true_updates_but_keeps_create_time(local_mgr):
    """overwrite=True 成功且 update_time 变化、create_time 不变。"""
    local_mgr.set_meta("alpha", "1m", "v1", "alice", "2024-01-01")
    first = local_mgr.get_meta("alpha")

    import time

    time.sleep(1.1)  # 确保 update_time 变化(秒级精度)
    local_mgr.set_meta("alpha", "1m", "v2", "alice", "2024-01-01", overwrite=True)
    second = local_mgr.get_meta("alpha")

    assert second["description"] == "v2"
    assert second["create_time"] == first["create_time"]
    assert second["update_time"] >= first["update_time"]


def test_get_all_metas_returns_dataframe(local_mgr):
    local_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    local_mgr.set_meta("b", "1m", "", "u", "2024-01-01")
    df = local_mgr.get_all_metas()
    assert len(df) == 2
    assert set(df["strategy"]) == {"a", "b"}


def test_update_strategy_status_invalid_raises(local_mgr):
    """F2:status 仅接受 '实盘'/'废弃',否则 raise ValueError。"""
    local_mgr.set_meta("alpha", "1m", "", "u", "2024-01-01")
    with pytest.raises(ValueError, match="无效的策略状态"):
        local_mgr.update_strategy_status("alpha", "invalid")


def test_update_strategy_status_valid(local_mgr):
    local_mgr.set_meta("alpha", "1m", "", "u", "2024-01-01", status="实盘")
    local_mgr.update_strategy_status("alpha", "废弃")
    meta = local_mgr.get_meta("alpha")
    assert meta["status"] == "废弃"


def test_update_strategy_status_missing_strategy_warns(local_mgr):
    # 不存在的策略仅 warning,不抛异常
    local_mgr.update_strategy_status("nonexistent", "实盘")


def test_get_strategies_by_status(local_mgr):
    """F3:None 返回所有;指定 status 仅返回匹配项。"""
    local_mgr.set_meta("a", "1m", "", "u", "2024-01-01", status="实盘")
    local_mgr.set_meta("b", "1m", "", "u", "2024-01-01", status="废弃")

    all_df = local_mgr.get_strategies_by_status()
    assert len(all_df) == 2

    live_df = local_mgr.get_strategies_by_status("实盘")
    assert list(live_df["strategy"]) == ["a"]

    dead_df = local_mgr.get_strategies_by_status("废弃")
    assert list(dead_df["strategy"]) == ["b"]
