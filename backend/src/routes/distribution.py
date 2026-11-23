"""版本化排片分发的 HTTP 路由。

- 签署、重试、重挂需要经办员（发布组）。
- 回执确认是接收端动作，登录用户均可调用，靠幂等键保证安全。
- 读取/核对对所有登录角色开放。
"""

from fastapi import APIRouter, Depends

from src.distribution.errors import DistributionError
from src.schemas.distribution import ConfirmForm, DeliveryAttemptForm, SignForm
from src.services.distribution_app import get_distribution_service, raise_http
from src.services.security import current_user, operator

router = APIRouter(prefix='/distribution', tags=['distribution'])


def _version_view(version):
    return {
        'version_id': version.version_id,
        'channel_id': version.channel_id,
        'sequence': version.sequence,
        'parent_version_id': version.parent_version_id,
        'signed_by': version.signed_by,
        'signed_at': version.signed_at,
        'entry_count': len(version.full_page.entries),
    }


@router.get('/channels')
def list_channels(_=Depends(current_user)):
    service = get_distribution_service()
    return [
        {'channel_id': c.channel_id, 'territory': c.territory, 'medium': c.medium}
        for c in service.repo.list_channels()
    ]


@router.post('/versions', status_code=201)
def sign_version(form: SignForm, actor=Depends(operator)):
    service = get_distribution_service()
    try:
        version, receipt = service.sign_version(
            form.territory, form.medium,
            [entry.model_dump() for entry in form.entries], actor)
    except DistributionError as exc:
        raise_http(exc)
    return {
        'version': _version_view(version),
        'receipt': service.receipt_view(receipt),
    }


@router.get('/channels/{channel_id}/deliveries/next')
def next_delivery(channel_id: str, _=Depends(current_user)):
    service = get_distribution_service()
    try:
        receipt = service.next_deliverable(channel_id)
    except DistributionError as exc:
        raise_http(exc)
    return service.receipt_view(receipt) if receipt else None


@router.post('/receipts/confirm')
def confirm_receipt(form: ConfirmForm, _=Depends(current_user)):
    service = get_distribution_service()
    try:
        receipt = service.confirm(form.idempotency_key)
    except DistributionError as exc:
        raise_http(exc)
    return service.receipt_view(receipt)


@router.post('/receipts/{receipt_id}/attempts', status_code=200)
def record_attempt(receipt_id: str, form: DeliveryAttemptForm,
                   actor=Depends(operator)):
    service = get_distribution_service()
    try:
        receipt = service.record_delivery_attempt(receipt_id, form.error)
    except DistributionError as exc:
        raise_http(exc)
    return service.receipt_view(receipt)


@router.post('/receipts/{receipt_id}/rearm')
def rearm_receipt(receipt_id: str, actor=Depends(operator)):
    service = get_distribution_service()
    try:
        receipt = service.rearm(receipt_id)
    except DistributionError as exc:
        raise_http(exc)
    return service.receipt_view(receipt)


@router.get('/channels/{channel_id}/overview')
def channel_overview(channel_id: str, _=Depends(current_user)):
    service = get_distribution_service()
    try:
        return service.channel_overview(channel_id)
    except DistributionError as exc:
        raise_http(exc)


@router.get('/channels/{channel_id}/pending')
def pending_changes(channel_id: str, _=Depends(current_user)):
    service = get_distribution_service()
    try:
        return service.pending_changes(channel_id)
    except DistributionError as exc:
        raise_http(exc)


@router.get('/channels/{channel_id}/effective')
def effective_version(channel_id: str, _=Depends(current_user)):
    service = get_distribution_service()
    try:
        return service.effective_version(channel_id)
    except DistributionError as exc:
        raise_http(exc)


@router.get('/channels/{channel_id}/receipts')
def list_channel_receipts(channel_id: str, _=Depends(current_user)):
    service = get_distribution_service()
    try:
        return [service.receipt_view(r)
                for r in service.repo.list_receipts(channel_id)]
    except DistributionError as exc:
        raise_http(exc)
