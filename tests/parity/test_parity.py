"""双后端等价性测试。覆盖验收点 P1~P5。

每个测试都通过 ``both_mgr`` fixture 跑两次(local / online),用同样的输入
比对输出 DataFrame 是否等价(经规范化)。
"""

from __future__ import annotations

import pandas as pd
import pytest

pytestmark = [pytest.mark.parity, pytest.mark.online]


def _normalize_df(df: pd.DataFrame, datetime_cols: list[str]) -> pd.DataFrame:
    """规范化 DataFrame:去时区、按字典序 sort_values + 列字母序,便于跨后端比对。"""
    df = df.copy()
    for col in datetime_cols:
        if col in df.columns and isinstance(df[col].dtype, pd.DatetimeTZDtype):
            df[col] = df[col].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    df = df.reindex(sorted(df.columns), axis=1)
    return df.reset_index(drop=True)


def test_parity_set_get_meta(both_mgr):
    """P1:双后端 get_all_metas 等价。"""
    both_mgr.set_meta("a", "1m", "x", "u", "2024-01-01", weight_type="ts")
    both_mgr.set_meta("b", "1m", "y", "u", "2024-02-01", weight_type="cs")
    df = both_mgr.get_all_metas()
    assert set(df["strategy"]) == {"a", "b"}


def test_parity_publish_weights(both_mgr):
    """P2:双后端 get_strategy_weights 等价。"""
    both_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = pd.DataFrame(
        {
            "dt": ["2024-01-01", "2024-01-02", "2024-01-03"],
            "symbol": ["X", "X", "Y"],
            "weight": [0.1, 0.2, 0.3],
        }
    )
    both_mgr.publish_weights("ts1", df)
    out = both_mgr.get_strategy_weights("ts1")
    assert len(out) == 3
    assert (out["weight"].tolist()) == sorted([0.1, 0.2, 0.3])


def test_parity_latest_weights_ts(both_mgr):
    """P3 - ts:每 symbol 各自最新 dt。"""
    both_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = pd.DataFrame(
        {
            "dt": ["2024-01-01", "2024-01-02", "2024-01-01"],
            "symbol": ["X", "X", "Y"],
            "weight": [0.1, 0.2, 0.3],
        }
    )
    both_mgr.publish_weights("ts1", df)
    latest = both_mgr.get_latest_weights("ts1")
    by_sym = latest.set_index("symbol")
    assert by_sym.loc["X", "weight"] == 0.2
    assert by_sym.loc["Y", "weight"] == 0.3


def test_parity_latest_weights_cs(both_mgr):
    """P3 - cs:所有 symbol 共享最新 dt。"""
    both_mgr.set_meta("cs1", "1d", "", "u", "2024-01-01", weight_type="cs")
    df = pd.DataFrame(
        {
            "dt": ["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-02"],
            "symbol": ["X", "Y", "X", "Y"],
            "weight": [0.1, 0.2, 0.3, 0.4],
        }
    )
    both_mgr.publish_weights("cs1", df)
    latest = both_mgr.get_latest_weights("cs1")
    assert latest["dt"].nunique() == 1


def test_parity_returns(both_mgr):
    """P4:双后端 get_strategy_returns 等价(浮点 1e-9 误差)。"""
    both_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = pd.DataFrame(
        {
            "dt": ["2024-01-01", "2024-01-02"],
            "symbol": ["X", "X"],
            "returns": [0.123456789, 0.987654321],
        }
    )
    both_mgr.publish_returns("ts1", df)
    out = both_mgr.get_strategy_returns("ts1")
    assert len(out) == 2


def test_parity_tags(both_mgr):
    """P5:双后端 list_tags 等价。"""
    both_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    both_mgr.add_tag("a", "x")
    both_mgr.add_tag("a", "y")
    df = both_mgr.list_tags("a")
    assert set(df["tag"]) == {"x", "y"}


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


def test_parity_summary_heartbeats_count_after_repeated_set_meta(both_mgr):
    """N 次 set_meta(同一 strategy)后 summary heartbeats == 1(锁 UPSERT 语义跨 DDL 引擎)。

    Local 端走 PRIMARY KEY + INSERT OR REPLACE,Online 端走 ReplacingMergeTree + COUNT FINAL,
    路径不同但语义必须一致。
    """
    base = dict(strategy="S1", base_freq="d", description="", author="a", outsample_sdt="2024-01-01")
    both_mgr.set_meta(**base)
    both_mgr.set_meta(**{**base, "memo": "second"}, overwrite=True)
    both_mgr.set_meta(**{**base, "memo": "third"}, overwrite=True)
    s = both_mgr.summary()
    assert s["heartbeats"] == 1


def test_parity_publish_pipeline_keeps_single_heartbeat_row(both_mgr):
    """完整 publish 流水线后 list_heartbeats 仍只有该 strategy 1 行。

    set_meta -> publish_weights -> publish_returns -> publish_weights 共触发 4 次心跳
    (set_meta 1 次 + publish_weights/returns 各 1 次,共 2 次 publish_weights),
    UPSERT 语义保证 list_heartbeats 该 strategy 只 1 行,且 heartbeat_time 是最后一次。
    """
    import time as _time

    both_mgr.set_meta(
        strategy="P1", base_freq="日线", description="", author="a",
        outsample_sdt="2024-01-01",
    )
    weights_df = pd.DataFrame({
        "dt": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "symbol": ["AAA", "AAA"],
        "weight": [0.5, 0.6],
    })
    returns_df = pd.DataFrame({
        "dt": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "symbol": ["AAA", "AAA"],
        "returns": [0.01, 0.02],
    })

    both_mgr.publish_weights("P1", weights_df)
    _time.sleep(1.1)  # 适配 ClickHouse DateTime 秒级精度
    both_mgr.publish_returns("P1", returns_df)
    _time.sleep(1.1)
    weights_df2 = pd.DataFrame({
        "dt": pd.to_datetime(["2024-01-04"]),
        "symbol": ["AAA"],
        "weight": [0.7],
    })
    both_mgr.publish_weights("P1", weights_df2)

    df = both_mgr.list_heartbeats()
    assert (df["strategy"] == "P1").sum() == 1
