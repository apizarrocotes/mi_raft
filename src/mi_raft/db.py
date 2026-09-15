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
  status TEXT NOT NULL DEFAULT 'todo'
    CHECK(status IN ('todo','in_progress','in_review','done','cancelled')),
  parent_task_id INTEGER,
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

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
  text, channel_id UNINDEXED, author_id UNINDEXED, message_id UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS message_fts_insert AFTER INSERT ON message BEGIN
  INSERT INTO message_fts (text, channel_id, author_id, message_id)
  VALUES (new.text, new.channel_id, new.author_id, new.id);
END;

CREATE TABLE IF NOT EXISTS webhook (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  events TEXT NOT NULL DEFAULT '[]',
  secret TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_message (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  tool TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_run_message ON run_message(run_id, seq);
"""

AGENT_COLUMNS = (
    "id, runtime, work_dir, instructions, model, provider, permissions_json, "
    "extra_args_json, max_concurrent, timeout_s, memory_file, server_port, wake_url, budget_usd, sandbox_json, web_search"
)

MIGRATIONS = (
    "ALTER TABLE agent ADD COLUMN memory_file TEXT",
    "ALTER TABLE agent ADD COLUMN server_port INTEGER",
    "ALTER TABLE agent ADD COLUMN wake_url TEXT",
    "ALTER TABLE agent ADD COLUMN budget_usd REAL",
    "ALTER TABLE run ADD COLUMN cost_usd REAL",
    "ALTER TABLE run ADD COLUMN tokens_in INTEGER",
    "ALTER TABLE run ADD COLUMN tokens_out INTEGER",
    "ALTER TABLE channel ADD COLUMN type TEXT NOT NULL DEFAULT 'channel'",
    "ALTER TABLE channel ADD COLUMN members_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE agent ADD COLUMN sandbox_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE agent ADD COLUMN web_search INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE run ADD COLUMN provider TEXT",
    "ALTER TABLE run ADD COLUMN model TEXT",
    "ALTER TABLE agent ADD COLUMN provider TEXT",
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
        self.on_message_created = None
        self.server_port: int | None = None
        self.escalate_to: str | None = None
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        fts_count = self.conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0]
        if fts_count == 0:
            self.conn.execute(
                "INSERT INTO message_fts (text, channel_id, author_id, message_id)"
                " SELECT text, channel_id, author_id, id FROM message"
            )
        self._migrate_task_table()
        self.conn.commit()

    def _migrate_task_table(self) -> None:
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='task'"
        ).fetchone()
        if row is None or "in_review" in row["sql"]:
            return
        self.conn.executescript(
            """
            DROP INDEX IF EXISTS idx_task_status;
            CREATE TABLE task_v2 (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'todo'
                CHECK(status IN ('todo','in_progress','in_review','done','cancelled')),
              parent_task_id INTEGER,
              assignee_type TEXT CHECK(assignee_type IN ('human','agent')),
              assignee_id TEXT,
              channel_id TEXT,
              thread_id INTEGER,
              created_by TEXT NOT NULL DEFAULT 'humano',
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              updated_at TEXT,
              result_text TEXT
            );
            INSERT INTO task_v2 (id, title, description, status, assignee_type, assignee_id,
                                 channel_id, thread_id, created_by, created_at, updated_at, result_text)
            SELECT id, title, description,
                   CASE status WHEN 'open' THEN 'todo' WHEN 'claimed' THEN 'in_progress' ELSE status END,
                   assignee_type, assignee_id, channel_id, thread_id, created_by,
                   created_at, updated_at, result_text
            FROM task;
            DROP TABLE task;
            ALTER TABLE task_v2 RENAME TO task;
            CREATE INDEX idx_task_status ON task(status, id);
            CREATE INDEX idx_task_parent ON task(parent_task_id);
            """
        )

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
            f"INSERT OR REPLACE INTO agent ({AGENT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                a.name,
                a.runtime,
                a.work_dir,
                a.instructions,
                a.model,
                a.provider,
                json.dumps(a.permissions),
                json.dumps(a.extra_args),
                a.max_concurrent,
                a.timeout_s,
                a.memory_file,
                a.server_port,
                a.wake_url,
                a.budget_usd,
                json.dumps(a.sandbox),
                1 if a.web_search else 0,
            ),
        )

    def create_agent(self, a: AgentConfig) -> None:
        with self.tx() as conn:
            self._upsert_agent(conn, a)

    def update_agent(self, agent_id: str, fields: dict) -> sqlite3.Row | None:
        allowed = {
            "instructions": str,
            "model": lambda v: v,
            "provider": lambda v: v,
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
            provider=row["provider"] if "provider" in row.keys() else None,
            permissions=json.loads(row["permissions_json"]),
            extra_args=json.loads(row["extra_args_json"]),
            max_concurrent=row["max_concurrent"],
            timeout_s=row["timeout_s"],
            memory_file=row["memory_file"],
            server_port=row["server_port"],
            wake_url=row["wake_url"],
            budget_usd=row["budget_usd"],
            sandbox=json.loads(row["sandbox_json"]) if "sandbox_json" in row.keys() else {},
            web_search=bool(row["web_search"]) if "web_search" in row.keys() else True,
        )

    def list_agents(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM agent ORDER BY id").fetchall()

    def list_channels(self, include_dms: bool = False) -> list[sqlite3.Row]:
        if include_dms:
            return self.conn.execute("SELECT * FROM channel ORDER BY id").fetchall()
        return self.conn.execute(
            "SELECT * FROM channel WHERE type = 'channel' ORDER BY id"
        ).fetchall()

    def get_or_create_dm(self, agent_id: str, human: str = "humano") -> sqlite3.Row:
        dm_id = f"dm-{agent_id}"
        members = sorted([human, agent_id])
        with self.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO channel (id, topic, type, members_json)"
                " VALUES (?,?,?,?)",
                (dm_id, f"DM con {agent_id}", "dm", json.dumps(members)),
            )
        return self.conn.execute(
            "SELECT * FROM channel WHERE id = ?", (dm_id,)
        ).fetchone()

    def list_dms(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM channel WHERE type = 'dm' ORDER BY id"
        ).fetchall()

    def search(
        self, query: str, channel_id: str | None = None,
        author: str | None = None, limit: int = 50,
    ) -> list[sqlite3.Row]:
        import re as _re

        tokens = _re.findall(r"\w+", query)
        if not tokens:
            return []
        match = " AND ".join(f'"{t}"' for t in tokens)
        sql = (
            "SELECT m.* FROM message_fts f JOIN message m ON m.id = f.message_id"
            " WHERE message_fts MATCH ?"
        )
        params: list = [match]
        if channel_id:
            sql += " AND f.channel_id = ?"
            params.append(channel_id)
        if author:
            sql += " AND f.author_id = ?"
            params.append(author)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def activity(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM message WHERE type = 'status' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def add_webhook(self, url: str, events: list[str], secret: str) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO webhook (url, events, secret) VALUES (?,?,?)",
                (url, json.dumps(events), secret),
            )
            return int(cur.lastrowid)

    def list_webhooks(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM webhook ORDER BY id").fetchall()

    def delete_webhook(self, webhook_id: int) -> bool:
        with self.tx() as conn:
            cur = conn.execute("DELETE FROM webhook WHERE id = ?", (webhook_id,))
            return cur.rowcount > 0

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
            mid = int(cur.lastrowid)
        if self.on_message_created:
            try:
                self.on_message_created(dict(self.get_message(mid)))
            except Exception:
                pass
        return mid

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
        cost_usd: float | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE run SET status=?, session_id=?, work_dir=?, result_text=?,"
                " error=?, cost_usd=?, tokens_in=?, tokens_out=?, provider=?, model=?,"
                " finished_at=datetime('now') WHERE id=?",
                (status, session_id, work_dir, result_text, error, cost_usd, tokens_in,
                 tokens_out, provider, model, run_id),
            )

    def set_agent_status(self, agent_id: str, status: str) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE agent SET status=? WHERE id=?", (status, agent_id))

    def insert_run_message(self, run_id: int, seq: int, ev_type: str, tool: str | None, payload: dict) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO run_message (run_id, seq, type, tool, payload_json) VALUES (?,?,?,?,?)",
                (run_id, seq, ev_type, tool, json.dumps(payload, default=str)[:4096]),
            )

    def list_run_messages(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM run_message WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()

    def recent_runs(self, agent_id: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if agent_id:
            return self.conn.execute(
                "SELECT id, agent_id, status, channel_id, thread_id, cost_usd, tokens_in,"
                " tokens_out, error, created_at, started_at, finished_at, provider, model"
                " FROM run WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT id, agent_id, status, channel_id, thread_id, cost_usd, tokens_in,"
            " tokens_out, error, created_at, started_at, finished_at, provider, model"
            " FROM run ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def escalations_in_thread(self, thread_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM message WHERE thread_id = ? AND author_id = 'mi_raft'"
            " AND text LIKE '⚠️%'",
            (thread_id,),
        ).fetchone()
        return int(row["n"])

    def task_for_thread(self, thread_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM task WHERE thread_id = ? AND status IN ('todo','in_progress','in_review')"
            " ORDER BY id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()

    def stuck_running_runs(self, grace_s: int = 120) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.*, a.timeout_s FROM run r JOIN agent a ON a.id = r.agent_id"
            " WHERE r.status = 'running'"
            " AND r.started_at < datetime('now', '-' || (a.timeout_s + ?) || ' seconds')",
            (grace_s,),
        ).fetchall()

    def agent_models(self, agent_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT provider, model, COUNT(*) AS runs, SUM(cost_usd) AS cost_usd"
            " FROM run WHERE agent_id = ? AND model IS NOT NULL"
            " GROUP BY provider, model ORDER BY MAX(id) DESC LIMIT 5",
            (agent_id,),
        ).fetchall()

    def agent_stats(self, agent_id: str) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT COUNT(*) AS runs, SUM(cost_usd) AS cost_usd,"
            " SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out"
            " FROM run WHERE agent_id = ? AND status = 'done'",
            (agent_id,),
        ).fetchone()

    def usage_by_agent(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT agent_id, COUNT(*) AS runs, SUM(cost_usd) AS cost_usd,"
            " SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out"
            " FROM run WHERE status='done' GROUP BY agent_id ORDER BY cost_usd DESC"
        ).fetchall()

    def usage_by_day(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT substr(started_at, 1, 10) AS day, COUNT(*) AS runs,"
            " SUM(cost_usd) AS cost_usd, SUM(tokens_in) AS tokens_in,"
            " SUM(tokens_out) AS tokens_out"
            " FROM run WHERE status='done' AND started_at IS NOT NULL"
            " GROUP BY day ORDER BY day DESC LIMIT 30"
        ).fetchall()

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
            "SELECT t.*, (SELECT COUNT(*) FROM task s WHERE s.parent_task_id = t.id)"
            " AS subtask_count FROM task t WHERE t.id = ?",
            (task_id,),
        ).fetchone()

    def list_tasks(self, status: str | None = None) -> list[sqlite3.Row]:
        base = (
            "SELECT t.*, (SELECT COUNT(*) FROM task s WHERE s.parent_task_id = t.id)"
            " AS subtask_count FROM task t"
        )
        if status:
            return self.conn.execute(
                base + " WHERE t.status = ? ORDER BY t.id", (status,)
            ).fetchall()
        return self.conn.execute(base + " ORDER BY t.id").fetchall()

    def claim_task(
        self, task_id: int, assignee_type: str, assignee_id: str
    ) -> sqlite3.Row | None:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE task SET status='in_progress', assignee_type=?, assignee_id=?,"
                " updated_at=datetime('now') WHERE id=? AND status='todo' RETURNING *",
                (assignee_type, assignee_id, task_id),
            )
            return cur.fetchone()

    def review_task(self, task_id: int) -> sqlite3.Row | None:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE task SET status='in_review', updated_at=datetime('now')"
                " WHERE id=? AND status='in_progress' RETURNING *",
                (task_id,),
            )
            return cur.fetchone()

    def finish_task(
        self, task_id: int, status: str, result_text: str | None = None
    ) -> sqlite3.Row | None:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE task SET status=?, result_text=?, updated_at=datetime('now')"
                " WHERE id=? AND status IN ('todo','in_progress','in_review') RETURNING *",
                (status, result_text, task_id),
            )
            return cur.fetchone()

    def list_subtasks(self, parent_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM task WHERE parent_task_id = ? ORDER BY id", (parent_id,)
        ).fetchall()

    def create_subtask(
        self, parent_id: int, title: str, description: str = "", channel_id: str | None = None
    ) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO task (title, description, channel_id, parent_task_id)"
                " VALUES (?,?,?,?)",
                (title, description, channel_id, parent_id),
            )
            return int(cur.lastrowid)
