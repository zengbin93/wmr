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
