"""OnlineManager 全量集成测试。

镜像 ``test_local_*.py`` 的核心场景到 ClickHouse 后端,补齐 OnlineManager
未被基础用例覆盖的分支(set_meta overwrite、各类过滤、tags 批量、clear_strategy
两种路径、heartbeat 不存在策略、summary 等)。
"""

from __future__ import annotations

import time

import pandas as pd
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.online]


# ---------- metas ----------
def test_set_meta_overwrite_false_skips(online_mgr):
    online_mgr.set_meta("alpha", "1m", "v1", "alice", "2024-01-01")
    online_mgr.set_meta("alpha", "1m", "v2_NEW", "bob", "2024-02-01")
    meta = online_mgr.get_meta("alpha")
    assert meta["description"] == "v1"


def test_set_meta_overwrite_true_keeps_create_time(online_mgr):
    online_mgr.set_meta("alpha", "1m", "v1", "alice", "2024-01-01")
    first = online_mgr.get_meta("alpha")

    time.sleep(1.1)
    online_mgr.set_meta("alpha", "1m", "v2", "alice", "2024-01-01", overwrite=True)
    second = online_mgr.get_meta("alpha")

    assert second["description"] == "v2"
    assert second["create_time"] == first["create_time"]


def test_get_all_metas_returns_dataframe(online_mgr):
    online_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    online_mgr.set_meta("b", "1m", "", "u", "2024-01-01")
    df = online_mgr.get_all_metas()
    assert set(df["strategy"]) == {"a", "b"}


def test_update_strategy_status_valid(online_mgr):
    online_mgr.set_meta("alpha", "1m", "", "u", "2024-01-01", status="实盘")
    online_mgr.update_strategy_status("alpha", "废弃")
    # ALTER 异步,等到生效
    deadline = time.time() + 5
    while time.time() < deadline:
        if online_mgr.get_meta("alpha").get("status") == "废弃":
            break
        time.sleep(0.2)
    assert online_mgr.get_meta("alpha")["status"] == "废弃"


def test_update_strategy_status_missing_strategy_warns(online_mgr):
    online_mgr.update_strategy_status("nonexistent", "实盘")


def test_get_strategies_by_status_filter(online_mgr):
    online_mgr.set_meta("a", "1m", "", "u", "2024-01-01", status="实盘")
    online_mgr.set_meta("b", "1m", "", "u", "2024-01-01", status="废弃")

    all_df = online_mgr.get_strategies_by_status()
    assert len(all_df) == 2

    live_df = online_mgr.get_strategies_by_status("实盘")
    assert list(live_df["strategy"]) == ["a"]


def test_get_meta_missing_returns_empty(online_mgr):
    assert online_mgr.get_meta("ghost") == {}


# ---------- weights ----------
def test_publish_returns_overwrites_same_day(online_mgr):
    online_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df1 = pd.DataFrame({"dt": ["2024-01-01", "2024-01-02"], "symbol": ["X", "X"], "returns": [0.01, 0.02]})
    online_mgr.publish_returns("ts1", df1)

    df2 = pd.DataFrame({"dt": ["2024-01-02"], "symbol": ["X"], "returns": [0.99]})
    online_mgr.publish_returns("ts1", df2)

    out = online_mgr.get_strategy_returns("ts1")
    assert len(out) == 2
    last = out[out["dt"] == out["dt"].max()].iloc[0]
    assert last["returns"] == 0.99


def test_get_latest_weights_ts_per_symbol(online_mgr):
    online_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = pd.DataFrame(
        {
            "dt": ["2024-01-01", "2024-01-02", "2024-01-01", "2024-01-03"],
            "symbol": ["X", "X", "Y", "Y"],
            "weight": [0.1, 0.2, 0.3, 0.4],
        }
    )
    online_mgr.publish_weights("ts1", df)

    latest = online_mgr.get_latest_weights("ts1")
    by_sym = latest.set_index("symbol")
    assert by_sym.loc["X", "weight"] == 0.2
    assert by_sym.loc["Y", "weight"] == 0.4


def test_get_latest_weights_cs_shared_dt(online_mgr):
    online_mgr.set_meta("cs1", "1d", "", "u", "2024-01-01", weight_type="cs")
    df = pd.DataFrame(
        {
            "dt": ["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-02"],
            "symbol": ["X", "Y", "X", "Y"],
            "weight": [0.1, 0.2, 0.3, 0.4],
        }
    )
    online_mgr.publish_weights("cs1", df)
    latest = online_mgr.get_latest_weights("cs1")
    assert latest["dt"].nunique() == 1


def test_get_strategy_weights_with_filters(online_mgr):
    online_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = pd.DataFrame(
        {
            "dt": ["2024-01-01", "2024-01-02", "2024-01-03"] * 2,
            "symbol": ["X"] * 3 + ["Y"] * 3,
            "weight": [0.1] * 6,
        }
    )
    online_mgr.publish_weights("ts1", df)

    out = online_mgr.get_strategy_weights("ts1", sdt="2024-01-02", edt="2024-01-03", symbols=["X"])
    assert len(out) == 2
    assert (out["symbol"] == "X").all()


def test_get_strategy_weights_single_symbol_string(online_mgr):
    online_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = pd.DataFrame({"dt": ["2024-01-01"], "symbol": ["X"], "weight": [0.1]})
    online_mgr.publish_weights("ts1", df)
    out = online_mgr.get_strategy_weights("ts1", symbols="X")
    assert (out["symbol"] == "X").all()
    assert len(out) == 1


