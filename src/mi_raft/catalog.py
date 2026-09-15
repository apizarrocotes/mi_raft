from __future__ import annotations

import subprocess
from pathlib import Path

_CACHE: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL = 300

CLAUDE_MODELS = [
    "claude-opus-4-1",
    "claude-sonnet-4-5",
    "claude-sonnet-4",
    "claude-haiku-4-5",
]


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return result.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def claude_catalog() -> list[dict]:
    return [{"provider": "anthropic", "model": m} for m in CLAUDE_MODELS]


def opencode_catalog() -> list[dict]:
    out: list[dict] = []
    for line in _run(["opencode", "models"]).splitlines():
        line = line.strip()
        if "/" not in line:
            continue
        provider, model = line.split("/", 1)
        out.append({"provider": provider.strip(), "model": model.strip()})
    return out


def pi_catalog() -> list[dict]:
    out: list[dict] = []
    for line in _run(["pi", "--list-models"]).splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            out.append({"provider": parts[0], "model": parts[1]})
    return out


def full_catalog(force: bool = False) -> dict[str, list[dict]]:
    import time

    now = time.monotonic()
    if not force and "all" in _CACHE and now - _CACHE["all"][0] < CACHE_TTL:
        return _CACHE["all"][1]
    catalog = {
        "claude": claude_catalog(),
        "opencode": opencode_catalog(),
        "pi": pi_catalog(),
    }
    _CACHE["all"] = (now, catalog)
    return catalog


def providers_for(runtime: str, catalog: dict[str, list[dict]] | None = None) -> list[str]:
    catalog = catalog or full_catalog()
    return sorted({m["provider"] for m in catalog.get(runtime, [])})


def models_for(runtime: str, provider: str, catalog: dict[str, list[dict]] | None = None) -> list[str]:
    catalog = catalog or full_catalog()
    return [m["model"] for m in catalog.get(runtime, []) if m["provider"] == provider]


def model_exists(runtime: str, provider: str | None, model: str) -> bool:
    catalog = full_catalog()
    entries = catalog.get(runtime, [])
    if provider:
        return any(m["provider"] == provider and m["model"] == model for m in entries)
    return any(m["model"] == model for m in entries)
