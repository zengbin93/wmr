"""LocalManager tags 接口集成测试。覆盖验收点 F5。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_add_tag_then_list(local_mgr):
    local_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    local_mgr.add_tag("a", "momentum", creator="alice")

    df = local_mgr.list_tags()
    assert len(df) == 1
    row = df.iloc[0]
    assert row["strategy"] == "a"
    assert row["tag"] == "momentum"
    assert row["creator"] == "alice"


def test_add_tag_idempotent_on_same_pair(local_mgr):
    """F5:同一 (strategy, tag) 重复 add 应幂等。"""
    local_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    local_mgr.add_tag("a", "x")
    local_mgr.add_tag("a", "x")
    local_mgr.add_tag("a", "x")
    assert len(local_mgr.list_tags()) == 1


def test_add_tags_batch(local_mgr):
    local_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    local_mgr.set_meta("b", "1m", "", "u", "2024-01-01")

    n = local_mgr.add_tags([("a", "t1"), ("a", "t2"), ("b", "t1")])
    assert n == 3
    assert len(local_mgr.list_tags()) == 3


def test_list_tags_filter_by_strategy(local_mgr):
    local_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    local_mgr.set_meta("b", "1m", "", "u", "2024-01-01")
    local_mgr.add_tag("a", "x")
    local_mgr.add_tag("b", "y")

    df = local_mgr.list_tags(strategy="a")
    assert list(df["strategy"]) == ["a"]


def test_list_tags_filter_by_tag(local_mgr):
    local_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    local_mgr.set_meta("b", "1m", "", "u", "2024-01-01")
    local_mgr.add_tag("a", "shared")
    local_mgr.add_tag("b", "shared")
    local_mgr.add_tag("a", "unique")

    df = local_mgr.list_tags(tag="shared")
    assert len(df) == 2
    assert set(df["strategy"]) == {"a", "b"}


def test_remove_tag(local_mgr):
    local_mgr.set_meta("a", "1m", "", "u", "2024-01-01")
    local_mgr.add_tag("a", "x")
    local_mgr.add_tag("a", "y")
    local_mgr.remove_tag("a", "x")
    df = local_mgr.list_tags("a")
    assert list(df["tag"]) == ["y"]


def test_remove_nonexistent_tag_silent(local_mgr):
    # 删除不存在的 tag 不应抛
    local_mgr.remove_tag("ghost", "nope")


def test_add_tags_empty_returns_zero(local_mgr):
    assert local_mgr.add_tags([]) == 0
