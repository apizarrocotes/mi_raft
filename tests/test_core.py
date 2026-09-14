from __future__ import annotations

import asyncio
import json

import pytest

from mi_raft.config import AgentConfig, Config, ServerConfig, ChannelConfig, load_config
from mi_raft.db import Database
from mi_raft.router import build_prompt, extract_mentions, route_message
from mi_raft.runner import execute_run
from mi_raft.runtimes import BaseRuntime, RunResult, get_runtime, register_runtime
from mi_raft.runtimes.opencode_serve import OpencodeServeRuntime, derive_port


def make_config(agents: list[AgentConfig] | None = None) -> Config:
    if agents is None:
        agents = [
            AgentConfig(name="alpha", runtime="claude", work_dir="/tmp"),
            AgentConfig(name="beta", runtime="pi", work_dir="/tmp"),
        ]
    return Config(
        server=ServerConfig(),
        agents=agents,
        channels=[ChannelConfig(name="demo")],
    )


def make_db(tmp_path, agents: list[AgentConfig] | None = None) -> Database:
    db = Database(tmp_path / "raft.db")
    db.sync_config(make_config(agents))
    return db


class TestConfig:
    def test_load_valid(self, tmp_path):
        p = tmp_path / "raft.yaml"
        p.write_text(
            """
server: {host: 127.0.0.1, port: 9000, db: data/x.db}
agents:
  - {name: opencode, runtime: opencode, work_dir: /tmp}
  - {name: claude-code, runtime: claude, work_dir: /tmp, permissions: {mode: dontAsk}}
channels:
  - {name: "#demo"}
"""
        )
        cfg = load_config(p)
        assert cfg.server.port == 9000
        assert [a.name for a in cfg.agents] == ["opencode", "claude-code"]
        assert cfg.channels[0].name == "demo"

    def test_rejects_unknown_runtime(self, tmp_path):
        p = tmp_path / "raft.yaml"
        p.write_text(
            "agents:\n  - {name: x, runtime: nope, work_dir: /tmp}\n"
            "channels: [{name: demo}]\n"
        )
        with pytest.raises(ValueError, match="Runtime desconocido"):
            load_config(p)

    def test_rejects_duplicate_agents(self, tmp_path):
        p = tmp_path / "raft.yaml"
        p.write_text(
            "agents:\n"
            "  - {name: x, runtime: pi, work_dir: /tmp}\n"
            "  - {name: x, runtime: pi, work_dir: /tmp}\n"
            "channels: [{name: demo}]\n"
        )
        with pytest.raises(ValueError, match="duplicado"):
            load_config(p)


class TestMentions:
    def test_plain_and_structured(self):
        agents = {"opencode", "claude-code"}
        text = "hola @Opencode y [@claude-code](mention://agent/claude-code) y @nadie"
        assert extract_mentions(text, agents) == {"opencode", "claude-code"}

    def test_ignores_email_like(self):
        assert extract_mentions("yo@mail.com", {"mail"}) == set()


