from __future__ import annotations

from .base import BaseRuntime, RunResult, RuntimeTimeout

_registry: dict[str, BaseRuntime] = {}

from . import claude as _claude
from . import opencode as _opencode
from . import opencode_serve as _opencode_serve
from . import pi as _pi

_claude.register(_registry)
_opencode.register(_registry)
_pi.register(_registry)
_opencode_serve.register(_registry)


def get_runtime(name: str) -> BaseRuntime:
    rt = _registry.get(name)
    if rt is None:
        raise KeyError(f"Runtime no registrado: {name}")
    return rt


def register_runtime(rt: BaseRuntime) -> None:
    _registry[rt.name] = rt


__all__ = ["BaseRuntime", "RunResult", "RuntimeTimeout", "get_runtime", "register_runtime"]
