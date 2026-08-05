"""SQLAlchemy models for the sandbox "fake company" data warehouse.

This is a small, self-contained SQLite warehouse used to exercise the
Data Lineage Investigator against realistic (but fake) pipeline data:

    raw_customers, raw_orders          -> raw ingestion layer
    stg_orders_cleaned                 -> cleaned/deduped staging layer
    fct_daily_revenue                  -> "dashboard metric" fact table

It is intentionally separate from the main application's DATABASE_URL
(see .env), which will later point at whatever real system is being
investigated. This sandbox always lives at a fixed local SQLite file so
it can be rebuilt deterministically.
"""

from pathlib import Path

from sqlalchemy import Column, Date, DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

SANDBOX_DIR = Path(__file__).resolve().parent
DEFAULT_SQLITE_PATH = SANDBOX_DIR / "warehouse.db"
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_SQLITE_PATH}"


class Base(DeclarativeBase):
    pass


class RawCustomer(Base):
    """Raw customer records as they land from the source system."""

    __tablename__ = "raw_customers"

    customer_id = Column(Integer, primary_key=True)
    region = Column(String, nullable=False)
    signup_date = Column(Date, nullable=False)


class RawOrder(Base):
    """Raw order events. Not unique on order_id: duplicate/late-arriving
    raw events for the same order_id can and do occur, mirroring a real
    append-only ingestion table."""

    __tablename__ = "raw_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, nullable=False, index=True)
    customer_id = Column(Integer, nullable=False, index=True)
    amount = Column(Float, nullable=False)
    status = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False)


class StgOrdersCleaned(Base):
    """Staging table: raw_orders deduplicated and joined to region,
    with cancelled/invalid rows filtered out. See
    sql_models/01_stg_orders_cleaned.sql."""

    __tablename__ = "stg_orders_cleaned"

    order_id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, nullable=False, index=True)
    region = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False)


class FctDailyRevenue(Base):
    """The "dashboard metric" fact table: total completed revenue per
    region per day. See sql_models/02_fct_daily_revenue.sql."""

    __tablename__ = "fct_daily_revenue"

    date = Column(Date, primary_key=True)
    region = Column(String, primary_key=True)
    total_revenue = Column(Float, nullable=False)


def get_engine(database_url: str = DEFAULT_DATABASE_URL):
    return create_engine(database_url)


def get_session(engine=None) -> Session:
    engine = engine or get_engine()
    session_factory = sessionmaker(bind=engine)
    return session_factory()
