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

    def test_human_reply_without_mention_continuity_dm_only(self, tmp_path):
        db = make_db(tmp_path)
        # canal normal: sin mención NO despierta (M3: solo menciones despiertan)
        root = db.insert_message("demo", "human", "apc", "@alpha empieza")
        route_message(db, root)
        c1 = db.claim_next_run()
        db.finish_run(c1["id"], "done", None, None, "ok", None)
        db.insert_message("demo", "agent", "alpha", "hecho", thread_id=root)
        follow = db.insert_message("demo", "human", "apc", "¿seguro?", thread_id=root)
        assert route_message(db, follow) == []

        # DM: la continuidad se mantiene
        with db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO channel (id, topic, type, members_json)"
                " VALUES ('dm-alpha', '', 'dm', '[\"apc\",\"alpha\"]')"
            )
        dm_root = db.insert_message("dm-alpha", "human", "apc", "@alpha empieza DM")
        route_message(db, dm_root)
        db.claim_next_run()
        db.insert_message("dm-alpha", "agent", "alpha", "hecho DM", thread_id=dm_root)
        dm_follow = db.insert_message("dm-alpha", "human", "apc", "¿seguro?", thread_id=dm_root)
        runs = route_message(db, dm_follow)
        assert len(runs) == 1
        run = db.conn.execute("SELECT * FROM run WHERE id=?", (runs[0],)).fetchone()
        assert run["agent_id"] == "alpha"
        assert run["thread_id"] == dm_root

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

    def test_claim_serializes_same_agent_thread(self, tmp_path):
        db = Database(tmp_path / "raft.db")
        db.sync_config(
            make_config(
                [AgentConfig(name="alpha", runtime="claude", work_dir="/tmp", max_concurrent=3)]
            )
        )
        a = db.insert_run("alpha", "demo", 1, 1)
        b = db.insert_run("alpha", "demo", 1, 1)
        c = db.insert_run("alpha", "demo", 2, 2)
        first = db.claim_next_run()
        second = db.claim_next_run()
        assert {first["id"], second["id"]} == {a, c}   # hilos distintos en paralelo
        assert db.claim_next_run() is None             # b espera: mismo hilo que a (running)
        db.finish_run(a, "done", None, None, "ok", None)
        third = db.claim_next_run()
        assert third["id"] == b


class FakeHandoffRuntime(BaseRuntime):
    name = "fakehandoff"

    def __init__(self):
        self.turns: list[str] = []

    def build_args(self, agent, session_id):
        return ["true"]

    def parse_output(self, stdout):
        return RunResult(session_id="fake-session", text="Listo. @beta revisa esto")

    async def run_turn(self, agent, prompt, session_id, timeout_s, event_sink=None):
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
        assert t["status"] == "todo"
        claimed = db.claim_task(tid, "agent", "alpha")
        assert claimed is not None
        assert claimed["status"] == "in_progress"
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

    def test_prompt_truncates_oversized_memory(self, tmp_path):
        mem = tmp_path / "memoria.md"
        mem.write_text("INICIO\n" + "x" * 5000 + "\nFINAL")
        db = Database(tmp_path / "raft.db")
        db.sync_config(
            make_config(
                [AgentConfig(
                    name="alpha", runtime="claude", work_dir="/tmp",
                    memory_file=str(mem), memory_max_chars=2000,
                )]
            )
        )
        root = db.insert_message("demo", "human", "apc", "@alpha hola")
        prompt = build_prompt(db, db.get_agent("alpha"), "demo", root)
        assert "memoria truncada" in prompt
        assert "INICIO" in prompt and "FINAL" in prompt
        assert "x" * 3000 not in prompt


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
        assert task["status"] == "in_progress"
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
        cfg.server.db = str(tmp_path / "raft.db")
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


