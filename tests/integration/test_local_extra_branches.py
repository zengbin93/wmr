"""LocalManager 边角分支补漏测试。

针对覆盖率报告里的 missing 行号:_to_naive None 路径、构造默认/:memory:、
close 幂等、各类过滤的稀有分支组合、heartbeat 不存在策略、_insert_or_replace
空 DataFrame、_scalar 空参数。
"""

from __future__ import annotations

import pandas as pd
import pytest

from wmr import LocalManager
from wmr.local import _to_naive

pytestmark = pytest.mark.integration


# ---------- _to_naive None ----------
def test_to_naive_none_returns_none():
    """line 60:无效输入(_ensure_timestamp 给出 NaT)→ 返回 None。"""
    assert _to_naive(None) is None
    assert _to_naive("") is None
    assert _to_naive("not-a-date") is None


# ---------- 构造路径 ----------
def test_init_default_db_path(tmp_path, monkeypatch):
    """line 98:db_path=None 走 DEFAULT_LOCAL_DB_PATH。"""
    fake_default = str(tmp_path / "default" / "weights.duckdb")
    monkeypatch.setattr("wmr.local.DEFAULT_LOCAL_DB_PATH", fake_default)
    with LocalManager(db_path=None) as mgr:
        assert mgr._db_path == fake_default
        mgr.initialize()
        assert mgr.summary()["strategies"] == 0


def test_init_memory_skips_mkdir():
    """line 99->101:`:memory:` 不应触发 parent.mkdir(否则会建 :memory: 目录)。"""
    with LocalManager(db_path=":memory:") as mgr:
        mgr.initialize()
        mgr.set_meta("a", "1d", "", "u", "2024-01-01")
        assert mgr.get_meta("a")["strategy"] == "a"


def test_close_when_not_connected_is_safe():
    """line 116->exit:未 connect 直接 close 应无副作用。"""
    mgr = LocalManager(db_path=":memory:")
    mgr.close()  # 未 connect,直接走 self._conn is None 分支
    mgr.close()  # 第二次仍安全


def test_close_idempotent_after_connect():
    """重复 close 安全。"""
    mgr = LocalManager(db_path=":memory:")
    mgr.connect()
    mgr.close()
    mgr.close()  # 再 close 一次走 self._conn is None 分支


# ---------- get_all_metas / get_strategies_by_status 空表 ----------
def test_get_all_metas_empty(local_mgr):
    """line 232->238:空表分支。"""
    df = local_mgr.get_all_metas()
    assert df.empty


def test_get_strategies_by_status_empty(local_mgr):
    """line 305->311:status 过滤后空 DataFrame。"""
    assert local_mgr.get_strategies_by_status("实盘").empty
    assert local_mgr.get_strategies_by_status().empty


# ---------- publish_weights / publish_returns 罕见分支 ----------
def test_publish_weights_symbol_not_in_latest(local_mgr):
    """line 330->332:新 symbol 不在 latest_weights 中,不过滤直接保留。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    local_mgr.publish_weights(
        "ts1",
        pd.DataFrame({"dt": ["2024-01-01"], "symbol": ["X"], "weight": [0.1]}),
    )
    # 第二次:symbol Y 全新,X 给已有日期 → X 被全部过滤 + Y 全部保留
    local_mgr.publish_weights(
        "ts1",
        pd.DataFrame(
            {
                "dt": ["2024-01-01", "2024-01-02"],
                "symbol": ["X", "Y"],
                "weight": [0.99, 0.5],
            }
        ),
    )
    out = local_mgr.get_strategy_weights("ts1")
    by_sym = out.set_index("symbol")
    # X 仍是 0.1(同日数据被仅追加语义过滤);Y 是 0.5(全新 symbol)
    assert by_sym.loc["X", "weight"] == 0.1
    assert by_sym.loc["Y", "weight"] == 0.5


def test_publish_returns_symbol_not_in_latest(local_mgr):
    """line 414-419:returns 同样的新 symbol 分支。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    local_mgr.publish_returns(
        "ts1",
        pd.DataFrame({"dt": ["2024-01-01"], "symbol": ["X"], "returns": [0.01]}),
    )
    local_mgr.publish_returns(
        "ts1",
        pd.DataFrame({"dt": ["2024-01-02"], "symbol": ["Y"], "returns": [0.02]}),
    )
    out = local_mgr.get_strategy_returns("ts1")
    assert set(out["symbol"]) == {"X", "Y"}


