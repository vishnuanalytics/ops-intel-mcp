"""Environment-driven config. Each backend is optional: its tools are only
registered when its connection settings are present."""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SupplyChainConfig:
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    postgres_url: str
    reference_date: dt.date  # "today" for contract-expiry maths on the seeded demo data


@dataclass(frozen=True)
class OrderWarsConfig:
    database_url: str
    schema: str = "order_wars"


@dataclass(frozen=True)
class SupportAutomationConfig:
    database_url: str
    tenant_id: str | None = None  # pin every tool to this one tenant


def _libpq_url(url: str) -> str:
    """SQLAlchemy-style URLs (postgresql+psycopg://) -> plain libpq URL, minus the
    Prisma-only `pgbouncer=true` flag that libpq rejects as an unknown parameter."""
    url = url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")
    return re.sub(r"([?&])pgbouncer=[^&]*&?", r"\1", url).rstrip("?&")


def supply_chain_config() -> SupplyChainConfig | None:
    uri, pw, pg = os.getenv("NEO4J_URI"), os.getenv("NEO4J_PASSWORD"), os.getenv("SUPPLY_CHAIN_POSTGRES_URL")
    if not (uri and pw and pg):
        return None
    raw_date = os.getenv("SUPPLY_CHAIN_REFERENCE_DATE")
    ref = dt.date.fromisoformat(raw_date) if raw_date else dt.date.today()
    return SupplyChainConfig(uri, os.getenv("NEO4J_USERNAME", "neo4j"), pw, pg, ref)


def order_wars_config() -> OrderWarsConfig | None:
    url = os.getenv("ORDER_WARS_DATABASE_URL")
    if not url:
        return None
    return OrderWarsConfig(_libpq_url(url), os.getenv("ORDER_WARS_SCHEMA", "order_wars"))


def support_automation_config() -> SupportAutomationConfig | None:
    url = os.getenv("SUPPORT_AUTOMATION_DATABASE_URL")
    if not url:
        return None
    tenant = os.getenv("SUPPORT_AUTOMATION_TENANT_ID") or None
    if tenant and not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", tenant):
        raise ValueError(f"SUPPORT_AUTOMATION_TENANT_ID must be a full tenant UUID, got {tenant!r}")
    return SupportAutomationConfig(_libpq_url(url), tenant)
