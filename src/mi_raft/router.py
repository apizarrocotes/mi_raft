from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .db import Database

MENTION_STRUCTURED = re.compile(r"\[@([^\]]+)\]\(mention://agent/([^)]+)\)")
MENTION_PLAIN = re.compile(r"(?<![\w./])@([\w-]+)", re.IGNORECASE)

MAX_AGENT_REPLIES_PER_THREAD = 20


def extract_mentions(text: str, known_agents: set[str]) -> set[str]:
    mentioned: set[str] = set()
    for _label, agent_id in MENTION_STRUCTURED.findall(text):
        if agent_id in known_agents:
            mentioned.add(agent_id)
    for name in MENTION_PLAIN.findall(text):
        if name.lower() in {a.lower() for a in known_agents}:
            for a in known_agents:
                if a.lower() == name.lower():
                    mentioned.add(a)
    return mentioned


def route_message(db: Database, message_id: int) -> list[int]:
    msg = db.get_message(message_id)
    if msg is None or msg["type"] != "comment":
        return []
    if msg["author_type"] == "system":
        return []
    known = {row["id"] for row in db.list_agents()}
    author = msg["author_id"] if msg["author_type"] == "agent" else None
    mentioned = extract_mentions(msg["text"], known)
    mentioned.discard(author)
    if (
        not mentioned
        and msg["author_type"] == "human"
        and msg["thread_id"] is not None
    ):
        last_agent = db.last_agent_in_thread(msg["thread_id"])
        if last_agent in known:
            mentioned.add(last_agent)
    if not mentioned:
        return []
    if author is not None and db.count_agent_messages_in_thread(msg["thread_id"]) >= MAX_AGENT_REPLIES_PER_THREAD:
        return []

    run_ids: list[int] = []
    for agent_id in sorted(mentioned):
        if author is not None and not db.org_allows(author, agent_id):
            db.insert_message(
                msg["channel_id"],
                "system",
                "mi_raft",
                f"handoff bloqueado por el organigrama: {author} → {agent_id}",
                thread_id=msg["thread_id"],
                msg_type="status",
            )
            continue
        thread_id = msg["thread_id"] if msg["thread_id"] else msg["id"]
        if db.coalesce_run(agent_id, msg["channel_id"], thread_id, message_id):
            continue
        run_ids.append(db.insert_run(agent_id, msg["channel_id"], thread_id, message_id))
    return run_ids


def build_prompt(db, agent, channel_id: str, thread_id: int) -> str:
    msgs = db.thread_messages(thread_id)
    lines: list[str] = []
    header = f'Eres el agente "{agent.name}" en mi_raft, un workspace multi-agente.'
    work_dir = Path(agent.work_dir).expanduser()
    boundary = (
        f"\n\nTu directorio de trabajo es {work_dir}: trabaja solo dentro de él"
        " (única excepción: tu memory_file). No escribas nunca fuera de ahí."
    )
    if agent.runtime == "external":
        boundary = ""
    if agent.instructions:
        header += f"\n\nInstrucciones:\n{agent.instructions.strip()}"
    header += boundary
    if agent.memory_file:
        mem_path = Path(agent.memory_file).expanduser()
        content = mem_path.read_text() if mem_path.exists() else "(vacía todavía)"
        header += (
            f"\n\nMemoria persistente — fichero: {mem_path}\n"
            f"Contenido actual:\n{content}\n"
            "(Puedes editar ese fichero con tus herramientas para guardar aprendizajes durables.)"
        )
    lines.append(header)
    lines.append(f"\nCanal: #{channel_id}. Hilo #{thread_id}.")
    lines.append("\nConversación reciente:")
    for m in msgs:
        author = f"agent:{m['author_id']}" if m["author_type"] == "agent" else m["author_id"]
        lines.append(f"- [{author}] {m['text']}")
    lines.append(
        "\nResponde como texto plano (será publicado en el hilo). Si quieres encargar "
        "algo a otro agente, menciónalo con @su-nombre en tu respuesta."
    )
    return "\n".join(lines)
