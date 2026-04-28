"""验收点:统一 verbose 模式 + 日志节奏重排(对应 docs/verbose-mode.md)。

测试设计:不走 loguru → caplog 桥接,直接给 Manager 注入 ``_LogCapture`` stub,
验证 ``_vlog`` / ``_vexc`` / ``self._logger.*`` 的调用层级与档位是否符合设计。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from wmr import LocalManager
from wmr.utils import _resolve_verbose, _truncate_seq

pytestmark = pytest.mark.unit


# ---------- 测试基础设施 ----------
class _LogCapture:
    """轻量 logger stub,记录 (level, message) 调用序列。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def info(self, msg: Any, *_: Any, **__: Any) -> None:
        self.records.append(("INFO", str(msg)))

    def debug(self, msg: Any, *_: Any, **__: Any) -> None:
        self.records.append(("DEBUG", str(msg)))

    def warning(self, msg: Any, *_: Any, **__: Any) -> None:
        self.records.append(("WARNING", str(msg)))

    def error(self, msg: Any, *_: Any, **__: Any) -> None:
        self.records.append(("ERROR", str(msg)))

    def log(self, level: Any, msg: Any, *_: Any, **__: Any) -> None:
        self.records.append((str(level).upper(), str(msg)))

    def exception(self, msg: Any, *_: Any, **__: Any) -> None:
        self.records.append(("EXCEPTION", str(msg)))

    def messages(self, level: str | None = None) -> list[str]:
        if level is None:
            return [m for _, m in self.records]
        return [m for lvl, m in self.records if lvl == level]


def _make_local(verbose: bool | None = None, *, db_path: str = ":memory:") -> tuple[LocalManager, _LogCapture]:
    """返回 (mgr, capture)。mgr 已 initialize,capture 从 initialize 之后开始干净状态。

    initialize 本身的日志归 capture(便于断言 verbose 下能看到 ``创建/复用表`` 等);
    构造 Manager 时由 _LogCapture 直接接收 __init__ 内的 _vlog 调用。
    """
    cap = _LogCapture()
    mgr = LocalManager(db_path=db_path, logger=cap, verbose=verbose)
    mgr.initialize()
    return mgr, cap


@pytest.fixture
def sample_weights_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "dt": pd.date_range("2026-01-01", periods=4, freq="D", tz="Asia/Shanghai"),
            "symbol": ["A", "A", "B", "B"],
            "weight": [0.1, 0.2, 0.3, 0.4],
        }
    )


def _set_meta(mgr: LocalManager, strategy: str = "S1") -> None:
    mgr.set_meta(
        strategy=strategy,
        base_freq="日线",
        description="test",
        author="t",
        outsample_sdt="2025-01-01",
    )


# ---------- _resolve_verbose ----------
@pytest.mark.parametrize(
    "env_val, expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("", False),
        ("garbage", False),
    ],
)
def test_resolve_verbose_env(monkeypatch: pytest.MonkeyPatch, env_val: str, expected: bool) -> None:
    monkeypatch.setenv("WMR_VERBOSE", env_val)
    assert _resolve_verbose(None) is expected


def test_resolve_verbose_explicit_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WMR_VERBOSE", "1")
    assert _resolve_verbose(False) is False
    assert _resolve_verbose(True) is True


def test_resolve_verbose_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WMR_VERBOSE", raising=False)
    assert _resolve_verbose(None) is False


# ---------- _vlog 三档路由 ----------
def test_vlog_default_goes_to_debug() -> None:
    mgr, cap = _make_local(verbose=False)
    cap.records.clear()
    mgr._vlog("detail msg")
    levels = [lvl for lvl, m in cap.records if "detail msg" in m]
    assert levels == ["DEBUG"]


def test_vlog_verbose_goes_to_info() -> None:
    mgr, cap = _make_local(verbose=True)
    cap.records.clear()
    mgr._vlog("detail msg")
    levels = [lvl for lvl, m in cap.records if "detail msg" in m]
    assert levels == ["INFO"]


