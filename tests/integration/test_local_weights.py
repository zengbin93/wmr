"""LocalManager weights / returns / latest_weights 接口集成测试。

覆盖验收点 A4 / F4 / F6。
"""

from __future__ import annotations

import time

import pandas as pd
import pytest

pytestmark = pytest.mark.integration


def _make_weights_df(symbols: list[str], dates: list[str]) -> pd.DataFrame:
    rows = []
    for s in symbols:
        for d in dates:
            rows.append({"dt": d, "symbol": s, "weight": 0.1})
    return pd.DataFrame(rows)


def test_publish_weights_appends_only_new_dt(local_mgr):
    """A4:publish_weights 仅追加(dt > latest_dt)。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = _make_weights_df(["X", "Y"], ["2024-01-01", "2024-01-02", "2024-01-03"])

    local_mgr.publish_weights("ts1", df)
    assert len(local_mgr.get_strategy_weights("ts1")) == 6

    # 重复发布相同区间:应被全部过滤,行数不变
    local_mgr.publish_weights("ts1", df)
    assert len(local_mgr.get_strategy_weights("ts1")) == 6

    # 追加新日期:仅新日期写入
    df2 = _make_weights_df(["X"], ["2024-01-03", "2024-01-04"])
    local_mgr.publish_weights("ts1", df2)
    out = local_mgr.get_strategy_weights("ts1")
    assert len(out) == 7  # 6 旧 + 1 新(X 的 01-04)


def test_publish_returns_overwrites_same_day(local_mgr):
    """A4:publish_returns 允许覆盖同日(dt >= latest_dt)。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df1 = pd.DataFrame(
        {
            "dt": ["2024-01-01", "2024-01-02"],
            "symbol": ["X", "X"],
            "returns": [0.01, 0.02],
        }
    )
    local_mgr.publish_returns("ts1", df1)

    # 覆盖 01-02
    df2 = pd.DataFrame({"dt": ["2024-01-02"], "symbol": ["X"], "returns": [0.99]})
    local_mgr.publish_returns("ts1", df2)

    out = local_mgr.get_strategy_returns("ts1")
    assert len(out) == 2
    last = out[out["dt"] == out["dt"].max()].iloc[0]
    assert last["returns"] == 0.99


def test_get_latest_weights_ts_per_symbol(local_mgr):
    """F4:ts 策略每个 symbol 各自最新 dt。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = pd.DataFrame(
        {
            "dt": [
                "2024-01-01",
                "2024-01-02",
                "2024-01-01",
                "2024-01-03",
            ],
            "symbol": ["X", "X", "Y", "Y"],
            "weight": [0.1, 0.2, 0.3, 0.4],
        }
    )
    local_mgr.publish_weights("ts1", df)

    latest = local_mgr.get_latest_weights("ts1")
    by_sym = latest.set_index("symbol")
    assert by_sym.loc["X", "weight"] == 0.2
    assert by_sym.loc["Y", "weight"] == 0.4


def test_get_latest_weights_cs_shared_dt(local_mgr):
    """F4:cs 策略所有 symbol 共享最新 dt。"""
    local_mgr.set_meta("cs1", "1d", "", "u", "2024-01-01", weight_type="cs")
    df = pd.DataFrame(
        {
            "dt": ["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-02"],
            "symbol": ["X", "Y", "X", "Y"],
            "weight": [0.1, 0.2, 0.3, 0.4],
        }
    )
    local_mgr.publish_weights("cs1", df)

    latest = local_mgr.get_latest_weights("cs1")
    assert len(latest) == 2
    assert latest["dt"].nunique() == 1
    assert latest["dt"].iloc[0].strftime("%Y-%m-%d") == "2024-01-02"


def test_heartbeat_updates_strictly_increasing(local_mgr):
    """F6:publish_weights 完成后 heartbeat_time 严格递增,且仅在 end 调一次心跳。"""
    local_mgr.set_meta(
        strategy="ts1", base_freq="日线", description="d", author="a",
        outsample_sdt="2024-01-01",
    )
    before = local_mgr.get_heartbeat("ts1")
    time.sleep(0.05)

    df = pd.DataFrame({
        "dt": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "symbol": ["AAA", "AAA"],
        "weight": [0.5, 0.6],
    })
    local_mgr.publish_weights("ts1", df)

    after = local_mgr.get_heartbeat("ts1")
    assert after > before, "publish 后 heartbeat 应严格大于 publish 前"
    # parity: 同一 strategy 多次心跳 UPSERT,行数不变
    assert (local_mgr.list_heartbeats()["strategy"] == "ts1").sum() == 1


def test_get_strategy_weights_with_filters(local_mgr):
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = _make_weights_df(["X", "Y"], ["2024-01-01", "2024-01-02", "2024-01-03"])
    local_mgr.publish_weights("ts1", df)

    out = local_mgr.get_strategy_weights("ts1", sdt="2024-01-02", symbols=["X"])
    assert len(out) == 2
    assert (out["symbol"] == "X").all()
    assert out["dt"].min().strftime("%Y-%m-%d") == "2024-01-02"


def test_get_strategy_weights_single_symbol_string(local_mgr):
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = _make_weights_df(["X", "Y"], ["2024-01-01"])
    local_mgr.publish_weights("ts1", df)
    out = local_mgr.get_strategy_weights("ts1", symbols="X")
    assert (out["symbol"] == "X").all()
    assert len(out) == 1
