from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from src.config.settings import IS_MYSQL, engine
from src.constants.workflow import Role
from src.models.tables import audit, cases, evidence, runs, schema, users
from src.services.domain import SPEC
from src.services.distribution_app import get_distribution_service
from src.services.security import hash_password
from src.utils.time import utc_time


def _entry(screening_id, film_id, title, start, end):
    day = '2026-12-04T'
    return {'screening_id': screening_id, 'film_id': film_id, 'title': title,
            'starts_at': f'{day}{start}+08:00', 'ends_at': f'{day}{end}+08:00'}


def seed_distribution():
    """幂等生成版本化排片分发演示：上海影院 v1 已生效、v2 待确认；杭州线上 v1 待确认。"""
    service = get_distribution_service()
    if service.repo.list_channels():
        return
    shanghai = [
        _entry('P-101', 'F-01', '潮线以北', '19:00', '20:30'),
        _entry('P-102', 'F-02', '雾港来信', '20:00', '21:00'),
    ]
    _, r1 = service.sign_version('上海', '影院', shanghai, 'demo')
    service.confirm(r1.idempotency_key)
    # 第二版：P-102 改期、新增 P-105、P-101 保留（不在增量里）
    service.sign_version('上海', '影院', [
        _entry('P-101', 'F-01', '潮线以北', '19:00', '20:30'),
        _entry('P-102', 'F-02', '雾港来信', '20:30', '21:30'),
        _entry('P-105', 'F-04', '夜航西飞', '21:45', '23:15'),
    ], 'demo')
    # 独立渠道，不受上海待确认批次影响
    service.sign_version('杭州', '线上', [
        _entry('H-201', 'F-03', '白鹭返航', '20:00', '21:00'),
    ], 'demo')


def keep_existing(conn, table, key, values):
    if IS_MYSQL:
        statement = mysql_insert(table).values(**values).on_duplicate_key_update(id=table.c.id)
    else:
        statement = postgres_insert(table).values(**values).on_conflict_do_nothing(index_elements=[key])
    conn.execute(statement)


def seed():
    with engine.begin() as conn:
        for role in Role:
            keep_existing(conn, users, 'username', {'username': role.value, 'role': role.value,
                'password_hash': hash_password('Demo-Walkthrough-2026')})
        keep_existing(conn, cases, 'slug', {'slug': 'demo', 'title': SPEC['name'],
            'payload': SPEC['sample'], 'author': 'demo', 'created_at': utc_time()})
        counts = {table.name: conn.execute(select(func.count()).select_from(table)).scalar_one()
                  for table in (users, cases, runs, evidence, audit)}
    seed_distribution()
    return counts


if __name__ == '__main__':
    schema.create_all(engine)
    print(seed())
