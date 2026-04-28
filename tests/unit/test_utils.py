"""``wmr.utils`` 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from wmr.utils import (
    DEFAULT_TZ,
    _ensure_series_tz,
    _ensure_timestamp,
    _format_for_db,
    _localize_dataframe_columns,
    mask_dsn_password,
)

pytestmark = pytest.mark.unit


# ---------- _ensure_timestamp ----------
class TestEnsureTimestamp:
    def test_none_returns_nat(self):
        assert pd.isna(_ensure_timestamp(None))

    def test_empty_string_returns_nat(self):
        assert pd.isna(_ensure_timestamp(""))
        assert pd.isna(_ensure_timestamp("   "))

    def test_invalid_string_returns_nat(self):
        assert pd.isna(_ensure_timestamp("not-a-date"))

    def test_naive_string_localizes_to_default_tz(self):
        ts = _ensure_timestamp("2024-01-01 09:00:00")
        assert ts.tzinfo is not None
        assert ts.tzinfo == DEFAULT_TZ

    def test_aware_datetime_converts_to_default_tz(self):
        dt = datetime(2024, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
        ts = _ensure_timestamp(dt)
        assert ts.tzinfo == DEFAULT_TZ
        # UTC 01:00 应转换为 Asia/Shanghai 09:00
        assert ts.hour == 9

    def test_pd_timestamp_passthrough(self):
        original = pd.Timestamp("2024-01-01", tz="Asia/Shanghai")
        ts = _ensure_timestamp(original)
        assert ts == original


# ---------- _ensure_series_tz ----------
class TestEnsureSeriesTz:
    def test_naive_series_localizes(self):
        s = pd.Series(pd.to_datetime(["2024-01-01", "2024-01-02"]))
        out = _ensure_series_tz(s)
        assert isinstance(out.dtype, pd.DatetimeTZDtype)

    def test_aware_series_converts(self):
        s = pd.Series(pd.to_datetime(["2024-01-01"], utc=True))
        out = _ensure_series_tz(s)
        # 与底层精度解耦:pandas 3.0 默认 [us],pandas 2.x 默认 [ns]
        assert isinstance(out.dtype, pd.DatetimeTZDtype)
        assert str(out.dtype.tz) == "Asia/Shanghai"

    def test_string_series(self):
        s = pd.Series(["2024-01-01 09:00", "2024-01-02 09:00"])
        out = _ensure_series_tz(s)
        assert isinstance(out.dtype, pd.DatetimeTZDtype)


# ---------- _format_for_db ----------
class TestFormatForDb:
    def test_format_string_no_tz_suffix(self):
        out = _format_for_db("2024-01-01 09:30:00")
        assert out == "2024-01-01 09:30:00"
        # 严格不带时区后缀
        assert "+" not in out
        assert "Z" not in out

    def test_none_returns_none(self):
        assert _format_for_db(None) is None

    def test_empty_string_returns_none(self):
        assert _format_for_db("") is None

    def test_aware_input_converts_then_drops_tz(self):
        dt = datetime(2024, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
        # UTC 01:00 → Shanghai 09:00,format 后无时区后缀
        assert _format_for_db(dt) == "2024-01-01 09:00:00"


# ---------- _localize_dataframe_columns ----------
class TestLocalizeDataFrameColumns:
    def test_localize_present_columns(self):
        df = pd.DataFrame(
            {
                "dt": pd.to_datetime(["2024-01-01"]),
                "value": [1.0],
            }
        )
        out = _localize_dataframe_columns(df, ["dt", "missing"])
        assert isinstance(out["dt"].dtype, pd.DatetimeTZDtype)
        assert "missing" not in out.columns

    def test_in_place_mutation(self):
        df = pd.DataFrame({"dt": pd.to_datetime(["2024-01-01"])})
        out = _localize_dataframe_columns(df, ["dt"])
        assert out is df


# ---------- mask_dsn_password ----------
class TestMaskDsnPassword:
    def test_mask_with_password(self):
        out = mask_dsn_password("clickhouse://user:secret@host:9000/db")
        assert "secret" not in out
        assert "***" in out
        assert "user" in out
        assert "host" in out

    def test_no_password_passthrough(self):
        dsn = "clickhouse://user@host:9000/db"
        assert mask_dsn_password(dsn) == dsn

    def test_none_returns_empty(self):
        assert mask_dsn_password(None) == ""

    def test_empty_returns_empty(self):
        assert mask_dsn_password("") == ""

    def test_invalid_returns_masked(self):
        out = mask_dsn_password("not a valid dsn")
        assert isinstance(out, str)

    def test_password_with_special_chars_fully_masked(self):
        """密码含 ``:`` / ``@`` 等特殊字符(percent-encoded)时,脱敏后不应残留任何片段。"""
        dsn = "clickhouse://alice:p%40ss%3Aword@host:9000/db"
        out = mask_dsn_password(dsn)
        # 脱敏后绝不允许出现原始或 URL-encoded 形式的密码片段
        assert "p%40ss%3Aword" not in out
        assert "p@ss:word" not in out
        assert "***" in out
        assert out.startswith("clickhouse://alice:***@host:9000")

    def test_password_only_no_user(self):
        """空 user + 有 password 仍能正确脱敏。"""
        out = mask_dsn_password("clickhouse://:secret@host/db")
        assert "secret" not in out
        assert "***" in out


# ---------- 参数化:不同时区输入统一收敛 ----------
@pytest.mark.parametrize(
    "value,tz,expected_hour",
    [
        ("2024-01-01 09:00:00", ZoneInfo("Asia/Shanghai"), 9),
        ("2024-01-01 01:00:00+00:00", ZoneInfo("Asia/Shanghai"), 9),
        (
            datetime(2024, 1, 1, 1, 0, tzinfo=timezone.utc),
            ZoneInfo("Asia/Shanghai"),
            9,
        ),
    ],
)
def test_ensure_timestamp_tz_alignment(value, tz, expected_hour):
    ts = _ensure_timestamp(value, tz=tz)
    assert ts.hour == expected_hour
    assert ts.tzinfo == tz
