import datetime as dt
import uuid
from decimal import Decimal

import pytest

from ops_intel_mcp.readonly import (
    ReadOnlyViolation,
    assert_read_only_cypher,
    assert_read_only_sql,
    to_jsonable,
)


@pytest.mark.parametrize("q", [
    "SELECT * FROM contracts",
    "select item_id, last_updated from inventory_levels;",  # trailing ; ok, 'updated' inside a name ok
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT 'please delete me' AS note",  # keyword inside a string literal
    "SELECT 1 -- drop table x",  # keyword inside a comment
    "SELECT created_at FROM shipments",
])
def test_sql_guard_allows_reads(q):
    assert_read_only_sql(q)


@pytest.mark.parametrize("q,msg", [
    ("DELETE FROM contracts", "must start with"),
    ("SELECT 1; DROP TABLE contracts", "single"),
    ("WITH d AS (DELETE FROM contracts RETURNING *) SELECT * FROM d", "DELETE"),
    ("SELECT set_config('x', 'y', false)", None),  # function name isn't the SET keyword -> allowed by guard
    ("", "Empty"),
])
def test_sql_guard_rejects_writes(q, msg):
    if msg is None:
        assert_read_only_sql(q)  # the READ ONLY transaction is the real boundary for this one
        return
    with pytest.raises(ReadOnlyViolation, match=msg):
        assert_read_only_sql(q)


@pytest.mark.parametrize("q", [
    "MATCH (s:Supplier) RETURN s.name",
    "OPTIONAL MATCH (n) RETURN count(n)",
    "MATCH (n) WHERE n.name = 'Create Co' RETURN n",
])
def test_cypher_guard_allows_reads(q):
    assert_read_only_cypher(q)


@pytest.mark.parametrize("q", [
    "CREATE (n:X) RETURN n",
    "MATCH (n) SET n.x = 1 RETURN n",
    "MATCH (n) DETACH DELETE n",
    "MATCH (n) CALL { WITH n MERGE (m:Y) } RETURN n",
    "MATCH (n) RETURN n; MATCH (m) DELETE m",
])
def test_cypher_guard_rejects_writes(q):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only_cypher(q)


def test_to_jsonable_handles_driver_types():
    u = uuid.uuid4()
    out = to_jsonable({"d": Decimal("1.50"), "day": dt.date(2026, 9, 21), "id": u, "xs": (1, Decimal("2"))})
    assert out == {"d": 1.5, "day": "2026-09-21", "id": str(u), "xs": [1, 2.0]}