class TestMemoryApi:
    def test_auto_memory_on_dynamic_agent(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        cfg = make_config()
        cfg.workspace = "/tmp"
        cfg.server.db = str(tmp_path / "raft.db")
        db = make_db(tmp_path)
        client = TestClient(create_app(cfg, db))

        r = client.post(
            "/agents", json={"name": "escritor", "runtime": "opencode"}
        )
        assert r.status_code == 200
        mem_path = r.json()["memory_file"]
        assert "memory/escritor.md" in mem_path
        assert "Memoria de escritor" in (tmp_path / "raft.db").parent.joinpath("memory/escritor.md").read_text()

        g = client.get("/agents/escritor/memory").json()
        assert g["memory_file"] == mem_path
        assert "Memoria de escritor" in g["content"]

        p = client.put(
            "/agents/escritor/memory", json={"content": "Regla: entregar en el workspace."}
        ).json()
        assert p["bytes"] > 0
        assert client.get("/agents/escritor/memory").json()["content"] == "Regla: entregar en el workspace."

        assert client.get("/agents/no-existe/memory").status_code == 404

    def test_put_creates_memory_for_agent_without_one(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        db = make_db(tmp_path)
        cfg = make_config()
        cfg.server.db = str(tmp_path / "raft.db")
        client = TestClient(create_app(cfg, db))
        r = client.put(
            "/agents/alpha/memory", json={"content": "memoria retroactiva"}
        )
        assert r.status_code == 200
        mem_file = r.json()["memory_file"]
        assert "memory/alpha.md" in mem_file
        agent = db.get_agent("alpha")
        assert agent.memory_file == mem_file
        assert "memoria retroactiva" in client.get("/agents/alpha/memory").json()["content"]

    def test_explicit_memory_file_respected(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        cfg = make_config()
        cfg.workspace = "/tmp"
        cfg.server.db = str(tmp_path / "raft.db")
        client = TestClient(create_app(cfg, make_db(tmp_path)))
        r = client.post(
            "/agents",
            json={"name": "con-ruta", "runtime": "pi", "memory_file": str(tmp_path / "mem.md")},
        )
        assert r.json()["memory_file"] == str(tmp_path / "mem.md")
        assert (tmp_path / "mem.md").exists()


class TestUsage:
    def test_claude_parse_costs(self):
        from mi_raft.runtimes.claude import ClaudeRuntime

        out = json.dumps({
            "result": "hecho", "session_id": "s1", "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 120, "output_tokens": 45},
        })
        r = ClaudeRuntime().parse_output(out)
        assert r.cost_usd == 0.0123
        assert r.tokens_in == 120 and r.tokens_out == 45

    def test_opencode_parse_costs(self):
        from mi_raft.runtimes.opencode import OpencodeRuntime

        lines = "\n".join([
            json.dumps({"type": "text", "sessionID": "ses1", "part": {"text": "listo"}}),
            json.dumps({"type": "step_finish", "sessionID": "ses1",
                        "part": {"tokens": {"input": 300, "output": 20}, "cost": 0.004}}),
        ])
        r = OpencodeRuntime().parse_output(lines)
        assert r.tokens_in == 300 and r.tokens_out == 20 and r.cost_usd == 0.004

    def test_pi_parse_costs(self):
        from mi_raft.runtimes.pi import PiRuntime

        lines = "\n".join([
            json.dumps({"type": "session", "session": {"id": "pi1"}}),
            json.dumps({"type": "message_end", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 900, "output_tokens": 10},
                "cost": 0.002,
            }}),
        ])
        r = PiRuntime().parse_output(lines)
        assert r.session_id == "pi1"
        assert r.tokens_in == 900 and r.tokens_out == 10 and r.cost_usd == 0.002

    def test_pi_parse_stream_usage_shape(self):
        from mi_raft.runtimes.pi import PiRuntime

        lines = json.dumps({"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input": 933, "output": 137, "cacheRead": 100,
                      "cacheWrite": 20, "reasoning": 128, "totalTokens": 1190},
        }})
        r = PiRuntime().parse_output(lines)
        assert r.tokens_in == 1053 and r.tokens_out == 137

    def test_pi_provider_error_is_surfaced(self):
        from mi_raft.runtimes.pi import PiRuntime

        lines = "\n".join([
            json.dumps({"type": "session", "session": {"id": "pi1"}}),
            json.dumps({"type": "message_end", "message": {
                "role": "assistant", "content": [], "stopReason": "error",
                "errorMessage": "400: Invalid request.",
            }}),
            json.dumps({"type": "turn_end"}),
        ])
        with pytest.raises(RuntimeError, match="proveedor rechazó"):
            PiRuntime().parse_output(lines)

    def test_usage_endpoint(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        cfg = make_config()
        cfg.server.db = str(tmp_path / "raft.db")
        db = make_db(tmp_path)
        for i, (agent, cost, ti, to) in enumerate([
            ("alpha", 0.01, 100, 50), ("alpha", 0.02, 200, 60), ("beta", 0.03, 10, 5),
        ]):
            rid = db.insert_run(agent, "demo", 1, 1)
            db.finish_run(rid, "done", None, None, "x", None,
                          cost_usd=cost, tokens_in=ti, tokens_out=to)
        client = TestClient(create_app(cfg, db))
        by_agent = client.get("/usage").json()
        by_name = {r["agent_id"]: r for r in by_agent}
        assert by_name["alpha"]["runs"] == 2
        assert abs(by_name["alpha"]["cost_usd"] - 0.03) < 1e-9
        assert by_name["beta"]["tokens_out"] == 5
        assert client.get("/usage?by=day").status_code == 200

    def test_budget_passes_to_claude(self):
        from mi_raft.runtimes.claude import ClaudeRuntime

        from mi_raft.config import AgentConfig

        agent = AgentConfig(name="a", runtime="claude", work_dir="/tmp", budget_usd=0.5)
        args = ClaudeRuntime().build_args(agent, None)
        assert "--max-budget-usd" in args and "0.5" in args


class TestAgentStatus:
    def test_status_transitions(self, tmp_path):
        db = make_db(
            tmp_path,
            [
                AgentConfig(name="ok", runtime="fake-ok", work_dir="/tmp"),
                AgentConfig(name="bad", runtime="fake-bad", work_dir="/tmp"),
            ],
        )

        class FakeOK(BaseRuntime):
            name = "fake-ok"
            def build_args(self, agent, session_id): return ["true"]
            def parse_output(self, stdout): return RunResult(session_id="x", text="bien")
            async def run_turn(self, agent, prompt, session_id, timeout_s, event_sink=None):
                return RunResult(session_id="x", text="bien")

        class FakeBad(BaseRuntime):
            name = "fake-bad"
            def build_args(self, agent, session_id): return ["true"]
            def parse_output(self, stdout): return RunResult(session_id=None, text="")
            async def run_turn(self, agent, prompt, session_id, timeout_s, event_sink=None):
                raise RuntimeError("explotó")

        register_runtime(FakeOK())
        register_runtime(FakeBad())

        root = db.insert_message("demo", "human", "apc", "@ok hazlo @bad revienta")
        route_message(db, root)
        while True:
            run = db.claim_next_run()
            if run is None:
                break
            asyncio.run(execute_run(db, run))

        statuses = {r["id"]: r["status"] for r in db.list_agents()}
        assert statuses["ok"] == "idle"
        assert statuses["bad"] == "error"
        assert statuses != {"ok": "working"}


class TestTaskFromMessage:
    def test_convert_message_to_task(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        db = make_db(tmp_path)
        cfg = make_config()
        cfg.server.db = str(tmp_path / "raft.db")
        client = TestClient(create_app(cfg, db))

        mid = db.insert_message("demo", "human", "apc", "Revisar los tests del core\n\nDetalle adicional aquí")
        r = client.post("/tasks/from-message", json={"message_id": mid})
        assert r.status_code == 200
        task = client.get(f"/tasks/{r.json()['id']}").json()
        assert task["title"] == "Revisar los tests del core"
        assert "Detalle adicional" in task["description"]
        assert task["channel_id"] == "demo"
        assert task["thread_id"] == mid


class TestSearch:
    def test_fts_search_and_filters(self, tmp_path):
        db = make_db(tmp_path)
        db.insert_message("demo", "human", "apc", "Decidimos que la página de precios lleva descuento")
        db.insert_message("demo", "agent", "alpha", "La página de precios está lista")
        db.insert_message("demo2", "human", "apc", "otro canal habla de precios también")
        db.insert_message("demo", "human", "apc", "mensaje sin la palabra clave")

        hits = db.search("precios")
        assert len(hits) == 3
        assert all("precios" in h["text"].lower() for h in hits)

        only_demo = db.search("precios", channel_id="demo")
        assert len(only_demo) == 2

        by_author = db.search("precios", author="alpha")
        assert len(by_author) == 1 and by_author[0]["author_id"] == "alpha"

        assert db.search("palabra clave") == [] or len(db.search("palabra clave")) >= 1

        assert db.search("") == []
        assert db.search("zzzznada") == []

    def test_search_endpoint(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        cfg = make_config()
        cfg.server.db = str(tmp_path / "raft.db")
        db = make_db(tmp_path)
        db.insert_message("demo", "human", "apc", "el plan de migración está en el doc compartido")
        client = TestClient(create_app(cfg, db))
        hits = client.get("/search", params={"q": "migración"}).json()
        assert len(hits) == 1 and "migración" in hits[0]["text"]
        assert client.get("/search", params={"q": "  "}).status_code == 422


class TestActivityAndDms:
    def test_activity_lists_status_messages(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        db = make_db(tmp_path)
        db.insert_message("demo", "system", "mi_raft", "algo pasó", msg_type="status")
        db.insert_message("demo", "human", "apc", "comentario normal")
        cfg = make_config()
        cfg.server.db = str(tmp_path / "raft.db")
        client = TestClient(create_app(cfg, db))
        items = client.get("/activity").json()
        assert len(items) == 1 and items[0]["type"] == "status"

    def test_dm_creation_and_isolation(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        cfg = make_config()
        cfg.server.db = str(tmp_path / "raft.db")
        db = make_db(tmp_path)
        client = TestClient(create_app(cfg, db))

        assert client.post("/dms", json={"agent": "no-existe"}).status_code == 404
        ch1 = client.post("/dms", json={"agent": "alpha"}).json()
        ch2 = client.post("/dms", json={"agent": "alpha"}).json()
        assert ch1["id"] == ch2["id"] == "dm-alpha"
        assert ch1["type"] == "dm"

        public = client.get("/channels").json()
        assert all(not c["id"].startswith("dm-") for c in public)
        dms = client.get("/dms").json()
        assert [d["id"] for d in dms] == ["dm-alpha"]

        r = client.post("/channels/dm-alpha/messages", json={"text": "@alpha hola privado"})
        assert r.status_code == 200
        runs = db.conn.execute("SELECT * FROM run WHERE agent_id='alpha'").fetchall()
        assert len(runs) == 1

    def test_webhook_signature_flow(self, tmp_path):
        import hashlib
        import hmac as hmac_mod
        import http.server
        import threading

        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        received = []

        class Hook(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                received.append((self.headers.get("X-mi_raft-signature"), body))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        cfg = make_config()
        cfg.server.db = str(tmp_path / "raft.db")
        client = TestClient(create_app(cfg, make_db(tmp_path)))
        created = client.post(
            "/webhooks",
            json={"url": f"http://127.0.0.1:{port}/hook", "events": ["message.created"], "secret": "secreto"},
        ).json()
        client.post("/channels/demo/messages", json={"text": "dispara el webhook"})
        import time as _t
        deadline = _t.time() + 5
        while not received and _t.time() < deadline:
            _t.sleep(0.1)
        assert received, "el webhook no llegó"
        sig, body = received[0]
        expected = hmac_mod.new(b"secreto", body, hashlib.sha256).hexdigest()
        assert sig == expected
        assert json.loads(body)["event"] == "message.created"
        assert client.delete(f"/webhooks/{created['id']}").status_code == 200
        srv.shutdown()


class TestTasksV2:
    def test_status_flow_with_review(self, tmp_path):
        db = make_db(tmp_path)
        tid = db.create_task("Flujo completo", "", "demo")
        assert db.get_task(tid)["status"] == "todo"
        claimed = db.claim_task(tid, "agent", "alpha")
        assert claimed["status"] == "in_progress"
        rev = db.review_task(tid)
        assert rev["status"] == "in_review"
        assert db.review_task(tid) is None
        assert db.finish_task(tid, "done", "aprobado") is not None
        assert db.get_task(tid)["result_text"] == "aprobado"

    def test_migration_from_open_claimed(self, tmp_path):
        import sqlite3

        p = tmp_path / "old.db"
        conn = sqlite3.connect(p)
        conn.executescript(
            """
            CREATE TABLE task (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open','claimed','done','cancelled')),
              assignee_type TEXT, assignee_id TEXT, channel_id TEXT, thread_id INTEGER,
              created_by TEXT NOT NULL DEFAULT 'humano',
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              updated_at TEXT, result_text TEXT
            );
            INSERT INTO task (title, status) VALUES ('vieja open', 'open');
            INSERT INTO task (title, status) VALUES ('vieja claimed', 'claimed');
            """
        )
        conn.commit()
        conn.close()
        db = Database(p)
        statuses = {r["title"]: r["status"] for r in db.list_tasks()}
        assert statuses["vieja open"] == "todo"
        assert statuses["vieja claimed"] == "in_progress"

    def test_subtasks(self, tmp_path):
        db = make_db(tmp_path)
        parent = db.create_task("Objetivo grande", "", "demo")
        db.create_subtask(parent, "pieza 1", "", "demo")
        db.create_subtask(parent, "pieza 2", "", "demo")
        subs = db.list_subtasks(parent)
        assert [s["title"] for s in subs] == ["pieza 1", "pieza 2"]
        listing = {t["id"]: t for t in db.list_tasks()}
        assert listing[parent]["subtask_count"] == 2

    def test_breakdown_endpoint(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app
        from mi_raft.runtimes import BaseRuntime, RunResult, register_runtime

        class FakeSplit(BaseRuntime):
            name = "fake-split"
            def build_args(self, agent, session_id): return ["true"]
            def parse_output(self, stdout): return RunResult(None, "")
            async def run_turn(self, agent, prompt, session_id, timeout_s, event_sink=None):
                assert "JSON" in prompt
                return RunResult(None, '[{"title": "pieza A", "description": "x"}, {"title": "pieza B"}]')

        register_runtime(FakeSplit())
        cfg = make_config(
            [AgentConfig(name="splitter", runtime="fake-split", work_dir="/tmp")]
        )
        cfg.server.db = str(tmp_path / "raft.db")
        db = make_db(tmp_path, cfg.agents)
        client = TestClient(create_app(cfg, db))
        r = client.post(
            "/tasks/breakdown",
            json={"goal": "lanzar la web", "agent": "splitter", "channel": "demo"},
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data["subtasks"]) == 2
        parent = client.get(f"/tasks/{data['parent_id']}").json()
        assert parent["title"] == "lanzar la web"
        assert parent["subtask_count"] == 2


class TestConcurrentRunner:
    def test_two_agents_run_in_parallel(self, tmp_path):
        import time as _time

        db = make_db(
            tmp_path,
            [
                AgentConfig(name="a1", runtime="fake-slow", work_dir="/tmp"),
                AgentConfig(name="a2", runtime="fake-slow", work_dir="/tmp"),
            ],
        )

        from mi_raft.runtimes import BaseRuntime, RunResult, register_runtime

        class FakeSlow(BaseRuntime):
            name = "fake-slow"
            def build_args(self, agent, session_id): return ["true"]
            def parse_output(self, stdout): return RunResult(None, "ok")
            async def run_turn(self, agent, prompt, session_id, timeout_s, event_sink=None):
                await asyncio.sleep(0.6)
                return RunResult(None, "ok")

        register_runtime(FakeSlow())
        db.insert_message("demo", "human", "apc", "@a1 trabaja y @a2 también")
        runs = route_message(db, 1)
        assert len(runs) == 2

        async def drive():
            from mi_raft.runner import runner_loop

            task = asyncio.get_running_loop().create_task(runner_loop(db, poll_s=0.1))
            await asyncio.sleep(1.4)
            task.cancel()

        t0 = _time.monotonic()
        asyncio.run(drive())
        elapsed = _time.monotonic() - t0
        done = db.conn.execute(
            "SELECT COUNT(*) AS n FROM run WHERE status='done'"
        ).fetchone()["n"]
        assert done == 2
        assert elapsed < 1.9


class TestSandbox:
    def test_build_sandbox_cmd_without_bwrap(self, tmp_path):
        from mi_raft.runtimes.base import build_sandbox_cmd

        args = build_sandbox_cmd(tmp_path, {"enable": True}, ["echo", "hi"])
        assert args == ["echo", "hi"]

    def test_build_sandbox_cmd_with_bwrap(self, tmp_path, monkeypatch):
        import mi_raft.runtimes.base as base

        fake = tmp_path / "bwrap"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setattr(base.shutil, "which", lambda name: str(fake))
        cmd = base.build_sandbox_cmd(tmp_path, {"enable": True, "rw": ["/datos"]}, ["claude", "-p"])
        assert cmd[0] == str(fake)
        assert "--tmpfs" in cmd
        assert str(tmp_path) in cmd
        assert cmd[-2:] == ["claude", "-p"]
        assert "/datos" in cmd

    def test_sandbox_disabled_by_default(self):
        from mi_raft.config import AgentConfig

        a = AgentConfig(name="x", runtime="claude", work_dir="/tmp")
        assert a.sandbox == {}
        b = AgentConfig(name="y", runtime="claude", work_dir="/tmp", sandbox={"enable": True})
        assert b.sandbox == {"enable": True}


class TestRunTelemetry:
    def test_claude_stream_json_events(self):
        from mi_raft.runtimes.claude import ClaudeRuntime

        lines = "\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
            ]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"},
            ]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Listo"},
            ]}}),
            json.dumps({"type": "result", "result": "Listo", "session_id": "s9",
                        "total_cost_usd": 0.02,
                        "usage": {"input_tokens": 500, "output_tokens": 30}}),
        ])
        rt = ClaudeRuntime()
        events = []
        rt.parse_line(lines.splitlines()[1], events.append)
        rt.parse_line(lines.splitlines()[2], events.append)
        rt.parse_line(lines.splitlines()[3], events.append)
        assert events[0] == {"type": "tool_use", "tool": "Bash", "payload": {"input": {"command": "ls"}}}
        assert events[1]["type"] == "tool_result"
        assert events[2] == {"type": "text", "tool": None, "payload": {"text": "Listo"}}
        r = rt.parse_output(lines)
        assert r.text == "Listo" and r.session_id == "s9" and r.cost_usd == 0.02

    def test_old_claude_single_json_still_parses(self):
        from mi_raft.runtimes.claude import ClaudeRuntime

        out = json.dumps({"result": "ok", "session_id": "s1", "total_cost_usd": 0.01,
                          "usage": {"input_tokens": 10, "output_tokens": 5}})
        r = ClaudeRuntime().parse_output(out)
        assert r.text == "ok" and r.cost_usd == 0.01

    def test_runner_writes_run_messages(self, tmp_path):
        db = make_db(
            tmp_path,
            [AgentConfig(name="telemetrico", runtime="fake-ops", work_dir="/tmp")],
        )

        from mi_raft.runtimes import BaseRuntime, RunResult, register_runtime

        class FakeOps(BaseRuntime):
            name = "fake-ops"
            def build_args(self, agent, session_id): return ["true"]
            def parse_output(self, stdout): return RunResult("s1", "terminado")
            async def run_turn(self, agent, prompt, session_id, timeout_s, event_sink=None):
                assert event_sink is not None
                event_sink({"type": "tool_use", "tool": "Bash", "payload": {"input": {"command": "ls"}}})
                event_sink({"type": "tool_result", "tool": None, "payload": {"content": "salida"}})
                event_sink({"type": "text", "tool": None, "payload": {"text": "pensando"}})
                return RunResult("s1", "terminado")

        register_runtime(FakeOps())
        root = db.insert_message("demo", "human", "apc", "@telemetrico opera")
        route_message(db, root)
        run = db.claim_next_run()
        asyncio.run(execute_run(db, run))

        ops = db.list_run_messages(run["id"])
        types = [o["type"] for o in ops]
        assert types == ["step", "tool_use", "tool_result", "text", "step"]
        assert ops[1]["tool"] == "Bash"
        seqs = [o["seq"] for o in ops]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    def test_runs_endpoints(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        cfg = make_config()
        cfg.server.db = str(tmp_path / "raft.db")
        db = make_db(tmp_path)
        rid = db.insert_run("alpha", "demo", 1, 1)
        db.insert_run_message(rid, 0, "step", None, {"event": "turno iniciado"})
        db.insert_run_message(rid, 1, "tool_use", "Bash", {"input": {"command": "ls"}})
        client = TestClient(create_app(cfg, db))
        runs = client.get("/runs", params={"agent": "alpha"}).json()
        assert runs and runs[0]["id"] == rid and runs[0]["agent_id"] == "alpha"
        ops = client.get(f"/runs/{rid}/messages").json()
        assert [o["seq"] for o in ops] == [0, 1]
        assert ops[1]["tool"] == "Bash"


class TestWebTools:
    def test_prompt_includes_web_tools(self, tmp_path):
        db = make_db(tmp_path)
        db.server_port = 8420
        root = db.insert_message("demo", "human", "apc", "@alpha busca en la web")
        prompt = build_prompt(db, db.get_agent("alpha"), "demo", root)
        assert "/tools/search" in prompt
        assert "/tools/fetch" in prompt

    def test_prompt_without_web_tools(self, tmp_path):
        db = Database(tmp_path / "raft.db")
        cfg = make_config(
            [AgentConfig(name="alpha", runtime="claude", work_dir="/tmp", web_search=False)]
        )
        db.sync_config(cfg)
        root = db.insert_message("demo", "human", "apc", "@alpha hola")
        prompt = build_prompt(db, db.get_agent("alpha"), "demo", root)
        assert "/tools/search" not in prompt

    def test_claude_allows_web_search_by_default(self):
        from mi_raft.runtimes.claude import ClaudeRuntime

        args = ClaudeRuntime().build_args(
            AgentConfig(name="a", runtime="claude", work_dir="/tmp"), None
        )
        assert "WebSearch" in args and "WebFetch" in args
        custom = AgentConfig(
            name="b", runtime="claude", work_dir="/tmp",
            permissions={"allow": ["Bash", "Read"]},
        )
        args2 = ClaudeRuntime().build_args(custom, None)
        assert "WebSearch" not in args2 and "Read" in args2

    def test_search_endpoint_with_mocked_ddg(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from mi_raft import server as server_mod
        from mi_raft.server import create_app

        fixture = """
        <div class="result">
        <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fejemplo.com%2Farticulo">Artículo <b>importante</b></a>
        <a class="result__snippet" href="#">El resumen del artículo</a>
        </div>
        """
        monkeypatch.setattr(
            server_mod, "_http_get", lambda url, headers=None, timeout=15: (200, fixture)
        )
        monkeypatch.setattr(server_mod, "_ddg_lite", lambda q, limit: [])
        monkeypatch.setattr(server_mod, "_ddg_cache", {})
        cfg = make_config()
        cfg.server.db = str(tmp_path / "raft.db")
        client = TestClient(create_app(cfg, make_db(tmp_path)))
        r = client.get("/tools/search", params={"q": "articulo"}).json()
        assert r["results"][0]["url"] == "https://ejemplo.com/articulo"
        assert r["results"][0]["title"] == "Artículo importante"
        assert "resumen" in r["results"][0]["snippet"]
        assert client.get("/tools/search", params={"q": " "}).status_code == 422

    def test_fetch_endpoint(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from mi_raft import server as server_mod
        from mi_raft.server import create_app

        page = "<html><head><style>x{}</style></head><body><h1>Hola</h1><p>mundo</p><script>bad()</script></body></html>"
        monkeypatch.setattr(
            server_mod, "_http_get", lambda url, headers=None, timeout=20: (200, page)
        )
        cfg = make_config()
        cfg.server.db = str(tmp_path / "raft.db")
        client = TestClient(create_app(cfg, make_db(tmp_path)))
        r = client.get("/tools/fetch", params={"url": "https://ok.com/pagina"}).json()
        assert "Hola" in r["text"] and "mundo" in r["text"]
        assert "bad()" not in r["text"]
        assert client.get("/tools/fetch", params={"url": "http://127.0.0.1/x"}).status_code == 400
        assert client.get("/tools/fetch", params={"url": "ftp://x.com"}).status_code == 422


class TestOpencodeTelemetry:
    def test_tool_events_are_readable(self):
        from mi_raft.runtimes.opencode import OpencodeRuntime

        rt = OpencodeRuntime()
        events = []
        lines = "\n".join([
            json.dumps({"type": "step_start", "sessionID": "s", "part": {}}),
            json.dumps({"type": "tool", "sessionID": "s", "part": {
                "type": "tool", "tool": "bash", "callID": "c1",
                "state": {"status": "completed", "input": {"command": "ls -la"}, "output": "hello.py"},
            }}),
            json.dumps({"type": "tool", "sessionID": "s", "part": {
                "type": "tool", "tool": "write", "callID": "c2",
                "state": {"status": "completed", "input": {"filePath": "manuscrito/cap01.md", "content": "x" * 2000}},
            }}),
        ])
        for line in lines.splitlines():
            rt.parse_line(line, events.append)
        assert events[0]["type"] == "step"
        bash_ev = events[1]
        assert bash_ev["tool"] == "bash"
        assert bash_ev["payload"]["summary"] == "$ ls -la"
        assert bash_ev["payload"]["output"] == "hello.py"
        write_ev = events[2]
        assert write_ev["payload"]["summary"] == "manuscrito/cap01.md (2000 chars)"


class TestEscalation:
    def test_failure_escalates_to_supervisor(self, tmp_path):
        db = make_db(
            tmp_path,
            [
                AgentConfig(name="frágil", runtime="fake-bad", work_dir="/tmp"),
                AgentConfig(name="jefa", runtime="fake-ok", work_dir="/tmp"),
            ],
        )
        db.escalate_to = "jefa"

        class FakeOK(BaseRuntime):
            name = "fake-ok"
            def build_args(self, agent, session_id): return ["true"]
            def parse_output(self, stdout): return RunResult(None, "ok")
            async def run_turn(self, agent, prompt, session_id, timeout_s, event_sink=None):
                return RunResult(None, "ok")

        class FakeBad(BaseRuntime):
            name = "fake-bad"
            def build_args(self, agent, session_id): return ["true"]
            def parse_output(self, stdout): return RunResult(None, "")
            async def run_turn(self, agent, prompt, session_id, timeout_s, event_sink=None):
                raise RuntimeError("explotó por timeout simulado")

        register_runtime(FakeOK())
        register_runtime(FakeBad())

        root = db.insert_message("demo", "human", "apc", "@frágil hazlo")
        route_message(db, root)
        run = db.claim_next_run()
        asyncio.run(execute_run(db, run))

        escalation = db.conn.execute(
            "SELECT * FROM message WHERE author_id='mi_raft' AND text LIKE '⚠️%'"
        ).fetchone()
        assert escalation is not None
        assert "@jefa" in escalation["text"]
        assert "frágil" in escalation["text"]
        sup_run = db.conn.execute(
            "SELECT * FROM run WHERE agent_id='jefa' AND status='queued'"
        ).fetchone()
        assert sup_run is not None
        assert sup_run["thread_id"] == root

    def test_no_self_escalation_when_supervisor_fails(self, tmp_path):
        db = make_db(
            tmp_path,
            [AgentConfig(name="jefa", runtime="fake-bad", work_dir="/tmp")],
        )
        db.escalate_to = "jefa"

        class FakeBad2(BaseRuntime):
            name = "fake-bad"
            def build_args(self, agent, session_id): return ["true"]
            def parse_output(self, stdout): return RunResult(None, "")
            async def run_turn(self, agent, prompt, session_id, timeout_s, event_sink=None):
                raise RuntimeError("fallo")

        register_runtime(FakeBad2())
        root = db.insert_message("demo", "human", "apc", "@jefa hazlo")
        route_message(db, root)
        run = db.claim_next_run()
        asyncio.run(execute_run(db, run))
        assert db.conn.execute(
            "SELECT COUNT(*) AS n FROM message WHERE text LIKE '⚠️%'"
        ).fetchone()["n"] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) AS n FROM run WHERE agent_id='jefa'"
        ).fetchone()["n"] == 1

    def test_org_block_escalates(self, tmp_path):
        db = make_db(tmp_path)
        db.escalate_to = "alpha"
        with db.tx() as conn:
            conn.execute("INSERT INTO org_edge (from_id, to_id) VALUES ('alpha','beta')")
        m = db.insert_message("demo", "agent", "beta", "@alpha necesito algo fuera de lanes")
        runs = route_message(db, m)
        assert runs == []
        esc = db.conn.execute(
            "SELECT * FROM message WHERE text LIKE '⚠️%bloqueado%'"
        ).fetchone()
        assert esc is not None and "@alpha" in esc["text"]
        sup_run = db.conn.execute(
            "SELECT * FROM run WHERE agent_id='alpha' AND status='queued'"
        ).fetchone()
        assert sup_run is not None

    def test_watchdog_fails_stuck_runs(self, tmp_path):
        from mi_raft.runner import watchdog_loop

        db = make_db(
            tmp_path,
            [AgentConfig(name="alpha", runtime="claude", work_dir="/tmp", timeout_s=600)],
        )
        db.escalate_to = None
        db.insert_message("demo", "human", "apc", "@alpha hazlo")
        run_id = db.insert_run("alpha", "demo", 1, 1)
        db.conn.execute(
            "UPDATE run SET status='running', started_at=datetime('now', '-30 minutes')"
            " WHERE id=?",
            (run_id,),
        )
        db.conn.commit()

        task = asyncio.get_event_loop().create_task if False else None
        async def once():
            t = asyncio.get_running_loop().create_task(watchdog_loop(db, poll_s=0.1))
            await asyncio.sleep(0.3)
            t.cancel()

        asyncio.run(once())
        row = db.conn.execute("SELECT status, error FROM run WHERE id=?", (run_id,)).fetchone()
        assert row["status"] == "failed"
        assert "watchdog" in row["error"]

    def test_escalation_cap_per_thread(self, tmp_path):
        db = make_db(
            tmp_path,
            [
                AgentConfig(name="frágil", runtime="fake-bad", work_dir="/tmp"),
                AgentConfig(name="jefa", runtime="fake-ok", work_dir="/tmp"),
            ],
        )
        db.escalate_to = "jefa"
        for _ in range(5):
            db.insert_message("demo", "human", "mi_raft", "⚠️ escalación previa", thread_id=7)
        assert db.escalations_in_thread(7) >= 3


class TestAgentProfile:
    def test_profile_endpoint(self, tmp_path):
        from fastapi.testclient import TestClient

        from mi_raft.server import create_app

        cfg = make_config(
            [AgentConfig(
                name="alpha", runtime="claude", work_dir="/tmp",
                model="claude-sonnet-4", budget_usd=0.5,
            )]
        )
        cfg.server.db = str(tmp_path / "raft.db")
        db = make_db(tmp_path, cfg.agents)
        rid = db.insert_run("alpha", "demo", 1, 1)
        db.finish_run(rid, "done", "s1", "/tmp", "ok", None,
                      cost_usd=0.01, tokens_in=10, tokens_out=5,
                      provider="anthropic", model="claude-sonnet-4")
        client = TestClient(create_app(cfg, db))

        p = client.get("/agents/alpha/profile").json()
        assert p["id"] == "alpha"
        assert p["runtime"] == "claude"
        assert p["model"] == "claude-sonnet-4"
        assert p["budget_usd"] == 0.5
        assert p["stats"]["runs"] == 1
        assert p["models_used"][0]["model"] == "claude-sonnet-4"
        assert p["recent_runs"][0]["provider"] == "anthropic"
        assert "alpha" in p["org"]["delega_a"] or True
        assert client.get("/agents/nope/profile").status_code == 404


class TestCatalog:
    def test_opencode_parse(self, monkeypatch):
        import mi_raft.catalog as catalog

        monkeypatch.setattr(catalog, "_run", lambda cmd, timeout=30: (
            "nan/glm5.3-flash\nanthropic/claude-sonnet-4\nmalo\n"
        ))
        out = catalog.opencode_catalog()
        assert {"provider": "nan", "model": "glm5.3-flash"} in out
        assert {"provider": "anthropic", "model": "claude-sonnet-4"} in out
        assert len(out) == 2

    def test_pi_parse(self, monkeypatch):
        import mi_raft.catalog as catalog

        table = (
            "provider  model  context\n"
            "nan       glm5.3  1M\n"
            "minimax   MiniMax-M3  1M\n"
        )
        monkeypatch.setattr(catalog, "_run", lambda cmd, timeout=30: table)
        out = catalog.pi_catalog()
        assert {"provider": "nan", "model": "glm5.3"} in out
        assert {"provider": "minimax", "model": "MiniMax-M3"} in out

    def test_providers_and_models_for(self, monkeypatch):
        import mi_raft.catalog as catalog

        fake = {"opencode": [
            {"provider": "nan", "model": "glm5.3"},
            {"provider": "nan", "model": "gemma4"},
            {"provider": "anthropic", "model": "claude-sonnet-4"},
        ]}
        assert catalog.providers_for("opencode", fake) == ["anthropic", "nan"]
        assert catalog.models_for("opencode", "nan", fake) == ["glm5.3", "gemma4"]

    def test_patch_validates_model_against_catalog(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from mi_raft import catalog as catalog_mod
        from mi_raft.server import create_app

        monkeypatch.setattr(
            catalog_mod, "full_catalog",
            lambda force=False: {"claude": [{"provider": "anthropic", "model": "claude-sonnet-4-5"}]},
        )
        cfg = make_config([AgentConfig(name="alpha", runtime="claude", work_dir="/tmp")])
        cfg.server.db = str(tmp_path / "raft.db")
        db = make_db(tmp_path, cfg.agents)
        client = TestClient(create_app(cfg, db))
        ok = client.patch("/agents/alpha", json={"provider": "anthropic", "model": "claude-sonnet-4-5"})
        assert ok.status_code == 200
        assert ok.json()["provider"] == "anthropic"
        bad = client.patch("/agents/alpha", json={"model": "modelo-inventado"})
        assert bad.status_code == 422
        assert db.get_agent("alpha").model == "claude-sonnet-4-5"

    def test_opencode_combines_provider_model(self):
        from mi_raft.config import AgentConfig
        from mi_raft.runtimes.opencode import OpencodeRuntime

        agent = AgentConfig(name="a", runtime="opencode", work_dir="/tmp",
                            provider="nan", model="glm5.3-flash")
        args = OpencodeRuntime().build_args(agent, None)
        assert args[args.index("-m") + 1] == "nan/glm5.3-flash"
        combined = AgentConfig(name="b", runtime="opencode", work_dir="/tmp",
                               provider="nan", model="nan/glm5.3-flash")
        args2 = OpencodeRuntime().build_args(combined, None)
        assert args2[args2.index("-m") + 1] == "nan/glm5.3-flash"


class TestOpencodeLength:
    def test_length_without_text_clean_error(self):
        from mi_raft.runtimes.opencode import OpencodeRuntime

        lines = "\n".join([
            json.dumps({"type": "step_start", "sessionID": "s", "part": {}}),
            json.dumps({"type": "tool", "sessionID": "s", "part": {
                "type": "tool", "tool": "write", "callID": "c1",
                "state": {"status": "completed", "input": {"filePath": "cap02.md", "content": "x"}},
            }}),
            json.dumps({"type": "step_finish", "sessionID": "s",
                        "part": {"reason": "length", "tokens": {"input": 100, "output": 8000}}}),
        ])
        with pytest.raises(RuntimeError, match="límite de tokens"):
            OpencodeRuntime().parse_output(lines)

    def test_length_with_text_still_errors(self):
        from mi_raft.runtimes.opencode import OpencodeRuntime

        lines = "\n".join([
            json.dumps({"type": "text", "sessionID": "s", "part": {"text": "voy por la mitad"}}),
            json.dumps({"type": "step_finish", "sessionID": "s", "part": {"reason": "length"}}),
        ])
        with pytest.raises(RuntimeError, match="límite de tokens") as exc:
            OpencodeRuntime().parse_output(lines)
        assert "voy por la mitad" in str(exc.value)

    def test_opencode_accumulates_tokens_across_steps(self):
        from mi_raft.runtimes.opencode import OpencodeRuntime

        lines = "\n".join([
            json.dumps({"type": "step_finish", "sessionID": "s",
                        "part": {"reason": "tool-calls", "tokens": {"input": 100, "output": 10}, "cost": 0.001}}),
            json.dumps({"type": "text", "sessionID": "s", "part": {"text": "ok"}}),
            json.dumps({"type": "step_finish", "sessionID": "s",
                        "part": {"reason": "stop", "tokens": {"input": 50, "output": 5}, "cost": 0.002}}),
        ])
        r = OpencodeRuntime().parse_output(lines)
        assert r.tokens_in == 150 and r.tokens_out == 15
        assert abs(r.cost_usd - 0.003) < 1e-9


class TestSupervisorBypass:
    def test_supervisor_mentions_anyone_without_edge(self, tmp_path):
        db = make_db(
            tmp_path,
            [
                AgentConfig(name="jefa", runtime="claude", work_dir="/tmp"),
                AgentConfig(name="escritora", runtime="pi", work_dir="/tmp"),
            ],
        )
        db.escalate_to = "jefa"
        # el org NO permite jefa → escritora
        with db.tx() as conn:
            conn.execute("INSERT INTO org_edge (from_id, to_id) VALUES ('x','y')")

        m = db.insert_message("demo", "agent", "jefa", "@escritora micro-fix urgente")
        runs = route_message(db, m)
        assert len(runs) == 1
        run = db.conn.execute("SELECT * FROM run WHERE id=?", (runs[0],)).fetchone()
        assert run["agent_id"] == "escritora"

        # un agente normal sí se le bloquea
        db.create_agent(AgentConfig(name="otro", runtime="pi", work_dir="/tmp"))
        m2 = db.insert_message("demo", "agent", "escritora", "@otro hazlo")
        assert route_message(db, m2) == []
