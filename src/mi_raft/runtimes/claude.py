from __future__ import annotations

import json

from .base import BaseRuntime, RunResult


class ClaudeRuntime(BaseRuntime):
    name = "claude"

    def build_args(self, agent, session_id: str | None) -> list[str]:
        args = ["claude", "-p", "--output-format", "json"]
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

    def parse_output(self, stdout: str) -> RunResult:
        try:
            obj = json.loads(stdout)
        except ValueError:
            raise RuntimeError(f"claude no devolvió JSON válido: {stdout[:500]}")
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


def register(registry: dict) -> None:
    registry[ClaudeRuntime.name] = ClaudeRuntime()
