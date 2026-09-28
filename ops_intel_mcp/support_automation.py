"""Support Automation tools: read-only access to the platform's Supabase Postgres
(tenants, flows, runs with their node-by-node traces, the Salesforce case
timeline, Knowledge Integrity Loop review tasks, token usage, worker health).

Deliberately no ad-hoc SQL escape hatch here, unlike the supply chain backend:
this database holds credentials (`connections.auth`, `tenant_integrations.secret`,
`flow_triggers.token`), and a direct connection bypasses the app's row-level
security. So every query is a purpose-built, parameterized one that never selects
those columns, and every tenant-scoped query filters by tenant itself. Set
SUPPORT_AUTOMATION_TENANT_ID to pin the server to a single tenant.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

_UUIDISH = re.compile(r"[0-9a-f-]{4,36}")
OUTCOMES = ("auto_reply", "ask_human", "handover", "need_info", "notify")


class Reader(Protocol):
    def query(self, q: str, params: Any = None, max_rows: int = 200) -> tuple[list[dict], bool]: ...


class SupportAutomationError(ValueError):
    """A user-facing problem with the request (unknown tenant, ambiguous run id, ...)."""


class SupportAutomation:
    def __init__(self, sql: Reader, pinned_tenant_id: str | None = None):
        self.sql = sql
        self.pinned = pinned_tenant_id.lower() if pinned_tenant_id else None

    # -- scoping ----------------------------------------------------------------

    def _tenant(self, tenant: str | None) -> str | None:
        """Tenant name fragment or id (prefix) -> tenant_id. None means every tenant,
        unless the server is pinned to one, in which case that one is always used."""
        if not tenant:
            return self.pinned
        text = tenant.strip()
        rows, _ = self.sql.query(
            """
            SELECT tenant_id::text AS tenant_id, name FROM tenants
            WHERE tenant_id::text LIKE %(p)s OR name ILIKE %(n)s
            ORDER BY name LIMIT 10
            """,
            {"p": text.lower() + "%", "n": "%" + text + "%"},
        )
        exact = [r for r in rows if r["name"].lower() == text.lower() or r["tenant_id"] == text.lower()]
        rows = exact or rows
        if not rows:
            raise SupportAutomationError(f"No tenant matches {tenant!r}. Use support_automation_list_tenants.")
        if len(rows) > 1:
            names = ", ".join(f"{r['name']} ({r['tenant_id'][:8]})" for r in rows)
            raise SupportAutomationError(f"{tenant!r} matches several tenants: {names}.")
        tid = rows[0]["tenant_id"]
        if self.pinned and tid != self.pinned:
            raise SupportAutomationError("This server is pinned to a single tenant; other tenants are not visible.")
        return tid

    def _resolve_run(self, run_id: str) -> str:
        prefix = run_id.strip().lower()
        if not _UUIDISH.fullmatch(prefix):
            raise SupportAutomationError(f"{run_id!r} is not a run id (a UUID or its first 8+ characters).")
        rows, _ = self.sql.query(
            """
            SELECT run_id::text AS run_id FROM runs
            WHERE run_id::text LIKE %(p)s AND (%(t)s::uuid IS NULL OR tenant_id = %(t)s::uuid)
            ORDER BY run_id LIMIT 5
            """,
            {"p": prefix + "%", "t": self.pinned},
        )
        if not rows:
            raise SupportAutomationError(f"No run with id starting {run_id!r}. Use support_automation_recent_runs.")
        if len(rows) > 1:
            raise SupportAutomationError(f"{run_id!r} matches several runs: {', '.join(r['run_id'] for r in rows)}.")
        return rows[0]["run_id"]

    # -- tools ------------------------------------------------------------------

    def list_tenants(self) -> list[dict]:
        rows, _ = self.sql.query(
            """
            SELECT t.tenant_id, t.name, p.slug AS plan, t.billing_status, t.case_connector, t.created_at,
                   (SELECT count(*) FROM flows f WHERE f.tenant_id = t.tenant_id) AS flows,
                   (SELECT count(*) FROM runs r WHERE r.tenant_id = t.tenant_id
                      AND r.created_at > now() - interval '30 days') AS runs_30d,
                   (SELECT max(r.created_at) FROM runs r WHERE r.tenant_id = t.tenant_id) AS last_run_at
            FROM tenants t
            LEFT JOIN plans p ON p.plan_id = t.plan_id
            WHERE %(t)s::uuid IS NULL OR t.tenant_id = %(t)s::uuid
            ORDER BY last_run_at DESC NULLS LAST, t.name
            """,
            {"t": self.pinned},
        )
        return rows

    def list_flows(self, tenant: str | None = None) -> list[dict]:
        tid = self._tenant(tenant)
        rows, _ = self.sql.query(
            """
            SELECT f.flow_id, t.name AS tenant, f.name, f.team, f.status, f.version, f.published_version,
                   f.sf_entry, f.updated_at,
                   (SELECT count(*) FROM runs r WHERE r.flow_id = f.flow_id) AS runs,
                   (SELECT max(r.created_at) FROM runs r WHERE r.flow_id = f.flow_id) AS last_run_at,
                   (SELECT coalesce(jsonb_agg(jsonb_build_object('kind', tr.kind, 'label', tr.label, 'cron', tr.cron,
                                                                 'enabled', tr.enabled, 'fire_count', tr.fire_count,
                                                                 'last_fired_at', tr.last_fired_at)), '[]'::jsonb)
                      FROM flow_triggers tr WHERE tr.flow_id = f.flow_id) AS triggers
            FROM flows f JOIN tenants t ON t.tenant_id = f.tenant_id
            WHERE %(t)s::uuid IS NULL OR f.tenant_id = %(t)s::uuid
            ORDER BY t.name, f.name
            """,
            {"t": tid},
        )
        return rows

    def outcome_stats(self, tenant: str | None = None, days: int = 30) -> dict:
        tid = self._tenant(tenant)
        cells, _ = self.sql.query(
            """
            SELECT t.name AS tenant, coalesce(f.name, '(deleted flow)') AS flow, r.flow_id,
                   coalesce(r.outcome, 'none') AS outcome, r.human_action, count(*) AS n,
                   sum(r.confidence) AS conf_sum, count(r.confidence) AS conf_n,
                   sum(r.edit_distance) AS edit_sum, count(r.edit_distance) AS edit_n,
                   coalesce(sum(r.tokens_total), 0) AS tokens
            FROM runs r
            JOIN tenants t ON t.tenant_id = r.tenant_id
            LEFT JOIN flows f ON f.flow_id = r.flow_id
            WHERE r.created_at > now() - make_interval(days => %(d)s)
              AND (%(t)s::uuid IS NULL OR r.tenant_id = %(t)s::uuid)
            GROUP BY 1, 2, 3, 4, 5
            """,
            {"t": tid, "d": days},
            max_rows=5000,
        )
        flows: dict[tuple, dict] = {}
        for c in cells:
            f = flows.setdefault((c["tenant"], c["flow"], c["flow_id"]), {
                "tenant": c["tenant"], "flow": c["flow"], "flow_id": c["flow_id"], "runs": 0,
                "outcomes": {}, "human_actions": {}, "_conf": [0.0, 0], "_edit": [0.0, 0], "tokens_total": 0,
            })
            f["runs"] += c["n"]
            f["outcomes"][c["outcome"]] = f["outcomes"].get(c["outcome"], 0) + c["n"]
            if c["human_action"]:
                f["human_actions"][c["human_action"]] = f["human_actions"].get(c["human_action"], 0) + c["n"]
            f["_conf"][0] += c["conf_sum"] or 0
            f["_conf"][1] += c["conf_n"]
            f["_edit"][0] += c["edit_sum"] or 0
            f["_edit"][1] += c["edit_n"]
            f["tokens_total"] += c["tokens"]
        out = []
        for f in flows.values():
            conf, edit, acts = f.pop("_conf"), f.pop("_edit"), f["human_actions"]
            f["avg_confidence"] = round(conf[0] / conf[1], 3) if conf[1] else None
            f["avg_edit_distance"] = round(edit[0] / edit[1], 3) if edit[1] else None
            reviewed = sum(acts.get(k, 0) for k in ("sent_as_is", "edited", "rewrote"))
            # Share of human-reviewed drafts that went out unchanged: the practical
            # "was the AI draft good enough" signal the feedback loop tracks.
            f["sent_as_is_rate"] = round(acts.get("sent_as_is", 0) / reviewed, 3) if reviewed else None
            out.append(f)
        out.sort(key=lambda f: -f["runs"])
        return {"window_days": days, "flows": out}

    def recent_runs(self, tenant: str | None = None, outcome: str | None = None, flow: str | None = None,
                    human_action: str | None = None, days: int = 30, limit: int = 50) -> dict:
        if outcome and outcome not in OUTCOMES:
            raise SupportAutomationError(f"Unknown outcome {outcome!r}. Use one of: {', '.join(OUTCOMES)}.")
        tid = self._tenant(tenant)
        rows, truncated = self.sql.query(
            """
            SELECT r.run_id, r.created_at, t.name AS tenant, f.name AS flow, r.flow_version, r.source,
                   r.case_id, r.subject, r.tier, r.outcome, r.confidence, (r.gate->>'pass')::boolean AS gate_pass,
                   r.human_action, r.edit_distance, r.tokens_total
            FROM runs r
            JOIN tenants t ON t.tenant_id = r.tenant_id
            LEFT JOIN flows f ON f.flow_id = r.flow_id
            WHERE r.created_at > now() - make_interval(days => %(d)s)
              AND (%(t)s::uuid IS NULL OR r.tenant_id = %(t)s::uuid)
              AND (%(o)s::text IS NULL OR r.outcome = %(o)s::text)
              AND (%(h)s::text IS NULL OR r.human_action = %(h)s::text)
              AND (%(f)s::text IS NULL OR f.name ILIKE '%%' || %(f)s::text || '%%' OR r.flow_id::text = %(f)s::text)
            ORDER BY r.created_at DESC
            """,
            {"t": tid, "d": days, "o": outcome, "h": human_action, "f": flow},
            max_rows=limit,
        )
        return {"runs": rows, "truncated": truncated}

    def run_detail(self, run_id: str) -> dict:
        rid = self._resolve_run(run_id)
        rows, _ = self.sql.query(
            """
            SELECT r.run_id, r.created_at, t.name AS tenant, f.name AS flow, r.flow_version, r.source, r.team,
                   r.case_id, r.subject, r.tier, r.region, r.outcome, r.confidence, r.gate,
                   (SELECT coalesce(jsonb_agg(jsonb_build_object(
                        'node_type', s->>'type', 'node_id', s->>'node_id', 'summary', s->>'summary',
                        'elapsed_ms', s->'data'->'elapsed_ms', 'model', s->'data'->'model')
                        ORDER BY ord), '[]'::jsonb)
                    FROM jsonb_array_elements(CASE WHEN jsonb_typeof(r.trace) = 'array' THEN r.trace ELSE '[]' END)
                         WITH ORDINALITY AS x(s, ord)) AS steps,
                   (SELECT coalesce(jsonb_agg(jsonb_build_object(
                        'doc_url', d->>'doc_url', 'heading_path', d->>'heading_path',
                        'rerank_score', d->'rerank_score')), '[]'::jsonb)
                    FROM jsonb_array_elements(CASE WHEN jsonb_typeof(r.retrieval) = 'array' THEN r.retrieval ELSE '[]' END)
                         AS d) AS retrieved,
                   r.draft, r.human_action, r.human_reply, r.edit_distance, r.correction_analysis,
                   r.clarify_round, r.tokens_total, r.tokens_by_model, r.tokens_by_node
            FROM runs r
            JOIN tenants t ON t.tenant_id = r.tenant_id
            LEFT JOIN flows f ON f.flow_id = r.flow_id
            WHERE r.run_id = %(r)s::uuid
            """,
            {"r": rid},
        )
        return rows[0]

    def case_timeline(self, case: str, tenant: str | None = None, limit: int = 200) -> dict:
        text = case.strip()
        if not text:
            raise SupportAutomationError("Give a case number (e.g. 00001234) or a Salesforce Case id.")
        tid = self._tenant(tenant)
        events, truncated = self.sql.query(
            """
            SELECT e.ts, t.name AS tenant, e.case_number, e.case_sf_id, e.actor, e.action,
                   e.from_status, e.to_status, e.routed_team, e.reason, e.confidence, e.run_id
            FROM case_events e JOIN tenants t ON t.tenant_id = e.tenant_id
            WHERE (e.case_number = %(c)s OR e.case_sf_id = %(c)s)
              AND (%(t)s::uuid IS NULL OR e.tenant_id = %(t)s::uuid)
            ORDER BY e.ts, e.event_id
            """,
            {"c": text, "t": tid},
            max_rows=limit,
        )
        # runs.case_id holds whichever identifier the channel had (case number or Case id)
        ids = sorted({text} | {e[k] for e in events for k in ("case_sf_id", "case_number") if e.get(k)})
        runs, _ = self.sql.query(
            """
            SELECT r.run_id, r.created_at, r.subject, r.outcome, r.confidence, r.human_action, r.edit_distance
            FROM runs r
            WHERE r.case_id = ANY(%(ids)s) AND (%(t)s::uuid IS NULL OR r.tenant_id = %(t)s::uuid)
            ORDER BY r.created_at
            """,
            {"ids": ids, "t": tid},
        )
        if not events and not runs:
            raise SupportAutomationError(f"No events or runs for case {case!r}.")
        return {"case": text, "events": events, "runs": runs, "truncated": truncated}

    def knowledge_reviews(self, tenant: str | None = None, status: str | None = None, limit: int = 50) -> dict:
        tid = self._tenant(tenant)
        rows, truncated = self.sql.query(
            """
            SELECT rt.id AS review_task_id, rt.created_at, t.name AS tenant, rt.kind, rt.trigger, rt.status,
                   rt.case_number, rt.run_id, rt.statement,
                   rt.verdict->>'relation' AS relation, (rt.verdict->>'flagged')::boolean AS flagged,
                   rt.verdict->'verdicts' AS verdicts, rt.reviewer_note, rt.reviewed_at,
                   wb.status AS writeback_status, wb.github_issue_url AS writeback_issue, wb.applied_at
            FROM review_tasks rt
            JOIN tenants t ON t.tenant_id = rt.tenant_id
            LEFT JOIN LATERAL (
                SELECT w.status, w.github_issue_url, w.applied_at FROM kb_doc_writebacks w
                WHERE w.review_task_id = rt.id::text ORDER BY w.applied_at DESC NULLS LAST LIMIT 1
            ) wb ON true
            WHERE (%(t)s::uuid IS NULL OR rt.tenant_id = %(t)s::uuid)
              AND (%(s)s::text IS NULL OR rt.status = %(s)s::text)
            ORDER BY rt.created_at DESC
            """,
            {"t": tid, "s": status},
            max_rows=limit,
        )
        return {"review_tasks": rows, "truncated": truncated}

    def token_usage(self, tenant: str | None = None, days: int = 30) -> dict:
        tid = self._tenant(tenant)
        by_tenant, _ = self.sql.query(
            """
            SELECT t.name AS tenant, p.slug AS plan, p.included_runs, p.included_tokens,
                   count(r.run_id) AS runs, coalesce(sum(r.tokens_total), 0) AS tokens
            FROM tenants t
            LEFT JOIN plans p ON p.plan_id = t.plan_id
            LEFT JOIN runs r ON r.tenant_id = t.tenant_id AND r.created_at > now() - make_interval(days => %(d)s)
            WHERE %(t)s::uuid IS NULL OR t.tenant_id = %(t)s::uuid
            GROUP BY t.name, p.slug, p.included_runs, p.included_tokens
            ORDER BY tokens DESC, t.name
            """,
            {"t": tid, "d": days},
        )
        by_model, _ = self.sql.query(
            """
            SELECT m.key AS model, sum((m.value)::bigint) AS tokens, count(*) AS runs
            FROM runs r, jsonb_each_text(CASE WHEN jsonb_typeof(r.tokens_by_model) = 'object'
                                              THEN r.tokens_by_model ELSE '{}' END) AS m
            WHERE r.created_at > now() - make_interval(days => %(d)s)
              AND (%(t)s::uuid IS NULL OR r.tenant_id = %(t)s::uuid)
              AND m.value ~ '^[0-9]+$'
            GROUP BY m.key ORDER BY tokens DESC
            """,
            {"t": tid, "d": days},
        )
        by_day, _ = self.sql.query(
            """
            SELECT date_trunc('day', r.created_at)::date AS day, count(*) AS runs,
                   coalesce(sum(r.tokens_total), 0) AS tokens
            FROM runs r
            WHERE r.created_at > now() - make_interval(days => %(d)s)
              AND (%(t)s::uuid IS NULL OR r.tenant_id = %(t)s::uuid)
            GROUP BY 1 ORDER BY 1
            """,
            {"t": tid, "d": days},
            max_rows=400,
        )
        return {"window_days": days, "by_tenant": by_tenant, "by_model": by_model, "by_day": by_day}

    def system_health(self) -> list[dict]:
        rows, _ = self.sql.query(
            """
            SELECT component, last_healthy_at, updated_at,
                   round((extract(epoch FROM now() - last_healthy_at) / 60)::numeric, 1) AS minutes_since_healthy,
                   detail
            FROM system_health ORDER BY component
            """
        )
        return rows
