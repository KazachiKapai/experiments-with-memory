import sqlite3
import os
import uuid
import json
from datetime import datetime
from typing import List, Dict, Any

# --- Database Setup ---
DATA_DIR = os.environ.get("DATA_DIR", "/tmp/memory_data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = f"{DATA_DIR}/memory.db"

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row


def init_db():
    conn.execute("""
        CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            user_id TEXT,
            messages TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS facts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            type TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            content TEXT,
            confidence REAL DEFAULT 0.9,
            active INTEGER DEFAULT 1,
            supersedes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_active ON facts(user_id, active)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_docs (
            user_id TEXT PRIMARY KEY,
            full_doc TEXT NOT NULL,
            sections TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_conversations (
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, session_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS md_chunks (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            section TEXT NOT NULL,
            content TEXT NOT NULL,
            embedding TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_md_chunks_user ON md_chunks(user_id)")
    conn.commit()


def store_facts(facts: List[Dict[str, Any]], user_id: str, session_id: str, turn_id: str):
    if not facts or not user_id:
        return

    now = datetime.utcnow().isoformat() + "Z"

    for fact in facts:
        if not isinstance(fact, dict):
            continue

        fact_key = fact.get("key", "").lower().strip()
        if not fact_key:
            continue

        cur = conn.execute(
            "SELECT id FROM facts WHERE user_id=? AND key=? AND active=1",
            (user_id, fact_key),
        )
        existing = cur.fetchone()

        supersedes = None
        if existing:
            conn.execute(
                "UPDATE facts SET active=0, updated_at=? WHERE id=?",
                (now, existing["id"]),
            )
            supersedes = existing["id"]

        fact_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO facts (id, user_id, session_id, turn_id, type, key, value, content, confidence, active, supersedes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
            (
                fact_id,
                user_id,
                session_id,
                turn_id,
                fact.get("type", "fact"),
                fact_key,
                str(fact.get("value", "")),
                json.dumps(fact),
                fact.get("confidence", 0.9),
                supersedes,
                now,
                now,
            ),
        )
    conn.commit()


def get_user_conversation_history(user_id: str) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT turn_json FROM user_conversations WHERE user_id=? ORDER BY created_at ASC",
        (user_id,),
    )
    history = []
    for row in cur.fetchall():
        try:
            history.append(json.loads(row["turn_json"]))
        except json.JSONDecodeError:
            pass # Or log the error
    return history


def get_all_user_conversations(user_id: str):
    cur = conn.execute(
        "SELECT session_id, turn_json, created_at FROM user_conversations WHERE user_id=? ORDER BY created_at ASC",
        (user_id,),
    )
    return cur.fetchall()


def get_active_facts(user_id: str):
    cur = conn.execute(
        "SELECT key, value, type, session_id FROM facts WHERE user_id=? AND active=1",
        (user_id,),
    )
    return cur.fetchall()


def upsert_user_doc(user_id: str, md_doc: str, now: str):
    conn.execute(
        "INSERT OR REPLACE INTO user_docs (user_id, full_doc, sections, updated_at) VALUES (?, ?, ?, ?)",
        (user_id, md_doc, "{}", now),
    )
    conn.commit()


def delete_user_chunks(user_id: str):
    conn.execute("DELETE FROM md_chunks WHERE user_id=?", (user_id,))
    conn.commit()


def insert_chunk(chunk_id: str, user_id: str, section: str, content: str, embedding_json: str, now: str):
    conn.execute(
        "INSERT INTO md_chunks (id, user_id, section, content, embedding, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (chunk_id, user_id, section, content, embedding_json, now),
    )
    conn.commit()

def create_turn(turn_id: str, req):
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        """
        INSERT INTO turns (id, session_id, user_id, messages, timestamp, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
        (
            turn_id,
            req.session_id,
            req.user_id,
            json.dumps([m.model_dump() for m in req.messages]),
            req.timestamp,
            json.dumps(req.metadata),
            now,
        ),
    )
    if req.user_id:
        turn_data = {
            "turn_id": turn_id,
            "session_id": req.session_id,
            "messages": [m.model_dump() for m in req.messages],
            "timestamp": req.timestamp,
            "metadata": req.metadata,
        }
        conn.execute(
            "INSERT OR REPLACE INTO user_conversations (user_id, session_id, turn_json, created_at) VALUES (?, ?, ?, ?)",
            (req.user_id, req.session_id, json.dumps(turn_data), now),
        )
    conn.commit()


def count_user_embeddings(user_id: str) -> int:
    cur = conn.execute("SELECT COUNT(*) as cnt FROM md_chunks WHERE user_id = ? AND embedding IS NOT NULL", (user_id,))
    row = cur.fetchone()
    return row['cnt'] if row else 0


def get_user_doc(user_id: str):
    cur = conn.execute("SELECT full_doc FROM user_docs WHERE user_id=?", (user_id,))
    return cur.fetchone()


def get_chunks_to_embed(user_id: str):
    cur = conn.execute("SELECT id, section, content FROM md_chunks WHERE user_id = ? AND embedding IS NULL", (user_id,))
    return cur.fetchall()


def update_chunk_embedding(chunk_id: str, embedding_json: str):
    conn.execute("UPDATE md_chunks SET embedding = ? WHERE id = ?", (embedding_json, chunk_id))
    conn.commit()


def get_all_chunks_with_embeddings(user_id: str):
    cur = conn.execute("SELECT id, user_id, section, content, embedding FROM md_chunks WHERE user_id = ? AND embedding IS NOT NULL", (user_id,))
    return cur.fetchall()

def get_all_chunks(user_id: str):
    cur = conn.execute("SELECT id, section, content FROM md_chunks WHERE user_id = ?", (user_id,))
    return cur.fetchall()

def get_user_memories(user_id: str):
    cur = conn.execute(
        "SELECT id, type, key, value, confidence, session_id, turn_id, created_at, updated_at, supersedes, active FROM facts WHERE user_id=? ORDER BY created_at DESC",
        (user_id,),
    )
    return cur.fetchall()

def search_facts(query: str, user_id: str, session_id: str, limit: int):
    cur = conn.execute(
        """
        SELECT f.value, f.key, f.user_id, f.session_id, t.timestamp, 1.0 as score
        FROM facts f
        LEFT JOIN turns t ON f.turn_id = t.id
        WHERE (f.user_id LIKE ? OR f.user_id IS NULL)
        AND (f.session_id LIKE ? OR ? = '%')
        AND (f.value LIKE ? OR f.key LIKE ?)
        LIMIT ?
    """,
        (user_id, session_id, session_id, f"%{query}%", f"%{query}%", limit,),
    )
    return cur.fetchall()

def delete_session(session_id: str):
    conn.execute("DELETE FROM facts WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM turns WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM user_conversations WHERE session_id=?", (session_id,))
    conn.commit()

def delete_user(user_id: str):
    conn.execute("DELETE FROM facts WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM turns WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM user_conversations WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM user_docs WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM md_chunks WHERE user_id=?", (user_id,))
    conn.commit()
