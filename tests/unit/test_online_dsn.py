"""``OnlineManager`` DSN 解析与脱敏单元测试(不连真实 ClickHouse)。"""

from __future__ import annotations

import pytest

from wmr.online import _parse_dsn

pytestmark = pytest.mark.unit


class TestParseDsn:
    def test_full_dsn(self):
        out = _parse_dsn("clickhouse://alice:s3cret@127.0.0.1:9000/trade")
        assert out["host"] == "127.0.0.1"
        assert out["port"] == 9000
        assert out["user"] == "alice"
        assert out["password"] == "s3cret"
        assert out["database"] == "trade"

    def test_no_database_returns_empty_string(self):
        out = _parse_dsn("clickhouse://alice:p@127.0.0.1:9000")
        assert out["database"] == ""

    def test_no_user_defaults_to_default(self):
        out = _parse_dsn("clickhouse://:@127.0.0.1:9000/db")
        assert out["user"] == "default"

    def test_invalid_scheme_raises(self):
        with pytest.raises(ValueError, match="DSN scheme"):
            _parse_dsn("postgres://x:y@h:1/d")

    def test_missing_host_raises(self):
        with pytest.raises(ValueError, match="DSN 缺少 host"):
            _parse_dsn("clickhouse://:9000")

    def test_missing_port_raises(self):
        with pytest.raises(ValueError, match="DSN 缺少 port"):
            _parse_dsn("clickhouse://alice:p@host/db")


class TestOnlineManagerInit:
    def test_dsn_none_no_env_raises(self, monkeypatch: pytest.MonkeyPatch):
        from wmr.online import OnlineManager

        monkeypatch.delenv("WMR_CLICKHOUSE_DSN", raising=False)
        with pytest.raises(ValueError, match="WMR_CLICKHOUSE_DSN"):
            OnlineManager()

    def test_dsn_from_env(self, monkeypatch: pytest.MonkeyPatch):
        from wmr.online import OnlineManager

        monkeypatch.setenv("WMR_CLICKHOUSE_DSN", "clickhouse://u:p@host:9000/db_from_env")
        mgr = OnlineManager()
        assert mgr._dsn_parts["host"] == "host"
        assert mgr._database == "db_from_env"

    def test_database_arg_overrides_dsn_path(self):
        from wmr.online import OnlineManager

        mgr = OnlineManager(dsn="clickhouse://u:p@host:9000/in_path", database="explicit_db")
        assert mgr._database == "explicit_db"

    def test_repr_masks_password(self):
        from wmr.online import OnlineManager

        mgr = OnlineManager(dsn="clickhouse://u:supersecret@host:9000/db")
        r = repr(mgr)
        assert "supersecret" not in r
        assert "***" in r

    def test_default_database_when_dsn_path_empty(self, monkeypatch: pytest.MonkeyPatch):
        from wmr.online import OnlineManager

        monkeypatch.delenv("WMR_DATABASE", raising=False)
        mgr = OnlineManager(dsn="clickhouse://u:p@host:9000")
        assert mgr._database == "czsc_strategy"
