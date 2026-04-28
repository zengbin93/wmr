"""wmr quickstart 示例。

演示 LocalManager(DuckDB)的基本用法。CI 中通过 ``pytest examples/`` 检验
本示例可直接运行(C6 验收要求)。

运行:
    uv run python examples/quickstart.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from wmr import LocalManager


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "weights.duckdb")
        with LocalManager(db_path=db_path) as mgr:
            mgr.initialize()

            # 1. 注册一个时序策略
            mgr.set_meta(
                strategy="alpha_v1",
                base_freq="1d",
                description="quickstart 示例策略",
                author="alice",
                outsample_sdt="2024-01-01",
                weight_type="ts",
            )

            # 2. 发布 3 天的持仓权重
            df = pd.DataFrame(
                {
                    "dt": ["2024-01-01", "2024-01-02", "2024-01-03"],
                    "symbol": ["BTC", "BTC", "BTC"],
                    "weight": [0.5, 0.6, 0.7],
                }
            )
            mgr.publish_weights("alpha_v1", df)

            # 3. 发布日收益
            ret = pd.DataFrame(
                {
                    "dt": ["2024-01-02", "2024-01-03"],
                    "symbol": ["BTC", "BTC"],
                    "returns": [0.01, -0.02],
                }
            )
            mgr.publish_returns("alpha_v1", ret)

            # 4. 加标签
            mgr.add_tag("alpha_v1", "momentum", creator="alice")

            # 5. 查询
            print("get_latest_weights:")
            print(mgr.get_latest_weights("alpha_v1"))
            print("\nget_all_metas:")
            print(mgr.get_all_metas()[["strategy", "weight_type", "status"]])
            print("\nlist_tags:")
            print(mgr.list_tags())
            print("\nsummary:")
            print(mgr.summary())


if __name__ == "__main__":
    main()
