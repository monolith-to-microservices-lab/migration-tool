"""READ-ONLY access to the legacy monolith PostgreSQL.

This is the ONLY place the migration-tool touches a database that holds business
data, and it only ever issues SELECTs. For PostgreSQL the connection is also
pinned to ``default_transaction_read_only=on`` as a hard backstop: any accidental
INSERT/UPDATE/DELETE would raise instead of mutating the source of truth.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .models import LegacySale, LegacyUser


class LegacyBase(DeclarativeBase):
    pass


class LegacyUserRow(LegacyBase):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LegacySaleRow(LegacyBase):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    item_name: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def build_engine(url: str) -> Engine:
    connect_args: dict = {}
    if url.startswith("postgresql"):
        # Hard read-only guarantee at the session level.
        connect_args["options"] = "-c default_transaction_read_only=on"
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True, future=True)


class LegacySource:
    """Batched, read-only reader over ``legacy.users`` / ``legacy.sales``."""

    def __init__(self, engine: Engine, batch_size: int = 100) -> None:
        self._engine = engine
        self._session_factory = sessionmaker(bind=engine, future=True)
        self.batch_size = batch_size

    @classmethod
    def from_url(cls, url: str, batch_size: int = 100) -> LegacySource:
        return cls(build_engine(url), batch_size=batch_size)

    def ping(self) -> None:
        with self._session_factory() as s:
            s.execute(select(func.now()))

    # -- counts -----------------------------------------------------------
    def count_users(self) -> int:
        with self._session_factory() as s:
            return int(s.scalar(select(func.count()).select_from(LegacyUserRow)) or 0)

    def count_sales(self) -> int:
        with self._session_factory() as s:
            return int(s.scalar(select(func.count()).select_from(LegacySaleRow)) or 0)

    # -- streamed iteration --------------------------------------------
    def iter_users(self) -> Iterator[LegacyUser]:
        yield from self._iter(LegacyUserRow, LegacyUser)

    def iter_sales(self) -> Iterator[LegacySale]:
        yield from self._iter(LegacySaleRow, LegacySale)

    def _iter(self, row_cls, model_cls):
        with self._session_factory() as session:  # type: Session
            stmt = select(row_cls).order_by(row_cls.id).execution_options(yield_per=self.batch_size)
            for row in session.scalars(stmt):
                yield model_cls.model_validate(row)

    # -- lookups -------------------------------------------------------
    def get_user(self, user_id: int) -> LegacyUser | None:
        with self._session_factory() as s:
            row = s.get(LegacyUserRow, user_id)
            return LegacyUser.model_validate(row) if row is not None else None

    def iter_sale_refs(self) -> Iterator[tuple[int, int]]:
        """Lightweight ``(sale_id, user_id)`` stream for referential checks."""
        with self._session_factory() as s:
            stmt = (
                select(LegacySaleRow.id, LegacySaleRow.user_id)
                .order_by(LegacySaleRow.id)
                .execution_options(yield_per=self.batch_size)
            )
            for sale_id, user_id in s.execute(stmt):
                yield int(sale_id), int(user_id)

    def distinct_sale_user_ids(self) -> list[int]:
        with self._session_factory() as s:
            return sorted(int(x) for x in s.scalars(select(LegacySaleRow.user_id).distinct()))

    def legacy_orphan_sales(self) -> list[tuple[int, int]]:
        """Sales whose user_id has no matching legacy user (should be empty)."""
        with self._session_factory() as s:
            stmt = (
                select(LegacySaleRow.id, LegacySaleRow.user_id)
                .outerjoin(LegacyUserRow, LegacyUserRow.id == LegacySaleRow.user_id)
                .where(LegacyUserRow.id.is_(None))
            )
            return [(int(a), int(b)) for a, b in s.execute(stmt).all()]
