"""Environment-driven config. Each backend is optional: its tools are only
registered when its connection settings are present."""

from __future__ import annotations

import datetime as dt
import os
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
    # SQLAlchemy-style URLs (postgresql+psycopg://) -> plain libpq URL
    url = url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")
    return OrderWarsConfig(url, os.getenv("ORDER_WARS_SCHEMA", "order_wars"))
