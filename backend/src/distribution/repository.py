"""仓储端口定义与内存实现。

生产环境由 SQLRepository（services/distribution_repo.py）实现同一接口；
单元测试使用 InMemoryRepository，无需任何第三方依赖。
所有方法的并发/事务保证由具体实现负责（SQL 端用行锁与唯一约束）。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Protocol

from .models import Channel, FullPage, Receipt, SignedVersion


class Repository(Protocol):
    @contextmanager
    def transactional(self):
        """将一次状态机写操作包进单个数据库事务；内存实现为空上下文。"""
        ...

    def lock_channel(self, channel_id: str) -> None:
        """同渠道写操作串行点（PostgreSQL 行锁；内存实现为空操作）。"""
        ...

    # ---- 渠道 ----
    def upsert_channel(self, channel: Channel) -> None: ...
    def get_channel(self, channel_id: str) -> Channel | None: ...
    def list_channels(self) -> list[Channel]: ...

    # ---- 签署版本 ----
    def next_sequence(self, channel_id: str) -> int:
        """返回该渠道下一个签署序号（首版为 1）。"""
        ...

    def save_version(self, version: SignedVersion) -> None: ...
    def get_version(self, version_id: str) -> SignedVersion | None: ...
    def get_channel_version(self, channel_id: str, sequence: int) -> SignedVersion | None: ...
    def latest_version(self, channel_id: str) -> SignedVersion | None: ...
    def list_versions(self, channel_id: str) -> list[SignedVersion]: ...

    # ---- 批次回执 ----
    def save_receipt(self, receipt: Receipt) -> None: ...
    def update_receipt(self, receipt: Receipt) -> None: ...
    def get_receipt(self, receipt_id: str) -> Receipt | None: ...
    def find_receipt_by_idempotency_key(self, key: str) -> Receipt | None: ...
    def find_receipt(self, channel_id: str, sequence: int) -> Receipt | None: ...
    def list_receipts(self, channel_id: str | None = None) -> list[Receipt]: ...


class InMemoryRepository:
    """进程内仓储，语义与 SQL 实现对齐：同一渠道按序号排序。"""

    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}
        self._versions: dict[str, SignedVersion] = {}
        self._receipts: dict[str, Receipt] = {}
        self._idem: dict[str, str] = {}

    @contextmanager
    def transactional(self):
        # 内存实现无需事务；异常自然向上抛出即可。
        yield

    def lock_channel(self, channel_id: str) -> None:
        # 内存实现无并发，行锁为空操作。
        return None

    def upsert_channel(self, channel: Channel) -> None:
        self._channels[channel.channel_id] = channel

    def get_channel(self, channel_id: str) -> Channel | None:
        return self._channels.get(channel_id)

    def list_channels(self) -> list[Channel]:
        return [self._channels[k] for k in sorted(self._channels)]

    def next_sequence(self, channel_id: str) -> int:
        used = [v.sequence for v in self._versions.values()
                if v.channel_id == channel_id]
        return (max(used) + 1) if used else 1

    def save_version(self, version: SignedVersion) -> None:
        self._versions[version.version_id] = version

    def get_version(self, version_id: str) -> SignedVersion | None:
        return self._versions.get(version_id)

    def get_channel_version(self, channel_id: str, sequence: int) -> SignedVersion | None:
        for version in self._versions.values():
            if version.channel_id == channel_id and version.sequence == sequence:
                return version
        return None

    def latest_version(self, channel_id: str) -> SignedVersion | None:
        rows = self.list_versions(channel_id)
        return rows[-1] if rows else None

    def list_versions(self, channel_id: str) -> list[SignedVersion]:
        rows = [v for v in self._versions.values() if v.channel_id == channel_id]
        return sorted(rows, key=lambda v: v.sequence)

    def save_receipt(self, receipt: Receipt) -> None:
        self._receipts[receipt.receipt_id] = receipt
        self._idem[receipt.idempotency_key] = receipt.receipt_id

    def update_receipt(self, receipt: Receipt) -> None:
        self._receipts[receipt.receipt_id] = receipt

    def get_receipt(self, receipt_id: str) -> Receipt | None:
        return self._receipts.get(receipt_id)

    def find_receipt_by_idempotency_key(self, key: str) -> Receipt | None:
        rid = self._idem.get(key)
        return self._receipts.get(rid) if rid else None

    def find_receipt(self, channel_id: str, sequence: int) -> Receipt | None:
        for receipt in self._receipts.values():
            if receipt.channel_id == channel_id and receipt.sequence == sequence:
                return receipt
        return None

    def list_receipts(self, channel_id: str | None = None) -> list[Receipt]:
        rows = self._receipts.values()
        if channel_id is not None:
            rows = [r for r in rows if r.channel_id == channel_id]
        return sorted(rows, key=lambda r: (r.channel_id, r.sequence))
