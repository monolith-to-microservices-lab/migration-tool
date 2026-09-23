"""Local operational state (SQLite by default).

This store holds ONLY migration bookkeeping - never a copy of the business data.
The one place it stores payload content is ``migration_items.payload_json``: the
exact body sent to a service for a row THIS run created, kept so rollback can
prove the destination row is still what the migration produced before deleting.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .models import EntityType, ImportAction, ItemStatus, RunStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MigrationRun(Base):
    __tablename__ = "migration_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(32), default="snapshot")
    dry_run: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.STARTED.value)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    legacy_source: Mapped[str] = mapped_column(String(255), default="")
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list["MigrationItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class MigrationItem(Base):
    __tablename__ = "migration_items"
    __table_args__ = (
        UniqueConstraint("run_id", "entity_type", "legacy_id", name="uq_item_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("migration_runs.run_id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(16))
    legacy_id: Mapped[int] = mapped_column(Integer)
    destination: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    run: Mapped[MigrationRun] = relationship(back_populates="items")


class StateStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._sf = sessionmaker(bind=engine, future=True, expire_on_commit=False)
        Base.metadata.create_all(engine)

    @classmethod
    def from_url(cls, url: str) -> "StateStore":
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            from sqlalchemy.pool import StaticPool

            kwargs["connect_args"] = {"check_same_thread": False}
            # ":memory:" / "sqlite://" needs a single shared connection.
            if ":memory:" in url or url in ("sqlite://", "sqlite:///:memory:"):
                kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)
        return cls(engine)

    def session(self) -> Session:
        return self._sf()

    # -- runs -----------------------------------------------------------
    def create_run(
        self, run_id: str, *, mode: str = "snapshot", dry_run: bool = False, legacy_source: str = ""
    ) -> MigrationRun:
        with self._sf() as s:
            run = MigrationRun(
                run_id=run_id,
                mode=mode,
                dry_run=dry_run,
                legacy_source=legacy_source,
                status=RunStatus.STARTED.value,
                stats_json={},
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            return run

    def get_run(self, run_id: str) -> MigrationRun | None:
        with self._sf() as s:
            return s.get(MigrationRun, run_id)

    def latest_run(self) -> MigrationRun | None:
        with self._sf() as s:
            return s.scalars(
                select(MigrationRun).order_by(MigrationRun.started_at.desc())
            ).first()

    def list_runs(self) -> list[MigrationRun]:
        with self._sf() as s:
            return list(s.scalars(select(MigrationRun).order_by(MigrationRun.started_at.desc())))

    def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        stats: dict | None = None,
        finished: bool = False,
        error: str | None = None,
    ) -> None:
        with self._sf() as s:
            run = s.get(MigrationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if status is not None:
                run.status = status.value
            if stats is not None:
                run.stats_json = stats
            if error is not None:
                run.error = error
            if finished:
                run.finished_at = _now()
            s.commit()

    # -- items --------------------------------------------------------
    def get_item(self, run_id: str, entity: EntityType, legacy_id: int) -> MigrationItem | None:
        with self._sf() as s:
            return s.scalars(
                select(MigrationItem).where(
                    MigrationItem.run_id == run_id,
                    MigrationItem.entity_type == entity.value,
                    MigrationItem.legacy_id == legacy_id,
                )
            ).first()

    def upsert_item(
        self,
        *,
        run_id: str,
        entity: EntityType,
        legacy_id: int,
        destination: str,
        action: ImportAction,
        status: ItemStatus,
        error: str | None = None,
        payload_hash: str | None = None,
        payload_json: str | None = None,
    ) -> None:
        with self._sf() as s:
            item = s.scalars(
                select(MigrationItem).where(
                    MigrationItem.run_id == run_id,
                    MigrationItem.entity_type == entity.value,
                    MigrationItem.legacy_id == legacy_id,
                )
            ).first()
            if item is None:
                item = MigrationItem(
                    run_id=run_id,
                    entity_type=entity.value,
                    legacy_id=legacy_id,
                    destination=destination,
                )
                s.add(item)
            item.action = action.value
            item.status = status.value
            item.error = error
            if payload_hash is not None:
                item.payload_hash = payload_hash
            if payload_json is not None:
                item.payload_json = payload_json
            s.commit()

    def items_for_run(
        self, run_id: str, *, entity: EntityType | None = None, action: ImportAction | None = None
    ) -> list[MigrationItem]:
        with self._sf() as s:
            stmt = select(MigrationItem).where(MigrationItem.run_id == run_id)
            if entity is not None:
                stmt = stmt.where(MigrationItem.entity_type == entity.value)
            if action is not None:
                stmt = stmt.where(MigrationItem.action == action.value)
            stmt = stmt.order_by(MigrationItem.legacy_id)
            return list(s.scalars(stmt))

    def set_item_status(
        self, item_id: int, status: ItemStatus, *, error: str | None = None
    ) -> None:
        with self._sf() as s:
            item = s.get(MigrationItem, item_id)
            if item is None:
                raise KeyError(item_id)
            item.status = status.value
            if error is not None:
                item.error = error
            s.commit()
