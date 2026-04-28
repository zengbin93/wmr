"""验收点 A1:``BaseManager`` 公共方法签名与 ``czsc.traders.cwc`` 函数完全一致。

去掉 cwc.py 函数级 API 中的 ``db`` / ``database`` / ``logger`` / ``tz`` 等
"基础设施"参数后,剩余参数(顺序、默认值)必须与 ``BaseManager`` 抽象方法对齐。

通过 inspect.signature 进行结构化比较,而非字符串比较。如果 czsc 包不可用,
本组测试自动 skip。
"""

from __future__ import annotations

import inspect

import pytest

from wmr.base import BaseManager

pytestmark = pytest.mark.unit

cwc = pytest.importorskip("czsc.traders.cwc", reason="czsc 包不可用")


def _strip_infra_params(params: list[inspect.Parameter]) -> list[inspect.Parameter]:
    """剔除 db / database / logger / tz / kwargs 等基础设施参数与 self。"""
    drop = {"db", "database", "logger", "tz", "kwargs"}
    return [p for p in params if p.name not in drop and p.name != "self" and p.kind != inspect.Parameter.VAR_KEYWORD]


def _params_compatible(cwc_params: list[inspect.Parameter], wmr_params: list[inspect.Parameter]) -> tuple[bool, str]:
    """判断 wmr 参数列表是否兼容 cwc 函数(去基础设施后)。

    要求:同名参数顺序一致,默认值一致(允许 wmr 多出 *args / **kwargs)。
    """
    if [p.name for p in cwc_params] != [p.name for p in wmr_params]:
        return False, (f"参数名/顺序不一致 cwc={[p.name for p in cwc_params]} wmr={[p.name for p in wmr_params]}")
    for cp, wp in zip(cwc_params, wmr_params, strict=True):
        if cp.default != wp.default:
            return False, f"默认值不一致 {cp.name}: cwc={cp.default!r} wmr={wp.default!r}"
    return True, ""


# (cwc 函数, BaseManager 方法名)
SIGNATURE_PAIRS: list[tuple[str, str]] = [
    ("get_meta", "get_meta"),
    ("get_all_metas", "get_all_metas"),
    ("set_meta", "set_meta"),
    ("update_strategy_status", "update_strategy_status"),
    ("get_strategies_by_status", "get_strategies_by_status"),
    ("publish_weights", "publish_weights"),
    ("get_strategy_weights", "get_strategy_weights"),
    ("get_latest_weights", "get_latest_weights"),
    ("publish_returns", "publish_returns"),
    ("get_strategy_returns", "get_strategy_returns"),
    ("clear_strategy", "clear_strategy"),
]


@pytest.mark.parametrize(("cwc_func", "wmr_method"), SIGNATURE_PAIRS)
def test_signature_alignment(cwc_func: str, wmr_method: str):
    cwc_sig = inspect.signature(getattr(cwc, cwc_func))
    wmr_sig = inspect.signature(getattr(BaseManager, wmr_method))

    cwc_params = _strip_infra_params(list(cwc_sig.parameters.values()))
    wmr_params = _strip_infra_params(list(wmr_sig.parameters.values()))

    ok, reason = _params_compatible(cwc_params, wmr_params)
    assert ok, f"{cwc_func} vs BaseManager.{wmr_method} 签名不对齐:{reason}"
