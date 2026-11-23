"""整版排片构建与相邻版本 diff。

接收端只拿到相对上一版的增量：新增 / 改期 / 取消。
整版仍完整保存用于发布页面核对与重放。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .errors import ValidationError
from .models import Change, ChangeOp, FullPage, Channel, ScheduleEntry

_TIMESTAMP = re.compile(
    r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::00)?'
    r'(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])'
)
_TEXT_FIELDS = ('screening_id', 'film_id', 'title', 'starts_at', 'ends_at')


def parse_minute(value: str, label: str) -> datetime:
    """解析带时区偏移的整分钟时间，返回 UTC datetime。"""
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValidationError(f'{label}须使用带时区的 ISO 8601 整分钟')
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f'{label}的日期时间无效') from exc
    if moment.tzinfo is None:
        raise ValidationError(f'{label}必须带时区偏移')
    if moment.second != 0 or moment.microsecond != 0:
        raise ValidationError(f'{label}必须为整分钟（秒值为零）')
    return moment.astimezone(timezone.utc)


def _require_text(record: dict, field_name: str, row_no: int) -> str:
    value = record.get(field_name)
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValidationError(f'排片第 {row_no} 行的{field_name}须为有效非空文本')
    return value.strip()


def build_full_page(territory: str, medium: str, raw_entries: list[dict]) -> FullPage:
    """校验并构造一个渠道的整版排片。

    校验项：地域/媒介为具体值（不允许 *）、字段齐全、编号唯一、
    时间为带偏移整分钟且结束晚于开始。
    """
    if not isinstance(territory, str) or not territory.strip() or territory == '*':
        raise ValidationError('地域须为具体代码，不能使用通配符')
    if not isinstance(medium, str) or not medium.strip() or medium == '*':
        raise ValidationError('媒介须为具体代码，不能使用通配符')
    if not isinstance(raw_entries, list):
        raise ValidationError('排片场次须为列表')

    channel = Channel.make(territory.strip(), medium.strip())
    entries: list[ScheduleEntry] = []
    seen: set[str] = set()
    for row_no, record in enumerate(raw_entries, start=1):
        if not isinstance(record, dict):
            raise ValidationError(f'排片第 {row_no} 行须为对象')
        values = {name: _require_text(record, name, row_no) for name in _TEXT_FIELDS}
        if values['screening_id'] in seen:
            raise ValidationError(
                f"排片第 {row_no} 行场次编号重复（{values['screening_id']}）")
        seen.add(values['screening_id'])
        start = parse_minute(values['starts_at'], f'排片第 {row_no} 行开始时间')
        end = parse_minute(values['ends_at'], f'排片第 {row_no} 行结束时间')
        if end <= start:
            raise ValidationError(f'排片第 {row_no} 行结束时间须晚于开始时间')
        entries.append(ScheduleEntry(
            screening_id=values['screening_id'], film_id=values['film_id'],
            title=values['title'],
            starts_at=start.isoformat(timespec='minutes'),
            ends_at=end.isoformat(timespec='minutes'),
        ))
    return FullPage(channel=channel, entries=tuple(entries))


def diff_versions(previous: FullPage | None, current: FullPage) -> tuple[Change, ...]:
    """计算 current 相对 previous 的增量。

    - 首版（previous 为 None）：全部场次为 added。
    - 同编号且业务字段一致：无变更，不出现在增量里。
    - 同编号但影片/片名/时间任一变化：rescheduled，携带新旧场次。
    - 旧版有、新版无：cancelled。
    - 新版有、旧版无：added。
    两个整版必须属于同一渠道。
    """
    if previous is not None and previous.channel.channel_id != current.channel.channel_id:
        raise ValidationError('只能对同一渠道的相邻版本计算增量')

    old = previous.index() if previous is not None else {}
    new = current.index()
    changes: list[Change] = []
    cid = current.channel.channel_id

    if previous is None:
        for entry in current.entries:
            changes.append(Change(cid, ChangeOp.ADDED, entry.screening_id, entry=entry))
        return tuple(changes)

    for screening_id, entry in new.items():
        prior = old.get(screening_id)
        if prior is None:
            changes.append(Change(cid, ChangeOp.ADDED, screening_id, entry=entry))
        elif prior.business_key() != entry.business_key():
            changes.append(Change(cid, ChangeOp.RESCHEDULED, screening_id,
                                  entry=entry, previous=prior))
    for screening_id, prior in old.items():
        if screening_id not in new:
            changes.append(Change(cid, ChangeOp.CANCELLED, screening_id, previous=prior))
    # 稳定排序便于核对与测试
    changes.sort(key=lambda c: (str(c.op), c.screening_id))
    return tuple(changes)
