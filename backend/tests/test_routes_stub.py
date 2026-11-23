"""路由层行为测试：用最小桩件替代 fastapi/pydantic，无需第三方库。

直接调用路由处理函数（传入内存仓储支撑的服务），验证状态码映射、
权限装饰后的核心行为以及请求/响应形状。生产环境使用真实 FastAPI。
运行：python3 tests/test_routes_stub.py
"""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---- 桩 fastapi ----
fastapi = types.ModuleType('fastapi')


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class APIRouter:
    def __init__(self, *a, **k):
        self.routes = []
        self.prefix = k.get('prefix', '')
        self.tags = k.get('tags', [])

    def _add(self, method, path, **k):
        def deco(fn):
            self.routes.append((method, path, fn, k))
            return fn
        return deco

    get = lambda self, path, **k: self._add('GET', path, **k)
    post = lambda self, path, **k: self._add('POST', path, **k)


def Depends(fn):
    return ('dep', getattr(fn, '__name__', str(fn)))


fastapi.APIRouter = APIRouter
fastapi.Depends = Depends
fastapi.HTTPException = HTTPException

pydantic = types.ModuleType('pydantic')


class BaseModel:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def model_dump(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}


def Field(*a, **k):
    return a[0] if a else k.get('default')


pydantic.BaseModel = BaseModel
pydantic.Field = Field

# 桩 src.services.security
security = types.ModuleType('src.services.security')
security.current_user = lambda: {'sub': 'tester', 'role': 'operator'}
security.operator = lambda: 'operator'

# 桩 src.services.distribution_app
app_pkg = types.ModuleType('src.services.distribution_app')
from src.distribution.repository import InMemoryRepository  # noqa: E402
from src.distribution.service import DistributionService  # noqa: E402
from src.distribution.errors import DistributionError  # noqa: E402

_shared_service = DistributionService(InMemoryRepository())
app_pkg.get_distribution_service = lambda: _shared_service


def raise_http(error):
    from src.distribution.errors import ConflictError, NotFoundError, ValidationError
    if isinstance(error, ValidationError):
        raise HTTPException(422, str(error))
    if isinstance(error, NotFoundError):
        raise HTTPException(404, str(error))
    if isinstance(error, ConflictError):
        raise HTTPException(409, str(error))
    raise HTTPException(400, str(error))


app_pkg.raise_http = raise_http

sys.modules['fastapi'] = fastapi
sys.modules['pydantic'] = pydantic
sys.modules['src.services.security'] = security
sys.modules['src.services.distribution_app'] = app_pkg

from src.routes import distribution as routes  # noqa: E402
from src.schemas.distribution import (  # noqa: E402
    ConfirmForm, DeliveryAttemptForm, EntryForm, SignForm,
)


def entry(sid, start='19:00', end='20:30'):
    return {'screening_id': sid, 'film_id': 'F-01', 'title': '潮线以北',
            'starts_at': f'2026-12-04T{start}+08:00',
            'ends_at': f'2026-12-04T{end}+08:00'}


def sign_form(territory, medium, entries):
    return SignForm(territory=territory, medium=medium,
                    entries=[EntryForm(**row) for row in entries])


class RouteBehaviourTest(unittest.TestCase):
    def setUp(self):
        _shared_service.repo = InMemoryRepository()
        self.service = _shared_service

    def _handlers(self):
        prefix = routes.router.prefix
        return {prefix + path: fn for method, path, fn, k in routes.router.routes}

    def test_sign_and_confirm_and_idempotent_retry(self):
        h = self._handlers()
        form = sign_form('上海', '影院', [entry('P-1')])
        created = h['/distribution/versions'](form)
        self.assertEqual(created['version']['sequence'], 1)
        self.assertEqual(created['receipt']['state'], 'pending')
        key = created['receipt']['idempotency_key']

        # 下一批可读取
        nxt = h['/distribution/channels/{channel_id}/deliveries/next']('上海|影院')
        self.assertEqual(nxt['idempotency_key'], key)

        ack = h['/distribution/receipts/confirm'](ConfirmForm(idempotency_key=key))
        self.assertEqual(ack['state'], 'acked')
        # 回执丢失重试：相同键，返回同一记录且无副作用
        retry = h['/distribution/receipts/confirm'](ConfirmForm(idempotency_key=key))
        self.assertEqual(retry['receipt_id'], ack['receipt_id'])
        self.assertEqual(retry['acked_at'], ack['acked_at'])
        self.assertEqual(len(self.service.repo.list_receipts()), 1)

    def test_blocking_returns_409_and_unknown_key_404(self):
        h = self._handlers()
        h['/distribution/versions'](sign_form('上海', '影院', [entry('P-1')]))
        second = h['/distribution/versions'](
            sign_form('上海', '影院', [entry('P-1'), entry('P-2')]))
        with self.assertRaises(HTTPException) as ctx:
            h['/distribution/receipts/confirm'](
                ConfirmForm(idempotency_key=second['receipt']['idempotency_key']))
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(HTTPException) as ctx:
            h['/distribution/receipts/confirm'](ConfirmForm(idempotency_key='x#v9'))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_validation_error_maps_to_422(self):
        h = self._handlers()
        bad = dict(entry('P-1'))
        bad['starts_at'] = '2026-12-04T19:00'  # 缺时区
        with self.assertRaises(HTTPException) as ctx:
            h['/distribution/versions'](sign_form('*', '影院', [bad]))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_overview_and_other_channel_independent(self):
        h = self._handlers()
        first = h['/distribution/versions'](
            sign_form('上海', '影院', [entry('P-1')]))
        h['/distribution/receipts/confirm'](
            ConfirmForm(idempotency_key=first['receipt']['idempotency_key']))
        h['/distribution/versions'](
            sign_form('上海', '影院', [entry('P-1'), entry('P-2')]))
        # 杭州独立渠道
        hz = h['/distribution/versions'](
            sign_form('杭州', '线上', [entry('H-1')]))
        h['/distribution/receipts/confirm'](
            ConfirmForm(idempotency_key=hz['receipt']['idempotency_key']))

        sh = h['/distribution/channels/{channel_id}/overview']('上海|影院')
        self.assertEqual(sh['applied_sequence'], 1)
        self.assertEqual(sh['latest_signed_sequence'], 2)
        self.assertEqual(sh['head_pending_sequence'], 2)
        self.assertEqual([e['screening_id'] for e in sh['applied_entries']], ['P-1'])

        channels = h['/distribution/channels']()
        self.assertEqual({c['channel_id'] for c in channels}, {'上海|影院', '杭州|线上'})

        # 投递失败计数与 rearm
        pending = h['/distribution/channels/{channel_id}/deliveries/next']('上海|影院')
        for _ in range(_shared_service.max_attempts):
            dead = h['/distribution/receipts/{receipt_id}/attempts'](
                pending['receipt_id'], DeliveryAttemptForm(error='timeout'))
        self.assertEqual(dead['state'], 'dead')
        rearmed = h['/distribution/receipts/{receipt_id}/rearm'](pending['receipt_id'])
        self.assertEqual(rearmed['state'], 'pending')


if __name__ == '__main__':
    unittest.main()
