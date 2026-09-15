from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .base import BaseRuntime, RunResult

DEFAULT_PORT_BASE = 14400
PORT_RANGE = 400


def derive_port(name: str) -> int:
    digest = hashlib.sha1(name.encode()).hexdigest()
    return DEFAULT_PORT_BASE + int(digest[:6], 16) % PORT_RANGE


class OpencodeServeRuntime(BaseRuntime):
    """Runtime persistente: un `opencode serve` por agente, turnos vía HTTP+SSE."""

    name = "opencode-serve"

    def build_args(self, agent, session_id: str | None) -> list[str]:
        return ["opencode", "serve"]

    def parse_output(self, stdout: str) -> RunResult:
        return RunResult(session_id=None, text=stdout)

    async def run_turn(
        self, agent, prompt: str, session_id: str | None, timeout_s: int, event_sink=None
    ) -> RunResult:
        port = agent.server_port or derive_port(agent.name)
        base = f"http://127.0.0.1:{port}/api"
        self._ensure_server(agent, port)
        loop = asyncio.get_running_loop()
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                sid, text = await loop.run_in_executor(
                    None, self._execute_turn, base, None, prompt, min(timeout_s, 180), event_sink
                )
                return RunResult(session_id=sid, text=text)
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(2)
        raise last_error  # type: ignore[misc]

    def _ensure_server(self, agent, port: int) -> None:
        if self._healthy(port):
            return
        work_dir = Path(agent.work_dir).expanduser()
        if not work_dir.is_dir():
            raise FileNotFoundError(f"work_dir no existe: {work_dir}")
        subprocess.Popen(
            ["opencode", "serve", "--port", str(port), "--hostname", "127.0.0.1"],
            cwd=str(work_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._healthy(port):
                return
            time.sleep(0.5)
        raise RuntimeError(f"opencode serve no quedó sano en el puerto {port}")

    @staticmethod
    def _healthy(port: int) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2
            ) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _execute_turn(
        self, base: str, session_id: str | None, prompt: str, timeout_s: int, event_sink=None
    ) -> tuple[str, str]:
        req = urllib.request.Request(f"{base}/event")
        texts: list[str] = []
        sid = session_id
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            sid = self._prompt(base, sid, prompt)
            for raw in resp:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:])
                except ValueError:
                    continue
                data = ev.get("data") or {}
                if data.get("sessionID") != sid:
                    continue
                etype = ev.get("type", "")
                if event_sink:
                    try:
                        if "tool" in etype:
                            event_sink({
                                "type": "tool_use",
                                "tool": data.get("tool") or etype.split(".")[-1],
                                "payload": data,
                            })
                        elif etype.endswith("text.ended"):
                            event_sink({
                                "type": "text", "tool": None,
                                "payload": {"text": data.get("text", "")[:800]},
                            })
                        elif etype.endswith(("step.started", "step.ended")):
                            event_sink({
                                "type": "step", "tool": None,
                                "payload": {"event": etype},
                            })
                    except Exception:
                        pass
                if etype.endswith("text.ended") and isinstance(data.get("text"), str):
                    texts.append(data["text"])
                elif etype.endswith("step.ended"):
                    if data.get("finish") == "error":
                        raise RuntimeError("opencode-serve: el turno terminó en error")
                    break
        text = "\n".join(t for t in texts if t.strip()).strip()
        if not text:
            raise RuntimeError("opencode-serve: turno sin texto (¿timeout del stream?)")
        return sid, text

    def _prompt(self, base: str, session_id: str | None, prompt: str) -> str:
        if session_id is None:
            session_id = self._create_session(base)
        try:
            self._http_json(
                "POST", f"{base}/session/{session_id}/prompt", {"prompt": {"text": prompt}}
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                session_id = self._create_session(base)
                self._http_json(
                    "POST",
                    f"{base}/session/{session_id}/prompt",
                    {"prompt": {"text": prompt}},
                )
            else:
                raise
        return session_id

    @staticmethod
    def _create_session(base: str) -> str:
        out = OpencodeServeRuntime._http_json(
            "POST", f"{base}/session", {"title": "mi_raft"}
        )
        data = out.get("data", out)
        return data["id"]

    @staticmethod
    def _http_json(method: str, url: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())


def register(registry: dict) -> None:
    registry[OpencodeServeRuntime.name] = OpencodeServeRuntime()
