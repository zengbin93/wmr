"""性能基准测试。覆盖验收点 N1 / N3 / N4。

默认 ``--run-perf`` 时才执行,通过 ``pytest-benchmark`` 收集数据。
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.perf


def _make_big_df(n: int, n_symbols: int = 100) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    dt_range = pd.date_range("2020-01-01", periods=n // n_symbols, freq="min")
    symbols = [f"SYM{i:03d}" for i in range(n_symbols)]
    data = {
        "dt": np.tile(dt_range, n_symbols)[:n],
        "symbol": np.repeat(symbols, len(dt_range))[:n],
        "weight": rng.random(n),
    }
    return pd.DataFrame(data)


def test_perf_publish_weights_1m_local(local_mgr):
    """N1:LocalManager publish_weights 100 万行 < 5s。"""
    local_mgr.set_meta("perf", "1m", "", "u", "2020-01-01", weight_type="ts")
    df = _make_big_df(1_000_000)

    start = time.perf_counter()
    local_mgr.publish_weights("perf", df, batch_size=200_000)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"DuckDB publish 1M rows {elapsed:.2f}s,目标 < 5s"


def test_perf_get_latest_weights_p95_local(local_mgr):
    """N3:LocalManager get_latest_weights P95 < 200ms(样本 100 策略 × 50 标的)。"""
    n_strategies = 100
    n_symbols = 50
    for i in range(n_strategies):
        s = f"s{i:03d}"
        local_mgr.set_meta(s, "1d", "", "u", "2020-01-01", weight_type="ts")
        df = pd.DataFrame(
            {
                "dt": pd.date_range("2024-01-01", periods=10, freq="D").tolist() * n_symbols,
                "symbol": [f"X{j:02d}" for j in range(n_symbols) for _ in range(10)],
                "weight": [0.01] * (10 * n_symbols),
            }
        )
        local_mgr.publish_weights(s, df, batch_size=100_000)

    samples: list[float] = []
    for _ in range(20):
        t0 = time.perf_counter()
        local_mgr.get_latest_weights()
        samples.append(time.perf_counter() - t0)
    p95 = sorted(samples)[int(0.95 * len(samples))]
    assert p95 < 0.2, f"latest_weights P95 {p95 * 1000:.0f}ms,目标 < 200ms"
