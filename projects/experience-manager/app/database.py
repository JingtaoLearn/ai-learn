"""SQLite + sqlite-vec database setup and access helpers."""

from __future__ import annotations

import json
import os
import sqlite3
import struct
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator, Optional

import sqlite_vec

DB_PATH = os.getenv("EMS_DB_PATH", "/data/ems.db")
EMBEDDING_DIM = 3072  # text-embedding-3-large


def _serialize_f32(v: list[float]) -> bytes:
    """Pack a list of floats to little-endian f32 bytes expected by sqlite-vec."""
    return struct.pack(f"{len(v)}f", *v)


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Create a new SQLite connection with sqlite-vec loaded."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@contextmanager
def get_db(db_path: str = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a connection that auto-commits or rolls back."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str = DB_PATH) -> None:
    """Create tables if they don't exist."""
    with get_db(db_path) as conn:
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS experiences (
                id          TEXT PRIMARY KEY,
                content     TEXT NOT NULL,
                category    TEXT NOT NULL CHECK(category IN ('guardrail', 'practice', 'lesson')),
                severity    TEXT NOT NULL CHECK(severity IN ('block', 'warn', 'info')),
                tags        TEXT NOT NULL DEFAULT '[]',
                scope       TEXT NOT NULL DEFAULT '{{}}',
                source      TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active', 'deprecated', 'merged')),
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS vec_experiences USING vec0(
                experience_id TEXT PRIMARY KEY,
                embedding float[{EMBEDDING_DIM}]
            );
        """)


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, parsing JSON fields."""
    d = dict(row)
    d["tags"] = json.loads(d.get("tags", "[]"))
    d["scope"] = json.loads(d.get("scope", "{}"))
    return d


# ---------------------------------------------------------------------------
# Experience CRUD
# ---------------------------------------------------------------------------

def insert_experience(
    conn: sqlite3.Connection,
    exp_id: str,
    content: str,
    category: str,
    severity: str,
    tags: list[str],
    scope: dict[str, Any],
    source: str,
    status: str,
    created_at: datetime,
    updated_at: datetime,
) -> None:
    now_str_c = created_at.isoformat()
    now_str_u = updated_at.isoformat()
    conn.execute(
        """
        INSERT INTO experiences
            (id, content, category, severity, tags, scope, source, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            exp_id, content, category, severity,
            json.dumps(tags), json.dumps(scope), source, status,
            now_str_c, now_str_u,
        ),
    )


def insert_embedding(
    conn: sqlite3.Connection,
    exp_id: str,
    embedding: list[float],
) -> None:
    conn.execute(
        "INSERT INTO vec_experiences (experience_id, embedding) VALUES (?, ?)",
        (exp_id, _serialize_f32(embedding)),
    )


def update_experience(
    conn: sqlite3.Connection,
    exp_id: str,
    fields: dict[str, Any],
) -> bool:
    if not fields:
        return False
    fields["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [exp_id]
    cursor = conn.execute(
        f"UPDATE experiences SET {set_clause} WHERE id = ?", values
    )
    return cursor.rowcount > 0


def update_embedding(
    conn: sqlite3.Connection,
    exp_id: str,
    embedding: list[float],
) -> None:
    # sqlite-vec virtual tables support INSERT OR REPLACE
    conn.execute(
        "INSERT OR REPLACE INTO vec_experiences (experience_id, embedding) VALUES (?, ?)",
        (exp_id, _serialize_f32(embedding)),
    )


def get_experience_by_id(
    conn: sqlite3.Connection, exp_id: str
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM experiences WHERE id = ?", (exp_id,)
    ).fetchone()
    return row_to_dict(row) if row else None


def list_experiences(
    conn: sqlite3.Connection,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    tags: Optional[list[str]] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if category:
        conditions.append("category = ?")
        params.append(category)
    if severity:
        conditions.append("severity = ?")
        params.append(severity)
    if status:
        conditions.append("status = ?")
        params.append(status)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = conn.execute(
        f"SELECT * FROM experiences {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    results = [row_to_dict(r) for r in rows]

    # Tag filter (post-query since tags are JSON-encoded)
    if tags:
        filtered = []
        for r in results:
            exp_tags = set(r["tags"])
            if any(t in exp_tags for t in tags):
                filtered.append(r)
        return filtered

    return results


def delete_experience_soft(conn: sqlite3.Connection, exp_id: str) -> bool:
    cursor = conn.execute(
        "UPDATE experiences SET status = 'deprecated', updated_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), exp_id),
    )
    return cursor.rowcount > 0


def vector_search(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    k: int = 50,
    candidate_ids: Optional[list[str]] = None,
) -> list[tuple[str, float]]:
    """Return list of (experience_id, distance) sorted by distance ascending."""
    serialized = _serialize_f32(query_embedding)

    if candidate_ids:
        # sqlite-vec doesn't support dynamic IN filters in WHERE for vec0,
        # so we do a broad search then filter in Python
        rows = conn.execute(
            """
            SELECT experience_id, distance
            FROM vec_experiences
            WHERE embedding MATCH ?
            AND k = ?
            ORDER BY distance
            """,
            (serialized, k * 2),
        ).fetchall()
        id_set = set(candidate_ids)
        return [(r["experience_id"], r["distance"]) for r in rows if r["experience_id"] in id_set][:k]

    rows = conn.execute(
        """
        SELECT experience_id, distance
        FROM vec_experiences
        WHERE embedding MATCH ?
        AND k = ?
        ORDER BY distance
        """,
        (serialized, k),
    ).fetchall()
    return [(r["experience_id"], r["distance"]) for r in rows]


def get_all_tags(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Return (tag, count) pairs across all active experiences."""
    rows = conn.execute(
        "SELECT tags FROM experiences WHERE status = 'active'"
    ).fetchall()
    tag_counts: dict[str, int] = {}
    for row in rows:
        for tag in json.loads(row["tags"]):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    return sorted(tag_counts.items(), key=lambda x: -x[1])
