"""MCP server exposing the Supply Chain Digital Twin, Order Wars and Support
Automation as read-only tools. Run over stdio: `ops-intel-mcp` (or `python -m ops_intel_mcp`)."""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Annotated, Any, Callable

from dotenv import find_dotenv, load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import order_wars_config, supply_chain_config, support_automation_config
from .order_wars import OrderWars, OrderWarsError
from .readonly import Neo4jReader, PostgresReader, ReadOnlyViolation
from .supply_chain import ENTITY_LABELS, SupplyChain, SupplyChainError
from .support_automation import OUTCOMES, SupportAutomation, SupportAutomationError

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

INSTRUCTIONS = """\
Read-only access to three portfolio systems.

Supply Chain Digital Twin (tools prefixed supply_chain_): a fictional automotive-parts
manufacturer. Relationships (suppliers, materials, parts, products, dealers, contracts)
live in Neo4j; inventory, contracts, shipments, invoices and sales live in Postgres.
Prefer the purpose-built tools; use supply_chain_describe_schema then
supply_chain_readonly_cypher / supply_chain_readonly_sql for anything else.
IDs look like S1 (supplier), RM1 (raw material), P1 (product), C1 (contract).

Order Wars (tools prefixed order_wars_): a multi-agent LLM strategy game. Each faction is an
AI agent with a role preset; every decision is logged with its rationale and scored by
DeepEval (Legal Action, Resource Efficiency, Role Alignment). Game ids can be given as
the first 8 characters.

Support Automation (tools prefixed support_automation_): a multi-tenant no-code support
automation platform. Each tenant has flows (LangGraph pipelines: retrieve -> draft ->
confidence_gate -> auto_reply / ask_human / handover ...); every execution is a run with
a node-by-node trace, and humans' responses to drafts are recorded (sent_as_is, edited,
rewrote). Case events are the Salesforce case timeline; review tasks are the Knowledge
Integrity Loop (KB contradiction checks awaiting manager review). Tenants can be given
by name or id; omit to cover every tenant visible to this server. Run ids can be given
as the first 8 characters. There is no ad-hoc SQL for this system by design.

Nothing here can modify data. Cite ids and numbers from tool results rather than guessing.
"""

# Shared argument types for the support_automation_ tools (module level so the SDK
# can resolve them from the string annotations).
Tenant = Annotated[str | None, Field(description="Tenant name or id; omit for every visible tenant")]
Days = Annotated[int, Field(ge=1, le=365, description="Look-back window in days")]

_USER_ERRORS = (SupplyChainError, OrderWarsError, SupportAutomationError, ReadOnlyViolation)


