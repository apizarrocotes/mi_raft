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

    def parse_line(self, line: str, sink) -> None:
        for ev in parse_json_lines(line):
            data = ev.get("data") if isinstance(ev.get("data"), dict) else ev
            msg = data.get("message") or {}
            blocks = msg.get("content") or []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = str(block.get("type", ""))
                if "tool" in btype:
                    sink({
                        "type": "tool_use",
                        "tool": block.get("tool") or block.get("name") or btype,
                        "payload": {k: v for k, v in block.items() if k != "type"},
                    })
                elif btype == "text" and str(block.get("text", "")).strip():
                    sink({"type": "text", "tool": None, "payload": {"text": block["text"][:800]}})
            if str(ev.get("type", "")) in ("message_start", "message_end", "turn_end"):
                sink({"type": "step", "tool": None, "payload": {"event": ev.get("type")}})

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
        cost_usd = tokens_in = tokens_out = None
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
            usage = msg.get("usage") or {}
            for k_in in ("input_tokens", "input", "prompt_tokens"):
                if isinstance(usage.get(k_in), (int, float)):
                    tokens_in = int(usage[k_in])
                    break
            for k_out in ("output_tokens", "output", "completion_tokens"):
                if isinstance(usage.get(k_out), (int, float)):
                    tokens_out = int(usage[k_out])
                    break
            if isinstance(msg.get("cost"), (int, float)):
                cost_usd = float(msg["cost"])
            elif isinstance(usage.get("cost"), (int, float)):
                cost_usd = float(usage["cost"])
        if not answer:
            raise RuntimeError(f"pi no devolvió respuesta de asistente: {stdout[:500]}")
        provider = None
        model = None
        for ev in events:
            if model is None:
                model = find_first(ev, "modelID") or find_first(ev, "model")
            if provider is None:
                provider = find_first(ev, "providerID") or find_first(ev, "provider")
        return RunResult(
            session_id=session_id, text=answer,
            cost_usd=cost_usd, tokens_in=tokens_in, tokens_out=tokens_out,
            provider=provider, model=model,
        )


def register(registry: dict) -> None:
    registry[PiRuntime.name] = PiRuntime()
