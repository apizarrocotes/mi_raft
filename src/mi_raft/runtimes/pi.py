from __future__ import annotations

from .base import BaseRuntime, RunResult, find_first, parse_json_lines


class PiRuntime(BaseRuntime):
    name = "pi"

    def build_args(self, agent, session_id: str | None) -> list[str]:
        args = ["pi", "-p", "--mode", "json"]
        tools = agent.permissions.get("tools")
        if tools:
            args += ["-t", ",".join(str(t) for t in tools)]
        if agent.model:
            args += ["--model", str(agent.model)]
        if session_id:
            args += ["--session-id", session_id]
        args += agent.extra_args
        return args

    def parse_output(self, stdout: str) -> RunResult:
        events = parse_json_lines(stdout)
        session_id = None
        for ev in events:
            if ev.get("type") == "session":
                session_id = ev.get("session", {}).get("id") or ev.get("id")
        if session_id is None:
            for ev in events:
                session_id = find_first(ev, "id")
                if session_id:
                    break

        answer = None
        for ev in events:
            if ev.get("type") != "message_end":
                continue
            msg = ev.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            blocks = msg.get("content") or []
            texts = [
                b.get("text")
                for b in blocks
                if isinstance(b, dict) and isinstance(b.get("text"), str)
            ]
            if texts:
                answer = "\n".join(texts)
        if not answer:
            raise RuntimeError(f"pi no devolvió respuesta de asistente: {stdout[:500]}")
        return RunResult(session_id=session_id, text=answer)


def register(registry: dict) -> None:
    registry[PiRuntime.name] = PiRuntime()
