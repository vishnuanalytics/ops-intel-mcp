"""The server end to end through the real MCP Client (in-process transport),
with fake database readers — no network, no credentials."""

import datetime as dt

import pytest
from mcp import Client

from ops_intel_mcp.config import support_automation_config
from ops_intel_mcp.order_wars import OrderWars
from ops_intel_mcp.server import build_server
from ops_intel_mcp.supply_chain import SupplyChain
from ops_intel_mcp.support_automation import SupportAutomation


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


# -- Support Automation ---------------------------------------------------------

ACME = "00000000-0000-0000-0000-000000000000"
GUNNER = "11111111-1111-4111-8111-111111111111"
SA_TOOLS = {
    "support_automation_list_tenants", "support_automation_list_flows", "support_automation_outcome_stats",
    "support_automation_recent_runs", "support_automation_run_detail", "support_automation_case_timeline",
    "support_automation_knowledge_reviews", "support_automation_token_usage", "support_automation_system_health",
}


def _sa(routes=(), pinned=None):
    return SupportAutomation(FakeReader(routes), pinned)


async def test_support_automation_tools_are_listed_without_a_sql_escape_hatch():
    async with Client(build_server(None, None, _sa())) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == SA_TOOLS
    assert all(t.annotations.read_only_hint for t in tools)


async def test_support_automation_never_queries_credential_columns():
    sa = _sa([("FROM tenants", [{"tenant_id": ACME, "name": "Acme (demo)"}]),
              ("run_id::text LIKE", [{"run_id": "ee2723ea-ef08-4cd9-a63e-29dcc8c7fc2e"}]),
              ("WHERE r.run_id =", [{"run_id": "ee2723ea-ef08-4cd9-a63e-29dcc8c7fc2e"}]),
              ("FROM case_events", [{"case_sf_id": "500X", "case_number": "00001268"}])])
    async with Client(build_server(None, None, sa)) as client:
        for name, args in [
            ("support_automation_list_tenants", {}), ("support_automation_list_flows", {"tenant": "acme"}),
            ("support_automation_outcome_stats", {}), ("support_automation_recent_runs", {}),
            ("support_automation_run_detail", {"run_id": "ee2723ea"}),
            ("support_automation_case_timeline", {"case": "00001268"}),
            ("support_automation_knowledge_reviews", {}), ("support_automation_token_usage", {}),
            ("support_automation_system_health", {}),
        ]:
            res = await client.call_tool(name, args)
            assert not res.is_error, (name, res.content)
    sql = "\n".join(q for q, _ in sa.sql.calls).lower()
    for forbidden in ("tenant_integrations", "from connections", ".auth", "secret", "tr.token", "vault"):
        assert forbidden not in sql


async def test_pinned_server_refuses_other_tenants_and_scopes_the_rest():
    sa = _sa([("FROM tenants", [{"tenant_id": GUNNER, "name": "gunner"}])], pinned=ACME)
    async with Client(build_server(None, None, sa)) as client:
        res = await client.call_tool("support_automation_recent_runs", {"tenant": "gunner"})
        assert res.is_error and "pinned" in res.content[0].text
        res = await client.call_tool("support_automation_recent_runs", {})
        assert not res.is_error
    # with no tenant given, the pinned id is what reaches the query
    assert sa.sql.calls[-1][1]["t"] == ACME


async def test_ambiguous_tenant_is_a_clean_error():
    sa = _sa([("FROM tenants", [{"tenant_id": ACME, "name": "Acme (demo)"},
                                {"tenant_id": GUNNER, "name": "Acme staging"}])])
    async with Client(build_server(None, None, sa)) as client:
        res = await client.call_tool("support_automation_list_flows", {"tenant": "acme"})
    assert res.is_error and "several tenants" in res.content[0].text


async def test_outcome_stats_rolls_up_cells_per_flow():
    cells = [
        {"tenant": "Acme", "flow": "Email", "flow_id": "f1", "outcome": "auto_reply", "human_action": None, "n": 6,
         "conf_sum": 4.8, "conf_n": 6, "edit_sum": None, "edit_n": 0, "tokens": 600},
        {"tenant": "Acme", "flow": "Email", "flow_id": "f1", "outcome": "ask_human", "human_action": "sent_as_is",
         "n": 3, "conf_sum": 1.2, "conf_n": 3, "edit_sum": 0.0, "edit_n": 3, "tokens": 300},
        {"tenant": "Acme", "flow": "Email", "flow_id": "f1", "outcome": "ask_human", "human_action": "edited",
         "n": 1, "conf_sum": 0.4, "conf_n": 1, "edit_sum": 0.4, "edit_n": 1, "tokens": 100},
        {"tenant": "Acme", "flow": "Chat", "flow_id": "f2", "outcome": "handover", "human_action": None, "n": 2,
         "conf_sum": None, "conf_n": 0, "edit_sum": None, "edit_n": 0, "tokens": 0},
    ]
    async with Client(build_server(None, None, _sa([("GROUP BY 1, 2, 3, 4, 5", cells)]))) as client:
        out = (await client.call_tool("support_automation_outcome_stats", {"days": 7})).structured_content
    email, chat = out["flows"]
    assert out["window_days"] == 7
    assert email["runs"] == 10 and email["outcomes"] == {"auto_reply": 6, "ask_human": 4}
    assert email["human_actions"] == {"sent_as_is": 3, "edited": 1}
    assert email["sent_as_is_rate"] == 0.75 and email["avg_confidence"] == 0.64
    assert email["avg_edit_distance"] == 0.1 and email["tokens_total"] == 1000
    assert chat["avg_confidence"] is None and chat["sent_as_is_rate"] is None


@pytest.mark.parametrize("args,expected", [
    ({"run_id": "not-a-run!"}, "not a run id"),
    ({"run_id": "deadbeef"}, "No run"),
])
async def test_run_id_resolution_errors(args, expected):
    async with Client(build_server(None, None, _sa())) as client:
        res = await client.call_tool("support_automation_run_detail", args)
    assert res.is_error and expected in res.content[0].text


async def test_unknown_outcome_is_rejected_before_any_query():
    sa = _sa()
    async with Client(build_server(None, None, sa)) as client:
        res = await client.call_tool("support_automation_recent_runs", {"outcome": "deleted"})
    assert res.is_error and "auto_reply" in res.content[0].text
    assert sa.sql.calls == []


def test_tenant_pin_must_be_a_full_uuid(monkeypatch):
    monkeypatch.setenv("SUPPORT_AUTOMATION_DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("SUPPORT_AUTOMATION_TENANT_ID", "acme")
    with pytest.raises(ValueError):
        support_automation_config()
    monkeypatch.setenv("SUPPORT_AUTOMATION_TENANT_ID", ACME)
    cfg = support_automation_config()
    assert cfg.database_url == "postgresql://u:p@h/db" and cfg.tenant_id == ACME
