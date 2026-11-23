"""分发状态机：签署版本、批次投递、幂等回执、核对视图。

关键规则（对应需求）：

1. 签署的是整版排片，接收端批次只保存相对上一版的增量
   （added/rescheduled/cancelled），整版快照仍保留用于核对。
2. 批次内容在签署/开始读取时即固化；之后再签新版不会改变已生成批次。
3. 同一渠道严格按签署顺序处理：只要存在序号更小且未确认的批次，
   后续批次不能确认（不跳过漏收批次）；不同渠道互不影响。
4. 回执以确定性幂等键重试：重复确认直接返回原结果，不重复生效。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .diffing import build_full_page, diff_versions
from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    Receipt, ReceiptState, SignedVersion,
)
from .repository import Repository

DEFAULT_MAX_ATTEMPTS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='minutes')


def idempotency_key(channel_id: str, sequence: int) -> str:
    """渠道+序号确定的幂等键，回执重试必须携带同一个键。"""
    return f'{channel_id}#v{sequence}'


class DistributionService:
    def __init__(self, repo: Repository, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 clock=_now):
        self.repo = repo
        self.max_attempts = max_attempts
        self.clock = clock

    # ------------------------------------------------------------------ 签署
    def sign_version(self, territory: str, medium: str,
                     entries: list[dict], actor: str) -> tuple[SignedVersion, Receipt]:
        """签署一版整版排片，生成下一序号的待确认批次。

        签署本身不被未确认批次阻塞（允许分批读取期间再签新版），
        新批次进入同渠道队列等待按序确认。
        """
        full_page = build_full_page(territory, medium, entries)
        channel = full_page.channel
        with self.repo.transactional():
            self.repo.upsert_channel(channel)
            # 同渠道签署串行：后续序号计算、版本与批次写入都在该行锁保护内。
            self.repo.lock_channel(channel.channel_id)

            parent = self.repo.latest_version(channel.channel_id)
            sequence = self.repo.next_sequence(channel.channel_id)
            version_id = uuid.uuid4().hex
            version = SignedVersion(
                version_id=version_id,
                channel_id=channel.channel_id,
                sequence=sequence,
                full_page=full_page,
                parent_version_id=parent.version_id if parent else None,
                signed_by=actor,
                signed_at=self.clock(),
            )
            # 增量在签署时计算并固化进批次，之后签新版不影响本批次。
            changes = diff_versions(parent.full_page if parent else None, full_page)
            receipt = Receipt(
                receipt_id=uuid.uuid4().hex,
                channel_id=channel.channel_id,
                version_id=version_id,
                sequence=sequence,
                state=ReceiptState.PENDING,
                changes=changes,
                idempotency_key=idempotency_key(channel.channel_id, sequence),
                created_at=self.clock(),
                base_snapshot_version_id=parent.version_id if parent else None,
            )
            self.repo.save_version(version)
            self.repo.save_receipt(receipt)
        return version, receipt

    # ------------------------------------------------------------- 投递/读取
    def next_deliverable(self, channel_id: str) -> Receipt | None:
        """返回接收端下一个可读取的批次：该渠道序号最小的未确认批次。

        后续批次被前序未确认批次挡住，因此一次只暴露最早的一批；
        其他渠道有独立队列，调用方按渠道分别读取即可。
        """
        self._require_channel(channel_id)
        pending = [r for r in self.repo.list_receipts(channel_id) if r.pending]
        return pending[0] if pending else None

    def record_delivery_attempt(self, receipt_id: str, error: str | None = None) -> Receipt:
        """记录一次投递尝试（接收端读取/落库尝试）。超过上限转为 dead，
        但 dead 仍属于未确认，继续挡住后续批次，不会被跳过。"""
        with self.repo.transactional():
            receipt = self._require_receipt(receipt_id)
            self.repo.lock_channel(receipt.channel_id)
            receipt = self._require_receipt(receipt_id)
            if receipt.state == ReceiptState.ACKED:
                return receipt
            receipt.attempts += 1
            receipt.last_error = error
            if receipt.attempts >= self.max_attempts:
                receipt.state = ReceiptState.DEAD
            self.repo.update_receipt(receipt)
            return receipt

    def rearm(self, receipt_id: str) -> Receipt:
        """把 dead 批次重新置回待确认队列（人工介入后重试，仍按原序号排队）。"""
        with self.repo.transactional():
            receipt = self._require_receipt(receipt_id)
            self.repo.lock_channel(receipt.channel_id)
            receipt = self._require_receipt(receipt_id)
            if receipt.state == ReceiptState.ACKED:
                raise ConflictError('该批次已确认，无需重试')
            receipt.state = ReceiptState.PENDING
            receipt.last_error = None
            self.repo.update_receipt(receipt)
            return receipt

    # ------------------------------------------------------------------ 回执
    def confirm(self, key: str) -> Receipt:
        """接收端落库后凭幂等键确认批次。

        - 键不存在：404（不能确认一个从未投递的批次）。
        - 已确认：原样返回，不重复生效（回执丢失后的安全重试）。
        - 待确认且存在更小序号的未确认批次：409，不允许跳过漏收批次。
        - 否则置为已确认，记录接收端实际生效版本序号。
        不同渠道各自维护序号，互不阻塞。
        """
        receipt = self.repo.find_receipt_by_idempotency_key(key)
        if receipt is None:
            raise NotFoundError('幂等键对应的批次不存在')
        with self.repo.transactional():
            # 锁住渠道行，串行化同渠道确认，避免确认期间状态被并发改写。
            self.repo.lock_channel(receipt.channel_id)
            # 事务内重新读取，拿到最新状态（含行锁）。
            receipt = self.repo.find_receipt_by_idempotency_key(key)
            if receipt is None:
                raise NotFoundError('幂等键对应的批次不存在')
            if receipt.state == ReceiptState.ACKED:
                # 幂等：回执丢失重试时走这里，没有任何重复副作用。
                return receipt

            blocker = self._earlier_unacked(receipt)
            if blocker is not None:
                raise ConflictError(
                    f'前序批次 v{blocker.sequence} 尚未确认，不能跳过漏收批次')
            receipt.state = ReceiptState.ACKED
            receipt.acked_at = self.clock()
            receipt.applied_sequence = receipt.sequence
            receipt.last_error = None
            self.repo.update_receipt(receipt)
            return receipt

    def _earlier_unacked(self, receipt: Receipt) -> Receipt | None:
        for candidate in self.repo.list_receipts(receipt.channel_id):
            if candidate.sequence < receipt.sequence and candidate.state != ReceiptState.ACKED:
                return candidate
        return None

    # ------------------------------------------------------------- 发布页核对
    def channel_overview(self, channel_id: str) -> dict:
        """发布页面核对数据：待确认变更 vs 接收端实际生效版本。"""
        channel = self._require_channel(channel_id)
        receipts = self.repo.list_receipts(channel_id)
        versions = self.repo.list_versions(channel_id)
        acked = [r for r in receipts if r.state == ReceiptState.ACKED]
        unacked = [r for r in receipts if r.state != ReceiptState.ACKED]
        applied_sequence = max((r.sequence for r in acked), default=None)
        applied_version = (self.repo.get_channel_version(channel_id, applied_sequence)
                           if applied_sequence is not None else None)

        expected_next = (applied_sequence or 0) + 1
        missed = [r.sequence for r in unacked if r.sequence < expected_next]
        blocked = bool(unacked) and unacked[0].sequence != expected_next
        # 队首即最早未确认批次；它是当前唯一可投递/可确认的批次
        head = unacked[0] if unacked else None

        return {
            'channel': {
                'channel_id': channel.channel_id,
                'territory': channel.territory,
                'medium': channel.medium,
            },
            'latest_signed_sequence': versions[-1].sequence if versions else 0,
            'applied_sequence': applied_sequence,
            'applied_version_id': applied_version.version_id if applied_version else None,
            'applied_entries': [
                {
                    'screening_id': e.screening_id, 'film_id': e.film_id,
                    'title': e.title, 'starts_at': e.starts_at, 'ends_at': e.ends_at,
                }
                for e in (applied_version.full_page.entries if applied_version else ())
            ],
            'head_pending_sequence': head.sequence if head else None,
            'missing_sequences': missed,
            'blocked': blocked,
            'in_flight': [self.receipt_view(r) for r in unacked],
        }

    def pending_changes(self, channel_id: str) -> list[dict]:
        """待确认变更清单（按批次序号、场次编号排序）。"""
        self._require_channel(channel_id)
        views = []
        for receipt in self.repo.list_receipts(channel_id):
            if receipt.state != ReceiptState.ACKED:
                views.append(self.receipt_view(receipt))
        return views

    def effective_version(self, channel_id: str) -> dict | None:
        """接收端当前实际生效的整版版本。"""
        overview = self.channel_overview(channel_id)
        if overview['applied_version_id'] is None:
            return None
        version = self.repo.get_version(overview['applied_version_id'])
        return {
            'version_id': version.version_id,
            'sequence': version.sequence,
            'signed_at': version.signed_at,
            'signed_by': version.signed_by,
            'entries': [
                {
                    'screening_id': e.screening_id, 'film_id': e.film_id,
                    'title': e.title, 'starts_at': e.starts_at, 'ends_at': e.ends_at,
                }
                for e in version.full_page.entries
            ],
        }

    # ------------------------------------------------------------------ 辅助
    def receipt_view(self, receipt: Receipt) -> dict:
        return {
            'receipt_id': receipt.receipt_id,
            'sequence': receipt.sequence,
            'version_id': receipt.version_id,
            'state': str(receipt.state),
            'idempotency_key': receipt.idempotency_key,
            'attempts': receipt.attempts,
            'last_error': receipt.last_error,
            'created_at': receipt.created_at,
            'acked_at': receipt.acked_at,
            'changes': [change.to_dict() for change in receipt.changes],
        }

    def _require_channel(self, channel_id: str):
        channel = self.repo.get_channel(channel_id)
        if channel is None:
            raise NotFoundError(f'渠道不存在：{channel_id}')
        return channel

    def _require_receipt(self, receipt_id: str) -> Receipt:
        receipt = self.repo.get_receipt(receipt_id)
        if receipt is None:
            raise NotFoundError('批次不存在')
        return receipt
