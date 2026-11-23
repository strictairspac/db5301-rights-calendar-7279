"""领域模型：渠道、签署版本、增量变更、批次回执。

时间统一存 UTC ISO 8601 整分钟字符串，展示层再转 UTC+08:00。
所有结构都是普通 dataclass，可直接序列化为 JSON。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import StrEnum
from typing import Any


class ChangeOp(StrEnum):
    """增量场次的操作类型。"""
    ADDED = 'added'        # 上一版没有、本版新增
    RESCHEDULED = 'rescheduled'  # 场次存在但时间/属性改期
    CANCELLED = 'cancelled'      # 上一版存在、本版移除


class ReceiptState(StrEnum):
    """批次在接收端的生命周期状态。"""
    PENDING = 'pending'      # 已投递，等待接收端确认
    ACKED = 'acked'          # 接收端落库并回执
    DEAD = 'dead'           # 超过投递次数仍未确认（保留可人工处理，不自动跳过）


@dataclass(frozen=True)
class Channel:
    """投递渠道 = 地域 + 媒介。同一渠道内严格按签署顺序处理。"""
    channel_id: str
    territory: str
    medium: str

    @staticmethod
    def make(territory: str, medium: str) -> 'Channel':
        return Channel(channel_id=f'{territory}|{medium}', territory=territory, medium=medium)


@dataclass(frozen=True)
class ScheduleEntry:
    """整版排片中的一场。场次编号在渠道内唯一、跨版本稳定。"""
    screening_id: str
    film_id: str
    title: str
    starts_at: str   # 带偏移 ISO 8601 整分钟
    ends_at: str

    def business_key(self) -> tuple[str, str, str, str]:
        """除编号外用于判断是否「改期/改动」的业务字段。"""
        return self.film_id, self.title, self.starts_at, self.ends_at


@dataclass(frozen=True)
class FullPage:
    """一次签署时某渠道的整版排片快照。"""
    channel: Channel
    entries: tuple[ScheduleEntry, ...]

    def index(self) -> dict[str, ScheduleEntry]:
        return {entry.screening_id: entry for entry in self.entries}


@dataclass(frozen=True)
class Change:
    """相对上一签署版本的单场增量。"""
    channel_id: str
    op: ChangeOp
    screening_id: str
    entry: ScheduleEntry | None = None        # added/rescheduled 时为新场次
    previous: ScheduleEntry | None = None     # rescheduled/cancelled 时为旧场次

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data['op'] = str(self.op)
        return data


@dataclass(frozen=True)
class SignedVersion:
    """发布组签署的一版整版排片（一个渠道一条）。"""
    version_id: str
    channel_id: str
    sequence: int                     # 渠道内单调递增的签署序号，从 1 开始
    full_page: FullPage
    parent_version_id: str | None     # 上一版；首版为 None
    signed_by: str
    signed_at: str


@dataclass
class Receipt:
    """一次批次投递的回执记录。

    幂等键 idempotency_key 由渠道+版本+批次序号确定，回执重试携带同一键，
    接收端重复确认不会重复生效。
    """
    receipt_id: str
    channel_id: str
    version_id: str
    sequence: int                     # 渠道内批次序号，等于签署版本序号
    state: ReceiptState
    changes: tuple[Change, ...]
    idempotency_key: str
    attempts: int = 0
    last_error: str | None = None
    created_at: str = ''
    acked_at: str | None = None
    # 接收端落库后该渠道实际生效到的版本序号；未确认时为 None
    applied_sequence: int | None = None
    # 本批次开始读取时所基于的版本快照，读取中途再签新版不改变本批次内容
    base_snapshot_version_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def pending(self) -> bool:
        return self.state == ReceiptState.PENDING
