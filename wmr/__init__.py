"""wmr — Weight Manager。策略持仓权重管理系统(DuckDB / ClickHouse 双后端)。"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from wmr.base import BaseManager
from wmr.local import LocalManager
from wmr.online import OnlineManager

try:
    __version__ = _pkg_version("wmr")
except PackageNotFoundError:
    # 源码模式(未通过 uv sync / pip install -e 安装)兜底,避免 import 直接失败。
    __version__ = "0.0.0+local"

__all__ = ["BaseManager", "LocalManager", "OnlineManager"]
