from __future__ import annotations

import asyncio
import hashlib
import hmac
import html as _html
import json
import re as _re
import secrets as _secrets
import shutil
import threading
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import field
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .catalog import full_catalog, models_for, providers_for
from .config import KNOWN_RUNTIMES, Config
from .db import Database
from .router import route_message
from .runner import start_background_tasks

STATIC_DIR = Path(__file__).parent / "static"
def _http_get(url: str, headers: dict | None = None, timeout: int = 15) -> tuple[int, str]:
    import subprocess

    curl = shutil.which("curl")
    if curl:
        cmd = [curl, "-s", "-m", str(timeout), "-w", "\n%{http_code}", "-A", "Mozilla/5.0 (mi_raft)"]
        for key, value in (headers or {}).items():
            cmd += ["-H", f"{key}: {value}"]
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        output = result.stdout.decode("utf-8", errors="replace")
        if "\n" in output:
            body, _, code = output.rpartition("\n")
            if code.strip().isdigit():
                return int(code.strip()), body
        return 200, output
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (mi_raft)", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _strip_tags(fragment: str) -> str:
    text = _re.sub(r"<[^>]+>", "", fragment)
    return _html.unescape(text).strip()


_ddg_cache: dict[str, tuple[float, list[dict]]] = {}
DDG_CACHE_TTL = 300


def _ddg_results(q: str, limit: int) -> list[dict]:
    import time as _time

    key = q.lower().strip()
    cached = _ddg_cache.get(key)
    if cached and _time.monotonic() - cached[0] < DDG_CACHE_TTL:
        return cached[1][:limit]

    out = _ddg_lite(q, limit)
    if not out:
        out = _ddg_html(q, limit)
    _ddg_cache[key] = (_time.monotonic(), out)
    return out


def _ddg_lite(q: str, limit: int) -> list[dict]:
    import subprocess

    curl = shutil.which("curl")
    if not curl:
        return []
    result = subprocess.run(
        [curl, "-s", "-m", "15", "-A", "Mozilla/5.0 (mi_raft)",
         "-d", f"q={urllib.parse.quote(q)}", "https://lite.duckduckgo.com/lite/"],
        capture_output=True, timeout=20,
    )
    html_text = result.stdout.decode("utf-8", errors="replace")
    anchors = _re.findall(
        r"<a[^>]*href=\"([^\"]+)\"[^>]*class='result-link'[^>]*>(.*?)</a>", html_text, _re.DOTALL
    )
    snippets = _re.findall(
        r"class='result-snippet'[^>]*>(.*?)</td>", html_text, _re.DOTALL
    )
    out: list[dict] = []
    for i, (href, title) in enumerate(anchors[:limit]):
        out.append({
            "title": _strip_tags(title),
            "url": href,
            "snippet": _strip_tags(snippets[i]) if i < len(snippets) else "",
        })
    return out


def _ddg_html(q: str, limit: int) -> list[dict]:
    _, html_text = _http_get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q))
    out: list[dict] = []
    anchors = _re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html_text, _re.DOTALL
    )
    snippets = _re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html_text, _re.DOTALL)
    for i, (href, title) in enumerate(anchors[:limit]):
        if "uddg=" in href:
            parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
            href = parsed.get("uddg", [href])[0]
        out.append({
            "title": _strip_tags(title),
            "url": href,
            "snippet": _strip_tags(snippets[i]) if i < len(snippets) else "",
        })
    return out


