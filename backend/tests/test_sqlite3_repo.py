"""纯标准库 sqlite3 的仓储冒烟测试，验证状态机在真实关系库上的持久化语义。

生产使用 src/services/distribution_repo.py 的 SQLAlchemy 实现；本测试用
内置 sqlite3 实现同一仓储端口，证明领域状态机与「真实数据库 + 事务 +
唯一约束」的协作正确（含跨连接重载、幂等、顺序阻塞、dead/rearm）。
运行：python3 tests/test_sqlite3_repo.py
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.distribution import (  # noqa: E402
    ChangeOp, ConflictError, DistributionService, NotFoundError, ReceiptState,
)
from src.distribution.models import Channel, Receipt, ReceiptState as RS  # noqa: E402

SCHEMA = """
CREATE TABLE dist_channels (
  channel_id TEXT PRIMARY KEY, territory TEXT NOT NULL, medium TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE dist_versions (
  version_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  full_page TEXT NOT NULL, parent_version_id TEXT, signed_by TEXT NOT NULL,
  signed_at TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(channel_id, sequence));
CREATE TABLE dist_receipts (
  receipt_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, version_id TEXT NOT NULL,
  sequence INTEGER NOT NULL, state TEXT NOT NULL, changes TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE, attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, base_snapshot_version_id TEXT, applied_sequence INTEGER,
  created_at TEXT NOT NULL, acked_at TEXT, UNIQUE(channel_id, sequence));
"""


def entry(sid, start='19:00', end='20:30', film='F-01', title='潮线以北'):
    return {'screening_id': sid, 'film_id': film, 'title': title,
            'starts_at': f'2026-12-04T{start}+08:00',
            'ends_at': f'2026-12-04T{end}+08:00'}


class Sqlite3Repository:
    """与 InMemoryRepository/SQLRepository 同端口的 sqlite3 实现，仅用于本测试。"""

    def __init__(self, path):
        self.path = path
        self._conn = None

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    class _Tx:
        def __init__(self, repo):
            self.repo = repo

        def __enter__(self):
            self.conn = self.repo._connect()
            self.repo._conn = self.conn
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
            self.conn.close()
            self.repo._conn = None
            return False

    def transactional(self):
        return self._Tx(self)

    def lock_channel(self, channel_id):  # sqlite 无行锁，唯一约束兜底
        return None

    def upsert_channel(self, channel: Channel):
        self._conn.execute(
            'INSERT OR IGNORE INTO dist_channels(channel_id, territory, medium, created_at) '
            "VALUES (?, ?, ?, 't')",
            (channel.channel_id, channel.territory, channel.medium))

    def get_channel(self, channel_id):
        conn = self._conn or self._connect()
        row = conn.execute('SELECT * FROM dist_channels WHERE channel_id=?',
                           (channel_id,)).fetchone()
        if self._conn is None:
            conn.close()
        return Channel(row['channel_id'], row['territory'], row['medium']) if row else None

    def list_channels(self):
        conn = self._connect()
        rows = conn.execute('SELECT * FROM dist_channels ORDER BY channel_id').fetchall()
        conn.close()
        return [Channel(r['channel_id'], r['territory'], r['medium']) for r in rows]

    def next_sequence(self, channel_id):
        row = self._conn.execute(
            'SELECT MAX(sequence) AS m FROM dist_versions WHERE channel_id=?',
            (channel_id,)).fetchone()
        return (row['m'] or 0) + 1

    def save_version(self, version):
        import json
        from src.utils.time import utc_time
        page = {'channel': version.full_page.channel.__dict__,
                'entries': [e.__dict__ for e in version.full_page.entries]}
        self._conn.execute(
            'INSERT INTO dist_versions(version_id, channel_id, sequence, full_page, '
            'parent_version_id, signed_by, signed_at, created_at) VALUES (?,?,?,?,?,?,?,?)',
            (version.version_id, version.channel_id, version.sequence,
             json.dumps(page, ensure_ascii=False), version.parent_version_id,
             version.signed_by, version.signed_at, utc_time()))

    def _version_from_row(self, row):
        import json
        from src.distribution.models import FullPage, ScheduleEntry, SignedVersion
        page = json.loads(row['full_page'])
        return SignedVersion(
            version_id=row['version_id'], channel_id=row['channel_id'],
            sequence=row['sequence'],
            full_page=FullPage(Channel(**page['channel']),
                               tuple(ScheduleEntry(**e) for e in page['entries'])),
            parent_version_id=row['parent_version_id'], signed_by=row['signed_by'],
            signed_at=row['signed_at'])

    def get_version(self, version_id):
        conn = self._connect()
        row = conn.execute('SELECT * FROM dist_versions WHERE version_id=?',
                           (version_id,)).fetchone()
        conn.close()
        return self._version_from_row(row) if row else None

    def get_channel_version(self, channel_id, sequence):
        conn = self._connect()
        row = conn.execute('SELECT * FROM dist_versions WHERE channel_id=? AND sequence=?',
                           (channel_id, sequence)).fetchone()
        conn.close()
        return self._version_from_row(row) if row else None

    def latest_version(self, channel_id):
        rows = self.list_versions(channel_id)
        return rows[-1] if rows else None

    def list_versions(self, channel_id):
        conn = self._connect()
        rows = conn.execute('SELECT * FROM dist_versions WHERE channel_id=? ORDER BY sequence',
                            (channel_id,)).fetchall()
        conn.close()
        return [self._version_from_row(r) for r in rows]

    def save_receipt(self, receipt: Receipt):
        import json
        self._conn.execute(
            'INSERT INTO dist_receipts(receipt_id, channel_id, version_id, sequence, state, '
            'changes, idempotency_key, attempts, last_error, base_snapshot_version_id, '
            'applied_sequence, created_at, acked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (receipt.receipt_id, receipt.channel_id, receipt.version_id, receipt.sequence,
             str(receipt.state), json.dumps([c.to_dict() for c in receipt.changes], ensure_ascii=False),
             receipt.idempotency_key, receipt.attempts, receipt.last_error,
             receipt.base_snapshot_version_id, receipt.applied_sequence,
             receipt.created_at, receipt.acked_at))

    def update_receipt(self, receipt: Receipt):
        cur = self._conn.execute(
            'UPDATE dist_receipts SET state=?, attempts=?, last_error=?, '
            'applied_sequence=?, acked_at=? WHERE receipt_id=?',
            (str(receipt.state), receipt.attempts, receipt.last_error,
             receipt.applied_sequence, receipt.acked_at, receipt.receipt_id))
        if cur.rowcount != 1:
            raise NotFoundError('批次不存在')

    def _receipt_from_row(self, row):
        import json
        from src.distribution.models import Change, ChangeOp, ScheduleEntry
        def change(d):
            return Change(d['channel_id'], ChangeOp(d['op']), d['screening_id'],
                          entry=ScheduleEntry(**d['entry']) if d.get('entry') else None,
                          previous=ScheduleEntry(**d['previous']) if d.get('previous') else None)
        return Receipt(
            receipt_id=row['receipt_id'], channel_id=row['channel_id'],
            version_id=row['version_id'], sequence=row['sequence'],
            state=RS(row['state']), changes=tuple(change(c) for c in json.loads(row['changes'])),
            idempotency_key=row['idempotency_key'], attempts=row['attempts'],
            last_error=row['last_error'], created_at=row['created_at'],
            acked_at=row['acked_at'], applied_sequence=row['applied_sequence'],
            base_snapshot_version_id=row['base_snapshot_version_id'])

    def get_receipt(self, receipt_id):
        conn = self._conn or self._connect()
        row = conn.execute('SELECT * FROM dist_receipts WHERE receipt_id=?',
                           (receipt_id,)).fetchone()
        if self._conn is None:
            conn.close()
        return self._receipt_from_row(row) if row else None

    def find_receipt_by_idempotency_key(self, key):
        conn = self._conn or self._connect()
        row = conn.execute('SELECT * FROM dist_receipts WHERE idempotency_key=?',
                           (key,)).fetchone()
        if self._conn is None:
            conn.close()
        return self._receipt_from_row(row) if row else None

    def find_receipt(self, channel_id, sequence):
        conn = self._connect()
        row = conn.execute('SELECT * FROM dist_receipts WHERE channel_id=? AND sequence=?',
                           (channel_id, sequence)).fetchone()
        conn.close()
        return self._receipt_from_row(row) if row else None

    def list_receipts(self, channel_id=None):
        conn = self._connect()
        sql = 'SELECT * FROM dist_receipts'
        params = ()
        if channel_id is not None:
            sql += ' WHERE channel_id=?'
            params = (channel_id,)
        sql += ' ORDER BY channel_id, sequence'
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [self._receipt_from_row(r) for r in rows]


class Sqlite3PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.path = self.tmp.name
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        self.repo = Sqlite3Repository(self.path)
        self.service = DistributionService(self.repo)
        self.cid = '上海|影院'

    def test_end_to_end_with_real_sql_database(self):
        _, r1 = self.service.sign_version('上海', '影院', [entry('P-1'), entry('P-2')], 'pub')
        self.service.confirm(r1.idempotency_key)
        _, r2 = self.service.sign_version('上海', '影院', [
            entry('P-1', '19:30', '21:00'),  # 改期
            entry('P-3', film='F-3', title='x'),  # 新增；P-2 取消
        ], 'pub')

        # 新服务实例 + 新连接，验证落库后重新加载
        fresh_service = DistributionService(Sqlite3Repository(self.path))
        overview = fresh_service.channel_overview(self.cid)
        self.assertEqual(overview['applied_sequence'], 1)
        self.assertEqual(overview['latest_signed_sequence'], 2)
        self.assertEqual(overview['head_pending_sequence'], 2)
        pending = fresh_service.pending_changes(self.cid)[0]
        ops = {c['screening_id']: c['op'] for c in pending['changes']}
        self.assertEqual(ops, {'P-1': str(ChangeOp.RESCHEDULED),
                               'P-2': str(ChangeOp.CANCELLED),
                               'P-3': str(ChangeOp.ADDED)})

        # 幂等：重复确认不重复生效
        ack = fresh_service.confirm(r2.idempotency_key)
        again = fresh_service.confirm(r2.idempotency_key)
        self.assertEqual(ack.receipt_id, again.receipt_id)
        self.assertEqual(again.acked_at, ack.acked_at)
        self.assertEqual(fresh_service.channel_overview(self.cid)['applied_sequence'], 2)

        # 未知幂等键
        with self.assertRaises(NotFoundError):
            fresh_service.confirm('nope#v9')

    def test_blocking_and_other_channel_on_real_db(self):
        self.service.sign_version('上海', '影院', [entry('P-1')], 'pub')
        _, r2 = self.service.sign_version('上海', '影院', [entry('P-1'), entry('P-2')], 'pub')
        fresh = DistributionService(Sqlite3Repository(self.path))
        with self.assertRaises(ConflictError):
            fresh.confirm(r2.idempotency_key)
        _, h1 = self.service.sign_version('杭州', '线上', [entry('H-1')], 'pub')
        self.assertEqual(fresh.confirm(h1.idempotency_key).state, ReceiptState.ACKED)

    def test_dead_rearm_and_unique_constraints(self):
        _, r1 = self.service.sign_version('上海', '影院', [entry('P-1')], 'pub')
        head = self.service.next_deliverable(self.cid)
        for _ in range(self.service.max_attempts):
            self.service.record_delivery_attempt(head.receipt_id, 'lost')
        fresh = DistributionService(Sqlite3Repository(self.path))
        self.assertEqual(fresh.repo.get_receipt(head.receipt_id).state, ReceiptState.DEAD)
        # 唯一约束：重复幂等键直接插入会被数据库拒绝
        conn = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO dist_receipts(receipt_id, channel_id, version_id, '
                         'sequence, state, changes, idempotency_key, created_at) '
                         "VALUES ('x', ?, 'v', 1, 'pending', '[]', ?, 't')",
                         (self.cid, r1.idempotency_key))
        conn.close()
        fresh.rearm(head.receipt_id)
        self.assertEqual(fresh.confirm(r1.idempotency_key).state, ReceiptState.ACKED)


if __name__ == '__main__':
    unittest.main()
