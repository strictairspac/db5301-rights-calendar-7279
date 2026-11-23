"""Versioned schedule distribution domain.

发布组按「地域 + 媒介」签署整版排片，接收端只拿到相对上一版的增量；
同一渠道严格按签署顺序投递，未确认批次挡住后续批次；回执重试幂等，
已生效版本与待确认变更可在发布页面核对。

本包只依赖 Python 标准库，数据库/HTTP 适配层位于 services、routes 目录。
"""

from .errors import DistributionError, ConflictError, NotFoundError, ValidationError
from .models import (
    Change,
    ChangeOp,
    Channel,
    FullPage,
    Receipt,
    ReceiptState,
    ScheduleEntry,
    SignedVersion,
)
from .diffing import build_full_page, diff_versions
from .service import DistributionService

__all__ = [
    'DistributionError', 'ConflictError', 'NotFoundError', 'ValidationError',
    'Change', 'ChangeOp', 'Channel', 'FullPage', 'Receipt', 'ReceiptState',
    'ScheduleEntry', 'SignedVersion', 'build_full_page', 'diff_versions',
    'DistributionService',
]
