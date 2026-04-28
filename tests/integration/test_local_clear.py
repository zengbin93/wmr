"""LocalManager clear_strategy / summary 集成测试。覆盖验收点 F7 / F8。"""

from __future__ import annotations

import pandas as pd
import pytest

pytestmark = pytest.mark.integration


def _seed_strategy(mgr, strategy: str = "alpha") -> None:
    mgr.set_meta(strategy, "1d", "", "u", "2024-01-01", weight_type="ts")
    mgr.publish_weights(
        strategy,
        pd.DataFrame(
            {
                "dt": ["2024-01-01", "2024-01-02"],
                "symbol": ["X", "X"],
                "weight": [0.1, 0.2],
            }
        ),
    )
    mgr.publish_returns(
        strategy,
        pd.DataFrame(
            {
                "dt": ["2024-01-01", "2024-01-02"],
                "symbol": ["X", "X"],
                "returns": [0.01, 0.02],
            }
        ),
    )
    mgr.add_tag(strategy, "momentum")


def test_clear_strategy_no_confirm(local_mgr):
    """F7:human_confirm=False 直接删除 metas/weights/returns/tags 四张表。"""
    _seed_strategy(local_mgr, "alpha")

    local_mgr.clear_strategy("alpha", human_confirm=False)

    assert local_mgr.get_meta("alpha") == {}
    assert local_mgr.get_strategy_weights("alpha").empty
    assert local_mgr.get_strategy_returns("alpha").empty
    assert local_mgr.list_tags(strategy="alpha").empty


def test_clear_strategy_confirm_delete(local_mgr, monkeypatch):
    """F7:input 'DELETE' 确认后级联清空 4 张表。"""
    _seed_strategy(local_mgr, "alpha")
    monkeypatch.setattr("builtins.input", lambda _msg: "DELETE")

    local_mgr.clear_strategy("alpha", human_confirm=True)

    assert local_mgr.get_meta("alpha") == {}


def test_clear_strategy_confirm_cancel(local_mgr, monkeypatch):
    """F7:input 非 'DELETE' 取消删除,数据保留。"""
    _seed_strategy(local_mgr, "alpha")
    monkeypatch.setattr("builtins.input", lambda _msg: "delete")  # 大小写敏感

    local_mgr.clear_strategy("alpha", human_confirm=True)

    assert local_mgr.get_meta("alpha")  # 未删除
    assert not local_mgr.get_strategy_weights("alpha").empty


def test_clear_strategy_missing_warns_only(local_mgr):
    # 不存在策略只 warn,不抛
    local_mgr.clear_strategy("ghost", human_confirm=False)


def test_summary_returns_counts(local_mgr):
    """F8:summary() 返回 dict 含各表行数与策略数。"""
    _seed_strategy(local_mgr, "a")
    _seed_strategy(local_mgr, "b")

    s = local_mgr.summary()
    assert s["metas"] == 2
    assert s["weights"] == 4
    assert s["returns"] == 4
    assert s["tags"] == 2
    assert s["strategies"] == 2
