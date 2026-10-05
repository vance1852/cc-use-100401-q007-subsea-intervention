"""水下干预闭环的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS intervention_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN (
        'isolation-engineer','intervention-supervisor','marine-supervisor',
        'diving-supervisor','onscene-commander','auditor'
    )),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 作业主表：状态机只允许沿签署闸门顺序推进。
CREATE TABLE IF NOT EXISTS intervention_jobs (
    job_id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'draft','sealed','isolated','active','paused','completed','cancelled'
    )),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    frozen_sha256 TEXT NOT NULL,
    trigger_evidence_id TEXT NOT NULL,
    current_gate TEXT,
    responsible_user_id TEXT REFERENCES intervention_users(user_id),
    sealed_by TEXT REFERENCES intervention_users(user_id),
    sealed_at TEXT,
    cancelled_by TEXT REFERENCES intervention_users(user_id),
    cancelled_at TEXT,
    cancel_reason TEXT,
    created_by TEXT NOT NULL REFERENCES intervention_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON intervention_jobs(state);

-- 草稿作业包暂存：创建时写入，密封时据此生成不可变版本快照。
CREATE TABLE IF NOT EXISTS job_drafts (
    job_id TEXT PRIMARY KEY REFERENCES intervention_jobs(job_id),
    package_json TEXT NOT NULL,
    updated_by TEXT NOT NULL REFERENCES intervention_users(user_id),
    updated_at TEXT NOT NULL
);

-- 每次修订密封生成一个不可变版本快照；迟到数据触发新版本，永不覆盖旧版本。
CREATE TABLE IF NOT EXISTS job_version_snapshots (
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    version INTEGER NOT NULL CHECK(version > 0),
    frozen_json TEXT NOT NULL,
    frozen_sha256 TEXT NOT NULL CHECK(length(frozen_sha256)=64),
    supersedes_version INTEGER,
    sealed_by TEXT NOT NULL REFERENCES intervention_users(user_id),
    sealed_at TEXT NOT NULL,
    PRIMARY KEY(job_id, version),
    UNIQUE(frozen_sha256)
);

CREATE TABLE IF NOT EXISTS job_equipment (
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    equipment_id TEXT NOT NULL,
    well_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    design_pressure_bar TEXT NOT NULL,
    PRIMARY KEY(job_id, version, equipment_id),
    FOREIGN KEY(job_id, version) REFERENCES job_version_snapshots(job_id, version)
);

CREATE TABLE IF NOT EXISTS job_evidence (
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    metric TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    PRIMARY KEY(job_id, version, evidence_id),
    FOREIGN KEY(job_id, version) REFERENCES job_version_snapshots(job_id, version)
);

CREATE TABLE IF NOT EXISTS job_barriers (
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    barrier_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    verification_method TEXT NOT NULL,
    PRIMARY KEY(job_id, version, barrier_id),
    FOREIGN KEY(job_id, version) REFERENCES job_version_snapshots(job_id, version)
);

CREATE TABLE IF NOT EXISTS job_steps (
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    step_no INTEGER NOT NULL,
    title TEXT NOT NULL,
    instruction TEXT NOT NULL,
    estimated_minutes INTEGER NOT NULL,
    requires_rov INTEGER NOT NULL CHECK(requires_rov IN (0,1)),
    requires_diving INTEGER NOT NULL CHECK(requires_diving IN (0,1)),
    PRIMARY KEY(job_id, version, step_no),
    FOREIGN KEY(job_id, version) REFERENCES job_version_snapshots(job_id, version)
);

CREATE TABLE IF NOT EXISTS job_qualifications (
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    person_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    qualification TEXT NOT NULL,
    certificate_ref TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    PRIMARY KEY(job_id, version, person_id),
    FOREIGN KEY(job_id, version) REFERENCES job_version_snapshots(job_id, version)
);

CREATE TABLE IF NOT EXISTS job_components (
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    component_id TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    name TEXT NOT NULL,
    resource_ref TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY(job_id, version, component_id),
    FOREIGN KEY(job_id, version) REFERENCES job_version_snapshots(job_id, version)
);

CREATE TABLE IF NOT EXISTS job_windows (
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    window_id TEXT NOT NULL,
    opens_at TEXT NOT NULL,
    closes_at TEXT NOT NULL,
    max_wave_height_m TEXT NOT NULL,
    max_current_ms TEXT NOT NULL,
    max_wind_ms TEXT NOT NULL,
    forecast_ref TEXT NOT NULL,
    PRIMARY KEY(job_id, version, window_id),
    FOREIGN KEY(job_id, version) REFERENCES job_version_snapshots(job_id, version)
);

CREATE TABLE IF NOT EXISTS job_recovery_plan (
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    action_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    resource_ref TEXT NOT NULL,
    owner_role TEXT NOT NULL,
    PRIMARY KEY(job_id, version, action_id),
    FOREIGN KEY(job_id, version) REFERENCES job_version_snapshots(job_id, version)
);

-- 屏障状态随闸门建立；每行记录在哪个版本、由谁、何时、用什么证据确认。
CREATE TABLE IF NOT EXISTS barrier_states (
    job_id TEXT NOT NULL,
    barrier_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','established','verified','released')),
    established_at TEXT,
    established_by TEXT REFERENCES intervention_users(user_id),
    gate TEXT,
    evidence_ref TEXT,
    released_at TEXT,
    released_by TEXT REFERENCES intervention_users(user_id),
    PRIMARY KEY(job_id, barrier_id),
    FOREIGN KEY(job_id) REFERENCES intervention_jobs(job_id)
);

-- 闸门签署：顺序、职责、与前一签署人不同都在服务层强制；表本身只追加不更新。
CREATE TABLE IF NOT EXISTS gate_signoffs (
    signoff_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    version INTEGER NOT NULL,
    gate TEXT NOT NULL CHECK(gate IN ('isolation','start','pause','resume','complete')),
    seq INTEGER NOT NULL,
    signer_id TEXT NOT NULL REFERENCES intervention_users(user_id),
    signer_role TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    note TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    new_revision INTEGER NOT NULL,
    UNIQUE(job_id, gate, seq)
);

CREATE INDEX IF NOT EXISTS idx_signoffs_job ON gate_signoffs(job_id, signoff_id);

-- 暂停/恢复可成对重复：周期闸门单独追加，绝不复用阶段闸门序号。
CREATE TABLE IF NOT EXISTS gate_cycle_signoffs (
    signoff_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    version INTEGER NOT NULL,
    gate TEXT NOT NULL CHECK(gate IN ('pause','resume')),
    cycle_no INTEGER NOT NULL CHECK(cycle_no > 0),
    signer_id TEXT NOT NULL REFERENCES intervention_users(user_id),
    signer_role TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    note TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    new_revision INTEGER NOT NULL,
    UNIQUE(job_id, gate, cycle_no)
);

CREATE INDEX IF NOT EXISTS idx_cycle_signoffs_job ON gate_cycle_signoffs(job_id, signoff_id);

-- 资源目录与承诺：resource_ref 在同一重叠时间窗内只能被一个作业承诺。
CREATE TABLE IF NOT EXISTS resource_registry (
    resource_ref TEXT PRIMARY KEY,
    resource_kind TEXT NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_commitments (
    commitment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_ref TEXT NOT NULL REFERENCES resource_registry(resource_ref),
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    component_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('committed','released')),
    committed_at TEXT NOT NULL,
    released_at TEXT,
    released_by TEXT REFERENCES intervention_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_commitments_resource
ON resource_commitments(resource_ref, state);

-- 当前仍占位的承诺：主键即资源本身，竞争双方只有一个 INSERT 能成功。
CREATE TABLE IF NOT EXISTS active_resource_commitments (
    resource_ref TEXT PRIMARY KEY REFERENCES resource_registry(resource_ref),
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    component_id TEXT NOT NULL,
    committed_at TEXT NOT NULL
);

-- 海况读数：只追加，闸门评估永远取最新一条，历史读数不被改写。
CREATE TABLE IF NOT EXISTS sea_state_readings (
    reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    wave_height_m TEXT NOT NULL,
    current_ms TEXT NOT NULL,
    wind_ms TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES intervention_users(user_id),
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sea_state_job
ON sea_state_readings(job_id, reading_id);

-- 迟到遥测：只追加。密封后到达的事实进隔离表，绝不改写已签署版本。
CREATE TABLE IF NOT EXISTS late_telemetry (
    telemetry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    equipment_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    metric TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('quarantined','incorporated','rejected')),
    incorporated_version INTEGER,
    recorded_by TEXT NOT NULL REFERENCES intervention_users(user_id),
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_late_telemetry_job
ON late_telemetry(job_id, telemetry_id);

-- 恢复动作实例：随密封版本生成，取消/完工后必须逐个闭环。
CREATE TABLE IF NOT EXISTS recovery_actions (
    job_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    resource_ref TEXT NOT NULL,
    owner_role TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','done')),
    completed_by TEXT REFERENCES intervention_users(user_id),
    completed_at TEXT,
    completion_note TEXT,
    PRIMARY KEY(job_id, action_id),
    FOREIGN KEY(job_id) REFERENCES intervention_jobs(job_id)
);

CREATE TABLE IF NOT EXISTS intervention_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS intervention_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intervention_audit_entity
ON intervention_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=check_same_thread
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