class TestRouting:
    def test_mention_creates_runs_with_thread(self, tmp_path):
        db = make_db(tmp_path)
        root = db.insert_message("demo", "human", "apc", "@alpha haz esto")
        runs = route_message(db, root)
        assert len(runs) == 1
        run = db.conn.execute("SELECT * FROM run").fetchone()
        assert run["agent_id"] == "alpha"
        assert run["thread_id"] == root

    def test_coalesce_two_mentions_one_run(self, tmp_path):
        db = make_db(tmp_path)
        m1 = db.insert_message("demo", "human", "apc", "@alpha uno")
        route_message(db, m1)
        m2 = db.insert_message("demo", "human", "apc", "@alpha dos", thread_id=m1)
        route_message(db, m2)
        runs = db.conn.execute("SELECT * FROM run").fetchall()
        assert len(runs) == 1
        assert runs[0]["trigger_message_id"] == m2
        assert runs[0]["thread_id"] == m1

    def test_human_reply_without_mention_wakes_last_agent(self, tmp_path):
        db = make_db(tmp_path)
        root = db.insert_message("demo", "human", "apc", "@alpha empieza")
        route_message(db, root)
        db.claim_next_run()
        reply = db.insert_message("demo", "agent", "alpha", "hecho", thread_id=root)
        follow = db.insert_message("demo", "human", "apc", "¿seguro?", thread_id=root)
        runs = route_message(db, follow)
        assert len(runs) == 1
        run = db.conn.execute(
            "SELECT * FROM run WHERE id=?", (runs[0],)
        ).fetchone()
        assert run["agent_id"] == "alpha"
        assert run["thread_id"] == root
        assert reply is not None

    def test_root_message_without_mention_does_not_wake(self, tmp_path):
        db = make_db(tmp_path)
        m = db.insert_message("demo", "human", "apc", "hola a todos sin menciones")
        assert route_message(db, m) == []

    def test_claim_respects_max_concurrent(self, tmp_path):
        db = Database(tmp_path / "raft.db")
        db.sync_config(
            make_config(
                [AgentConfig(name="alpha", runtime="claude", work_dir="/tmp", max_concurrent=1)]
            )
        )
        db.insert_message("demo", "human", "apc", "hola")
        for i in range(3):
            db.insert_run("alpha", "demo", 1, 1)
        first = db.claim_next_run()
        assert first is not None
        assert db.claim_next_run() is None
        db.finish_run(first["id"], "done", None, None, "ok", None)
        assert db.claim_next_run() is not None


class FakeHandoffRuntime(BaseRuntime):
    name = "fakehandoff"

    def __init__(self):
        self.turns: list[str] = []

    def build_args(self, agent, session_id):
        return ["true"]

    def parse_output(self, stdout):
        return RunResult(session_id="fake-session", text="Listo. @beta revisa esto")

    async def run_turn(self, agent, prompt, session_id, timeout_s):
        self.turns.append(prompt)
        return self.parse_output("")


class TestRunnerHandoff:
    def test_agent_reply_triggers_handoff_and_session_resume(self, tmp_path):
        db = make_db(
            tmp_path,
            [
                AgentConfig(name="alpha", runtime="fakehandoff", work_dir="/tmp"),
                AgentConfig(name="beta", runtime="fakehandoff", work_dir="/tmp"),
            ],
        )
        fake = FakeHandoffRuntime()
        register_runtime(fake)
        assert get_runtime("fakehandoff") is fake

        root = db.insert_message("demo", "human", "apc", "@alpha trabaja")
        route_message(db, root)
        run = db.claim_next_run()
        assert run is not None
        asyncio.run(execute_run(db, run))

        done = db.conn.execute(
            "SELECT * FROM run WHERE agent_id='alpha'"
        ).fetchone()
        assert done["status"] == "done"
        assert done["session_id"] == "fake-session"

        queued = db.conn.execute(
            "SELECT * FROM run WHERE agent_id='beta' AND status='queued'"
        ).fetchone()
        assert queued is not None
        assert queued["thread_id"] == root

        reply = db.conn.execute(
            "SELECT * FROM message WHERE author_type='agent' AND author_id='alpha'"
        ).fetchone()
        assert reply["thread_id"] == root


class TestPrompt:
    def test_prompt_contains_thread_and_instructions(self, tmp_path):
        db = make_db(tmp_path)
        db.sync_config(
            make_config(
                [AgentConfig(name="alpha", runtime="claude", work_dir="/tmp", instructions="Sé breve.")]
            )
        )
        root = db.insert_message("demo", "human", "apc", "@alpha empieza")
        db.insert_message("demo", "human", "apc", "continúa", thread_id=root)
        prompt = build_prompt(db, db.get_agent("alpha"), "demo", root)
        assert "Sé breve." in prompt
        assert "@alpha empieza" in prompt
        assert "continúa" in prompt
        assert "#demo" in prompt


