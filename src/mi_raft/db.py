from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import AgentConfig, Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent (
  id TEXT PRIMARY KEY,
  runtime TEXT NOT NULL,
  work_dir TEXT NOT NULL,
  instructions TEXT NOT NULL DEFAULT '',
  model TEXT,
  permissions_json TEXT NOT NULL DEFAULT '{}',
  extra_args_json TEXT NOT NULL DEFAULT '[]',
  max_concurrent INTEGER NOT NULL DEFAULT 1,
  timeout_s INTEGER NOT NULL DEFAULT 600,
  status TEXT NOT NULL DEFAULT 'idle'
);

CREATE TABLE IF NOT EXISTS org_edge (
  from_id TEXT NOT NULL,
  to_id TEXT NOT NULL,
  PRIMARY KEY (from_id, to_id)
);

CREATE TABLE IF NOT EXISTS task (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open'
    CHECK(status IN ('open','claimed','done','cancelled')),
  assignee_type TEXT CHECK(assignee_type IN ('human','agent')),
  assignee_id TEXT,
  channel_id TEXT,
  thread_id INTEGER,
  created_by TEXT NOT NULL DEFAULT 'humano',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT,
  result_text TEXT
);

CREATE TABLE IF NOT EXISTS channel (
  id TEXT PRIMARY KEY,
  topic TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS message (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL,
  thread_id INTEGER,
  author_type TEXT NOT NULL CHECK(author_type IN ('human','agent','system')),
  author_id TEXT NOT NULL,
  text TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT 'comment',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL REFERENCES agent(id),
  channel_id TEXT NOT NULL,
  thread_id INTEGER NOT NULL,
  trigger_message_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK(status IN ('queued','running','done','failed')),
  session_id TEXT,
  work_dir TEXT,
  result_text TEXT,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  started_at TEXT,
  finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_message_channel ON message(channel_id, id);
CREATE INDEX IF NOT EXISTS idx_message_thread ON message(thread_id);
CREATE INDEX IF NOT EXISTS idx_run_agent ON run(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_task_status ON task(status, id);
"""

AGENT_COLUMNS = (
    "id, runtime, work_dir, instructions, model, permissions_json, "
    "extra_args_json, max_concurrent, timeout_s, memory_file, server_port, wake_url"
)

MIGRATIONS = (
    "ALTER TABLE agent ADD COLUMN memory_file TEXT",
    "ALTER TABLE agent ADD COLUMN server_port INTEGER",
    "ALTER TABLE agent ADD COLUMN wake_url TEXT",
)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if sqlite3.sqlite_version_info < (3, 35):
            raise RuntimeError(
                f"SQLite {sqlite3.sqlite_version} es demasiado antiguo (se necesita >= 3.35 por RETURNING)"
            )
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.Lock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    @contextmanager
    def tx(self):
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def close(self) -> None:
        self.conn.close()

    def sync_config(self, cfg: Config) -> None:
        with self.tx() as conn:
            for a in cfg.agents:
                self._upsert_agent(conn, a)
            for c in cfg.channels:
                conn.execute(
                    "INSERT OR REPLACE INTO channel (id, topic) VALUES (?,?)",
                    (c.name, c.topic),
                )
            conn.execute("DELETE FROM org_edge")
            for lead, reports in cfg.org.items():
                for r in reports:
                    conn.execute(
                        "INSERT OR IGNORE INTO org_edge (from_id, to_id) VALUES (?,?)",
                        (lead, r),
                    )

    @staticmethod
    def _upsert_agent(conn: sqlite3.Connection, a: AgentConfig) -> None:
        conn.execute(
            f"INSERT OR REPLACE INTO agent ({AGENT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                a.name,
                a.runtime,
                a.work_dir,
                a.instructions,
                a.model,
                json.dumps(a.permissions),
                json.dumps(a.extra_args),
                a.max_concurrent,
                a.timeout_s,
                a.memory_file,
                a.server_port,
                a.wake_url,
            ),
        )

    def create_agent(self, a: AgentConfig) -> None:
        with self.tx() as conn:
            self._upsert_agent(conn, a)

    def update_agent(self, agent_id: str, fields: dict) -> sqlite3.Row | None:
        allowed = {
            "instructions": str,
            "model": lambda v: v,
            "memory_file": lambda v: v,
            "permissions": lambda v: json.dumps(v or {}),
        }
        sets, vals = [], []
        for key, cast in allowed.items():
            if key in fields:
                sets.append(f"{key}{'_json' if key == 'permissions' else ''}=?")
                vals.append(cast(fields[key]))
        if not sets:
            return self.get_agent_row(agent_id)
        vals.append(agent_id)
        with self.tx() as conn:
            conn.execute(f"UPDATE agent SET {', '.join(sets)} WHERE id=?", vals)
        return self.get_agent_row(agent_id)

    def get_agent_row(self, agent_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM agent WHERE id = ?", (agent_id,)
        ).fetchone()

    def create_channel(self, name: str, topic: str = "") -> bool:
        name = name.lstrip("#")
        if not name:
            raise ValueError("Nombre de canal vacío")
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO channel (id, topic) VALUES (?,?)", (name, topic)
            )
            return cur.rowcount > 0

    def org_edges(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT from_id, to_id FROM org_edge ORDER BY from_id, to_id"
        ).fetchall()

    def org_allows(self, from_id: str, to_id: str) -> bool:
        total = self.conn.execute("SELECT COUNT(*) AS n FROM org_edge").fetchone()["n"]
        if total == 0:
            return True
        row = self.conn.execute(
            "SELECT 1 FROM org_edge WHERE from_id=? AND to_id=?", (from_id, to_id)
        ).fetchone()
        return row is not None

    def get_agent(self, agent_id: str) -> AgentConfig:
        row = self.conn.execute(
            f"SELECT {AGENT_COLUMNS} FROM agent WHERE id = ?", (agent_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Agente desconocido: {agent_id}")
        return AgentConfig(
            name=row["id"],
            runtime=row["runtime"],
            work_dir=row["work_dir"],
            instructions=row["instructions"],
            model=row["model"],
            permissions=json.loads(row["permissions_json"]),
            extra_args=json.loads(row["extra_args_json"]),
            max_concurrent=row["max_concurrent"],
            timeout_s=row["timeout_s"],
            memory_file=row["memory_file"],
            server_port=row["server_port"],
            wake_url=row["wake_url"],
        )

    def list_agents(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM agent ORDER BY id").fetchall()

    def list_channels(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM channel ORDER BY id").fetchall()

    def channel_exists(self, channel_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM channel WHERE id = ?", (channel_id,)
        ).fetchone()
        return row is not None

    def insert_message(
        self,
        channel_id: str,
        author_type: str,
        author_id: str,
        text: str,
        thread_id: int | None = None,
        msg_type: str = "comment",
    ) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO message (channel_id, thread_id, author_type, author_id, text, type)"
                " VALUES (?,?,?,?,?,?)",
                (channel_id, thread_id, author_type, author_id, text, msg_type),
            )
            return int(cur.lastrowid)

    def get_message(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM message WHERE id = ?", (message_id,)
        ).fetchone()

    def list_channel_messages(self, channel_id: str, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM message WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()[::-1]

    def list_messages_since(self, last_id: int, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM message WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, limit),
        ).fetchall()

    def thread_messages(self, thread_id: int, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM message WHERE id = ? OR thread_id = ? ORDER BY id LIMIT ?",
            (thread_id, thread_id, limit),
        ).fetchall()

    def reply_from_agent_since(
        self, thread_id: int, agent_id: str, after_message_id: int
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM message WHERE thread_id = ? AND author_type = 'agent'"
            " AND author_id = ? AND id > ? ORDER BY id LIMIT 1",
            (thread_id, agent_id, after_message_id),
        ).fetchone()

    def count_agent_messages_in_thread(self, thread_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM message WHERE thread_id = ? AND author_type = 'agent'",
            (thread_id,),
        ).fetchone()
        return int(row["n"])

    def last_agent_in_thread(self, thread_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT author_id FROM message WHERE thread_id = ? AND author_type = 'agent'"
            " ORDER BY id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return row["author_id"] if row else None

    def reset_stuck_runs(self) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE run SET status='failed', error='interrumpido (reinicio del server)',"
                " finished_at=datetime('now') WHERE status='running'"
            )
            return cur.rowcount

    def claim_next_run(self) -> sqlite3.Row | None:
        with self.tx() as conn:
            cur = conn.execute(
                """
                UPDATE run SET status='running', started_at=datetime('now')
                WHERE id = (
                  SELECT r.id FROM run r
                  JOIN agent a ON a.id = r.agent_id
                  WHERE r.status='queued'
                    AND (SELECT COUNT(*) FROM run r2
                         WHERE r2.agent_id=r.agent_id AND r2.status='running') < a.max_concurrent
                  ORDER BY r.id
                  LIMIT 1
                )
                RETURNING id, agent_id, channel_id, thread_id, trigger_message_id
                """
            )
            return cur.fetchone()

    def coalesce_run(
        self, agent_id: str, channel_id: str, thread_id: int, message_id: int
    ) -> bool:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT id FROM run WHERE agent_id=? AND channel_id=? AND thread_id=?"
                " AND status='queued' ORDER BY id LIMIT 1",
                (agent_id, channel_id, thread_id),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE run SET trigger_message_id=? WHERE id=?",
                (message_id, row["id"]),
            )
            return True

    def insert_run(
        self, agent_id: str, channel_id: str, thread_id: int, trigger_message_id: int
    ) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO run (agent_id, channel_id, thread_id, trigger_message_id)"
                " VALUES (?,?,?,?)",
                (agent_id, channel_id, thread_id, trigger_message_id),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        session_id: str | None,
        work_dir: str | None,
        result_text: str | None,
        error: str | None,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE run SET status=?, session_id=?, work_dir=?, result_text=?,"
                " error=?, finished_at=datetime('now') WHERE id=?",
                (status, session_id, work_dir, result_text, error, run_id),
            )

    def last_session_for(self, agent_id: str, thread_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT session_id FROM run WHERE agent_id=? AND thread_id=? AND status='done'"
            " AND session_id IS NOT NULL ORDER BY id DESC LIMIT 1",
            (agent_id, thread_id),
        ).fetchone()
        return row["session_id"] if row else None

    def create_task(
        self,
        title: str,
        description: str = "",
        channel_id: str | None = None,
        created_by: str = "humano",
    ) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO task (title, description, channel_id, created_by)"
                " VALUES (?,?,?,?)",
                (title, description, channel_id, created_by),
            )
            return int(cur.lastrowid)

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM task WHERE id = ?", (task_id,)
        ).fetchone()

    def list_tasks(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.conn.execute(
                "SELECT * FROM task WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        return self.conn.execute("SELECT * FROM task ORDER BY id").fetchall()

    def claim_task(
        self, task_id: int, assignee_type: str, assignee_id: str
    ) -> sqlite3.Row | None:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE task SET status='claimed', assignee_type=?, assignee_id=?,"
                " updated_at=datetime('now') WHERE id=? AND status='open' RETURNING *",
                (assignee_type, assignee_id, task_id),
            )
            return cur.fetchone()

    def finish_task(
        self, task_id: int, status: str, result_text: str | None = None
    ) -> sqlite3.Row | None:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE task SET status=?, result_text=?, updated_at=datetime('now')"
                " WHERE id=? AND status IN ('open','claimed') RETURNING *",
                (status, result_text, task_id),
            )
            return cur.fetchone()
