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


def test_set_meta_no_overwrite_keeps_heartbeat(local_mgr):
    """set_meta(overwrite=False) 命中已存在策略时早 return,不应刷新心跳。"""
    _seed_meta(local_mgr, "S1")
    hb1 = local_mgr.get_heartbeat("S1")
    time.sleep(0.05)
    # overwrite 默认 False:策略已存在,应该 warning 后早返,不写心跳
    local_mgr.set_meta(
        strategy="S1",
        base_freq="日线",
        description="二次提交",
        author="tester",
        outsample_sdt="2024-01-01",
    )
    hb2 = local_mgr.get_heartbeat("S1")
    assert hb1 == hb2, "overwrite=False 命中已存在策略不应刷新 heartbeat"


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


def test_publish_returns_triggers_heartbeat(local_mgr):
    """publish_returns 完成后心跳更新(原本两端漏调,本次补齐)。"""
    _seed_meta(local_mgr, "S1")
    before = local_mgr.get_heartbeat("S1")
    time.sleep(0.05)

    df = pd.DataFrame(
        {
            "dt": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "symbol": ["AAA", "AAA"],
            "returns": [0.01, 0.02],
        }
    )
    local_mgr.publish_returns("S1", df)
    after = local_mgr.get_heartbeat("S1")
    assert after > before
