"""``check_schema_drift`` 的表级差集。

钉两条会**安静地**变错的性质：

1. ``alembic_version`` 不算遗留表。它由 alembic 自己建、没有模型， diff 出零表名的
   任何写法都会让它出现在"上一代功能留下的表"里，于是运维照着名单去 drop，第一张
   就被删掉的是迁移记录本身。
2. 模型那一侧真的来自 ``models``。``_table_gap`` 靠 ``import models`` 的副作用把
   ``__tablename__`` 注册进 ``Base.metadata``；那行 import 被删掉后 metadata 是空的，
   函数不会报错——它会把**库里的每一张表**都报成遗留表，并且再也不报 missing。
   一个每次都给出"全部该删"答案的检查器，比没有检查器更糟。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest
import sqlalchemy

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _load_script():
    path = os.path.join(_BACKEND, "scripts", "check_schema_drift.py")
    spec = importlib.util.spec_from_file_location("check_schema_drift", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def table_gap(monkeypatch):
    """返回 ``gap_for(db_tables)``，inspect() 被换成报出给定表名的桩。"""
    script = _load_script()

    def gap_for(db_tables: list[str]):
        monkeypatch.setattr(
            sqlalchemy,
            "inspect",
            lambda _engine: SimpleNamespace(get_table_names=lambda: set(db_tables)),
        )
        return script._table_gap(SimpleNamespace())

    return gap_for


def test_库里的表与模型的表各多出来什么(table_gap):
    from database import Base
    import models  # noqa: F401

    modeled = set(Base.metadata.tables)

    gap = table_gap([*modeled, "alembic_version", "chats"])
    assert gap["unmanaged_tables"] == ["chats"]
    assert gap["missing_tables"] == []
    # 反方向：代码里有、库里没有，就是忘了 upgrade 的那一半
    assert table_gap(["users"])["missing_tables"] == sorted(modeled - {"users"})


def test_迁移记录表不算遗留表(table_gap):
    gap = table_gap(["alembic_version", "tickets"])
    assert "alembic_version" not in gap["unmanaged_tables"]


def test_模型清单非空且真的注册了工单表(table_gap):
    """``_table_gap`` 依赖 ``import models`` 的副作用；这条把它失守时的症状抓住。

    少了那行 import，metadata 会是空的，于是差集把每张表都判成遗留表。这里断言的不是
    差集而是它的输入：模型侧必须**有**东西，而且是当前这一代的表。
    """
    from database import Base
    import models  # noqa: F401

    tables = set(Base.metadata.tables)
    assert {"tickets", "cs_operations", "ticket_outbox"} <= tables
    # 这条才是上面那个失效模式的直接守卫
    assert table_gap(["tickets"])["unmanaged_tables"] == []
