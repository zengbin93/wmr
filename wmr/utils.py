"""时区与格式化工具。

从 ``czsc.traders.cwc`` 移植,语义对齐(对应飞书设计文档"七、时区与数据处理约定")。

所有时间字段统一使用 ``Asia/Shanghai`` 时区:
- ClickHouse 列定义为 ``DateTime('Asia/Shanghai')``
- DuckDB 列为 TIMESTAMP(naive),由 Python 端附 Asia/Shanghai 时区
- 返回的 DataFrame 中 datetime 列统一带 Asia/Shanghai 时区
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

import pandas as pd
from pandas._libs.tslibs.nattype import NaTType

DEFAULT_TZ = ZoneInfo("Asia/Shanghai")


def _ensure_timestamp(value: Any, tz: ZoneInfo = DEFAULT_TZ) -> pd.Timestamp | NaTType:
    """将任意时间对象转换为带时区的 ``pd.Timestamp``。

    Args:
        value: 时间值,支持 ``str`` / ``datetime`` / ``pd.Timestamp`` / ``None`` 等
            可被 ``pd.to_datetime`` 解析的输入。
        tz: 目标时区,默认 ``Asia/Shanghai``。

    Returns:
        带时区的 ``pd.Timestamp``;``None`` / 空字符串 / 解析失败均返回 ``pd.NaT``。
    """
    if value is None:
        return pd.NaT
    if isinstance(value, str) and not value.strip():
        return pd.NaT

    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return pd.NaT

    if ts.tzinfo is None:
        return ts.tz_localize(tz)
    return ts.tz_convert(tz)


def _ensure_series_tz(series: pd.Series, tz: ZoneInfo = DEFAULT_TZ) -> pd.Series:
    """确保 Series 中的时间字段带有指定时区。

    Args:
        series: 时间序列,可为 datetime64 或可解析为时间的字符串序列。
        tz: 目标时区,默认 ``Asia/Shanghai``。

    Returns:
        带时区的 datetime64 序列。原序列若已带时区则做 ``tz_convert``,
        否则 ``tz_localize``。
    """
    ser = pd.to_datetime(series, errors="coerce")
    if isinstance(ser.dtype, pd.DatetimeTZDtype):
        return ser.dt.tz_convert(tz)
    return ser.dt.tz_localize(tz)


def _format_for_db(value: Any, tz: ZoneInfo = DEFAULT_TZ) -> str | None:
    """将带时区的时间对象格式化为 ``YYYY-MM-DD HH:MM:SS``(无时区后缀)。

    用于写入 ClickHouse / DuckDB 的字符串字面量。

    Args:
        value: 时间值。
        tz: 目标时区,默认 ``Asia/Shanghai``。

    Returns:
        格式化字符串;无效输入返回 ``None``。
    """
    ts = _ensure_timestamp(value, tz=tz)
    if not isinstance(ts, pd.Timestamp):
        return None
    return ts.astimezone(tz).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _localize_dataframe_columns(df: pd.DataFrame, columns: Iterable[str], tz: ZoneInfo = DEFAULT_TZ) -> pd.DataFrame:
    """将 DataFrame 中指定列本地化为指定时区。

    原地修改并返回同一 DataFrame。列若不存在则跳过。

    Args:
        df: 输入 DataFrame。
        columns: 需要本地化的列名集合。
        tz: 目标时区,默认 ``Asia/Shanghai``。

    Returns:
        同一 DataFrame(原地修改)。
    """
    for col in columns:
        if col in df.columns:
            df[col] = _ensure_series_tz(df[col], tz=tz)
    return df


def _to_naive(ts: Any, tz: ZoneInfo = DEFAULT_TZ) -> pd.Timestamp | None:
    """把任意时间对象转为 naive ``pd.Timestamp``(去掉 tz)。

    用于 DuckDB ``TIMESTAMP`` 列的写入与查询;无效输入返回 ``None``。
    """
    t = _ensure_timestamp(ts, tz=tz)
    if not isinstance(t, pd.Timestamp):
        return None
    return t.tz_convert(tz).tz_localize(None)


def _series_to_naive(series: pd.Series, tz: ZoneInfo = DEFAULT_TZ) -> pd.Series:
    """``_to_naive`` 的 Series 版本。"""
    s = _ensure_series_tz(series, tz=tz)
    return s.dt.tz_convert(tz).dt.tz_localize(None)


def mask_dsn_password(dsn: str | None) -> str:
    """将 DSN 中的密码替换为 ``***``,用于日志或 ``__repr__`` 输出。

    安全规则要求 DSN 中的密码绝不能以明文输出到终端 / 日志(对应
    docs/code-quality.md §3.8)。实现上**直接重建 netloc** 而不是 string
    replace,确保密码含 ``@`` / ``:`` 等特殊字符时仍能彻底脱敏。

    Args:
        dsn: 原始 DSN 字符串,如 ``clickhouse://user:pass@host:9000/db``。
            ``None`` 与空串返回空串。

    Returns:
        脱敏后的 DSN 字符串。
    """
    if not dsn:
        return ""
    try:
        parsed = urlparse(dsn)
        if not parsed.password:
            return dsn
        # 重建 netloc:user:***@host[:port],避免 string replace 在密码含特殊字符时漏抹。
        userinfo = f"{parsed.username or ''}:***"
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        netloc = f"{userinfo}@{host}" if host else f"{userinfo}@"
        return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        return "***"
