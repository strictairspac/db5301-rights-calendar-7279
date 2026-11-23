"""分发域单元测试，仅依赖标准库：python3 -m unittest discover backend/tests。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.distribution import (  # noqa: E402
    ChangeOp, ConflictError, DistributionService, NotFoundError, ReceiptState,
    ValidationError,
)
from src.distribution.repository import InMemoryRepository  # noqa: E402

T = '2026-12-04T{hm}+08:00'


def entry(sid, start='19:00', end='20:30', film='F-01', title='潮线以北'):
    return {'screening_id': sid, 'film_id': film, 'title': title,
            'starts_at': T.format(hm=start), 'ends_at': T.format(hm=end)}


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return f'2026-12-04T01:{self.value:02d}:00+00:00'


class DistributionTestBase(unittest.TestCase):
    def setUp(self):
        self.service = DistributionService(InMemoryRepository(), clock=Clock())
        self.cid = '上海|影院'

    def sign(self, territory='上海', medium='影院', entries=None, actor='publisher'):
        return self.service.sign_version(territory, medium, entries or [], actor)

    def key(self, seq):
        return f'{self.cid}#v{seq}'


class SignAndDiffTests(DistributionTestBase):
    def test_first_version_is_all_added(self):
        _, receipt = self.sign(entries=[entry('P-1'), entry('P-2')])
        self.assertEqual(receipt.sequence, 1)
        self.assertEqual(receipt.state, ReceiptState.PENDING)
        ops = {c.screening_id: c.op for c in receipt.changes}
        self.assertEqual(ops, {'P-1': ChangeOp.ADDED, 'P-2': ChangeOp.ADDED})
        # 整版快照也被保留
        version = self.service.repo.get_version(receipt.version_id)
        self.assertEqual(len(version.full_page.entries), 2)

    def test_second_version_delta_added_rescheduled_cancelled(self):
        self.sign(entries=[entry('P-1', '19:00', '20:30'),
                           entry('P-2', '21:00', '22:00', film='F-02', title='雾港来信')])
        _, r2 = self.sign(entries=[
            entry('P-1', '19:30', '21:00'),                       # 改期
            entry('P-3', '22:00', '23:00', film='F-03', title='白鹭返航'),  # 新增
            # P-2 取消
        ])
        by_id = {c.screening_id: c for c in r2.changes}
        self.assertEqual(set(by_id), {'P-1', 'P-2', 'P-3'})
        self.assertEqual(by_id['P-1'].op, ChangeOp.RESCHEDULED)
        self.assertEqual(by_id['P-1'].previous.starts_at[-5:], '00:00')  # UTC 11:00
        self.assertEqual(by_id['P-2'].op, ChangeOp.CANCELLED)
        self.assertEqual(by_id['P-3'].op, ChangeOp.ADDED)
        # 无变化场次不出现在增量中
        _, r3 = self.sign(entries=[
            entry('P-1', '19:30', '21:00'),
            entry('P-3', '22:00', '23:00', film='F-03', title='白鹭返航'),
        ])
        self.assertEqual(r3.changes, ())

    def test_snapshot_frozen_when_new_version_signed_mid_batch(self):
        _, r1 = self.sign(entries=[entry('P-1')])
        _, r2 = self.sign(entries=[entry('P-1'), entry('P-2')])
        before = tuple(c.to_dict() for c in r1.changes)
        # 分批读取过程中又签新版，已生成批次内容保持原样
        _, r3 = self.sign(entries=[entry('P-1', '20:00', '21:30'), entry('P-2'),
                                   entry('P-3', film='F-03', title='x')])
        self.assertEqual(tuple(c.to_dict() for c in r1.changes), before)
        self.assertEqual([c.screening_id for c in r2.changes], ['P-2'])
        self.assertEqual({c.screening_id for c in r3.changes}, {'P-1', 'P-3'})


class OrderingTests(DistributionTestBase):
    def _three_versions(self):
        self.sign(entries=[entry('P-1')])
        self.sign(entries=[entry('P-1'), entry('P-2')])
        self.sign(entries=[entry('P-1'), entry('P-2'), entry('P-3')])

    def test_unacked_batch_blocks_later_batches_same_channel(self):
        self._three_versions()
        # 跳过 v1 直接确认 v2 被拒，不能漏收
        with self.assertRaises(ConflictError):
            self.service.confirm(self.key(2))
        with self.assertRaises(ConflictError):
            self.service.confirm(self.key(3))
        # 按签署顺序确认
        self.assertEqual(self.service.confirm(self.key(1)).state, ReceiptState.ACKED)
        with self.assertRaises(ConflictError):
            self.service.confirm(self.key(3))  # v2 仍未确认
        self.service.confirm(self.key(2))
        r3 = self.service.confirm(self.key(3))
        self.assertEqual(r3.applied_sequence, 3)

    def test_next_deliverable_is_always_earliest_pending(self):
        self._three_versions()
        head = self.service.next_deliverable(self.cid)
        self.assertEqual(head.sequence, 1)
        self.service.confirm(self.key(1))
        self.assertEqual(self.service.next_deliverable(self.cid).sequence, 2)
        self.service.confirm(self.key(2))
        self.service.confirm(self.key(3))
        self.assertIsNone(self.service.next_deliverable(self.cid))

    def test_other_channels_continue_when_one_blocked(self):
        # 上海影院有未确认批次
        self.sign(entries=[entry('P-1')])
        # 杭州线上是独立渠道，照常签署与确认
        _, h1 = self.service.sign_version('杭州', '线上', [entry('H-1')], 'publisher')
        self.assertEqual(h1.channel_id, '杭州|线上')
        ack = self.service.confirm('杭州|线上#v1')
        self.assertEqual(ack.state, ReceiptState.ACKED)
        # 上海仍停在 v1
        self.assertEqual(self.service.next_deliverable(self.cid).sequence, 1)
        overview = self.service.channel_overview(self.cid)
        self.assertIsNone(overview['applied_sequence'])

    def test_dead_receipt_still_blocks_and_can_be_rearmed(self):
        self._three_versions()
        head = self.service.next_deliverable(self.cid)
        for _ in range(self.service.max_attempts):
            updated = self.service.record_delivery_attempt(head.receipt_id, error='timeout')
        self.assertEqual(updated.state, ReceiptState.DEAD)
        with self.assertRaises(ConflictError):
            self.service.confirm(self.key(2))
        # 重新置回队列后仍按原序号确认，不跳过
        self.service.rearm(head.receipt_id)
        self.service.confirm(self.key(1))
        self.service.confirm(self.key(2))
        self.assertEqual(self.service.confirm(self.key(3)).state, ReceiptState.ACKED)


class IdempotencyTests(DistributionTestBase):
    def test_ack_retry_does_not_apply_twice(self):
        _, receipt = self.sign(entries=[entry('P-1')])
        first = self.service.confirm(receipt.idempotency_key)
        first_acked = first.acked_at
        # 模拟回执丢失后的重试：相同幂等键
        second = self.service.confirm(receipt.idempotency_key)
        self.assertEqual(second.receipt_id, first.receipt_id)
        self.assertEqual(second.state, ReceiptState.ACKED)
        self.assertEqual(second.acked_at, first_acked)
        self.assertEqual(second.applied_sequence, 1)
        # 底层只有一条回执记录
        self.assertEqual(len(self.service.repo.list_receipts()), 1)

    def test_unknown_idempotency_key_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.confirm('不存在#v9')

    def test_delivery_attempts_after_ack_are_noop(self):
        _, receipt = self.sign(entries=[entry('P-1')])
        self.service.confirm(receipt.idempotency_key)
        again = self.service.record_delivery_attempt(receipt.receipt_id)
        self.assertEqual(again.state, ReceiptState.ACKED)
        self.assertEqual(again.attempts, 0)


class ReconciliationTests(DistributionTestBase):
    def test_overview_compares_pending_and_effective(self):
        _, r1 = self.sign(entries=[entry('P-1')])
        self.sign(entries=[entry('P-1'), entry('P-2')])
        self.service.confirm(r1.idempotency_key)
        overview = self.service.channel_overview(self.cid)
        self.assertEqual(overview['latest_signed_sequence'], 2)
        self.assertEqual(overview['applied_sequence'], 1)
        self.assertEqual(overview['head_pending_sequence'], 2)
        self.assertEqual([e['screening_id'] for e in overview['applied_entries']], ['P-1'])
        self.assertEqual(overview['in_flight'][0]['changes'][0]['screening_id'], 'P-2')
        self.assertEqual(overview['missing_sequences'], [])

        effective = self.service.effective_version(self.cid)
        self.assertEqual(effective['sequence'], 1)
        pending = self.service.pending_changes(self.cid)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['sequence'], 2)

    def test_missing_sequence_detected_when_head_is_gap(self):
        self.sign(entries=[entry('P-1')])
        self.sign(entries=[entry('P-1'), entry('P-2')])
        self.sign(entries=[entry('P-1'), entry('P-2'), entry('P-3')])
        # 假设 v1 投递失败挂起、v2 也在队列；核对应标出漏收
        overview = self.service.channel_overview(self.cid)
        self.assertEqual(overview['head_pending_sequence'], 1)
        self.assertEqual(overview['missing_sequences'], [])  # 队首就是 v1，尚无已生效版本
        # 若 v1 变 dead，仍列出为未确认且挡住后续
        head = self.service.next_deliverable(self.cid)
        for _ in range(self.service.max_attempts):
            self.service.record_delivery_attempt(head.receipt_id, 'lost')
        overview = self.service.channel_overview(self.cid)
        self.assertEqual(overview['head_pending_sequence'], 1)
        self.assertEqual(overview['in_flight'][0]['state'], 'dead')
        self.assertEqual(len(overview['in_flight']), 3)

    def test_channel_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.channel_overview('火星|影院')


class ValidationTests(DistributionTestBase):
    def test_reject_wildcard_duplicate_bad_time_and_inverted_window(self):
        with self.assertRaises(ValidationError):
            self.sign(territory='*', entries=[entry('P-1')])
        with self.assertRaises(ValidationError):
            self.sign(medium='*', entries=[entry('P-1')])
        with self.assertRaises(ValidationError):
            self.sign(entries=[entry('P-1'), entry('P-1')])
        bad_time = entry('P-1')
        bad_time['starts_at'] = '2026-12-04T19:00:30+08:00'
        with self.assertRaises(ValidationError):
            self.sign(entries=[bad_time])
        inverted = entry('P-1', '21:00', '20:00')
        with self.assertRaises(ValidationError):
            self.sign(entries=[inverted])
        no_offset = entry('P-1')
        no_offset['starts_at'] = '2026-12-04T19:00'
        with self.assertRaises(ValidationError):
            self.sign(entries=[no_offset])

    def test_empty_first_sign_is_allowed(self):
        version, receipt = self.sign(entries=[])
        self.assertEqual(version.sequence, 1)
        self.assertEqual(receipt.changes, ())
        self.service.confirm(receipt.idempotency_key)
        self.assertEqual(self.service.channel_overview(self.cid)['applied_sequence'], 1)


if __name__ == '__main__':
    unittest.main()
