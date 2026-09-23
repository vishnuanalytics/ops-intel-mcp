"""Against the real databases (needs .env). Run with: pytest -m live

Checks the tools against ground truth from the Supply Chain twin's own eval set
(eval/test_questions.json), and that the *databases* reject writes even when
the client-side guard is bypassed."""

import os
import sys

import pytest
from dotenv import load_dotenv
from mcp import Client, StdioServerParameters

load_dotenv()

from ops_intel_mcp.config import order_wars_config, supply_chain_config  # noqa: E402
from ops_intel_mcp.order_wars import OrderWars  # noqa: E402
from ops_intel_mcp.readonly import Neo4jReader, PostgresReader  # noqa: E402
from ops_intel_mcp.server import build_server  # noqa: E402
from ops_intel_mcp.supply_chain import SupplyChain  # noqa: E402

pytestmark = pytest.mark.live
SC_CFG, OW_CFG = supply_chain_config(), order_wars_config()
needs_sc = pytest.mark.skipif(SC_CFG is None, reason="supply chain not configured")
needs_ow = pytest.mark.skipif(OW_CFG is None, reason="order wars not configured")


@pytest.fixture(scope="module")
def sc():
    graph = Neo4jReader(SC_CFG.neo4j_uri, SC_CFG.neo4j_user, SC_CFG.neo4j_password)
    sql = PostgresReader(SC_CFG.postgres_url)
    yield SupplyChain(graph, sql, SC_CFG.reference_date)
    graph.close()
    sql.close()


@pytest.fixture(scope="module")
def ow():
    sql = PostgresReader(OW_CFG.database_url)
    yield OrderWars(sql, OW_CFG.schema)
    sql.close()


@needs_sc
def test_single_source_materials_match_the_twins_ground_truth(sc):
    # Same checks as the twin's eval (scenario_02): must include / must exclude.
    # (The graph has grown since — RM11/RM12 were added later, also single-source.)
    ids = {r["material_id"] for r in sc.single_source_materials()}
    assert {"RM2", "RM4", "RM5", "RM6", "RM7", "RM8", "RM10"} <= ids
    assert not ids & {"RM1", "RM3", "RM9"}


@needs_sc
def test_titan_aluminum_disruption_matches_ground_truth(sc):
    out = sc.supplier_disruption_impact("Titan Aluminum", 21)
    assert out["supplier"]["id"] == "S2"
    assert {m["material_id"] for m in out["materials"]} == {"RM3", "RM4"}
    assert {"P1", "P2", "P3", "P4", "P5"} <= {p["id"] for p in out["affected_products"]}
    assert all(m["effective_lead_time_days"] == m["normal_lead_time_days"] + 21 for m in out["materials"])


@needs_sc
def test_expiring_contracts_are_sorted_and_within_window(sc):
    out = sc.expiring_contracts(90)
    days = [c["days_until_expiry"] for c in out["contracts"]]
    assert days == sorted(days) and all(0 <= d <= 90 for d in days)


@needs_sc
def test_postgres_rejects_a_write_even_without_the_guard(sc):
    import psycopg

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        sc.sql.query("CREATE TABLE mcp_write_probe (x int)")  # bypasses assert_read_only_sql
    rows, _ = sc.sql.query("SELECT to_regclass('public.mcp_write_probe') AS t")
    assert rows == [{"t": None}]


@needs_sc
def test_neo4j_rejects_a_write_even_without_the_guard(sc):
    from neo4j.exceptions import ClientError

    with pytest.raises(ClientError):
        sc.graph.query("CREATE (n:McpWriteProbe) RETURN n")  # bypasses assert_read_only_cypher
    rows, _ = sc.graph.query("MATCH (n:McpWriteProbe) RETURN count(n) AS c")
    assert rows == [{"c": 0}]


@needs_ow
def test_order_wars_readme_demo_game(ow):
    games = {g["game_id"][:8]: g for g in ow.list_games(50)}
    assert "568d753f" in games
    s = ow.game_summary("568d753f")
    assert {f["faction_name"] for f in s["factions"]} == {"Rome", "Carthage", "Gaul", "Iberia"}
    assert any(
        {d["faction_a"], d["faction_b"]} == {"Carthage", "Rome"} and d["status"] == "alliance"
        for d in s["diplomacy"]
    )
    assert all(d["status"] != "neutral" for d in s["diplomacy"])
    events = ow.game_events("568d753f", faction="gaul", event_type="declare_war")["events"]
    assert events and all(e["faction"] == "Gaul" and e["rationale"] for e in events)
    # the demo game was evaluated, so every decision carries its own scores
    assert all(set(e["scores"]) == {"Legal Action", "Resource Efficiency", "Role Alignment"} for e in events)


@needs_ow
def test_role_preset_scores_cover_the_four_presets(ow):
    presets = {r["role_preset"] for r in ow.role_preset_scores()}
    assert {"expansionist", "warmonger", "diplomat_trader", "isolationist"} <= presets


@pytest.mark.skipif(SC_CFG is None or OW_CFG is None, reason="needs both backends")
async def test_stdio_end_to_end():
    """The real entry point as a subprocess over stdio — what Claude Code runs."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "ops_intel_mcp"], env={**os.environ},
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert len(names) == 12
        res = await client.call_tool("supply_chain_supplier_disruption_impact", {"supplier": "S2", "delay_days": 21})
        assert not res.is_error and res.structured_content["supplier"]["id"] == "S2"
        res = await client.call_tool("supply_chain_readonly_sql", {"query": "DROP TABLE contracts"})
        assert res.is_error


@pytest.mark.skipif(SC_CFG is None or OW_CFG is None, reason="needs both backends")
async def test_in_process_server_with_real_backends(sc, ow):
    async with Client(build_server(sc, ow)) as client:
        res = await client.call_tool("order_wars_game_summary", {"game_id": "568d753f"})
        assert not res.is_error and len(res.structured_content["factions"]) == 4
