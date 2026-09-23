"""The server end to end through the real MCP Client (in-process transport),
with fake database readers — no network, no credentials."""

import datetime as dt

import pytest
from mcp import Client

from ops_intel_mcp.order_wars import OrderWars
from ops_intel_mcp.server import build_server
from ops_intel_mcp.supply_chain import SupplyChain


class FakeReader:
    """Answers a query with the rows of the first registered substring it contains."""

    def __init__(self, routes=None):
        self.routes = list(routes or [])
        self.calls = []

    def query(self, q, params=None, max_rows=200):
        self.calls.append((q, params))
        for needle, rows in self.routes:
            if needle in q:
                rows = [dict(r) for r in rows]
                return rows[:max_rows], len(rows) > max_rows
        return [], False


def _sc(graph_routes=(), sql_routes=()):
    return SupplyChain(FakeReader(graph_routes), FakeReader(sql_routes), dt.date(2026, 9, 21))


SC_TOOLS = {
    "supply_chain_search_entities", "supply_chain_single_source_materials",
    "supply_chain_supplier_disruption_impact", "supply_chain_low_stock",
    "supply_chain_expiring_contracts", "supply_chain_describe_schema",
    "supply_chain_readonly_cypher", "supply_chain_readonly_sql",
}
OW_TOOLS = {
    "order_wars_list_games", "order_wars_game_summary",
    "order_wars_game_events", "order_wars_role_preset_scores",
}


async def test_lists_all_tools_and_marks_them_read_only():
    async with Client(build_server(_sc(), OrderWars(FakeReader()))) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == SC_TOOLS | OW_TOOLS
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)
    assert all(t.description for t in tools)


async def test_only_configured_backends_get_tools():
    async with Client(build_server(None, OrderWars(FakeReader()))) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == OW_TOOLS


async def test_disruption_impact_combines_graph_and_inventory():
    sc = _sc(
        graph_routes=[
            ("RETURN labels(n)[0] AS type", [{"type": "Supplier", "id": "S2", "name": "Titan Aluminum Works"}]),
            ("BACKUP_FOR", [{"id": "S6", "name": "Diversified Metals"}]),
            ("SOURCED_FROM]->(s:Supplier {id: $id})", [
                {"material_id": "RM3", "material_name": "Aluminum Alloy Billet", "normal_lead_time_days": 25,
                 "is_primary_source": True,
                 "products": [{"id": "P1", "name": "Standard Brake"}, {"id": None, "name": None}],
                 "alternative_sources": [{"id": "S6", "name": "Diversified Metals"}]},
                {"material_id": "RM4", "material_name": "Aluminum Sheet", "normal_lead_time_days": 20,
                 "is_primary_source": True, "products": [{"id": "P3", "name": "Sedan Suspension"}],
                 "alternative_sources": [{"id": None, "name": None}]},
            ]),
        ],
        sql_routes=[("inventory_levels", [
            {"item_id": "RM3", "on_hand": 217.75, "reorder_point": 451.32},
            {"item_id": "RM4", "on_hand": 882.24, "reorder_point": 678.57},
        ])],
    )
    async with Client(build_server(sc, None)) as client:
        res = await client.call_tool("supply_chain_supplier_disruption_impact", {"supplier": "titan", "delay_days": 21})
    assert not res.is_error
    out = res.structured_content
    by_id = {m["material_id"]: m for m in out["materials"]}
    assert by_id["RM3"]["effective_lead_time_days"] == 46
    assert by_id["RM3"]["risk"] == "high"  # 217.75 <= 451.32
    assert by_id["RM4"]["risk"] == "medium"  # above reorder point, within 2x
    assert by_id["RM4"]["alternative_sources"] == []  # null OPTIONAL MATCH rows dropped
    assert [p["id"] for p in out["affected_products"]] == ["P1", "P3"]
    assert out["backup_suppliers"] == [{"id": "S6", "name": "Diversified Metals"}]


async def test_ambiguous_supplier_is_a_clean_tool_error():
    sc = _sc(graph_routes=[("RETURN labels(n)[0] AS type", [
        {"type": "Supplier", "id": "S1", "name": "Great Lakes Steel"},
        {"type": "Supplier", "id": "S4", "name": "Lakes Alloy"},
    ])])
    async with Client(build_server(sc, None)) as client:
        res = await client.call_tool("supply_chain_supplier_disruption_impact", {"supplier": "lakes", "delay_days": 7})
    assert res.is_error
    assert "several suppliers" in res.content[0].text and "S1" in res.content[0].text


async def test_write_query_is_rejected_before_reaching_the_database():
    sc = _sc()
    async with Client(build_server(sc, None)) as client:
        res = await client.call_tool("supply_chain_readonly_sql", {"query": "DELETE FROM contracts"})
        res2 = await client.call_tool("supply_chain_readonly_cypher", {"query": "MATCH (n) DETACH DELETE n"})
    assert res.is_error and "must start with" in res.content[0].text
    assert res2.is_error and "write/admin keyword" in res2.content[0].text
    assert sc.sql.calls == [] and sc.graph.calls == []


async def test_readonly_sql_reports_truncation():
    sc = _sc(sql_routes=[("FROM contracts", [{"contract_id": f"C{i}"} for i in range(5)])])
    async with Client(build_server(sc, None)) as client:
        res = await client.call_tool("supply_chain_readonly_sql", {"query": "SELECT * FROM contracts", "max_rows": 3})
    assert res.structured_content == {
        "rows": [{"contract_id": "C0"}, {"contract_id": "C1"}, {"contract_id": "C2"}],
        "row_count": 3, "truncated": True,
    }


async def test_argument_validation_happens_before_the_tool_runs():
    sc = _sc()
    async with Client(build_server(sc, None)) as client:
        res = await client.call_tool("supply_chain_supplier_disruption_impact", {"supplier": "S2", "delay_days": -3})
    assert res.is_error
    assert sc.graph.calls == []


@pytest.mark.parametrize("game_id,rows,expected", [
    ("zzzz", [], "not a game id"),
    ("568d753f", [], "No game"),
    ("568d", [{"id": "568d0000-0000-0000-0000-000000000001"}, {"id": "568d0000-0000-0000-0000-000000000002"}],
     "several games"),
])
async def test_game_id_resolution_errors(game_id, rows, expected):
    ow = OrderWars(FakeReader([("LIKE %(p)s", rows)]))
    async with Client(build_server(None, ow)) as client:
        res = await client.call_tool("order_wars_game_summary", {"game_id": game_id})
    assert res.is_error and expected in res.content[0].text


def test_order_wars_rejects_an_unsafe_schema_name():
    with pytest.raises(ValueError):
        OrderWars(FakeReader(), schema="order_wars; drop table games")
