from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, field
from pathlib import Path

import yaml

KNOWN_RUNTIMES = ("claude", "opencode", "pi", "opencode-serve", "external")


@dataclass
class AgentConfig:
    name: str
    runtime: str
    work_dir: str
    instructions: str = ""
    model: str | None = None
    permissions: dict = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)
    max_concurrent: int = 1
    timeout_s: int = 600
    memory_file: str | None = None
    server_port: int | None = None
    wake_url: str | None = None
    budget_usd: float | None = None
    sandbox: dict = field(default_factory=dict)
    web_search: bool = True


@dataclass
class ChannelConfig:
    name: str
    topic: str = ""


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8420
    db: str = "data/raft.db"
    api_keys: list[str] = field(default_factory=list)
    web_search_provider: str = "ddg"
    brave_key: str = ""


@dataclass
class Config:
    server: ServerConfig
    agents: list[AgentConfig]
    channels: list[ChannelConfig]
    team: str = "mi_raft"
    workspace: str | None = None
    org: dict[str, list[str]] = field(default_factory=dict)
    escalate_to: str | None = None


def example_config_text() -> str:
    return (
        importlib.resources.files("mi_raft").joinpath("config.example.yaml").read_text()
    )


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"No existe {path}. Crea uno con: python -m mi_raft init"
        )
    raw = yaml.safe_load(path.read_text()) or {}
    server = ServerConfig(**(raw.get("server") or {}))
    defaults = raw.get("defaults") or {}
    team = str(raw.get("team") or "mi_raft")
    workspace = raw.get("workspace")
    org_raw = raw.get("org") or {}
    org: dict[str, list[str]] = {}
    for lead, reports in org_raw.items():
        if not isinstance(reports, list):
            raise ValueError(f"org.{lead} debe ser una lista de nombres de agente")
        org[str(lead)] = [str(r) for r in reports]

    agents: list[AgentConfig] = []
    seen: set[str] = set()
    for item in raw.get("agents") or []:
        name = item.get("name")
        runtime = item.get("runtime")
        if not name or not runtime:
            raise ValueError("Cada agente necesita 'name' y 'runtime'")
        if name in seen:
            raise ValueError(f"Agente duplicado: {name}")
        seen.add(name)
        if runtime not in KNOWN_RUNTIMES:
            raise ValueError(
                f"Runtime desconocido '{runtime}' (conocidos: {', '.join(KNOWN_RUNTIMES)})"
            )
        work_dir = item.get("work_dir")
        if not work_dir:
            raise ValueError(f"El agente '{name}' necesita 'work_dir'")
        agents.append(
            AgentConfig(
                name=name,
                runtime=runtime,
                work_dir=str(work_dir),
                instructions=item.get("instructions") or "",
                model=item.get("model"),
                permissions=item.get("permissions") or {},
                extra_args=[str(a) for a in (item.get("extra_args") or [])],
                max_concurrent=int(item.get("max_concurrent", defaults.get("max_concurrent", 1))),
                timeout_s=int(item.get("timeout_s", defaults.get("timeout_s", 600))),
                memory_file=item.get("memory_file"),
                server_port=item.get("server_port"),
                wake_url=item.get("wake_url"),
                budget_usd=item.get("budget_usd"),
                sandbox=(
                    {"enable": True}
                    if item.get("sandbox") is True
                    else (item.get("sandbox") or {})
                ),
                web_search=bool(item.get("web_search", True)),
            )
        )
    if not agents:
        raise ValueError("Define al menos un agente en 'agents'")

    channels: list[ChannelConfig] = []
    seen_channels: set[str] = set()
    for item in raw.get("channels") or []:
        cname = item.get("name")
        if not cname:
            raise ValueError("Cada canal necesita 'name'")
        cname = cname.lstrip("#")
        if cname in seen_channels:
            raise ValueError(f"Canal duplicado: {cname}")
        seen_channels.add(cname)
        channels.append(ChannelConfig(name=cname, topic=item.get("topic") or ""))

    return Config(server=server, agents=agents, channels=channels, team=team,
                  workspace=workspace, org=org, escalate_to=raw.get("escalate_to"))
