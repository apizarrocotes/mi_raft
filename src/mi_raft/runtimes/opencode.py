from __future__ import annotations

import json

from .base import BaseRuntime, RunResult, find_first, parse_json_lines


class OpencodeRuntime(BaseRuntime):
    name = "opencode"

    def build_args(self, agent, session_id: str | None) -> list[str]:
        args = ["opencode", "run", "--format", "json"]
        mode = agent.permissions.get("mode", "auto")
        if mode == "auto":
            args.append("--auto")
        model = agent.model
        if model and agent.provider and "/" not in str(model):
            model = f"{agent.provider}/{model}"
        if model:
            args += ["-m", str(model)]
        if session_id:
            args += ["-s", session_id]
        args += agent.extra_args
        return args

    def parse_line(self, line: str, sink) -> None:
        for ev in parse_json_lines(line):
            part = ev.get("part") or {}
            ev_type = ev.get("type", "")
            if ev_type in ("step_start", "step_finish"):
                sink({"type": "step", "tool": None, "payload": {"event": ev_type}})
            elif ev_type == "text" and isinstance(part.get("text"), str):
                sink({"type": "text", "tool": None, "payload": {"text": part["text"][:800]}})
            elif isinstance(part, dict) and "tool" in str(part.get("type", "")):
                tool = part.get("tool") or part.get("toolName") or str(part.get("type"))
                state = part.get("state") or {}
                state_input = state.get("input")
                payload: dict = {"summary": _tool_summary(tool, state_input)}
                output = state.get("output") if isinstance(state, dict) else None
                if output:
                    payload["output"] = str(output)[:600]
                sink({"type": "tool_use", "tool": tool, "payload": payload})


    def parse_output(self, stdout: str) -> RunResult:
        events = parse_json_lines(stdout)
        texts: list[str] = []
        session_id = None
        cost_usd = tokens_in = tokens_out = None
        finish_reason = None
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
                if part.get("reason"):
                    finish_reason = part["reason"]
                tokens = part.get("tokens") or {}
                if isinstance(tokens.get("input"), (int, float)):
                    tokens_in = int(tokens["input"])
                if isinstance(tokens.get("output"), (int, float)):
                    tokens_out = int(tokens["output"])
                if isinstance(part.get("cost"), (int, float)):
                    cost_usd = float(part["cost"])
        text = "\n\n".join(t for t in texts if t.strip()).strip()
        if not text:
            if finish_reason == "length":
                raise RuntimeError(
                    "opencode agotó el límite de tokens de salida del modelo (razón=length). "
                    "El trabajo parcial escrito por tools queda guardado en el workspace; "
                    "continúa en otro turno o reparte el trabajo en trozos menores"
                )
            raise RuntimeError(
                f"opencode no devolvió texto (razón={finish_reason or 'desconocida'})"
            )
        provider = None
        model = None
        for ev in events:
            if model is None:
                model = find_first(ev, "modelID")
            if provider is None:
                provider = find_first(ev, "providerID")
        return RunResult(
            session_id=session_id, text=text,
            cost_usd=cost_usd, tokens_in=tokens_in, tokens_out=tokens_out,
            provider=provider, model=model,
        )


def _tool_summary(tool: str, state_input) -> str:
    if not isinstance(state_input, dict):
        return str(state_input)[:160]
    if tool == "bash":
        return f"$ {str(state_input.get('command', ''))[:150]}"
    for key in ("filePath", "path", "file"):
        if state_input.get(key):
            extra = ""
            if isinstance(state_input.get("content"), str):
                extra = f" ({len(state_input['content'])} chars)"
            return f"{state_input[key]}{extra}"
    if state_input.get("pattern"):
        return f"pattern={state_input['pattern']}"
    return json.dumps(state_input, default=str)[:160]


def register(registry: dict) -> None:
    registry[OpencodeRuntime.name] = OpencodeRuntime()
