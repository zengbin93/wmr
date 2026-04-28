"""wmr — Weight Manager。策略持仓权重管理系统(DuckDB / ClickHouse 双后端)。"""

from wmr.base import BaseManager
from wmr.local import LocalManager
from wmr.online import OnlineManager

__version__ = "0.1.0"
__all__ = ["BaseManager", "LocalManager", "OnlineManager"]
