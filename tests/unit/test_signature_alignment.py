"""验收点 A1:``BaseManager`` 公共方法签名与 ``czsc.traders.cwc`` 对齐。

设计文档"九、测试方案 / 9.5 关键测试用例清单"明确要求:用 ``inspect.signature``
比对 ``BaseManager`` 与上游 cwc.py 函数签名(去 ``db`` / ``database`` 后,参数
顺序与默认值完全一致)。

本测试有两层防线:

1. **硬编码期望签名**(始终运行):把 cwc.py 的预期签名以 ``EXPECTED_SIGNATURES``
   常量表达出来,作为权威。任何对 ``BaseManager`` 公共方法签名的非兼容改动
   都会被捕获。
2. **czsc 真实比对**(仅当 ``czsc`` 可导入时):额外断言 ``BaseManager`` 与
   实际 ``czsc.traders.cwc`` 函数签名一致(去 db/database 后),作为"上游
   漂移"的早期告警。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from wmr.base import BaseManager

pytestmark = pytest.mark.unit


# ---------- 硬编码期望签名(对齐 cwc.py + 项目设计文档"三、API 接口定义") ----------
# 元组语义:(参数名, 默认值)。`inspect.Parameter.empty` 用 _NO_DEFAULT 哨兵替代。
_NO_DEFAULT: Any = inspect.Parameter.empty

EXPECTED_SIGNATURES: dict[str, list[tuple[str, Any]]] = {
    "set_meta": [
        ("strategy", _NO_DEFAULT),
        ("base_freq", _NO_DEFAULT),
        ("description", _NO_DEFAULT),
        ("author", _NO_DEFAULT),
        ("outsample_sdt", _NO_DEFAULT),
        ("weight_type", "ts"),
        ("status", "实盘"),
        ("memo", ""),
        ("overwrite", False),
    ],
    "update_strategy_status": [
        ("strategy", _NO_DEFAULT),
        ("status", _NO_DEFAULT),
    ],
    "get_strategies_by_status": [
        ("status", None),
    ],
    "publish_weights": [
        ("strategy", _NO_DEFAULT),
        ("df", _NO_DEFAULT),
        ("batch_size", 100000),
    ],
    "get_strategy_weights": [
        ("strategy", _NO_DEFAULT),
        ("sdt", None),
        ("edt", None),
        ("symbols", None),
    ],
    "get_latest_weights": [
        ("strategy", None),
    ],
    "publish_returns": [
        ("strategy", _NO_DEFAULT),
        ("df", _NO_DEFAULT),
        ("batch_size", 100000),
    ],
    "get_strategy_returns": [
        ("strategy", _NO_DEFAULT),
        ("sdt", None),
        ("edt", None),
        ("symbols", None),
    ],
    "add_tag": [
        ("strategy", _NO_DEFAULT),
        ("tag", _NO_DEFAULT),
        ("creator", "system"),
    ],
    "add_tags": [
        ("items", _NO_DEFAULT),
        ("batch_size", 500),
    ],
    "list_tags": [
        ("strategy", None),
        ("tag", None),
    ],
    "remove_tag": [
        ("strategy", _NO_DEFAULT),
        ("tag", _NO_DEFAULT),
    ],
    "heartbeat": [
        ("strategy", _NO_DEFAULT),
    ],
    "get_heartbeat": [
        ("strategy", _NO_DEFAULT),
    ],
    "list_heartbeats": [],
    "clear_strategy": [
        ("strategy", _NO_DEFAULT),
        ("human_confirm", True),
    ],
}


def _params_without_self(method) -> list[inspect.Parameter]:
    sig = inspect.signature(method)
    return [p for name, p in sig.parameters.items() if name != "self"]


@pytest.mark.parametrize("method_name", sorted(EXPECTED_SIGNATURES.keys()))
def test_base_manager_signature_matches_expected(method_name: str):
    """A1:BaseManager 公共方法的参数顺序与默认值与 cwc.py 一致。"""
    expected = EXPECTED_SIGNATURES[method_name]
    method = getattr(BaseManager, method_name)
    params = _params_without_self(method)

    actual = [(p.name, p.default if p.default is not inspect.Parameter.empty else _NO_DEFAULT) for p in params]

    assert actual == expected, f"方法 BaseManager.{method_name} 签名漂移:\n  期望 {expected}\n  实际 {actual}"


def test_czsc_cwc_alignment():
    """如果 ``czsc`` 可导入,额外比对真实 cwc.py 函数签名。

    czsc 不在 wmr 的运行/dev 依赖里,只是上游约定;若环境无 czsc 直接 skip。
    比对规则:去掉 cwc 函数中的 ``db`` / ``database`` 参数后,与 BaseManager
    对应方法的参数(顺序 + 默认值)严格相等。
    """
    pytest.importorskip("czsc.traders.cwc")
    from czsc.traders import cwc  # type: ignore[import-not-found]

    drift: list[str] = []
    for method_name in EXPECTED_SIGNATURES:
        cwc_func = getattr(cwc, method_name, None)
        if cwc_func is None:
            continue
        cwc_params = [
            (p.name, p.default if p.default is not inspect.Parameter.empty else _NO_DEFAULT)
            for p in inspect.signature(cwc_func).parameters.values()
            if p.name not in {"db", "database"}
        ]
        method = getattr(BaseManager, method_name)
        wmr_params = [
            (p.name, p.default if p.default is not inspect.Parameter.empty else _NO_DEFAULT)
            for p in _params_without_self(method)
        ]
        if cwc_params != wmr_params:
            drift.append(f"{method_name}: cwc={cwc_params}, wmr={wmr_params}")

    assert not drift, "BaseManager 与 czsc.traders.cwc 签名漂移:\n" + "\n".join(drift)
