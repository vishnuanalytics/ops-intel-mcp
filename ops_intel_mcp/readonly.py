"""Read-only database access, enforced twice.

1. A cheap client-side guard (`assert_read_only_sql` / `assert_read_only_cypher`)
   rejects anything that isn't a single read statement, so an obviously wrong
   query fails fast with a clear message.
2. The databases themselves enforce it: every Postgres query runs inside a
   `SET TRANSACTION READ ONLY` transaction that is always rolled back, and every
   Cypher query runs in a Neo4j read transaction (`execute_read`). A write that
   slips past the regex (say, via a function call) is still rejected server-side.

The guard alone is not the security boundary — (2) is. The guard exists for
fast, readable errors.
"""

from __future__ import annotations

import datetime as dt
import re
import threading
import uuid
from decimal import Decimal
from typing import Any

_WRITE_WORDS = (
    "insert|update|delete|merge|upsert|create|drop|alter|truncate|grant|revoke|"
    "copy|vacuum|reindex|cluster|comment|refresh|lock|call|do|set|remove|detach|"
    "foreach|load|execute|prepare|listen|notify"
)
_WRITE_RE = re.compile(rf"\b({_WRITE_WORDS})\b", re.IGNORECASE)
_SQL_START_RE = re.compile(r"^\s*(select|with|explain)\b", re.IGNORECASE)
_CYPHER_START_RE = re.compile(r"^\s*(match|optional\s+match|with|unwind|return)\b", re.IGNORECASE)
_STRING_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"")
_COMMENT_RE = re.compile(r"--[^\n]*|//[^\n]*|/\*.*?\*/", re.DOTALL)


class ReadOnlyViolation(ValueError):
    """The query is not a single read-only statement."""


def _strip(query: str) -> str:
    """Drop comments and string literals so keywords inside them don't count,
    and a trailing semicolon."""
    body = _COMMENT_RE.sub(" ", query)
    body = _STRING_RE.sub("''", body)
    return body.strip().rstrip(";").strip()


def _check(query: str, start_re: re.Pattern, kind: str, starts: str) -> None:
    body = _strip(query)
    if not body:
        raise ReadOnlyViolation(f"Empty {kind} query.")
    if ";" in body:
        raise ReadOnlyViolation(f"Only a single {kind} statement is allowed (found ';').")
    if not start_re.match(body):
        raise ReadOnlyViolation(f"{kind} queries must start with {starts}.")
    hit = _WRITE_RE.search(body)
    if hit:
        raise ReadOnlyViolation(f"{kind} query contains a write/admin keyword: {hit.group(0).upper()!r}.")


def assert_read_only_sql(query: str) -> None:
    _check(query, _SQL_START_RE, "SQL", "SELECT, WITH or EXPLAIN")


def assert_read_only_cypher(query: str) -> None:
    _check(query, _CYPHER_START_RE, "Cypher", "MATCH, OPTIONAL MATCH, WITH, UNWIND or RETURN")


def to_jsonable(value: Any) -> Any:
    """Driver values (Decimal, dates, UUIDs, Neo4j temporal/graph types) -> JSON-safe."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "iso_format"):  # neo4j.time.Date / DateTime / Duration
        return value.iso_format()
    if hasattr(value, "labels") and hasattr(value, "items"):  # neo4j.graph.Node
        return {"labels": sorted(value.labels), **{k: to_jsonable(v) for k, v in value.items()}}
    if hasattr(value, "type") and hasattr(value, "items"):  # neo4j.graph.Relationship
        return {"type": value.type, **{k: to_jsonable(v) for k, v in value.items()}}
    return str(value)


class PostgresReader:
    """One lazily opened connection, reused; every query in its own READ ONLY
    transaction that is rolled back afterwards."""

    def __init__(self, url: str, statement_timeout_ms: int = 10_000):
        self._url = url
        self._timeout_ms = int(statement_timeout_ms)
        self._conn = None
        self._lock = threading.Lock()

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self._url, row_factory=dict_row, connect_timeout=15)

    def query(self, sql: str, params: dict | tuple | None = None, max_rows: int = 200) -> tuple[list[dict], bool]:
        """Returns (rows, truncated). Caller-facing SQL must pass the guard first;
        internal parameterized queries go through here too, so the transaction
        is the enforcement point for both."""
        import psycopg

        with self._lock:
            for attempt in (1, 2):
                if self._conn is None or self._conn.closed:
                    self._conn = self._connect()
                try:
                    with self._conn.cursor() as cur:
                        cur.execute("SET TRANSACTION READ ONLY")
                        cur.execute(f"SET LOCAL statement_timeout = {self._timeout_ms}")
                        cur.execute(sql, params)
                        rows = cur.fetchmany(max_rows + 1) if cur.description else []
                    return [to_jsonable(dict(r)) for r in rows[:max_rows]], len(rows) > max_rows
                except psycopg.OperationalError:
                    # stale pooled connection (Neon idles them out) — reconnect once
                    self._close()
                    if attempt == 2:
                        raise
                finally:
                    if self._conn is not None and not self._conn.closed:
                        self._conn.rollback()
        raise RuntimeError("unreachable")

    def _close(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        finally:
            self._conn = None

    def close(self) -> None:
        with self._lock:
            self._close()


class Neo4jReader:
    """Every query runs in a Neo4j read transaction."""

    def __init__(self, uri: str, user: str, password: str):
        from neo4j import GraphDatabase

        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    def query(self, cypher: str, params: dict | None = None, max_rows: int = 200) -> tuple[list[dict], bool]:
        from neo4j import READ_ACCESS

        def work(tx):
            out = []
            for i, record in enumerate(tx.run(cypher, params or {})):
                if i >= max_rows:
                    return out, True
                out.append(to_jsonable(dict(record.items())))
            return out, False

        with self._driver.session(default_access_mode=READ_ACCESS) as session:
            return session.execute_read(work)

    def close(self) -> None:
        self._driver.close()
