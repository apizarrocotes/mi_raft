from __future__ import annotations

import asyncio
import json
import shutil
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..config import AgentConfig

_warned_no_bwrap = False


def build_sandbox_cmd(work_dir: Path, sandbox: dict, args: list[str]) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        global _warned_no_bwrap
        if not _warned_no_bwrap:
            import logging

            logging.getLogger("mi_raft").warning(
                "sandbox activado pero bwrap no está instalado: se ejecuta SIN aislamiento"
            )
            _warned_no_bwrap = True
        return args
    home = Path.home()
    cmd = [
        bwrap,
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", str(home),
        "--ro-bind", str(home / ".claude"), str(home / ".claude"),
        "--ro-bind", str(home / ".config"), str(home / ".config"),
        "--ro-bind", str(home / ".local"), str(home / ".local"),
        "--bind", str(work_dir), str(work_dir),
    ]
    for ro in sandbox.get("ro", []):
        cmd += ["--ro-bind", str(ro), str(ro)]
    for rw in sandbox.get("rw", []):
        cmd += ["--bind", str(rw), str(rw)]
    cmd += ["--"] + args
    return cmd


@dataclass
class RunResult:
    session_id: str | None
    text: str
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


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
        if agent.sandbox.get("enable"):
            args = build_sandbox_cmd(work_dir, agent.sandbox, args)
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
