from __future__ import annotations

import json

from .base import BaseRuntime, RunResult, parse_json_lines


class ClaudeRuntime(BaseRuntime):
    name = "claude"

    def build_args(self, agent, session_id: str | None) -> list[str]:
        args = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        mode = agent.permissions.get("mode", "dontAsk")
        args += ["--permission-mode", str(mode)]
        if agent.model:
            args += ["--model", str(agent.model)]
        if agent.budget_usd:
            args += ["--max-budget-usd", str(agent.budget_usd)]
        if session_id:
            args += ["--resume", session_id]
        args += agent.extra_args
        return args

    def parse_line(self, line: str, sink) -> None:
        for obj in _claude_events(line):
            ev_type = obj.get("type")
            if ev_type == "assistant":
                for block in (obj.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        sink({
                            "type": "tool_use",
                            "tool": block.get("name"),
                            "payload": {"input": block.get("input")},
                        })
                    elif block.get("type") == "text" and block.get("text", "").strip():
                        sink({
                            "type": "text",
                            "tool": None,
                            "payload": {"text": block["text"][:800]},
                        })
            elif ev_type == "user":
                for block in (obj.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        sink({
                            "type": "tool_result",
                            "tool": None,
                            "payload": {"content": str(block.get("content"))[:800]},
                        })

    def parse_output(self, stdout: str) -> RunResult:
        for line in reversed(stdout.splitlines()):
            for obj in _claude_events(line):
                if obj.get("type") != "result":
                    continue
                text = obj.get("result")
                if not isinstance(text, str):
                    continue
                usage = obj.get("usage") or {}
                return RunResult(
                    session_id=obj.get("session_id"),
                    text=text,
                    cost_usd=obj.get("total_cost_usd"),
                    tokens_in=usage.get("input_tokens"),
                    tokens_out=usage.get("output_tokens"),
                )
        try:
            obj = json.loads(stdout)
        except ValueError:
            raise RuntimeError(f"claude no devolvió resultado: {stdout[:500]}")
        text = obj.get("result")
        if not isinstance(text, str):
            raise RuntimeError(f"claude no devolvió 'result': {str(obj)[:500]}")
        usage = obj.get("usage") or {}
        return RunResult(
            session_id=obj.get("session_id"),
            text=text,
            cost_usd=obj.get("total_cost_usd"),
            tokens_in=usage.get("input_tokens"),
            tokens_out=usage.get("output_tokens"),
        )


def _claude_events(line: str):
    line = line.strip()
    if not line.startswith("{"):
        return []
    try:
        obj = json.loads(line)
    except ValueError:
        return []
    return [obj] if isinstance(obj, dict) else []


def register(registry: dict) -> None:
    registry[ClaudeRuntime.name] = ClaudeRuntime()
