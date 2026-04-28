"""验收点 A3:3 个视图(cs / ts / latest)在 LocalManager 中可正常创建并查询。"""

from __future__ import annotations

import pandas as pd
import pytest

pytestmark = pytest.mark.integration


def test_views_created_and_queryable(local_mgr):
    """直接对 3 个视图执行 SELECT,确认它们存在且查询成功。"""
    c = local_mgr.conn
    cs = c.execute("SELECT * FROM cs_latest_weights").df()
    ts = c.execute("SELECT * FROM ts_latest_weights").df()
    union = c.execute("SELECT * FROM latest_weights").df()
    assert cs.empty
    assert ts.empty
    assert union.empty


def test_latest_weights_view_unions_correctly(local_mgr):
    """A3:latest_weights = ts_latest_weights UNION ALL cs_latest_weights。"""
    local_mgr.set_meta("ts1", "1d", "", "u", "2024-01-01", weight_type="ts")
    local_mgr.set_meta("cs1", "1d", "", "u", "2024-01-01", weight_type="cs")

    local_mgr.publish_weights(
        "ts1",
        pd.DataFrame({"dt": ["2024-01-01", "2024-01-02"], "symbol": ["X", "X"], "weight": [0.1, 0.2]}),
    )
    local_mgr.publish_weights(
        "cs1",
        pd.DataFrame({"dt": ["2024-01-01", "2024-01-01"], "symbol": ["X", "Y"], "weight": [0.5, 0.5]}),
    )

    union = local_mgr.get_latest_weights()
    assert set(union["strategy"]) == {"ts1", "cs1"}
    assert len(union) == 3  # ts1: 1 (X 最新), cs1: 2 (X+Y 最新)
