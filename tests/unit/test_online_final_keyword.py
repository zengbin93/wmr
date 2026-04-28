"""验收点 A5(静态扫描):``OnlineManager`` 所有 SELECT 必须含 ``FINAL`` 关键字。

通过 AST 提取 ``wmr/online.py`` 中的字符串字面量(SQL 模板),并区分:

- ``CREATE TABLE`` / ``CREATE DATABASE`` / ``DROP`` / ``ALTER ... UPDATE`` /
  ``ALTER ... DELETE`` / ``INSERT INTO ... VALUES`` / 顶级 ``DELETE FROM``:
  这些 DDL/DML 不要求 FINAL,整段跳过。
- ``CREATE VIEW <x> AS <select>``:仅扫描 ``AS`` 之后的 SELECT 主体,因为视图
  内部对底表的引用同样必须含 FINAL,否则外层 ``SELECT * FROM view FINAL``
  无法把 FINAL 传播到子查询(子查询会读到未合并的 part 形成笛卡尔积)。
- 其余字面量:整段扫描每个 ``FROM <db>.<table>`` 是否紧随 FINAL。

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

# 仅这些关键字代表"整段不需要 FINAL"。CREATE VIEW 不在此列,需要单独提取 AS 之后的 SELECT。
SKIP_KEYWORDS = (
    "CREATE TABLE",
    "CREATE DATABASE",
    "DROP ",
    "ALTER TABLE",
    "INSERT INTO",
)

# 匹配 FROM <prefix>.<table>[ AS alias][ FINAL],prefix 可含 {db} 占位符。
# 允许 alias 出现在 FINAL 前(ClickHouse 标准写法 `FROM t AS a FINAL`)。
PATTERN = re.compile(
    r"FROM\s+\{?\w[\w\.]*?\}?\.(" + "|".join(TABLES) + r")\b(?:\s+AS\s+\w+)?(\s+FINAL)?",
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


def _extract_scannable_parts(sql: str) -> list[str]:
    """从一段 SQL 字面量里提取需要扫描 FINAL 的子串。

    - CREATE TABLE / CREATE DATABASE / DROP / ALTER / INSERT 整段跳过(返回空列表)
    - CREATE VIEW <x> AS <select>:只返回 AS 之后的 SELECT 主体
    - 其他:返回整个 sql
    """
    upper = sql.upper()
    if any(kw in upper for kw in SKIP_KEYWORDS):
        return []
    if "CREATE VIEW" in upper:
        # 找到 "AS" 后面的 SELECT 主体(忽略大小写)
        m = re.search(r"\bAS\b", sql, re.IGNORECASE)
        if m:
            return [sql[m.end() :]]
        return []
    return [sql]


def test_all_select_statements_use_final():
    text = ONLINE_PY.read_text(encoding="utf-8")
    violations: list[str] = []

    for sql in _collect_sql_literals(text):
        for body in _extract_scannable_parts(sql):
            for m in PATTERN.finditer(body):
                if m.group(1) is None:
                    continue  # 防御:正则未捕获到表名时跳过
                if m.group(2) is None:
                    snippet = body[max(0, m.start() - 20) : m.end() + 20].replace("\n", " ").strip()
                    violations.append(f"table={m.group(1)} — '...{snippet}...'")

    assert not violations, "FINAL 关键字静态扫描违规:\n" + "\n".join(violations)


def test_views_inner_select_has_final():
    """直接断言三个视图的 CREATE VIEW 主体内部对 weights/metas 的引用含 FINAL。"""
    text = ONLINE_PY.read_text(encoding="utf-8")

    # 提取 CREATE VIEW 子句(粗粒度匹配:遇到 ENGINE 或下一个 CREATE 截止)
    view_blocks = re.findall(
        r"CREATE VIEW IF NOT EXISTS\s+\{db\}\.(\w+)\s+AS(.+?)(?=CREATE\s|self\._logger|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    assert view_blocks, "未在 online.py 中找到任何 CREATE VIEW 块"

    inner_views = {"cs_latest_weights", "ts_latest_weights"}
    for name, body in view_blocks:
        if name not in inner_views:
            continue
        # 视图主体里至少有一个 FROM weights ... FINAL 与一个 JOIN metas ... FINAL
        assert re.search(r"FROM\s+\{db\}\.weights[^\n]*FINAL", body, re.IGNORECASE), (
            f"视图 {name} 缺少 FROM weights FINAL"
        )
        assert re.search(r"JOIN\s+\{db\}\.metas[^\n]*FINAL", body, re.IGNORECASE), f"视图 {name} 缺少 JOIN metas FINAL"