def test_vlog_verbose_custom_level() -> None:
    mgr, cap = _make_local(verbose=True)
    cap.records.clear()
    mgr._vlog("warn msg", level="WARNING")
    assert ("WARNING", "warn msg") in cap.records


# ---------- _vexc 异常路由 ----------
def test_vexc_default_only_error() -> None:
    mgr, cap = _make_local(verbose=False)
    cap.records.clear()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        mgr._vexc("expected boom")
    assert ("ERROR", "expected boom") in cap.records
    assert not any(lvl == "EXCEPTION" for lvl, _ in cap.records)


def test_vexc_verbose_uses_exception() -> None:
    mgr, cap = _make_local(verbose=True)
    cap.records.clear()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        mgr._vexc("expected boom")
    assert ("EXCEPTION", "expected boom") in cap.records


# ---------- env 兜底通过 LocalManager ----------
def test_local_manager_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WMR_VERBOSE", "1")
    mgr, _ = _make_local()  # verbose=None,读 env
    assert mgr._verbose is True


def test_local_manager_explicit_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WMR_VERBOSE", "1")
    mgr, _ = _make_local(verbose=False)
    assert mgr._verbose is False


# ---------- publish 入口/出口 event 默认可见 ----------
def test_publish_entry_exit_visible_by_default(sample_weights_df: pd.DataFrame) -> None:
    mgr, cap = _make_local(verbose=False)
    _set_meta(mgr)
    cap.records.clear()
    mgr.publish_weights("S1", sample_weights_df)
    info_msgs = cap.messages("INFO")
    assert any(m.startswith("开始 publish_weights") and "S1" in m for m in info_msgs)
    assert any(m.startswith("完成 publish_weights") and "实际写入" in m and "耗时" in m for m in info_msgs)


def test_publish_progress_silent_by_default(sample_weights_df: pd.DataFrame) -> None:
    """中间细节(批次进度、过滤摘要)默认不应出现在 INFO 流。"""
    mgr, cap = _make_local(verbose=False)
    _set_meta(mgr)
    cap.records.clear()
    mgr.publish_weights("S1", sample_weights_df)
    info_msgs = cap.messages("INFO")
    assert not any("批次" in m for m in info_msgs)
    assert not any("历史最新时间" in m for m in info_msgs)
    assert not any("过滤后剩余" in m for m in info_msgs)


def test_publish_full_chain_visible_in_verbose(sample_weights_df: pd.DataFrame) -> None:
    """verbose 模式下,中间细节升级到 INFO。"""
    mgr, cap = _make_local(verbose=True)
    _set_meta(mgr)
    cap.records.clear()
    mgr.publish_weights("S1", sample_weights_df)
    info_msgs = cap.messages("INFO")
    assert any(m.startswith("开始 publish_weights") for m in info_msgs)
    # 首次发布无历史 → 走"无历史数据"分支
    assert any("无 weights 历史数据" in m for m in info_msgs)
    assert any("批次 1" in m for m in info_msgs)
    assert any(m.startswith("heartbeat(S1) ok") for m in info_msgs)
    assert any(m.startswith("完成 publish_weights") for m in info_msgs)


def test_publish_returns_entry_exit(sample_weights_df: pd.DataFrame) -> None:
    """publish_returns 入口/出口对称。"""
    df = sample_weights_df.rename(columns={"weight": "returns"})
    mgr, cap = _make_local(verbose=False)
    _set_meta(mgr)
    cap.records.clear()
    mgr.publish_returns("S1", df)
    info_msgs = cap.messages("INFO")
    assert any(m.startswith("开始 publish_returns") for m in info_msgs)
    assert any(m.startswith("完成 publish_returns") and "耗时" in m for m in info_msgs)


# ---------- 读路径 ----------
def test_read_paths_default_silent(sample_weights_df: pd.DataFrame) -> None:
    mgr, cap = _make_local(verbose=False)
    _set_meta(mgr)
    mgr.publish_weights("S1", sample_weights_df)
    cap.records.clear()
    mgr.get_strategy_weights("S1")
    mgr.get_latest_weights("S1")
    mgr.get_all_metas()
    info_msgs = cap.messages("INFO")
    assert not any("get_strategy_weights" in m for m in info_msgs)
    assert not any("get_latest_weights" in m for m in info_msgs)
    assert not any("get_all_metas" in m for m in info_msgs)


