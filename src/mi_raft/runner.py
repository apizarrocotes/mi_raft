from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request

from .db import Database
from .router import build_prompt, escalate, route_message
from .runtimes import get_runtime

log = logging.getLogger("mi_raft.runner")


GLOBAL_MAX_INFLIGHT = 4


async def runner_loop(db: Database, poll_s: float = 0.5) -> None:
    inflight: set[asyncio.Task] = set()
    while True:
        inflight = {t for t in inflight if not t.done()}
        run = None
        if len(inflight) < GLOBAL_MAX_INFLIGHT:
            try:
                run = db.claim_next_run()
            except Exception:
                log.exception("Error reclamando run de la cola")
        if run is None:
            await asyncio.sleep(poll_s)
            continue
        task = asyncio.get_running_loop().create_task(execute_run(db, run))
        inflight.add(task)


async def execute_run(db: Database, run) -> None:
    agent = db.get_agent(run["agent_id"])
    db.set_agent_status(agent.name, "working")
    seq = {"n": 0}

    def sink(ev: dict) -> None:
        db.insert_run_message(
            run["id"], seq["n"], str(ev.get("type") or "event"),
            ev.get("tool"), ev.get("payload") or {},
        )
        seq["n"] += 1

    if agent.runtime == "external":
        sink({"type": "step", "tool": None, "payload": {"event": f"wake enviado a {agent.wake_url}"}})
        await execute_external_run(db, run, agent)
        db.set_agent_status(agent.name, "idle")
        return
    runtime = get_runtime(agent.runtime)
    session_id = db.last_session_for(agent.name, run["thread_id"])
    prompt = build_prompt(db, agent, run["channel_id"], run["thread_id"])
    sink({"type": "step", "tool": None, "payload": {"event": "turno iniciado"}})
    try:
        result = await runtime.run_turn(agent, prompt, session_id, agent.timeout_s, event_sink=sink)
    except Exception as exc:
        _fail_run(db, run, agent, str(exc))
        return
    sink({"type": "step", "tool": None, "payload": {"event": "turno completado"}})
    db.finish_run(
        run["id"], "done", result.session_id, agent.work_dir, result.text, None,
        cost_usd=result.cost_usd, tokens_in=result.tokens_in, tokens_out=result.tokens_out,
    )
    db.set_agent_status(agent.name, "idle")
    reply_id = db.insert_message(
        run["channel_id"], "agent", agent.name, result.text, thread_id=run["thread_id"]
    )
    route_message(db, reply_id)


async def execute_external_run(db: Database, run, agent) -> None:
    if not agent.wake_url:
        _fail_run(db, run, agent, "runtime external sin wake_url")
        return
    payload = {
        "eventId": f"run-{run['id']}",
        "agentId": agent.name,
        "channel": run["channel_id"],
        "threadId": run["thread_id"],
        "messageId": run["trigger_message_id"],
    }
    loop = asyncio.get_running_loop()

    def post_wake() -> None:
        req = urllib.request.Request(
            agent.wake_url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15):
            pass

    try:
        await loop.run_in_executor(None, post_wake)
    except Exception as exc:
        _fail_run(db, run, agent, f"wake no entregado a {agent.wake_url}: {exc}")
        return

    deadline = time.monotonic() + agent.timeout_s
    while time.monotonic() < deadline:
        reply = db.reply_from_agent_since(run["thread_id"], agent.name, run["trigger_message_id"])
        if reply is not None:
            db.finish_run(run["id"], "done", None, None, reply["text"], None)
            return
        await asyncio.sleep(1)
    _fail_run(db, run, agent, "el agente externo no respondió a tiempo")


GLOBAL_MAX_INFLIGHT = 4
def _fail_run(db: Database, run, agent, error: str) -> None:
    db.set_agent_status(agent.name, "error")
    db.finish_run(run["id"], "failed", None, None, None, error)
    db.insert_message(
        run["channel_id"],
        "system",
        "mi_raft",
        f"el agente {agent.name} falló: {error}",
        thread_id=run["thread_id"],
        msg_type="status",
    )
    task = db.task_for_thread(run["thread_id"])
    task_note = f" (task #{task['id']} afectada)" if task else ""
    if agent.name == db.escalate_to:
        return
    escalate(
        db,
        run["channel_id"],
        run["thread_id"],
        f"el agente {agent.name} falló en este hilo: {error[:140]}{task_note}. "
        "Revisa y re-delega o reajusta el alcance.",
    )


async def watchdog_loop(db: Database, poll_s: float = 60.0) -> None:
    """Detecta runs atascados en 'running' más allá del timeout + gracia."""
    while True:
        try:
            for run in db.stuck_running_runs(grace_s=120):
                agent = db.get_agent(run["agent_id"])
                db.finish_run(
                    run["id"], "failed", None, None, None,
                    "watchdog: excedió timeout + gracia sin terminar",
                )
                _fail_run(db, run, agent, "watchdog: excedió timeout + gracia sin terminar")
                log.warning("watchdog: run %s de %s terminado a la fuerza", run["id"], agent.name)
        except Exception:
            log.exception("Error en el watchdog")
        await asyncio.sleep(poll_s)


def start_runner(db: Database) -> asyncio.Task:
    loop = asyncio.get_running_loop()
    return loop.create_task(runner_loop(db))


def start_background_tasks(db: Database) -> list[asyncio.Task]:
    loop = asyncio.get_running_loop()
    return [
        loop.create_task(runner_loop(db)),
        loop.create_task(watchdog_loop(db)),
    ]