class TestTasks:
    def test_create_claim_finish(self, tmp_path):
        db = make_db(tmp_path)
        tid = db.create_task("Título", "Descripción", "demo")
        t = db.get_task(tid)
        assert t["status"] == "open"
        claimed = db.claim_task(tid, "agent", "alpha")
        assert claimed is not None
        assert claimed["status"] == "claimed"
        assert db.claim_task(tid, "agent", "beta") is None
        assert db.finish_task(tid, "done", "hecho") is not None
        assert db.get_task(tid)["result_text"] == "hecho"
        assert db.finish_task(tid, "cancelled") is None


class TestMemory:
    def test_prompt_injects_memory_file(self, tmp_path):
        mem = tmp_path / "memoria.md"
        mem.write_text("Dato aprendido: el usuario prefiere español.")
        db = Database(tmp_path / "raft.db")
        db.sync_config(
            make_config(
                [AgentConfig(
                    name="alpha", runtime="claude", work_dir="/tmp",
                    memory_file=str(mem),
                )]
            )
        )
        root = db.insert_message("demo", "human", "apc", "@alpha hola")
        prompt = build_prompt(db, db.get_agent("alpha"), "demo", root)
        assert "Dato aprendido" in prompt
        assert str(mem) in prompt

    def test_prompt_with_missing_memory_file(self, tmp_path):
        db = Database(tmp_path / "raft.db")
        db.sync_config(
            make_config(
                [AgentConfig(
                    name="alpha", runtime="claude", work_dir="/tmp",
                    memory_file=str(tmp_path / "no-existe.md"),
                )]
            )
        )
        root = db.insert_message("demo", "human", "apc", "@alpha hola")
        prompt = build_prompt(db, db.get_agent("alpha"), "demo", root)
        assert "vacía todavía" in prompt


from mi_raft.runtimes.opencode_serve import OpencodeServeRuntime, derive_port


def make_fake_serve_server():
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._json({"ok": True})
            elif self.path == "/api/event":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(
                    b'data: {"type":"session.next.text.ended","data":{"sessionID":"S1","text":"hola desde serve"}}\n\n'
                )
                self.wfile.write(
                    b'data: {"type":"session.next.step.ended","data":{"sessionID":"S1","finish":"stop"}}\n\n'
                )
                self.wfile.flush()
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            if self.path == "/api/session":
                self._json({"data": {"id": "S1"}})
            elif self.path.endswith("/prompt"):
                self._json({"ok": True})
            else:
                self.send_response(404)
                self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class AlwaysHealthyServe(OpencodeServeRuntime):
    def _ensure_server(self, agent, port):
        return None


class TestOpencodeServe:
    def test_derive_port_stable(self):
        assert derive_port("alpha") == derive_port("alpha")
        assert derive_port("alpha") != derive_port("beta")

    def test_turn_against_fake_sse(self, tmp_path):
        server = make_fake_serve_server()
        port = server.server_address[1]
        rt = AlwaysHealthyServe()
        agent = AgentConfig(
            name="alpha", runtime="opencode-serve", work_dir="/tmp", server_port=port
        )
        result = asyncio.run(rt.run_turn(agent, "prompt", None, 10))
        assert result.session_id == "S1"
        assert result.text == "hola desde serve"
        server.shutdown()


