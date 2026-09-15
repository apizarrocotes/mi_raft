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
    provider: str | None = None
    model: str | None = None


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
        event_sink=None,
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
            limit=64 * 1024 * 1024,
        )
        stdout_lines: list[str] = []

        async def pump_stdout() -> None:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace")
                stdout_lines.append(line)
                if event_sink:
                    try:
                        self.parse_line(line, event_sink)
                    except Exception:
                        pass

        async def pump_stderr() -> str:
            assert proc.stderr is not None
            data = await proc.stderr.read()
            return data.decode(errors="replace")

        loop = asyncio.get_running_loop()
        pump_task = loop.create_task(pump_stdout())
        stderr_task = loop.create_task(pump_stderr())

        async def feed_stdin() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        stdin_task = loop.create_task(feed_stdin())
        try:
            async with asyncio.timeout(timeout_s):
                await pump_task
                await proc.wait()
        except TimeoutError:
            self._kill_tree(proc)
            raise RuntimeTimeout(
                f"{self.name} excedió el timeout de {timeout_s}s y fue terminado"
            )
        finally:
            stdin_task.cancel()
        stderr = await stderr_task
        if proc.returncode != 0:
            stdout_tail = "".join(stdout_lines)[-600:]
            raise RuntimeError(
                f"{self.name} salió con código {proc.returncode}: "
                f"stderr={stderr.strip()[:600] or '(vacío)'} | stdout_tail={stdout_tail}"
            )
        return self.parse_output("".join(stdout_lines))

    def parse_line(self, line: str, sink) -> None:
        return None

    @staticmethod
    def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        import os
        import signal

        def descendants(pid: int) -> list[int]:
            found: list[int] = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    stat = (entry / "stat").read_text()
                    ppid = int(stat.rsplit(")", 1)[1].split()[1])
                    if ppid == pid:
                        found.append(int(entry.name))
                        found.extend(descendants(int(entry.name)))
                except (OSError, ValueError, IndexError):
                    continue
            return found

        victims = descendants(proc.pid)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
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
