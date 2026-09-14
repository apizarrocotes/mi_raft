from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import field
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .config import KNOWN_RUNTIMES, Config
from .db import Database
from .router import route_message
from .runner import start_runner

STATIC_DIR = Path(__file__).parent / "static"


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
    memory_file: str | None = None
    permissions: dict | None = None


class MemoryIn(BaseModel):
    content: str


def create_app(cfg: Config, db: Database) -> FastAPI:
    stuck = db.reset_stuck_runs()
    if stuck:
        print(f"mi_raft: {stuck} run(s) huérfanos marcados como failed")
    db.sync_config(cfg)
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
        task = start_runner(db)
        yield
        task.cancel()

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
                raise HTTPException(409, f"Task {tid} no está open")
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
            raise HTTPException(409, f"Task {task_id} no está open")
        mid = _assign_task_message(task_id, body.agent)
        return {"claimed": True, "message_id": mid}

    @app.post("/tasks/{task_id}/done", dependencies=[Depends(require_key)])
    def done_task(task_id: int, body: TaskDoneIn):
        row = db.finish_task(task_id, "done", body.result)
        if row is None:
            raise HTTPException(409, f"Task {task_id} no está open ni claimed")
        return {"status": "done"}

    @app.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_key)])
    def cancel_task(task_id: int):
        row = db.finish_task(task_id, "cancelled")
        if row is None:
            raise HTTPException(409, f"Task {task_id} no está open ni claimed")
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

    @app.get("/meta")
    def meta():
        from . import __version__

        return {
            "team": cfg.team,
            "version": __version__,
            "workspace": cfg.workspace,
        }

    @app.get("/org", dependencies=[Depends(require_key)])
    def org():
        return {
            "team": cfg.team,
            "agents": [r["id"] for r in db.list_agents()],
            "edges": [dict(r) for r in db.org_edges()],
        }

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
        row = db.update_agent(agent_id, body.model_dump(exclude_none=True))
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
