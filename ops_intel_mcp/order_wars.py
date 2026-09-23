"""Order Wars tools: read-only access to the game's Postgres schema
(games, factions, events, snapshots, diplomacy, DeepEval scores)."""

from __future__ import annotations

import re
from typing import Any, Protocol

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Reader(Protocol):
    def query(self, q: str, params: Any = None, max_rows: int = 200) -> tuple[list[dict], bool]: ...


class OrderWarsError(ValueError):
    """A user-facing problem with the request (unknown game, ambiguous prefix, ...)."""


class OrderWars:
    def __init__(self, sql: Reader, schema: str = "order_wars"):
        if not _IDENT.match(schema):
            raise ValueError(f"Invalid schema name: {schema!r}")
        self.sql = sql
        self.s = schema

    def list_games(self, limit: int = 20) -> list[dict]:
        rows, _ = self.sql.query(
            f"""
            SELECT g.id AS game_id, g.status, g.current_turn, g.started_at, g.ended_at,
                   sc.name AS scenario, w.faction_name AS winner,
                   (SELECT count(*) FROM {self.s}.game_factions f WHERE f.game_id = g.id) AS factions,
                   (SELECT count(*) FROM {self.s}.game_events e WHERE e.game_id = g.id) AS events
            FROM {self.s}.games g
            LEFT JOIN {self.s}.scenarios sc ON sc.id = g.scenario_id
            LEFT JOIN {self.s}.game_factions w ON w.id = g.winner_faction_id
            ORDER BY g.started_at DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {"limit": limit},
        )
        return rows

    def _resolve_game(self, game_id: str) -> str:
        prefix = game_id.strip().lower()
        if not re.fullmatch(r"[0-9a-f-]{4,36}", prefix):
            raise OrderWarsError(f"{game_id!r} is not a game id (a UUID or its first 8+ characters).")
        rows, _ = self.sql.query(
            f"SELECT id::text AS id FROM {self.s}.games WHERE id::text LIKE %(p)s ORDER BY id LIMIT 5",
            {"p": prefix + "%"},
        )
        if not rows:
            raise OrderWarsError(f"No game with id starting {game_id!r}. Use list_games.")
        if len(rows) > 1:
            raise OrderWarsError(f"{game_id!r} matches several games: {', '.join(r['id'] for r in rows)}.")
        return rows[0]["id"]

    def game_summary(self, game_id: str) -> dict:
        gid = self._resolve_game(game_id)
        game, _ = self.sql.query(
            f"""
            SELECT g.id AS game_id, g.status, g.current_turn, g.started_at, g.ended_at,
                   sc.name AS scenario, sc.max_turns, w.faction_name AS winner
            FROM {self.s}.games g
            LEFT JOIN {self.s}.scenarios sc ON sc.id = g.scenario_id
            LEFT JOIN {self.s}.game_factions w ON w.id = g.winner_faction_id
            WHERE g.id = %(g)s::uuid
            """,
            {"g": gid},
        )
        factions, _ = self.sql.query(
            f"""
            SELECT f.faction_name, lower(f.role_preset::text) AS role_preset, f.is_alive, f.eliminated_at_turn,
                   s.turn AS as_of_turn, s.resources, s.territory_count, s.unit_count
            FROM {self.s}.game_factions f
            LEFT JOIN LATERAL (
                SELECT * FROM {self.s}.faction_state_snapshots s
                WHERE s.faction_id = f.id ORDER BY s.turn DESC LIMIT 1
            ) s ON true
            WHERE f.game_id = %(g)s::uuid
            ORDER BY f.faction_name
            """,
            {"g": gid},
        )
        diplomacy, _ = self.sql.query(
            f"""
            SELECT a.faction_name AS faction_a, b.faction_name AS faction_b,
                   lower(d.status::text) AS status, d.turn_changed
            FROM {self.s}.diplomatic_relations d
            JOIN {self.s}.game_factions a ON a.id = d.faction_a_id
            JOIN {self.s}.game_factions b ON b.id = d.faction_b_id
            WHERE d.game_id = %(g)s::uuid AND lower(d.status::text) <> 'neutral'
            ORDER BY d.turn_changed, faction_a
            """,
            {"g": gid},
        )
        actions, _ = self.sql.query(
            f"""
            SELECT event_type, count(*) AS n FROM {self.s}.game_events
            WHERE game_id = %(g)s::uuid GROUP BY event_type ORDER BY n DESC, event_type
            """,
            {"g": gid},
        )
        scores, _ = self.sql.query(
            f"""
            SELECT f.faction_name, es.metric_name, round(avg(es.score)::numeric, 3) AS avg_score, count(*) AS n
            FROM {self.s}.eval_scores es
            JOIN {self.s}.game_events e ON e.id = es.game_event_id
            JOIN {self.s}.game_factions f ON f.id = e.faction_id
            WHERE e.game_id = %(g)s::uuid
            GROUP BY f.faction_name, es.metric_name ORDER BY f.faction_name, es.metric_name
            """,
            {"g": gid},
        )
        return {
            **(game[0] if game else {"game_id": gid}),
            "factions": factions,
            "diplomacy": diplomacy,
            "action_counts": {r["event_type"]: r["n"] for r in actions},
            "eval_scores": scores,
        }

    def game_events(self, game_id: str, faction: str | None = None, event_type: str | None = None,
                    limit: int = 100) -> dict:
        gid = self._resolve_game(game_id)
        rows, truncated = self.sql.query(
            f"""
            SELECT e.turn, f.faction_name AS faction, e.event_type,
                   e.payload->>'rationale' AS rationale, e.payload->>'resolution' AS resolution,
                   e.payload->>'target_province' AS target_province,
                   e.payload->>'target_faction' AS target_faction,
                   sc.scores, sc.role_alignment_reason
            FROM {self.s}.game_events e
            LEFT JOIN {self.s}.game_factions f ON f.id = e.faction_id
            LEFT JOIN LATERAL (
                SELECT jsonb_object_agg(es.metric_name, round(es.score::numeric, 3)) AS scores,
                       max(es.reason) FILTER (WHERE es.metric_name = 'Role Alignment') AS role_alignment_reason
                FROM {self.s}.eval_scores es WHERE es.game_event_id = e.id
            ) sc ON true
            WHERE e.game_id = %(g)s::uuid
              AND (%(faction)s::text IS NULL OR lower(f.faction_name) = lower(%(faction)s::text))
              AND (%(etype)s::text IS NULL OR e.event_type = %(etype)s::text)
            ORDER BY e.turn, e.created_at
            """,
            {"g": gid, "faction": faction, "etype": event_type},
            max_rows=limit,
        )
        return {"game_id": gid, "events": rows, "truncated": truncated}

    def role_preset_scores(self) -> list[dict]:
        rows, _ = self.sql.query(
            f"""
            SELECT lower(f.role_preset::text) AS role_preset, es.metric_name, round(avg(es.score)::numeric, 3) AS avg_score,
                   count(*) AS decisions, count(DISTINCT e.game_id) AS games
            FROM {self.s}.eval_scores es
            JOIN {self.s}.game_events e ON e.id = es.game_event_id
            JOIN {self.s}.game_factions f ON f.id = e.faction_id
            GROUP BY 1, es.metric_name
            ORDER BY 1, es.metric_name
            """
        )
        return rows
