from __future__ import annotations

from .base import BaseRuntime, RunResult, find_first, parse_json_lines


class OpencodeRuntime(BaseRuntime):
    name = "opencode"

    def build_args(self, agent, session_id: str | None) -> list[str]:
        args = ["opencode", "run", "--format", "json"]
        mode = agent.permissions.get("mode", "auto")
        if mode == "auto":
            args.append("--auto")
        if agent.model:
            args += ["-m", str(agent.model)]
        if session_id:
            args += ["-s", session_id]
        args += agent.extra_args
        return args

    def parse_output(self, stdout: str) -> RunResult:
        events = parse_json_lines(stdout)
        texts: list[str] = []
        session_id = None
        cost_usd = tokens_in = tokens_out = None
        for ev in events:
            if session_id is None:
                session_id = find_first(ev, "sessionID")
            if ev.get("type") == "text":
                part = ev.get("part")
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif isinstance(ev.get("text"), str):
                    texts.append(ev["text"])
            if ev.get("type") == "step_finish" or ev.get("type") == "step-finish":
                part = ev.get("part") or {}
                tokens = part.get("tokens") or {}
                if isinstance(tokens.get("input"), (int, float)):
                    tokens_in = int(tokens["input"])
                if isinstance(tokens.get("output"), (int, float)):
                    tokens_out = int(tokens["output"])
                if isinstance(part.get("cost"), (int, float)):
                    cost_usd = float(part["cost"])
        text = "\n\n".join(t for t in texts if t.strip()).strip()
        if not text:
            raise RuntimeError(f"opencode no devolvió texto: {stdout[:500]}")
        return RunResult(
            session_id=session_id, text=text,
            cost_usd=cost_usd, tokens_in=tokens_in, tokens_out=tokens_out,
        )


def register(registry: dict) -> None:
    registry[OpencodeRuntime.name] = OpencodeRuntime()