def _user_errors(fn: Callable) -> Callable:
    """Turn expected, user-fixable errors into a clean tool error message."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _USER_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def build_server(supply_chain: SupplyChain | None, order_wars: OrderWars | None,
                 support: SupportAutomation | None = None) -> MCPServer:
    server = MCPServer("ops-intel", instructions=INSTRUCTIONS, version="0.1.0")

    def tool(fn: Callable) -> Callable:
        return server.tool(annotations=READ_ONLY)(_user_errors(fn))

    if supply_chain is not None:
        sc = supply_chain

        @tool
        def supply_chain_search_entities(
            text: Annotated[str, Field(description="Name fragment or exact id, e.g. 'aluminum' or 'S2'")],
            entity_type: Annotated[str | None, Field(description=f"Optional label: {', '.join(ENTITY_LABELS)}")] = None,
        ) -> list[dict]:
            """Find suppliers, materials, parts, products, dealers, contracts etc. by name or id."""
            return sc.search_entities(text, entity_type)

        @tool
        def supply_chain_single_source_materials() -> list[dict]:
            """Raw materials with exactly one supplier on file (single points of failure),
            with the finished products each one feeds."""
            return sc.single_source_materials()

        @tool
        def supply_chain_supplier_disruption_impact(
            supplier: Annotated[str, Field(description="Supplier id (S2) or unambiguous name fragment")],
            delay_days: Annotated[int, Field(ge=0, le=365, description="Length of the delay in days")],
        ) -> dict[str, Any]:
            """What a supplier delay affects: each material it supplies (normal vs delayed lead
            time, on-hand stock vs reorder point, risk), the downstream products, alternative
            sources and designated backup suppliers. Deterministic — no LLM involved."""
            return sc.supplier_disruption_impact(supplier, delay_days)

        @tool
        def supply_chain_low_stock(
            limit: Annotated[int, Field(ge=1, le=200)] = 50,
        ) -> list[dict]:
            """Items at or below their reorder point, or within 2x of it, most urgent first."""
            return sc.low_stock(limit)

        @tool
        def supply_chain_expiring_contracts(
            within_days: Annotated[int, Field(ge=1, le=730, description="Look-ahead window in days")] = 90,
        ) -> dict[str, Any]:
            """Supplier contracts ending within the window, soonest first, with value, penalty
            clause, auto-renew flag and account manager."""
            return sc.expiring_contracts(within_days)

        @tool
        def supply_chain_describe_schema() -> dict[str, Any]:
            """Neo4j labels/relationship types with counts and every Postgres table's columns.
            Call this before writing a custom query."""
            return sc.describe_schema()

        @tool
        def supply_chain_readonly_cypher(
            query: Annotated[str, Field(description="A single read-only Cypher statement (MATCH ... RETURN ...)")],
            params: Annotated[dict[str, Any] | None, Field(description="Query parameters")] = None,
            max_rows: Annotated[int, Field(ge=1, le=500)] = 100,
        ) -> dict[str, Any]:
            """Run a read-only Cypher query against the supply chain graph. Writes are rejected
            (guarded, and executed inside a Neo4j read transaction)."""
            return sc.readonly_cypher(query, params, max_rows)

        @tool
        def supply_chain_readonly_sql(
            query: Annotated[str, Field(description="A single SELECT/WITH statement")],
            max_rows: Annotated[int, Field(ge=1, le=500)] = 100,
        ) -> dict[str, Any]:
            """Run a read-only SQL query against the supply chain Postgres database. Writes are
            rejected (guarded, and executed in a READ ONLY transaction that is rolled back)."""
            return sc.readonly_sql(query, max_rows)

    if order_wars is not None:
        ow = order_wars

        @tool
        def order_wars_list_games(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> list[dict]:
            """Recent Order Wars games: status, turn, scenario, winner, faction and event counts."""
            return ow.list_games(limit)

        @tool
        def order_wars_game_summary(
            game_id: Annotated[str, Field(description="Game UUID or its first 8+ characters")],
        ) -> dict[str, Any]:
            """One game's outcome: each faction's role and final state, non-neutral diplomacy
            (wars, alliances, truces), how often each action was taken, and DeepEval averages."""
            return ow.game_summary(game_id)

        @tool
        def order_wars_game_events(
            game_id: Annotated[str, Field(description="Game UUID or its first 8+ characters")],
            faction: Annotated[str | None, Field(description="Only this faction's decisions")] = None,
            event_type: Annotated[str | None, Field(description="e.g. move_army, declare_war, negotiate")] = None,
            limit: Annotated[int, Field(ge=1, le=500)] = 100,
        ) -> dict[str, Any]:
            """Turn-by-turn decisions with each agent's own stated rationale, the rules
            engine's resolution, and that decision's DeepEval scores (null if the game
            hasn't been evaluated) plus the LLM judge's Role Alignment reasoning."""
            return ow.game_events(game_id, faction, event_type, limit)

        @tool
        def order_wars_role_preset_scores() -> list[dict]:
            """Average DeepEval score per role preset (expansionist, warmonger, diplomat_trader,
            isolationist) and metric, across every evaluated game."""
            return ow.role_preset_scores()

    if support is not None:
        sa = support

        @tool
        def support_automation_list_tenants() -> list[dict]:
            """Tenants with their plan, billing status, case connector, flow count, runs in the
            last 30 days and when they last ran."""
            return sa.list_tenants()

        @tool
        def support_automation_list_flows(tenant: Tenant = None) -> list[dict]:
            """Flows per tenant: status (draft/published), current vs published version, run
            count, last run, and their webhook/schedule triggers."""
            return sa.list_flows(tenant)

        @tool
        def support_automation_outcome_stats(tenant: Tenant = None, days: Days = 30) -> dict[str, Any]:
            """Per flow over the window: how runs ended (auto_reply, ask_human, handover, ...),
            what humans did with the drafts (sent_as_is, edited, rewrote, ...), the share of
            reviewed drafts sent unchanged, average gate confidence and edit distance, tokens."""
            return sa.outcome_stats(tenant, days)

        @tool
        def support_automation_recent_runs(
            tenant: Tenant = None,
            outcome: Annotated[str | None, Field(description=f"One of: {', '.join(OUTCOMES)}")] = None,
            flow: Annotated[str | None, Field(description="Flow name fragment or flow id")] = None,
            human_action: Annotated[str | None, Field(description="e.g. sent_as_is, edited, rewrote, pending")] = None,
            days: Days = 30,
            limit: Annotated[int, Field(ge=1, le=500)] = 50,
        ) -> dict[str, Any]:
            """Recent runs, newest first: subject, case, outcome, confidence, whether the gate
            passed, what the human did with the draft, edit distance, tokens."""
            return sa.recent_runs(tenant, outcome, flow, human_action, days, limit)

        @tool
        def support_automation_run_detail(
            run_id: Annotated[str, Field(description="Run UUID or its first 8+ characters")],
        ) -> dict[str, Any]:
            """Everything about one run: the confidence-gate breakdown, each node step in order
            (type, summary, latency, model), the KB passages retrieved, the AI draft, what the
            human sent instead and the correction analysis, and tokens by model and node."""
            return sa.run_detail(run_id)

        @tool
        def support_automation_case_timeline(
            case: Annotated[str, Field(description="Salesforce case number or Case id")],
            tenant: Tenant = None,
        ) -> dict[str, Any]:
            """A support case's history: routing, gate decisions, human pings, reassignments,
            sends and SLA breaches in order, plus every run the pipeline made for it."""
            return sa.case_timeline(case, tenant)

        @tool
        def support_automation_knowledge_reviews(
            tenant: Tenant = None,
            status: Annotated[str | None, Field(description="e.g. open, correct, approved, rejected")] = None,
            limit: Annotated[int, Field(ge=1, le=500)] = 50,
        ) -> dict[str, Any]:
            """Knowledge Integrity Loop review tasks: the statement checked against the KB, the
            contradiction verdict, reviewer decision and note, and whether an approved KB
            rewrite was written back."""
            return sa.knowledge_reviews(tenant, status, limit)

        @tool
        def support_automation_token_usage(tenant: Tenant = None, days: Days = 30) -> dict[str, Any]:
            """LLM usage over the window: runs and tokens per tenant against the plan's included
            runs/tokens, tokens per model, and a daily series."""
            return sa.token_usage(tenant, days)

        @tool
        def support_automation_system_health() -> list[dict]:
            """Heartbeats of the platform's background components (poller, worker, slackbot,
            CDC listener, Neo4j sync): last healthy time and minutes since."""
            return sa.system_health()

    return server


def _load_env() -> None:
    """OPS_INTEL_ENV_FILE if set, else a .env in the working directory, else the
    repo's own .env. Real environment variables always win over the file."""
    explicit = os.getenv("OPS_INTEL_ENV_FILE")
    candidates = [explicit] if explicit else [find_dotenv(usecwd=True), str(Path(__file__).resolve().parent.parent / ".env")]
    for path in candidates:
        if path and Path(path).is_file():
            load_dotenv(path, override=False)
            return


def build_from_env() -> MCPServer:
    _load_env()
    sc_cfg, ow_cfg, sa_cfg = supply_chain_config(), order_wars_config(), support_automation_config()
    sc = ow = sa = None
    if sc_cfg:
        sc = SupplyChain(
            Neo4jReader(sc_cfg.neo4j_uri, sc_cfg.neo4j_user, sc_cfg.neo4j_password),
            PostgresReader(sc_cfg.postgres_url),
            sc_cfg.reference_date,
        )
    if ow_cfg:
        ow = OrderWars(PostgresReader(ow_cfg.database_url), ow_cfg.schema)
    if sa_cfg:
        sa = SupportAutomation(PostgresReader(sa_cfg.database_url), sa_cfg.tenant_id)
    if sc is None and ow is None and sa is None:
        raise SystemExit(
            "ops-intel-mcp: nothing configured. Set NEO4J_URI/NEO4J_PASSWORD/SUPPLY_CHAIN_POSTGRES_URL, "
            "ORDER_WARS_DATABASE_URL and/or SUPPORT_AUTOMATION_DATABASE_URL (see .env.example)."
        )
    return build_server(sc, ow, sa)


def main() -> None:
    build_from_env().run("stdio")