def test_read_paths_visible_in_verbose(sample_weights_df: pd.DataFrame) -> None:
    mgr, cap = _make_local(verbose=True)
    _set_meta(mgr)
    mgr.publish_weights("S1", sample_weights_df)
    cap.records.clear()
    mgr.get_strategy_weights("S1", sdt="2026-01-01")
    mgr.get_latest_weights("S1")
    mgr.get_all_metas()
    info_msgs = cap.messages("INFO")
    assert any("get_strategy_weights" in m and "→" in m for m in info_msgs)
    assert any("get_latest_weights" in m and "→" in m for m in info_msgs)
    assert any("get_all_metas" in m and "→" in m for m in info_msgs)


# ---------- initialize 步骤展开 ----------
def test_initialize_steps_visible_in_verbose() -> None:
    cap = _LogCapture()
    mgr = LocalManager(db_path=":memory:", logger=cap, verbose=True)
    cap.records.clear()  # 排除 __init__ 自身的 _vlog
    mgr.initialize()
    info_msgs = cap.messages("INFO")
    # 4 张表 + 3 个视图 + 1 条总结
    for table in ("metas", "weights", "returns", "tags"):
        assert any(f"创建/复用表: {table}" in m for m in info_msgs), f"verbose 下应看到 {table} 表创建日志"
    for view in ("cs_latest_weights", "ts_latest_weights", "latest_weights"):
        assert any(f"创建/复用视图: {view}" in m for m in info_msgs), f"verbose 下应看到 {view} 视图创建日志"
    assert any("LocalManager initialize 完成" in m for m in info_msgs)


def test_initialize_silent_by_default() -> None:
    cap = _LogCapture()
    mgr = LocalManager(db_path=":memory:", logger=cap, verbose=False)
    cap.records.clear()
    mgr.initialize()
    info_msgs = cap.messages("INFO")
    # 默认下只有 1 条总结 INFO,各表/视图细节走 DEBUG
    assert sum(1 for m in info_msgs if "initialize 完成" in m) == 1
    assert not any("创建/复用表:" in m for m in info_msgs)
    assert not any("创建/复用视图:" in m for m in info_msgs)


# ---------- clear_strategy 压缩 ----------
def test_clear_strategy_compact_dict_summary(sample_weights_df: pd.DataFrame) -> None:
    """概况从 6 行 dict 压缩为 1 行 INFO,出口附耗时。"""
    mgr, cap = _make_local(verbose=False)
    _set_meta(mgr)
    mgr.publish_weights("S1", sample_weights_df)
    cap.records.clear()
    mgr.clear_strategy("S1", human_confirm=False)
    info_msgs = cap.messages("INFO")
    # 概况合并为单行
    summary_lines = [m for m in info_msgs if "即将清空" in m]
    assert len(summary_lines) == 1
    assert "weights=" in summary_lines[0] and "tags=" in summary_lines[0]
    # 出口含耗时
    assert any("清空完成" in m and "耗时" in m and "metas=1" in m for m in info_msgs)
    # 装饰行已删除
    assert not any("=" * 60 in m for m in info_msgs)


# ---------- __repr__ ----------
def test_repr_contains_verbose_field() -> None:
    mgr_v, _ = _make_local(verbose=True)
    mgr_q, _ = _make_local(verbose=False)
    assert "verbose=True" in repr(mgr_v)
    assert "verbose=False" in repr(mgr_q)


# ---------- _truncate_seq ----------
@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "None"),
        ("foo", "'foo'"),
        ([], "[]"),
        (["a"], "['a']"),
        (["a", "b", "c"], "['a', 'b', 'c']"),
        (["a", "b", "c", "d", "e", "f"], "['a', 'b', 'c', 'd', 'e', ..., 共 6 个]"),
    ],
)
def test_truncate_seq(value: Any, expected: str) -> None:
    assert _truncate_seq(value) == expected
