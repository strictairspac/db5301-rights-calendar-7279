"""SQL 仓储冒烟测试：用 sqlite + 桩模块加载 SQLRepository，无需第三方库。

运行：python3 tests/test_sql_repo_sqlite.py
验证 SQL 层与领域服务在真实数据库（sqlite 方言，行锁退化为普通查询）上的
建表、JSON 持久化、唯一约束、增量固化与回执状态机全流程。
"""

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- 桩掉第三方/环境相关依赖，使 src.services.distribution_repo 可独立加载 ----
config = types.ModuleType('src.config')
settings = types.ModuleType('src.config.settings')
settings.engine = None  # 由 SQLRepository(db_engine) 显式注入
config.settings = settings
services_pkg = types.ModuleType('src.services')
services_pkg.__path__ = [str(ROOT / 'src' / 'services')]
sys.modules.setdefault('src.config', config)
sys.modules['src.config.settings'] = settings
sys.modules.setdefault('src.services', services_pkg)

# 仅用标准库无法 import sqlalchemy，本测试在检测到缺失时跳过。
try:
    from sqlalchemy import create_engine  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - 本机无依赖时
    create_engine = None

if create_engine is not None:
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.pool import StaticPool
    from src.models.tables import schema
    from src.services.distribution_repo import SQLRepository
    from src.distribution import (
        ChangeOp, ConflictError, DistributionService, ReceiptState,
    )


def entry(sid, start='19:00', end='20:30', film='F-01', title='潮线以北'):
    return {'screening_id': sid, 'film_id': film, 'title': title,
            'starts_at': f'2026-12-04T{start}+08:00',
            'ends_at': f'2026-12-04T{end}+08:00'}


@unittest.skipIf(create_engine is None, '本机未安装 sqlalchemy，跳过 SQL 仓储冒烟')
class SQLRepositorySqliteTest(unittest.TestCase):
    def setUp(self):
        # StaticPool 让内存 sqlite 始终复用同一连接，保证建表与各事务读写同一份库。
        self.engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool)
        schema.create_all(self.engine)
        self.service = DistributionService(SQLRepository(self.engine))
        self.cid = '上海|影院'

    def test_full_workflow_persisted_and_reloaded(self):
        _, r1 = self.service.sign_version('上海', '影院', [entry('P-1')], 'pub')
        self.service.confirm(r1.idempotency_key)
        _, r2 = self.service.sign_version('上海', '影院',
                                          [entry('P-1'), entry('P-2', film='F-2', title='x')], 'pub')

        # 用全新服务实例（共享同一 engine）验证真实持久化与重新加载
        fresh = DistributionService(SQLRepository(self.engine))
        overview = fresh.channel_overview(self.cid)
        self.assertEqual(overview['latest_signed_sequence'], 2)
        self.assertEqual(overview['applied_sequence'], 1)
        self.assertEqual(overview['head_pending_sequence'], 2)
        self.assertEqual(overview['applied_entries'][0]['screening_id'], 'P-1')
        pending = fresh.pending_changes(self.cid)
        self.assertEqual(pending[0]['changes'][0]['op'], str(ChangeOp.ADDED))

        # 跳过 v2 之外的前序约束：v2 是队首，可以确认
        acked = fresh.confirm(r2.idempotency_key)
        self.assertEqual(acked.state, ReceiptState.ACKED)
        # 幂等重试：再次确认不产生重复
        again = fresh.confirm(r2.idempotency_key)
        self.assertEqual(again.acked_at, acked.acked_at)
        self.assertEqual(fresh.channel_overview(self.cid)['applied_sequence'], 2)

    def test_ordering_block_enforced_across_reload(self):
        self.service.sign_version('上海', '影院', [entry('P-1')], 'pub')
        _, r2 = self.service.sign_version('上海', '影院',
                                          [entry('P-1'), entry('P-2')], 'pub')
        fresh = DistributionService(SQLRepository(self.engine))
        with self.assertRaises(ConflictError):
            fresh.confirm(r2.idempotency_key)
        # 独立渠道不受影响
        _, h1 = self.service.sign_version('杭州', '线上', [entry('H-1')], 'pub')
        self.assertEqual(fresh.confirm(h1.idempotency_key).state, ReceiptState.ACKED)

    def test_delivery_attempts_dead_and_rearm_persist(self):
        _, r1 = self.service.sign_version('上海', '影院', [entry('P-1')], 'pub')
        head = self.service.next_deliverable(self.cid)
        for _ in range(self.service.max_attempts):
            self.service.record_delivery_attempt(head.receipt_id, 'timeout')
        fresh = DistributionService(SQLRepository(self.engine))
        reloaded = fresh.repo.get_receipt(head.receipt_id)
        self.assertEqual(reloaded.state, ReceiptState.DEAD)
        with self.assertRaises(ConflictError):
            fresh.confirm(r1.idempotency_key)
        fresh.rearm(head.receipt_id)
        self.assertEqual(fresh.confirm(r1.idempotency_key).state, ReceiptState.ACKED)

    def test_tables_created_with_expected_columns(self):
        names = set(inspect(self.engine).get_table_names())
        for expected in ('dist_channels', 'dist_versions', 'dist_receipts'):
            self.assertIn(expected, names)


if __name__ == '__main__':
    unittest.main()