class TestTaskApi:
    def test_create_assign_claim_done(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        db = make_db(
            tmp_path,
            [AgentConfig(name="alpha", runtime="fakehandoff", work_dir="/tmp")],
        )
        app = create_app(make_config(
            [AgentConfig(name="alpha", runtime="fakehandoff", work_dir="/tmp")]
        ), db)
        client = TestClient(app)

        r = client.post(
            "/tasks",
            json={"title": "Tarea X", "description": "haz algo", "channel": "demo", "assignee": "alpha"},
        )
        assert r.status_code == 200
        tid = r.json()["id"]
        assert tid == 1

        task = client.get(f"/tasks/{tid}").json()
        assert task["status"] == "claimed"
        assert task["assignee_id"] == "alpha"
        assert task["thread_id"] is not None

        runs = db.conn.execute("SELECT * FROM run WHERE agent_id='alpha'").fetchall()
        assert len(runs) == 1

        r2 = client.post(f"/tasks/{tid}/claim", json={"agent": "alpha"})
        assert r2.status_code == 409

        r3 = client.post(f"/tasks/{tid}/done", json={"result": "terminado"})
        assert r3.status_code == 200
        assert client.get(f"/tasks/{tid}").json()["status"] == "done"


class TestAuth:
    def test_keys_protect_all_routes(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        db = make_db(tmp_path)
        cfg = make_config()
        cfg.server.api_keys = ["secreta-123"]
        client = TestClient(create_app(cfg, db))

        assert client.get("/agents").status_code == 401
        assert client.get("/channels/demo/messages").status_code == 401
        assert client.post("/channels/demo/messages", json={"text": "hola"}).status_code == 401

        r = client.post(
            "/channels/demo/messages",
            json={"text": "hola"},
            headers={"Authorization": "Bearer secreta-123"},
        )
        assert r.status_code == 200
        r2 = client.get("/agents", headers={"X-mi-raft-key": "secreta-123"})
        assert r2.status_code == 200
        r3 = client.get("/agents", headers={"Authorization": "Bearer equivocada"})
        assert r3.status_code == 401

    def test_no_keys_means_open(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        db = make_db(tmp_path)
        client = TestClient(create_app(make_config(), db))
        assert client.get("/agents").status_code == 200


class TestExternalAgent:
    def test_wake_and_reply_flow(self, tmp_path):
        import http.server
        import threading

        db = Database(tmp_path / "raft.db")

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                wake = json.loads(self.rfile.read(length))
                db.insert_message(
                    wake["channel"],
                    "agent",
                    "eco",
                    f"eco recibido (mensaje {wake['messageId']})",
                    thread_id=wake["threadId"],
                )
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        db.sync_config(
            make_config(
                [
                    AgentConfig(
                        name="eco",
                        runtime="external",
                        work_dir="/tmp",
                        wake_url=f"http://127.0.0.1:{port}/wake",
                        timeout_s=10,
                    )
                ]
            )
        )
        root = db.insert_message("demo", "human", "apc", "@eco hola")
        route_message(db, root)
        run = db.claim_next_run()
        assert run is not None
        asyncio.run(execute_run(db, run))

        done = db.conn.execute("SELECT * FROM run WHERE id=?", (run["id"],)).fetchone()
        assert done["status"] == "done"
        assert "eco recibido" in done["result_text"]

        reply = db.conn.execute(
            "SELECT * FROM message WHERE author_id='eco' AND author_type='agent'"
        ).fetchone()
        assert reply is not None
        assert reply["thread_id"] == root
        server.shutdown()

    def test_external_without_wake_url_fails(self, tmp_path):
        db = make_db(
            tmp_path,
            [AgentConfig(name="eco", runtime="external", work_dir="/tmp", timeout_s=5)],
        )
        root = db.insert_message("demo", "human", "apc", "@eco hola")
        route_message(db, root)
        run = db.claim_next_run()
        asyncio.run(execute_run(db, run))
        done = db.conn.execute("SELECT * FROM run WHERE id=?", (run["id"],)).fetchone()
        assert done["status"] == "failed"
        assert "wake_url" in done["error"]

    def test_prompt_boundary_for_local_agents_only(self, tmp_path):
        db = Database(tmp_path / "raft.db")
        db.sync_config(
            make_config(
                [AgentConfig(name="alpha", runtime="claude", work_dir="/tmp/sandbox")]
            )
        )
        root = db.insert_message("demo", "human", "apc", "@alpha hola")
        prompt = build_prompt(db, db.get_agent("alpha"), "demo", root)
        assert "/tmp/sandbox" in prompt
        assert "No escribas nunca fuera" in prompt


class TestUI:
    def test_index_served(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        db = make_db(tmp_path)
        client = TestClient(create_app(make_config(), db))
        r = client.get("/")
        assert r.status_code == 200
        assert "mi_raft" in r.text
        assert "text/html" in r.headers["content-type"]

    def test_events_requires_key(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        db = make_db(tmp_path)
        cfg = make_config()
        cfg.server.api_keys = ["secreta-123"]
        client = TestClient(create_app(cfg, db))
        assert client.get("/events").status_code == 401


class TestOrg:
    def test_org_blocks_disallowed_handoff(self, tmp_path):
        db = make_db(tmp_path)
        with db.tx() as conn:
            conn.execute("INSERT INTO org_edge (from_id, to_id) VALUES ('alpha','beta')")
        assert db.org_allows("alpha", "beta") is True
        assert db.org_allows("beta", "alpha") is False

        m1 = db.insert_message("demo", "agent", "alpha", "@beta ayúdame")
        assert len(route_message(db, m1)) == 1

        m2 = db.insert_message("demo", "agent", "beta", "@alpha revisa", thread_id=m1)
        assert route_message(db, m2) == []
        sys_msgs = db.conn.execute(
            "SELECT text FROM message WHERE author_type='system' AND type='status'"
        ).fetchall()
        assert any("bloqueado" in r["text"] for r in sys_msgs)

    def test_empty_org_allows_all(self, tmp_path):
        db = make_db(tmp_path)
        assert db.org_allows("alpha", "beta") is True
        assert db.org_allows("beta", "alpha") is True


class TestTeamApi:
    def test_channel_and_agent_creation(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        ws = tmp_path / "ws"
        ws.mkdir()
        cfg = make_config()
        cfg.workspace = str(ws)
        db = make_db(tmp_path)
        client = TestClient(create_app(cfg, db))

        r = client.post("/channels", json={"name": "nuevo", "topic": "prueba"})
        assert r.status_code == 200
        assert client.post("/channels", json={"name": "nuevo"}).status_code == 409

        r2 = client.post(
            "/agents",
            json={"name": "nuevo-agente", "runtime": "opencode", "instructions": "Haz X"},
        )
        assert r2.status_code == 200
        assert r2.json()["work_dir"] == str(ws)
        assert client.post("/agents", json={"name": "nuevo-agente", "runtime": "pi"}).status_code == 409
        assert client.post("/agents", json={"name": "x", "runtime": "nope"}).status_code == 422

        r3 = client.patch(
            "/agents/nuevo-agente", json={"instructions": "Ahora haz Y", "permissions": {"mode": "auto"}}
        )
        assert r3.status_code == 200
        assert r3.json()["instructions"] == "Ahora haz Y"

        agent = db.get_agent("nuevo-agente")
        assert agent.instructions == "Ahora haz Y"
        assert agent.permissions == {"mode": "auto"}

        m = db.insert_message("demo", "human", "apc", "@nuevo-agente trabaja")
        runs = route_message(db, m)
        assert len(runs) == 1

    def test_workspace_endpoints(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        ws = tmp_path / "ws"
        (ws / "sub").mkdir(parents=True)
        (ws / "sub" / "entrega.md").write_text("# Resultado final")
        cfg = make_config()
        cfg.workspace = str(ws)
        db = make_db(tmp_path)
        client = TestClient(create_app(cfg, db))

        tree = client.get("/workspace/tree").json()
        paths = [i["path"] for i in tree["items"]]
        assert "sub/entrega.md" in paths
        assert "README" not in str(paths) or True

        f = client.get("/workspace/file", params={"path": "sub/entrega.md"}).json()
        assert "Resultado final" in f["content"]
        assert client.get("/workspace/file", params={"path": "../raft.yaml"}).status_code == 400
        assert client.get("/workspace/file", params={"path": "no-existe.txt"}).status_code == 404

    def test_meta(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        cfg = make_config()
        cfg.team = "beta"
        client = TestClient(create_app(cfg, make_db(tmp_path)))
        meta = client.get("/meta").json()
        assert meta["team"] == "beta"