def _brave_results(q: str, limit: int, brave_key: str) -> list[dict]:
    if not brave_key:
        raise RuntimeError("brave_key no configurada en raft.yaml")
    req = urllib.request.Request(
        "https://api.brave.com/res/v1/web/search?q=" + urllib.parse.quote(q),
        headers={"X-Subscription-Token": brave_key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
        for r in (data.get("web", {}).get("results") or [])[:limit]
    ]



class MessageIn(BaseModel):
    text: str
    author: str = "humano"
    thread_id: int | None = None


class TaskIn(BaseModel):
    title: str
    description: str = ""
    channel: str | None = None
    assignee: str | None = None
    created_by: str = "humano"


class TaskClaimIn(BaseModel):
    agent: str


class TaskDoneIn(BaseModel):
    result: str | None = None


class TaskCommentIn(BaseModel):
    text: str
    author: str = "humano"


class ChannelIn(BaseModel):
    name: str
    topic: str = ""


class AgentIn(BaseModel):
    name: str
    runtime: str
    work_dir: str | None = None
    instructions: str = ""
    model: str | None = None
    permissions: dict = field(default_factory=dict)
    memory_file: str | None = None


class AgentPatchIn(BaseModel):
    instructions: str | None = None
    model: str | None = None
    provider: str | None = None
    memory_file: str | None = None
    permissions: dict | None = None


class MemoryIn(BaseModel):
    content: str


class TaskFromMessageIn(BaseModel):
    message_id: int


class DmIn(BaseModel):
    agent: str


class WebhookIn(BaseModel):
    url: str
    events: list[str] = field(default_factory=lambda: ["message.created"])
    secret: str = ""


class BreakdownIn(BaseModel):
    goal: str
    agent: str
    channel: str | None = None


def create_app(cfg: Config, db: Database) -> FastAPI:
    stuck = db.reset_stuck_runs()
    if stuck:
        print(f"mi_raft: {stuck} run(s) huérfanos marcados como failed")
    db.sync_config(cfg)
    db.server_port = cfg.server.port
    db.escalate_to = cfg.escalate_to
    api_keys = [k for k in cfg.server.api_keys if k]

    if db.channel_exists("general") and not db.list_messages_since(0):
        welcome = (
            f"Bienvenido al equipo {cfg.team}. Soy tu agente de onboarding: "
            "pídeme ayuda para dar de alta agentes, crear canales o repartir la primera task."
        )
        mid = db.insert_message("general", "human", "mi_raft", f"@onboarding {welcome}")
        route_message(db, mid)

    def require_key(
        authorization: str | None = Header(default=None),
        x_mi_raft_key: str | None = Header(default=None),
        api_key: str | None = None,
    ):
        if not api_keys:
            return
        key = None
        if authorization and authorization.startswith("Bearer "):
            key = authorization[len("Bearer "):]
        key = key or x_mi_raft_key or api_key
        if not key or key not in api_keys:
            raise HTTPException(401, "API key inválida o ausente")

    def author_identity(author: str) -> tuple[str, str]:
        for row in db.list_agents():
            if row["id"] == author:
                return "agent", author
        return "human", author

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = start_background_tasks(db)
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="mi_raft", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/events", dependencies=[Depends(require_key)])
    async def events(last_id: int = 0):
        async def gen():
            last = last_id
            yield "retry: 3000\n\n"
            while True:
                rows = db.list_messages_since(last)
                for r in rows:
                    last = r["id"]
                    payload = json.dumps(dict(r), ensure_ascii=False)
                    yield f"id: {r['id']}\nevent: message\ndata: {payload}\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/agents", dependencies=[Depends(require_key)])
    def agents():
        return [dict(r) for r in db.list_agents()]

    @app.get("/channels", dependencies=[Depends(require_key)])
    def channels():
        return [dict(r) for r in db.list_channels()]

    @app.post("/channels/{name}/messages", dependencies=[Depends(require_key)])
    def post_message(name: str, msg: MessageIn):
        name = name.lstrip("#")
        if not db.channel_exists(name):
            raise HTTPException(404, f"Canal desconocido: #{name}")
        if not msg.text.strip():
            raise HTTPException(422, "El mensaje está vacío")
        author_type, author = author_identity(msg.author)
        mid = db.insert_message(name, author_type, author, msg.text, msg.thread_id)
        runs = route_message(db, mid)
        return {"id": mid, "routed_runs": runs}

    @app.get("/channels/{name}/messages", dependencies=[Depends(require_key)])
    def list_messages(name: str, limit: int = 200):
        name = name.lstrip("#")
        if not db.channel_exists(name):
            raise HTTPException(404, f"Canal desconocido: #{name}")
        rows = db.list_channel_messages(name, min(limit, 1000))
        return [dict(r) for r in rows]

    def _assign_task_message(task_id: int, agent_id: str) -> int:
        task = db.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"Task {task_id} no existe")
        channel = task["channel_id"]
        if channel is None:
            channels = db.list_channels()
            if not channels:
                raise HTTPException(422, "No hay canales definidos; crea uno en raft.yaml")
            channel = channels[0]["id"]
        text = f"[task #{task_id}] {task['title']}"
        if task["description"]:
            text += f"\n{task['description']}"
        text += f"\n@{agent_id} trabaja en esta task y responde en este hilo."
        mid = db.insert_message(channel, "human", task["created_by"], text)
        with db.tx() as conn:
            conn.execute(
                "UPDATE task SET thread_id=?, updated_at=datetime('now') WHERE id=?",
                (mid, task_id),
            )
        route_message(db, mid)
        return mid

    @app.post("/tasks", dependencies=[Depends(require_key)])
    def create_task(task: TaskIn):
        channel = task.channel.lstrip("#") if task.channel else None
        if channel and not db.channel_exists(channel):
            raise HTTPException(404, f"Canal desconocido: #{channel}")
        tid = db.create_task(task.title, task.description, channel, task.created_by)
        claimed = None
        if task.assignee:
            claimed = db.claim_task(tid, "agent", task.assignee)
            if claimed is None:
                raise HTTPException(409, f"Task {tid} no está todo")
            _assign_task_message(tid, task.assignee)
        return {"id": tid, "assignee": task.assignee}

    @app.get("/tasks", dependencies=[Depends(require_key)])
    def list_tasks(status: str | None = None):
        return [dict(r) for r in db.list_tasks(status)]

    @app.get("/tasks/{task_id}", dependencies=[Depends(require_key)])
    def get_task(task_id: int):
        t = db.get_task(task_id)
        if t is None:
            raise HTTPException(404, f"Task {task_id} no existe")
        return dict(t)

    @app.post("/tasks/{task_id}/claim", dependencies=[Depends(require_key)])
    def claim_task(task_id: int, body: TaskClaimIn):
        t = db.get_task(task_id)
        if t is None:
            raise HTTPException(404, f"Task {task_id} no existe")
        claimed = db.claim_task(task_id, "agent", body.agent)
        if claimed is None:
            raise HTTPException(409, f"Task {task_id} no está todo")
        mid = _assign_task_message(task_id, body.agent)
        return {"claimed": True, "message_id": mid}

    @app.post("/tasks/{task_id}/review", dependencies=[Depends(require_key)])
    def review_task(task_id: int):
        row = db.review_task(task_id)
        if row is None:
            raise HTTPException(409, f"Task {task_id} no está in_progress")
        return {"status": "in_review"}

    @app.post("/tasks/{task_id}/done", dependencies=[Depends(require_key)])
    def done_task(task_id: int, body: TaskDoneIn):
        row = db.finish_task(task_id, "done", body.result)
        if row is None:
            raise HTTPException(409, f"Task {task_id} no está activa (todo/in_progress/in_review)")
        if row["channel_id"] and row["thread_id"]:
            db.insert_message(
                row["channel_id"], "system", "mi_raft",
                f"task #{task_id} done: {row['title']}",
                thread_id=row["thread_id"], msg_type="status",
            )
        dispatch_webhooks("task.updated", dict(row))
        return {"status": "done"}

    @app.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_key)])
    def cancel_task(task_id: int):
        row = db.finish_task(task_id, "cancelled")
        if row is None:
            raise HTTPException(409, f"Task {task_id} no está activa (todo/in_progress/in_review)")
        if row["channel_id"] and row["thread_id"]:
            db.insert_message(
                row["channel_id"], "system", "mi_raft",
                f"task #{task_id} cancelled: {row['title']}",
                thread_id=row["thread_id"], msg_type="status",
            )
        dispatch_webhooks("task.updated", dict(row))
        return {"status": "cancelled"}

    @app.post("/tasks/{task_id}/comment", dependencies=[Depends(require_key)])
    def comment_task(task_id: int, body: TaskCommentIn):
        t = db.get_task(task_id)
        if t is None:
            raise HTTPException(404, f"Task {task_id} no existe")
        if t["channel_id"] is None:
            raise HTTPException(422, "La task no tiene canal asociado")
        author_type, author = author_identity(body.author)
        mid = db.insert_message(
            t["channel_id"], author_type, author, body.text, thread_id=t["thread_id"]
        )
        runs = route_message(db, mid)
        return {"id": mid, "routed_runs": runs}

    @app.post("/tasks/from-message", dependencies=[Depends(require_key)])
    def task_from_message(body: TaskFromMessageIn):
        msg = db.get_message(body.message_id)
        if msg is None:
            raise HTTPException(404, f"Mensaje {body.message_id} no existe")
        if msg["type"] != "comment":
            raise HTTPException(422, "Solo se pueden convertir comentarios")
        first_line = msg["text"].strip().splitlines()[0][:120]
        tid = db.create_task(first_line, msg["text"], msg["channel_id"], msg["author_id"])
        thread = msg["thread_id"] or msg["id"]
        with db.tx() as conn:
            conn.execute(
                "UPDATE task SET thread_id=?, updated_at=datetime('now') WHERE id=?",
                (thread, tid),
            )
        db.insert_message(
            msg["channel_id"], "human", "mi_raft",
            f"[task #{tid}] creada desde este mensaje: {first_line}",
            thread_id=thread,
        )
        return {"id": tid, "thread_id": thread}

    @app.post("/tasks/breakdown", dependencies=[Depends(require_key)])
    def breakdown(body: BreakdownIn):
        if db.get_agent_row(body.agent) is None:
            raise HTTPException(404, f"Agente {body.agent} no existe")
        agent = db.get_agent(body.agent)
        if agent.runtime == "external":
            raise HTTPException(422, "El desglose requiere un agente local")
        channel = body.channel.lstrip("#") if body.channel else None
        if channel and not db.channel_exists(channel):
            raise HTTPException(404, f"Canal desconocido: #{channel}")
        prompt = (
            f"Objetivo: {body.goal}\n\n"
            "Divide este objetivo en subtasks independientes (máximo 6) que no se bloqueen entre sí.\n"
            'Responde SOLO con un array JSON de objetos {"title": "...", "description": "..."} '
            "sin texto adicional."
        )

        async def call():
            from .runtimes import get_runtime

            rt = get_runtime(agent.runtime)
            return await rt.run_turn(agent, prompt, None, min(agent.timeout_s, 180))

        import asyncio as _asyncio

        try:
            result = _asyncio.run(call())
        except Exception as exc:
            raise HTTPException(502, f"El desglose falló: {exc}")
        import re as _re

        match = _re.search(r"\[.*\]", result.text, _re.DOTALL)
        if not match:
            raise HTTPException(502, f"El agente no devolvió JSON: {result.text[:200]}")
        try:
            items = json.loads(match.group(0))
        except ValueError:
            raise HTTPException(502, f"JSON inválido del agente: {result.text[:200]}")
        if not isinstance(items, list) or not items:
            raise HTTPException(502, "El agente devolvió un desglose vacío")
        parent = db.create_task(body.goal[:120], body.goal, channel, created_by=body.agent)
        for item in items[:6]:
            if isinstance(item, dict) and item.get("title"):
                db.create_subtask(parent, str(item["title"])[:120], str(item.get("description") or ""))
        subtasks = db.list_subtasks(parent)
        dispatch_webhooks("task.updated", dict(db.get_task(parent)))
        return {"parent_id": parent, "subtasks": [dict(s) for s in subtasks]}

    @app.get("/agents/{agent_id}/profile", dependencies=[Depends(require_key)])
    def agent_profile(agent_id: str):
        row = db.get_agent_row(agent_id)
        if row is None:
            raise HTTPException(404, f"Agente {agent_id} no existe")
        edges_out = [e["to_id"] for e in db.org_edges() if e["from_id"] == agent_id]
        edges_in = [e["from_id"] for e in db.org_edges() if e["to_id"] == agent_id]
        memory_path = Path(row["memory_file"]).expanduser() if row["memory_file"] else None
        stats = db.agent_stats(agent_id)
        return {
            "id": row["id"],
            "runtime": row["runtime"],
            "status": row["status"],
            "work_dir": row["work_dir"],
            "model": row["model"],
            "provider": row["provider"] if "provider" in row.keys() else None,
            "instructions": row["instructions"],
            "permissions": json.loads(row["permissions_json"]),
            "memory_file": row["memory_file"],
            "memory_bytes": memory_path.stat().st_size if memory_path and memory_path.exists() else 0,
            "timeout_s": row["timeout_s"],
            "budget_usd": row["budget_usd"],
            "sandbox": json.loads(row["sandbox_json"]) if "sandbox_json" in row.keys() else {},
            "web_search": bool(row["web_search"]) if "web_search" in row.keys() else True,
            "org": {"delega_a": edges_out, "recibe_de": edges_in},
            "models_used": [dict(m) for m in db.agent_models(agent_id)],
            "stats": dict(stats),
            "recent_runs": [dict(r) for r in db.recent_runs(agent_id, limit=5)],
        }

    @app.get("/models", dependencies=[Depends(require_key)])
    def models(runtime: str | None = None):
        catalog = full_catalog()
        if runtime:
            if runtime not in catalog:
                raise HTTPException(404, f"Runtime desconocido: {runtime}")
            return {
                "runtime": runtime,
                "providers": providers_for(runtime, catalog),
                "models": catalog[runtime],
            }
        return {
            rt: {"providers": providers_for(rt, catalog), "models": ms}
            for rt, ms in catalog.items()
        }

    @app.get("/meta")
    def meta():
        from . import __version__

        return {
            "team": cfg.team,
            "version": __version__,
            "workspace": cfg.workspace,
        }

    @app.get("/tools/search")
    def tools_search(q: str, limit: int = 8):
        if not q.strip():
            raise HTTPException(422, "q vacía")
        limit = min(limit, 20)
        try:
            if cfg.server.web_search_provider == "brave":
                results = _brave_results(q, limit, cfg.server.brave_key)
            else:
                results = _ddg_results(q, limit)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"búsqueda falló: {exc}")
        return {"query": q, "results": results}

    @app.get("/tools/fetch")
    def tools_fetch(url: str, max_chars: int = 20000):
        if not url.startswith(("http://", "https://")):
            raise HTTPException(422, "url debe empezar por http(s)://")
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname in ("127.0.0.1", "localhost", "0.0.0.0") or (parsed.hostname or "").startswith("169.254."):
            raise HTTPException(400, "fetch de direcciones locales bloqueado")
        try:
            _, text = _http_get(url, timeout=20)
        except Exception as exc:
            raise HTTPException(502, f"fetch falló: {exc}")
        text = _re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
        text = _re.sub(r"(?s)<[^>]+>", " ", text)
        text = _re.sub(r"\s+", " ", _html.unescape(text)).strip()
        max_chars = min(max_chars, 100_000)
        return {"url": url, "chars": len(text), "text": text[:max_chars]}

    @app.get("/org", dependencies=[Depends(require_key)])
    def org():
        return {
            "team": cfg.team,
            "agents": [r["id"] for r in db.list_agents()],
            "edges": [dict(r) for r in db.org_edges()],
        }

    @app.get("/usage", dependencies=[Depends(require_key)])
    def usage(by: str = "agent"):
        if by == "day":
            return [dict(r) for r in db.usage_by_day()]
        return [dict(r) for r in db.usage_by_agent()]

    def dispatch_webhooks(event: str, payload: dict) -> None:
        hooks = [w for w in db.list_webhooks() if event in json.loads(w["events"])]
        if not hooks:
            return
        body = json.dumps({"event": event, "payload": payload}, default=str).encode()

        def post(w) -> None:
            sig = hmac.new(w["secret"].encode(), body, hashlib.sha256).hexdigest()
            req = urllib.request.Request(
                w["url"], data=body, method="POST",
                headers={"Content-Type": "application/json", "X-mi_raft-signature": sig},
            )
            try:
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except Exception:
                pass

        for w in hooks:
            threading.Thread(target=post, args=(w,), daemon=True).start()

    def _on_message_created(row: dict) -> None:
        dispatch_webhooks("message.created", row)

    db.on_message_created = _on_message_created

    @app.get("/search", dependencies=[Depends(require_key)])
    def search(q: str, channel: str | None = None, author: str | None = None, limit: int = 50):
        if not q.strip():
            raise HTTPException(422, "q vacía")
        rows = db.search(q, channel, author, min(limit, 200))
        return [dict(r) for r in rows]

    @app.get("/activity", dependencies=[Depends(require_key)])
    def activity(limit: int = 50):
        return [dict(r) for r in db.activity(min(limit, 200))]

    @app.get("/runs", dependencies=[Depends(require_key)])
    def runs(agent: str | None = None, limit: int = 50):
        return [dict(r) for r in db.recent_runs(agent, min(limit, 200))]

    @app.get("/runs/{run_id}/messages", dependencies=[Depends(require_key)])
    def run_messages(run_id: int):
        return [dict(r) for r in db.list_run_messages(run_id)]

    @app.get("/dms", dependencies=[Depends(require_key)])
    def list_dms():
        return [dict(r) for r in db.list_dms()]

    @app.post("/dms", dependencies=[Depends(require_key)])
    def open_dm(body: DmIn):
        if db.get_agent_row(body.agent) is None:
            raise HTTPException(404, f"Agente {body.agent} no existe")
        row = db.get_or_create_dm(body.agent)
        return dict(row)

    @app.post("/webhooks", dependencies=[Depends(require_key)])
    def add_webhook(body: WebhookIn):
        secret = body.secret or _secrets.token_hex(16)
        wid = db.add_webhook(body.url, body.events, secret)
        return {"id": wid, "secret": secret}

    @app.get("/webhooks", dependencies=[Depends(require_key)])
    def list_webhooks():
        return [dict(r) for r in db.list_webhooks()]

    @app.delete("/webhooks/{webhook_id}", dependencies=[Depends(require_key)])
    def remove_webhook(webhook_id: int):
        if not db.delete_webhook(webhook_id):
            raise HTTPException(404, f"Webhook {webhook_id} no existe")
        return {"deleted": True}

    @app.post("/channels", dependencies=[Depends(require_key)])
    def new_channel(body: ChannelIn):
        if not db.create_channel(body.name, body.topic):
            raise HTTPException(409, f"El canal #{body.name.lstrip('#')} ya existe")
        return {"id": body.name.lstrip("#")}

    @app.post("/agents", dependencies=[Depends(require_key)])
    def new_agent(body: AgentIn):
        if db.get_agent_row(body.name) is not None:
            raise HTTPException(409, f"Ya existe un agente '{body.name}'")
        if body.runtime not in KNOWN_RUNTIMES:
            raise HTTPException(422, f"Runtime desconocido: {body.runtime}")
        if body.runtime == "external":
            raise HTTPException(422, "Los agentes external se definen en raft.yaml (necesitan wake_url)")
        work_dir = body.work_dir or cfg.workspace
        if not work_dir:
            raise HTTPException(422, "work_dir obligatorio (este equipo no tiene workspace definido)")
        from .config import AgentConfig

        memory_file = body.memory_file or default_memory_file(body.name)
        agent = AgentConfig(
            name=body.name,
            runtime=body.runtime,
            work_dir=str(work_dir),
            instructions=body.instructions,
            model=body.model,
            permissions=body.permissions,
            memory_file=memory_file,
        )
        db.create_agent(agent)
        ensure_memory_file(memory_file, agent.name)
        return {"id": agent.name, "work_dir": agent.work_dir, "memory_file": agent.memory_file}

    def default_memory_file(name: str) -> str:
        db_dir = Path(cfg.server.db).expanduser().resolve().parent
        return str(db_dir / "memory" / f"{name}.md")

    def ensure_memory_file(memory_file: str, name: str) -> None:
        path = Path(memory_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                f"# Memoria de {name}\n\n"
                "(El agente edita este fichero para guardar aprendizajes durables.)\n"
            )

    @app.get("/agents/{agent_id}/memory", dependencies=[Depends(require_key)])
    def get_memory(agent_id: str):
        row = db.get_agent_row(agent_id)
        if row is None:
            raise HTTPException(404, f"Agente {agent_id} no existe")
        if not row["memory_file"]:
            return {"memory_file": None, "content": None}
        path = Path(row["memory_file"]).expanduser()
        content = path.read_text(errors="replace") if path.exists() else ""
        return {"memory_file": row["memory_file"], "content": content}

    @app.put("/agents/{agent_id}/memory", dependencies=[Depends(require_key)])
    def put_memory(agent_id: str, body: MemoryIn):
        row = db.get_agent_row(agent_id)
        if row is None:
            raise HTTPException(404, f"Agente {agent_id} no existe")
        memory_file = row["memory_file"] or default_memory_file(agent_id)
        if memory_file != row["memory_file"]:
            db.update_agent(agent_id, {"memory_file": memory_file})
        ensure_memory_file(memory_file, agent_id)
        path = Path(memory_file).expanduser()
        path.write_text(body.content)
        return {"memory_file": memory_file, "bytes": len(body.content)}

    @app.patch("/agents/{agent_id}", dependencies=[Depends(require_key)])
    def patch_agent(agent_id: str, body: AgentPatchIn):
        if db.get_agent_row(agent_id) is None:
            raise HTTPException(404, f"Agente {agent_id} no existe")
        fields = body.model_dump(exclude_none=True)
        from .catalog import model_exists

        if "model" in fields and fields["model"]:
            runtime_row = db.get_agent_row(agent_id)
            if not model_exists(runtime_row["runtime"], fields.get("provider"), fields["model"]):
                raise HTTPException(
                    422,
                    f"Modelo {fields['model']} no está en el catálogo de {runtime_row['runtime']} "
                    "(consulta GET /models)",
                )
        row = db.update_agent(agent_id, fields)
        return dict(row)

    def workspace_root() -> Path:
        if not cfg.workspace:
            raise HTTPException(404, "Este equipo no tiene workspace compartido definido")
        root = Path(cfg.workspace).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    @app.get("/workspace/tree", dependencies=[Depends(require_key)])
    def workspace_tree():
        root = workspace_root()
        skip = {".git", "__pycache__", "node_modules"}
        out: list[dict] = []
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if any(part in skip for part in rel.parts):
                continue
            if len(out) >= 500:
                out.append({"path": "…", "type": "limit"})
                break
            out.append({"path": str(rel), "type": "dir" if p.is_dir() else "file"})
        return {"root": str(root), "items": out}

    @app.get("/workspace/file", dependencies=[Depends(require_key)])
    def workspace_file(path: str):
        root = workspace_root()
        target = (root / path).resolve()
        if not str(target).startswith(str(root)):
            raise HTTPException(400, "Ruta fuera del workspace")
        if not target.is_file():
            raise HTTPException(404, f"No existe: {path}")
        content = target.read_text(errors="replace")
        if len(content) > 100_000:
            content = content[:100_000] + "\n… (truncado)"
        return {"path": path, "content": content}

    return app
