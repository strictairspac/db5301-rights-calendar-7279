"""分发域的 SQLAlchemy 仓储实现。

与 InMemoryRepository 实现同一组方法。写操作必须在 ``transactional()``
单事务内完成；PostgreSQL 上对渠道行加 ``FOR UPDATE`` 行锁，保证同一渠道
的签署/确认按到达顺序串行，不同渠道互不阻塞。结构化快照与增量用通用
JSON 列存储（与既有 business_cases/business_evidence 表一致，可在
PostgreSQL/MySQL/sqlite 间移植；如部署需要 JSONB，可在迁移中统一调整）。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.settings import engine
from src.distribution.errors import ConflictError, ValidationError
from src.distribution.models import (
    Change, ChangeOp, Channel, FullPage, Receipt, ReceiptState, ScheduleEntry,
    SignedVersion,
)
from src.models.tables import dist_channels, dist_receipts, dist_versions
from src.utils.time import utc_time

_current_conn: ContextVar = ContextVar('dist_conn', default=None)


def _entry_from_dict(data: dict) -> ScheduleEntry:
    return ScheduleEntry(**data)


def _change_from_dict(data: dict) -> Change:
    return Change(
        channel_id=data['channel_id'],
        op=ChangeOp(data['op']),
        screening_id=data['screening_id'],
        entry=_entry_from_dict(data['entry']) if data.get('entry') else None,
        previous=_entry_from_dict(data['previous']) if data.get('previous') else None,
    )


class SQLRepository:
    def __init__(self, db_engine=None) -> None:
        self.engine = db_engine or engine

    @contextmanager
    def transactional(self):
        if _current_conn.get() is not None:
            # 服务层嵌套调用时复用同一事务。
            yield _current_conn.get()
            return
        with self.engine.begin() as conn:
            token = _current_conn.set(conn)
            try:
                yield conn
            finally:
                _current_conn.reset(token)

    def _tx(self):
        conn = _current_conn.get()
        if conn is None:
            raise RuntimeError('写操作必须在 transactional() 事务内执行')
        return conn

    def _is_pg(self, conn) -> bool:
        return conn.dialect.name == 'postgresql'

    def lock_channel(self, channel_id: str) -> None:
        """同渠道写操作的串行点：PostgreSQL 行锁，其他方言退化为普通查询。"""
        conn = self._tx()
        statement = select(dist_channels).where(
            dist_channels.c.channel_id == channel_id)
        if self._is_pg(conn):
            statement = statement.with_for_update()
        conn.execute(statement)

    # ---- 渠道 ----
    def upsert_channel(self, channel: Channel) -> None:
        conn = self._tx()
        statement = select(dist_channels).where(
            dist_channels.c.channel_id == channel.channel_id)
        if self._is_pg(conn):
            statement = statement.with_for_update()
        existing = conn.execute(statement).mappings().first()
        if existing is None:
            try:
                conn.execute(dist_channels.insert().values(
                    channel_id=channel.channel_id, territory=channel.territory,
                    medium=channel.medium, created_at=utc_time()))
            except IntegrityError as exc:
                # 并发首签：唯一约束兜底，调用方重试即可。
                raise ConflictError('渠道并发创建冲突，请重试') from exc

    def get_channel(self, channel_id: str) -> Channel | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(dist_channels).where(
                dist_channels.c.channel_id == channel_id)).mappings().first()
        return Channel(row['channel_id'], row['territory'], row['medium']) if row else None

    def list_channels(self) -> list[Channel]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(dist_channels).order_by(
                dist_channels.c.channel_id)).mappings()
        return [Channel(r['channel_id'], r['territory'], r['medium']) for r in rows]

    # ---- 签署版本 ----
    def next_sequence(self, channel_id: str) -> int:
        conn = self._tx()
        latest = conn.execute(
            select(dist_versions.c.sequence).where(
                dist_versions.c.channel_id == channel_id)
            .order_by(dist_versions.c.sequence.desc()).limit(1)
        ).scalar_one_or_none()
        return (latest + 1) if latest is not None else 1

    def save_version(self, version: SignedVersion) -> None:
        conn = self._tx()
        payload = {
            'channel': {
                'channel_id': version.full_page.channel.channel_id,
                'territory': version.full_page.channel.territory,
                'medium': version.full_page.channel.medium,
            },
            'entries': [entry.__dict__ for entry in version.full_page.entries],
        }
        try:
            conn.execute(dist_versions.insert().values(
                version_id=version.version_id, channel_id=version.channel_id,
                sequence=version.sequence, full_page=payload,
                parent_version_id=version.parent_version_id,
                signed_by=version.signed_by, signed_at=version.signed_at,
                created_at=utc_time()))
        except IntegrityError as exc:
            raise ConflictError('该渠道下存在同序号版本，签署冲突，请重试') from exc

    def _version_from_row(self, row) -> SignedVersion:
        page = row['full_page']
        full_page = FullPage(
            channel=Channel(**page['channel']),
            entries=tuple(_entry_from_dict(e) for e in page['entries']),
        )
        return SignedVersion(
            version_id=row['version_id'], channel_id=row['channel_id'],
            sequence=row['sequence'], full_page=full_page,
            parent_version_id=row['parent_version_id'],
            signed_by=row['signed_by'], signed_at=row['signed_at'])

    def get_version(self, version_id: str) -> SignedVersion | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(dist_versions).where(
                dist_versions.c.version_id == version_id)).mappings().first()
        return self._version_from_row(row) if row else None

    def get_channel_version(self, channel_id: str, sequence: int) -> SignedVersion | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(dist_versions).where(
                dist_versions.c.channel_id == channel_id,
                dist_versions.c.sequence == sequence)).mappings().first()
        return self._version_from_row(row) if row else None

    def latest_version(self, channel_id: str) -> SignedVersion | None:
        rows = self.list_versions(channel_id)
        return rows[-1] if rows else None

    def list_versions(self, channel_id: str) -> list[SignedVersion]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(dist_versions).where(
                dist_versions.c.channel_id == channel_id)
                .order_by(dist_versions.c.sequence)).mappings()
        return [self._version_from_row(r) for r in rows]

    # ---- 批次回执 ----
    def save_receipt(self, receipt: Receipt) -> None:
        conn = self._tx()
        try:
            conn.execute(dist_receipts.insert().values(
                receipt_id=receipt.receipt_id, channel_id=receipt.channel_id,
                version_id=receipt.version_id, sequence=receipt.sequence,
                state=str(receipt.state),
                changes=[c.to_dict() for c in receipt.changes],
                idempotency_key=receipt.idempotency_key, attempts=receipt.attempts,
                last_error=receipt.last_error,
                base_snapshot_version_id=receipt.base_snapshot_version_id,
                applied_sequence=receipt.applied_sequence,
                created_at=receipt.created_at or utc_time(),
                acked_at=receipt.acked_at))
        except IntegrityError as exc:
            raise ConflictError('批次或幂等键已存在，投递冲突') from exc

    def update_receipt(self, receipt: Receipt) -> None:
        conn = self._tx()
        count = conn.execute(dist_receipts.update().where(
            dist_receipts.c.receipt_id == receipt.receipt_id).values(
                state=str(receipt.state), attempts=receipt.attempts,
                last_error=receipt.last_error,
                applied_sequence=receipt.applied_sequence,
                acked_at=receipt.acked_at)).rowcount
        if count != 1:
            raise ValidationError('批次更新失败：记录不存在')

    def _receipt_from_row(self, row) -> Receipt:
        return Receipt(
            receipt_id=row['receipt_id'], channel_id=row['channel_id'],
            version_id=row['version_id'], sequence=row['sequence'],
            state=ReceiptState(row['state']),
            changes=tuple(_change_from_dict(c) for c in row['changes']),
            idempotency_key=row['idempotency_key'], attempts=row['attempts'],
            last_error=row['last_error'], created_at=row['created_at'],
            acked_at=row['acked_at'], applied_sequence=row['applied_sequence'],
            base_snapshot_version_id=row['base_snapshot_version_id'])

    def _select_receipt(self, statement, lock: bool):
        conn = _current_conn.get()
        if conn is not None:
            if lock and self._is_pg(conn):
                statement = statement.with_for_update()
            row = conn.execute(statement).mappings().first()
        else:
            with self.engine.connect() as bound:
                row = bound.execute(statement).mappings().first()
        return self._receipt_from_row(row) if row else None

    def get_receipt(self, receipt_id: str) -> Receipt | None:
        return self._select_receipt(
            select(dist_receipts).where(dist_receipts.c.receipt_id == receipt_id),
            lock=False)

    def find_receipt_by_idempotency_key(self, key: str) -> Receipt | None:
        return self._select_receipt(
            select(dist_receipts).where(dist_receipts.c.idempotency_key == key),
            lock=True)

    def find_receipt(self, channel_id: str, sequence: int) -> Receipt | None:
        return self._select_receipt(
            select(dist_receipts).where(
                dist_receipts.c.channel_id == channel_id,
                dist_receipts.c.sequence == sequence),
            lock=False)

    def list_receipts(self, channel_id: str | None = None) -> list[Receipt]:
        statement = select(dist_receipts)
        if channel_id is not None:
            statement = statement.where(dist_receipts.c.channel_id == channel_id)
        statement = statement.order_by(dist_receipts.c.channel_id,
                                       dist_receipts.c.sequence)
        conn = _current_conn.get()
        if conn is not None:
            rows = conn.execute(statement).mappings()
        else:
            with self.engine.connect() as bound:
                rows = bound.execute(statement).mappings()
        return [self._receipt_from_row(r) for r in rows]