# ---------- 过滤组合 ----------
def test_get_strategy_weights_only_sdt(local_mgr):
    """line 366-369:仅传 sdt,不传 edt/symbols。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    local_mgr.publish_weights(
        "ts1",
        pd.DataFrame(
            {
                "dt": ["2024-01-01", "2024-01-02", "2024-01-03"],
                "symbol": ["X"] * 3,
                "weight": [0.1, 0.2, 0.3],
            }
        ),
    )
    out = local_mgr.get_strategy_weights("ts1", sdt="2024-01-02")
    assert len(out) == 2


def test_get_strategy_weights_only_edt(local_mgr):
    """line 370-373:仅传 edt。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    local_mgr.publish_weights(
        "ts1",
        pd.DataFrame(
            {
                "dt": ["2024-01-01", "2024-01-02", "2024-01-03"],
                "symbol": ["X"] * 3,
                "weight": [0.1, 0.2, 0.3],
            }
        ),
    )
    out = local_mgr.get_strategy_weights("ts1", edt="2024-01-02")
    assert len(out) == 2


def test_get_strategy_weights_invalid_sdt_passthrough(local_mgr):
    """sdt 解析为 NaT 时(空串)应跳过该过滤。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    local_mgr.publish_weights(
        "ts1",
        pd.DataFrame({"dt": ["2024-01-01"], "symbol": ["X"], "weight": [0.1]}),
    )
    out = local_mgr.get_strategy_weights("ts1", sdt="", edt="not-a-date")
    assert len(out) == 1


def test_get_strategy_returns_full_filter_combo(local_mgr):
    """line 447-463:returns 同时传 sdt + edt + symbols(list)。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    local_mgr.publish_returns(
        "ts1",
        pd.DataFrame(
            {
                "dt": ["2024-01-01", "2024-01-02", "2024-01-03"] * 2,
                "symbol": ["X"] * 3 + ["Y"] * 3,
                "returns": [0.01] * 6,
            }
        ),
    )
    out = local_mgr.get_strategy_returns("ts1", sdt="2024-01-02", edt="2024-01-02", symbols=["X", "Y"])
    assert len(out) == 2
    assert set(out["symbol"]) == {"X", "Y"}


def test_get_strategy_returns_single_symbol_string(local_mgr):
    """单字符串 symbol 路径。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    local_mgr.publish_returns(
        "ts1",
        pd.DataFrame(
            {
                "dt": ["2024-01-01"] * 2,
                "symbol": ["X", "Y"],
                "returns": [0.01, 0.02],
            }
        ),
    )
    out = local_mgr.get_strategy_returns("ts1", symbols="X")
    assert (out["symbol"] == "X").all()


def test_get_strategy_returns_invalid_dates(local_mgr):
    """无效 sdt/edt 应不附加过滤。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    local_mgr.publish_returns(
        "ts1",
        pd.DataFrame({"dt": ["2024-01-01"], "symbol": ["X"], "returns": [0.01]}),
    )
    out = local_mgr.get_strategy_returns("ts1", sdt="", edt="")
    assert len(out) == 1


# ---------- heartbeat / _scalar / _insert_or_replace ----------
def test_heartbeat_missing_strategy_warns_only(local_mgr):
    """line 523-524:不存在策略只警告。"""
    local_mgr.heartbeat("ghost")  # 不抛


def test_insert_or_replace_empty_df_noop(local_mgr):
    """line 589:空 df 走 early return。"""
    empty = pd.DataFrame(columns=["dt", "symbol", "weight", "strategy", "update_time"])
    local_mgr._insert_or_replace("weights", empty, key_cols=["strategy", "dt", "symbol"])
    assert local_mgr.summary()["weights"] == 0


def test_scalar_with_none_params(local_mgr):
    """line 601:_scalar 默认 params=None 走 `or []` 分支。"""
    n = local_mgr._scalar("SELECT count(*) FROM metas")
    assert n == 0
