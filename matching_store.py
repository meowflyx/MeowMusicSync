"""SQLite migration and transactional writes for matching decisions."""

from __future__ import annotations

import json
import sqlite3
from enum import Enum

from matching import ALGORITHM_VERSION, Decision

SCHEMA_VERSION = 4

class MappingResult(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    CONFLICT = "conflict"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate_matching(conn: sqlite3.Connection) -> None:
    """Add provenance without replacing or deleting existing user rows."""
    changes = {
        "mappings": {"provenance": "TEXT NOT NULL DEFAULT 'legacy'",
                     "mode": "TEXT", "algorithm_version": "INTEGER NOT NULL DEFAULT 0",
                     "validated_at": "TEXT", "status": "TEXT NOT NULL DEFAULT 'active'"},
        "pending_syncs": {"score_type": "TEXT NOT NULL DEFAULT 'legacy'",
                          "purpose": "TEXT NOT NULL DEFAULT 'add'", "mode": "TEXT",
                          "algorithm_version": "INTEGER NOT NULL DEFAULT 0",
                          "metadata_rank": "REAL", "jev_probability": "REAL",
                          "reasons": "TEXT", "diagnostics": "TEXT"},
        "failed_syncs": {"algorithm_version": "INTEGER NOT NULL DEFAULT 0",
                         "mode": "TEXT", "reason": "TEXT"},
    }
    with conn:
        for table, fields in changes.items():
            existing = _columns(conn, table)
            for name, definition in fields.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        conn.execute("DROP TABLE IF EXISTS yandex_cache")
        conn.execute("DROP TABLE IF EXISTS spotify_cache")
        conn.execute("DROP TABLE IF EXISTS blacklist")
        conn.execute("DELETE FROM pending_syncs WHERE purpose = 'add' AND ("
                     "key IN (SELECT 'ym_to_sp:' || ym_id FROM mappings WHERE status = 'active') "
                     "OR key IN (SELECT 'sp_to_ym:' || sp_id FROM mappings WHERE status = 'active'))")
        conn.execute("DELETE FROM failed_syncs WHERE "
                     "key IN (SELECT 'ym_to_sp:' || ym_id FROM mappings WHERE status = 'active') "
                     "OR key IN (SELECT 'sp_to_ym:' || sp_id FROM mappings WHERE status = 'active')")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def save_mapping(conn: sqlite3.Connection, ym_id: str, sp_id: str,
                 mode: str, provenance: str) -> MappingResult:
    """Write only if neither platform ID belongs to another mapping."""
    with conn:
        rows = conn.execute("SELECT ym_id, sp_id FROM mappings WHERE ym_id = ? OR sp_id = ?",
                            (str(ym_id), str(sp_id))).fetchall()
        if any(tuple(row) != (str(ym_id), str(sp_id)) for row in rows):
            return MappingResult.CONFLICT
        if rows:
            conn.execute("UPDATE mappings SET provenance = ?, mode = ?, algorithm_version = ?, "
                         "validated_at = CURRENT_TIMESTAMP, status = 'active' "
                         "WHERE ym_id = ? AND sp_id = ?",
                         (provenance, mode, ALGORITHM_VERSION, str(ym_id), str(sp_id)))
            result = MappingResult.UPDATED
        else:
            conn.execute("INSERT INTO mappings (ym_id, sp_id, provenance, mode, algorithm_version, "
                         "validated_at, status) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'active')",
                         (str(ym_id), str(sp_id), provenance, mode, ALGORITHM_VERSION))
            result = MappingResult.CREATED
        keys = (f"ym_to_sp:{ym_id}", f"sp_to_ym:{sp_id}")
        conn.execute("DELETE FROM pending_syncs WHERE key IN (?, ?)", keys)
        conn.execute("DELETE FROM failed_syncs WHERE key IN (?, ?)", keys)
        return result


def save_pending(conn: sqlite3.Connection, key: str, direction: str,
                 source: str, decision: Decision, purpose: str = "add") -> None:
    candidate = decision.candidate
    diagnostics = [{"id": item.candidate.id, "label": item.candidate.label,
                    "rank": round(item.features.rank, 1), "reasons": item.features.reasons,
                    "jev": item.jev_probability} for item in decision.considered[:5]]
    with conn:
        conn.execute("INSERT INTO pending_syncs (key, direction, source, found, found_id, score, "
                     "score_type, purpose, mode, algorithm_version, metadata_rank, jev_probability, "
                     "reasons, diagnostics) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET source=excluded.source, direction=excluded.direction, "
                     "found=excluded.found, found_id=excluded.found_id, "
                     "score=excluded.score, score_type=excluded.score_type, purpose=excluded.purpose, "
                     "mode=excluded.mode, algorithm_version=excluded.algorithm_version, "
                     "metadata_rank=excluded.metadata_rank, jev_probability=excluded.jev_probability, "
                     "reasons=excluded.reasons, diagnostics=excluded.diagnostics",
                     (key, direction, source, candidate.label if candidate else "",
                      candidate.id if candidate else "", round((decision.jev_probability or 0) * 100)
                      if decision.jev_probability is not None else round(decision.metadata_rank or 0),
                      decision.source, purpose, decision.mode.value, ALGORITHM_VERSION,
                      decision.metadata_rank, decision.jev_probability,
                      json.dumps(decision.reasons, ensure_ascii=False),
                      json.dumps(diagnostics, ensure_ascii=False)))


def save_failed(conn: sqlite3.Connection, key: str, query: str,
                mode: str, reason: str) -> None:
    with conn:
        conn.execute("INSERT INTO failed_syncs (key, query, algorithm_version, mode, reason) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET query=excluded.query, "
                     "algorithm_version=excluded.algorithm_version, mode=excluded.mode, reason=excluded.reason",
                     (key, query, ALGORITHM_VERSION, mode, reason))
