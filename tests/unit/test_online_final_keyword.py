"""验收点 A5(静态扫描):``OnlineManager`` 所有 SELECT 必须含 ``FINAL`` 关键字。

通过 AST 提取 ``wmr/online.py`` 中的字符串字面量(SQL 模板),逐个判断:
- DDL 字面量(含 ``CREATE`` / ``DROP`` / ``ALTER`` / ``INSERT`` / ``DELETE``)整段跳过
- 其余字面量中每出现一次 ``FROM <db>.<table>``,必须紧随出现 ``FINAL``

设计文档第 4 章明确要求"OnlineManager 的所有 SELECT 必须在表名/视图后追加 FINAL"。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ONLINE_PY = Path(__file__).resolve().parent.parent.parent / "wmr" / "online.py"

TABLES = (
    "metas",
    "weights",
    "returns",
    "tags",
    "latest_weights",
    "cs_latest_weights",
    "ts_latest_weights",
)
DDL_KEYWORDS = ("CREATE ", "DROP ", "ALTER ", "INSERT ", "DELETE ")

# 匹配 FROM <prefix>.<table>[ FINAL],prefix 可含 {db} 占位符
PATTERN = re.compile(
    r"FROM\s+\{?\w[\w\.]*?\}?\.(" + "|".join(TABLES) + r")\b(\s+FINAL)?",
    re.IGNORECASE,
)


def _collect_sql_literals(source: str) -> list[str]:
    """用 AST 抽取所有字符串字面量(SQL 模板)。"""
    tree = ast.parse(source)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            # f-string:把所有子段拼起来作为整体扫描
            out.append(
                "".join(
                    seg.value if isinstance(seg, ast.Constant) and isinstance(seg.value, str) else "{...}"
                    for seg in node.values
                )
            )
    return out


def test_all_select_statements_use_final():
    text = ONLINE_PY.read_text(encoding="utf-8")
    violations: list[str] = []

    for sql in _collect_sql_literals(text):
        upper = sql.upper()
        if any(kw in upper for kw in DDL_KEYWORDS):
            continue
        for m in PATTERN.finditer(sql):
            if m.group(2) is None:
                snippet = sql[max(0, m.start() - 20) : m.end() + 20].replace("\n", " ").strip()
                violations.append(f"table={m.group(1)} — '...{snippet}...'")

    assert not violations, "FINAL 关键字静态扫描违规:\n" + "\n".join(violations)
