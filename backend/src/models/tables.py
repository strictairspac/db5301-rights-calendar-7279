from sqlalchemy import (JSON, Column, ForeignKey, Integer, MetaData, String,
                        Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))

# ---- 版本化排片分发 ----
dist_channels = Table('dist_channels', schema,
    Column('channel_id', String(80), primary_key=True),
    Column('territory', String(40), nullable=False),
    Column('medium', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
dist_versions = Table('dist_versions', schema,
    Column('version_id', String(32), primary_key=True),
    Column('channel_id', ForeignKey('dist_channels.channel_id'), nullable=False, index=True),
    Column('sequence', Integer, nullable=False),
    Column('full_page', JSON, nullable=False),
    Column('parent_version_id', String(32)),
    Column('signed_by', String(40), nullable=False),
    Column('signed_at', String(32), nullable=False),
    Column('created_at', String(32), nullable=False),
    # 渠道内签署序号唯一，保证同一渠道按签署顺序排列
    UniqueConstraint('channel_id', 'sequence', name='uq_dist_version_channel_sequence'))
dist_receipts = Table('dist_receipts', schema,
    Column('receipt_id', String(32), primary_key=True),
    Column('channel_id', ForeignKey('dist_channels.channel_id'), nullable=False, index=True),
    Column('version_id', ForeignKey('dist_versions.version_id'), nullable=False),
    Column('sequence', Integer, nullable=False),
    Column('state', String(20), nullable=False, index=True),
    Column('changes', JSON, nullable=False),
    Column('idempotency_key', String(120), nullable=False, unique=True),
    Column('attempts', Integer, nullable=False, server_default='0'),
    Column('last_error', Text),
    Column('base_snapshot_version_id', String(32)),
    Column('applied_sequence', Integer),
    Column('created_at', String(32), nullable=False),
    Column('acked_at', String(32)),
    UniqueConstraint('channel_id', 'sequence', name='uq_dist_receipt_channel_sequence'))
