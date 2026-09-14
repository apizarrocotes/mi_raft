from __future__ import annotations

import asyncio
import json
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..config import AgentConfig


@dataclass
class RunResult:
    session_id: str | None
    text: str


class RuntimeTimeout(RuntimeError):
    pass


class BaseRuntime(ABC):
    name: str = "base"

    @abstractmethod
    def build_args(self, agent: AgentConfig, session_id: str | None) -> list[str]:
        ...

    @abstractmethod
    def parse_output(self, stdout: str) -> RunResult:
        ...

    async def run_turn(
        self,
        agent: AgentConfig,
        prompt: str,
        session_id: str | None,
        timeout_s: int,
    ) -> RunResult:
        work_dir = Path(agent.work_dir).expanduser()
        if not work_dir.is_dir():
            raise FileNotFoundError(f"work_dir no existe: {work_dir}")
        args = self.build_args(agent, session_id)
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=work_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            self._kill_tree(proc)
            raise RuntimeTimeout(
                f"{self.name} excedió el timeout de {timeout_s}s y fue terminado"
            )
        if proc.returncode != 0:
            stderr = stderr_b.decode(errors="replace").strip()
            raise RuntimeError(
                f"{self.name} salió con código {proc.returncode}: {stderr[:2000]}"
            )
        return self.parse_output(stdout_b.decode(errors="replace"))

    @staticmethod
    def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        try:
            import os

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def parse_json_lines(stdout: str) -> list[dict]:
    out: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def find_first(obj: dict, key: str):
    if key in obj:
        return obj[key]
    for value in obj.values():
        if isinstance(value, dict):
            found = find_first(value, key)
            if found is not None:
                return found
    return None