def test_get_latest_weights_no_strategy_returns_all(online_mgr):
    online_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    online_mgr.set_meta("cs1", "1d", "", "u", "2024-01-01", weight_type="cs")
    online_mgr.publish_weights(
        "ts1",
        pd.DataFrame({"dt": ["2024-01-01"], "symbol": ["X"], "weight": [0.1]}),
    )
    online_mgr.publish_weights(
        "cs1",
        pd.DataFrame({"dt": ["2024-01-01", "2024-01-01"], "symbol": ["X", "Y"], "weight": [0.5, 0.5]}),
    )
    union = online_mgr.get_latest_weights()
    assert set(union["strategy"]) == {"ts1", "cs1"}


# ---------- returns ----------
def test_get_strategy_returns_with_filters(online_mgr):
    online_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    df = pd.DataFrame(
        {
            "dt": ["2024-01-01", "2024-01-02", "2024-01-03"] * 2,
            "symbol": ["X"] * 3 + ["Y"] * 3,
            "returns": [0.01] * 6,
        }
    )
    online_mgr.publish_returns("ts1", df)

    out = online_mgr.get_strategy_returns("ts1", sdt="2024-01-02", edt="2024-01-03", symbols="X")
    assert len(out) == 2
    assert (out["symbol"] == "X").all()


# ---------- tags ----------
def test_add_tag_then_list(online_mgr):
    online_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    online_mgr.add_tag("a", "momentum", creator="alice")

    df = online_mgr.list_tags()
    assert len(df) == 1
    row = df.iloc[0]
    assert row["strategy"] == "a"
    assert row["tag"] == "momentum"


def test_add_tags_batch(online_mgr):
    online_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    online_mgr.set_meta("b", "1m", "", "u", "2024-01-01")
    n = online_mgr.add_tags([("a", "t1"), ("a", "t2"), ("b", "t1")])
    assert n == 3
    assert len(online_mgr.list_tags()) == 3


def test_add_tags_empty_returns_zero(online_mgr):
    assert online_mgr.add_tags([]) == 0


def test_list_tags_filter_by_tag(online_mgr):
    online_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    online_mgr.set_meta("b", "1m", "", "u", "2024-01-01")
    online_mgr.add_tag("a", "shared")
    online_mgr.add_tag("b", "shared")
    online_mgr.add_tag("a", "unique")

    df = online_mgr.list_tags(tag="shared")
    assert len(df) == 2


def test_remove_tag(online_mgr):
    online_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    online_mgr.add_tag("a", "x")
    online_mgr.add_tag("a", "y")
    online_mgr.remove_tag("a", "x")
    deadline = time.time() + 5
    while time.time() < deadline:
        df = online_mgr.list_tags("a")
        if list(df["tag"]) == ["y"]:
            break
        time.sleep(0.2)
    assert list(online_mgr.list_tags("a")["tag"]) == ["y"]


# ---------- 心跳与运维 ----------
def test_heartbeat_missing_strategy_warns(online_mgr):
    online_mgr.heartbeat("ghost")  # 不抛,只警告


def test_clear_strategy_no_confirm(online_mgr):
    online_mgr.set_meta("alpha", "1d", "", "u", "2024-01-01", weight_type="ts")
    online_mgr.publish_weights(
        "alpha",
        pd.DataFrame({"dt": ["2024-01-01"], "symbol": ["X"], "weight": [0.1]}),
    )
    online_mgr.publish_returns(
        "alpha",
        pd.DataFrame({"dt": ["2024-01-01"], "symbol": ["X"], "returns": [0.01]}),
    )
    online_mgr.add_tag("alpha", "x")

    online_mgr.clear_strategy("alpha", human_confirm=False)

    deadline = time.time() + 5
    while time.time() < deadline:
        if online_mgr.get_meta("alpha") == {}:
            break
        time.sleep(0.2)
    assert online_mgr.get_meta("alpha") == {}


def test_clear_strategy_confirm_delete(online_mgr, monkeypatch):
    online_mgr.set_meta("alpha", "1d", "", "u", "2024-01-01", weight_type="ts")
    monkeypatch.setattr("builtins.input", lambda _msg: "DELETE")
    online_mgr.clear_strategy("alpha", human_confirm=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        if online_mgr.get_meta("alpha") == {}:
            break
        time.sleep(0.2)
    assert online_mgr.get_meta("alpha") == {}


def test_clear_strategy_confirm_cancel(online_mgr, monkeypatch):
    online_mgr.set_meta("alpha", "1d", "", "u", "2024-01-01", weight_type="ts")
    monkeypatch.setattr("builtins.input", lambda _msg: "delete")  # 大小写敏感
    online_mgr.clear_strategy("alpha", human_confirm=True)
    assert online_mgr.get_meta("alpha")  # 未删


def test_clear_strategy_missing_warns_only(online_mgr):
    online_mgr.clear_strategy("ghost", human_confirm=False)


def test_summary_returns_counts(online_mgr):
    online_mgr.set_meta("a", "1d", "", "u", "2024-01-01", weight_type="ts")
    online_mgr.publish_weights(
        "a",
        pd.DataFrame({"dt": ["2024-01-01"], "symbol": ["X"], "weight": [0.1]}),
    )
    online_mgr.add_tag("a", "x")
    s = online_mgr.summary()
    assert s["strategies"] >= 1
    assert s["weights"] >= 1
    assert s["tags"] >= 1
