"""Supply Chain Digital Twin tools: Neo4j (relationships) + Postgres (transactions).

Graph shape (see the twin's agent/schema_context.py):
  (RawMaterial)-[:SOURCED_FROM {lead_time_days, cost_per_unit, is_primary}]->(Supplier)
  (backup:Supplier)-[:BACKUP_FOR]->(primary:Supplier)
  (RawMaterial)-[:USED_IN]->(IntermediatePart)-[:USED_IN]->(Product)
  (Supplier)-[:HAS_CONTRACT]->(Contract)-[:COVERS]->(RawMaterial)
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Protocol

from .readonly import assert_read_only_cypher, assert_read_only_sql

ENTITY_LABELS = (
    "Supplier", "RawMaterial", "IntermediatePart", "Product", "ThirdPartyVendor",
    "Facility", "Warehouse", "Dealer", "Region", "Contract",
)


class Reader(Protocol):
    def query(self, q: str, params: Any = None, max_rows: int = 200) -> tuple[list[dict], bool]: ...


class SupplyChainError(ValueError):
    """A user-facing problem with the request (unknown id, ambiguous name, ...)."""


class SupplyChain:
    def __init__(self, graph: Reader, sql: Reader, reference_date: dt.date):
        self.graph = graph
        self.sql = sql
        self.reference_date = reference_date

    # -- lookups --------------------------------------------------------------

    def search_entities(self, text: str, entity_type: str | None = None, limit: int = 20) -> list[dict]:
        if entity_type and entity_type not in ENTITY_LABELS:
            raise SupplyChainError(f"Unknown entity_type {entity_type!r}. Use one of: {', '.join(ENTITY_LABELS)}.")
        rows, _ = self.graph.query(
            """
            MATCH (n)
            WHERE any(l IN labels(n) WHERE l IN $labels)
              AND (toLower(coalesce(n.name, '')) CONTAINS toLower($text)
                   OR toLower(coalesce(n.id, n.contract_id, '')) = toLower($text))
            RETURN labels(n)[0] AS type, coalesce(n.id, n.contract_id) AS id, n.name AS name
            ORDER BY type, id
            LIMIT $limit
            """,
            {"labels": [entity_type] if entity_type else list(ENTITY_LABELS), "text": text.strip(), "limit": limit},
        )
        return rows

    def _resolve_supplier(self, supplier: str) -> dict:
        matches = self.search_entities(supplier, "Supplier")
        exact = [m for m in matches if m["id"].lower() == supplier.strip().lower()]
        if exact:
            return exact[0]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise SupplyChainError(f"No supplier matches {supplier!r}. Try search_entities first.")
        options = ", ".join(f"{m['id']} ({m['name']})" for m in matches)
        raise SupplyChainError(f"{supplier!r} matches several suppliers: {options}. Pass the id.")

    # -- analyses -------------------------------------------------------------

    def single_source_materials(self) -> list[dict]:
        rows, _ = self.graph.query(
            """
            MATCH (rm:RawMaterial)-[r:SOURCED_FROM]->(s:Supplier)
            WITH rm, collect({id: s.id, name: s.name}) AS suppliers
            WHERE size(suppliers) = 1
            OPTIONAL MATCH (rm)-[:USED_IN]->(:IntermediatePart)-[:USED_IN]->(p:Product)
            RETURN rm.id AS material_id, rm.name AS material_name,
                   suppliers[0].id AS supplier_id, suppliers[0].name AS supplier_name,
                   collect(DISTINCT p.id) AS downstream_products
            ORDER BY toInteger(substring(rm.id, 2))
            """
        )
        return rows

    def supplier_disruption_impact(self, supplier: str, delay_days: int) -> dict:
        if delay_days < 0:
            raise SupplyChainError("delay_days must be >= 0.")
        s = self._resolve_supplier(supplier)
        materials, _ = self.graph.query(
            """
            MATCH (rm:RawMaterial)-[r:SOURCED_FROM]->(s:Supplier {id: $id})
            OPTIONAL MATCH (rm)-[:USED_IN]->(:IntermediatePart)-[:USED_IN]->(p:Product)
            OPTIONAL MATCH (rm)-[alt:SOURCED_FROM]->(other:Supplier) WHERE other.id <> $id
            RETURN rm.id AS material_id, rm.name AS material_name,
                   r.lead_time_days AS normal_lead_time_days, r.is_primary AS is_primary_source,
                   collect(DISTINCT {id: p.id, name: p.name}) AS products,
                   collect(DISTINCT {id: other.id, name: other.name}) AS alternative_sources
            ORDER BY material_id
            """,
            {"id": s["id"]},
        )
        backups, _ = self.graph.query(
            "MATCH (b:Supplier)-[:BACKUP_FOR]->(:Supplier {id: $id}) RETURN b.id AS id, b.name AS name ORDER BY id",
            {"id": s["id"]},
        )
        stock = self._stock_by_item([m["material_id"] for m in materials])

        affected, products = [], {}
        for m in materials:
            on_hand, reorder = stock.get(m["material_id"], (None, None))
            m["products"] = [p for p in m["products"] if p.get("id")]
            m["alternative_sources"] = [a for a in m["alternative_sources"] if a.get("id")]
            for p in m["products"]:
                products[p["id"]] = p["name"]
            lead = m["normal_lead_time_days"]
            affected.append({
                **m,
                "effective_lead_time_days": lead + delay_days if lead is not None else None,
                "on_hand_quantity": on_hand,
                "reorder_point": reorder,
                "risk": _risk(on_hand, reorder),
            })
        return {
            "supplier": s,
            "delay_days": delay_days,
            "materials": affected,
            "affected_products": [{"id": k, "name": v} for k, v in sorted(products.items())],
            "backup_suppliers": backups,
            "method": "Deterministic graph traversal + current inventory; no LLM involved.",
        }

    def low_stock(self, limit: int = 50) -> list[dict]:
        rows, _ = self.sql.query(
            """
            SELECT item_id, item_type, SUM(quantity_on_hand) AS on_hand, MAX(reorder_point) AS reorder_point,
                   MAX(unit_of_measure) AS unit, COUNT(*) AS warehouses
            FROM inventory_levels
            GROUP BY item_id, item_type
            HAVING SUM(quantity_on_hand) <= 2 * MAX(reorder_point)
            ORDER BY SUM(quantity_on_hand) / NULLIF(MAX(reorder_point), 0), item_id
            LIMIT %(limit)s
            """,
            {"limit": limit},
        )
        names = self._names([r["item_id"] for r in rows])
        for r in rows:
            r["name"] = names.get(r["item_id"])
            r["status"] = "at_or_below_reorder_point" if r["on_hand"] <= r["reorder_point"] else "below_2x_buffer"
        return rows

    def expiring_contracts(self, within_days: int = 90) -> dict:
        end = self.reference_date + dt.timedelta(days=within_days)
        rows, _ = self.sql.query(
            """
            SELECT contract_id, supplier_id, end_date, (end_date - %(ref)s) AS days_until_expiry,
                   contract_value, payment_terms, penalty_clause, auto_renew, account_manager
            FROM contracts
            WHERE end_date BETWEEN %(ref)s AND %(end)s
            ORDER BY end_date
            """,
            {"ref": self.reference_date, "end": end},
        )
        names = self._names([r["supplier_id"] for r in rows])
        for r in rows:
            r["supplier_name"] = names.get(r["supplier_id"])
        return {"reference_date": self.reference_date.isoformat(), "within_days": within_days, "contracts": rows}

    def describe_schema(self) -> dict:
        labels, _ = self.graph.query(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS nodes ORDER BY label"
        )
        rels, _ = self.graph.query(
            "MATCH ()-[r]->() RETURN type(r) AS relationship, count(*) AS edges ORDER BY relationship"
        )
        cols, _ = self.sql.query(
            """
            SELECT table_name, string_agg(column_name || ' ' || data_type, ', ' ORDER BY ordinal_position) AS columns
            FROM information_schema.columns
            WHERE table_schema = 'public'
            GROUP BY table_name ORDER BY table_name
            """,
            max_rows=500,
        )
        return {"neo4j_labels": labels, "neo4j_relationships": rels, "postgres_tables": cols}

    # -- escape hatches (guarded + read-only transaction) -----------------------

    def readonly_cypher(self, query: str, params: dict | None = None, max_rows: int = 100) -> dict:
        assert_read_only_cypher(query)
        rows, truncated = self.graph.query(query, params or {}, max_rows=max_rows)
        return {"rows": rows, "row_count": len(rows), "truncated": truncated}

    def readonly_sql(self, query: str, max_rows: int = 100) -> dict:
        assert_read_only_sql(query)
        rows, truncated = self.sql.query(query, None, max_rows=max_rows)
        return {"rows": rows, "row_count": len(rows), "truncated": truncated}

    # -- helpers --------------------------------------------------------------

    def _stock_by_item(self, item_ids: list[str]) -> dict[str, tuple[float, float]]:
        if not item_ids:
            return {}
        rows, _ = self.sql.query(
            """
            SELECT item_id, SUM(quantity_on_hand) AS on_hand, MAX(reorder_point) AS reorder_point
            FROM inventory_levels WHERE item_id = ANY(%(ids)s) GROUP BY item_id
            """,
            {"ids": item_ids},
        )
        return {r["item_id"]: (r["on_hand"], r["reorder_point"]) for r in rows}

    def _names(self, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        rows, _ = self.graph.query(
            "MATCH (n) WHERE n.id IN $ids RETURN n.id AS id, n.name AS name", {"ids": sorted(set(ids))}
        )
        return {r["id"]: r["name"] for r in rows}


def _risk(on_hand: float | None, reorder: float | None) -> str:
    """Same thresholds the twin's simulate_scenario uses: at/below the reorder
    point is high, up to 2x the reorder point is medium, otherwise low.
    Alternative sources are reported separately rather than folded in here,
    so this matches the twin's own answers."""
    if on_hand is None or reorder is None:
        return "unknown"
    if on_hand <= reorder:
        return "high"
    if on_hand <= 2 * reorder:
        return "medium"
    return "low"
