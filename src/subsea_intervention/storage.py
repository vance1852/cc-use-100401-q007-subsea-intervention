"""水下干预闭环服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intervention_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('planner','isolation_officer','supervisor','resource_controller','telemetry','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wells (
    well_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    field_name TEXT NOT NULL,
    water_depth_m TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'anomaly' CHECK (state IN ('anomaly','under_intervention','normal','shut_in')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wellhead_equipment (
    equipment_id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    kind TEXT NOT NULL,
    serial_no TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    state TEXT NOT NULL DEFAULT 'in_service' CHECK (state IN ('in_service','isolated','faulted')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intervention_jobs (
    job_id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    title TEXT NOT NULL,
    anomaly_summary TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 999),
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK (state IN ('draft','frozen','isolated','in_progress','paused','completed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    current_version_no INTEGER,
    created_by TEXT NOT NULL REFERENCES intervention_users(user_id),
    created_at TEXT NOT NULL,
    cancelled_at TEXT,
    cancel_reason TEXT
);

CREATE TABLE IF NOT EXISTS fault_evidence (
    evidence_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    attached_by TEXT NOT NULL REFERENCES intervention_users(user_id),
    attached_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_versions (
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    plan_json TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    frozen_by TEXT NOT NULL REFERENCES intervention_users(user_id),
    frozen_at TEXT NOT NULL,
    PRIMARY KEY (job_id, version_no)
);

CREATE TABLE IF NOT EXISTS job_version_equipment (
    job_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    equipment_id TEXT NOT NULL REFERENCES wellhead_equipment(equipment_id),
    kind TEXT NOT NULL,
    serial_no TEXT NOT NULL,
    equipment_revision INTEGER NOT NULL,
    PRIMARY KEY (job_id, version_no, equipment_id),
    FOREIGN KEY (job_id, version_no) REFERENCES job_versions(job_id, version_no)
);

CREATE TABLE IF NOT EXISTS milestones (
    milestone_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    kind TEXT NOT NULL CHECK (kind IN ('isolation','start','pause','resume','complete')),
    actor_id TEXT NOT NULL REFERENCES intervention_users(user_id),
    actor_role TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    previous_fact_hash TEXT NOT NULL,
    fact_hash TEXT NOT NULL UNIQUE,
    signed_at TEXT NOT NULL,
    UNIQUE (job_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_milestones_job ON milestones(job_id, sequence);

CREATE TABLE IF NOT EXISTS barrier_states (
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    version_no INTEGER NOT NULL,
    barrier_id TEXT NOT NULL,
    barrier_name TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    confirmed_by TEXT NOT NULL REFERENCES intervention_users(user_id),
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (job_id, barrier_id)
);

CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('rov','spare-part','dive-support')),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_commitments (
    commitment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    job_version_no INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'committed' CHECK (state IN ('committed','released')),
    committed_by TEXT NOT NULL REFERENCES intervention_users(user_id),
    committed_at TEXT NOT NULL,
    released_by TEXT REFERENCES intervention_users(user_id),
    released_at TEXT,
    release_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_commitment_per_resource
ON resource_commitments(resource_id) WHERE state = 'committed';

CREATE INDEX IF NOT EXISTS idx_commitments_job ON resource_commitments(job_id, state);

CREATE TABLE IF NOT EXISTS telemetry_records (
    telemetry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    metric TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES intervention_users(user_id),
    late INTEGER NOT NULL DEFAULT 0 CHECK (late IN (0,1)),
    late_reason TEXT,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_telemetry_well ON telemetry_records(well_id, metric, observed_at);

CREATE TABLE IF NOT EXISTS well_current_state (
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    metric TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (well_id, metric)
);

CREATE TABLE IF NOT EXISTS recovery_actions (
    job_id TEXT NOT NULL REFERENCES intervention_jobs(job_id),
    action_id TEXT NOT NULL,
    title TEXT NOT NULL,
    required_role TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done')),
    note TEXT,
    evidence_ref TEXT,
    completed_by TEXT REFERENCES intervention_users(user_id),
    completed_at TEXT,
    PRIMARY KEY (job_id, action_id)
);

CREATE TABLE IF NOT EXISTS intervention_idempotency (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
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

REQUIRED_TABLES = frozenset({
    "schema_meta", "intervention_users", "wells", "wellhead_equipment", "intervention_jobs",
    "fault_evidence", "job_versions", "job_version_equipment", "milestones", "barrier_states",
    "resources", "resource_commitments", "telemetry_records", "well_current_state",
    "recovery_actions", "intervention_idempotency", "intervention_audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
