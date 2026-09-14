from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import example_config_text, load_config


def _http_json(
    method: str, url: str, payload: dict | None = None, key: str | None = None
) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"HTTP {exc.code} en {url}: {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"No se pudo conectar a {url}. ¿Está corriendo 'raft serve'?: {exc.reason}"
        )


def _server_url(args) -> str:
    if getattr(args, "url", None):
        return args.url.rstrip("/")
    cfg = load_config(args.config)
    return f"http://{cfg.server.host}:{cfg.server.port}"


def _api_key(args) -> str | None:
    if getattr(args, "url", None):
        return None
    try:
        cfg = load_config(args.config)
    except Exception:
        return None
    return cfg.server.api_keys[0] if cfg.server.api_keys else None


def cmd_init(args) -> None:
    path = Path(args.config)
    if path.exists() and not args.force:
        raise SystemExit(f"{path} ya existe (usa --force para sobreescribir)")
    text = example_config_text()
    path.write_text(text)
    sandbox = path.parent / "data" / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    if not (sandbox / ".git").exists():
        import subprocess

        try:
            subprocess.run(
                ["git", "init", "-q"], cwd=sandbox, check=True, timeout=15
            )
        except (OSError, subprocess.SubprocessError):
            print("aviso: git no disponible; el sandbox no queda aislado como repo")
    (sandbox / "README.md").write_text(
        "# Sandbox de agentes\n\nLos agentes de mi_raft trabajan aquí dentro.\n"
    )
    print(f"Config escrita en {path}")
    print(f"Sandbox creado en {sandbox} (repo git aislado)")
    print("Siguiente paso: edita raft.yaml y arranca 'miraft serve'")


def cmd_serve(args) -> None:
    import uvicorn

    from .db import Database
    from .server import create_app

    cfg = load_config(args.config)
    db = Database(cfg.server.db)
    app = create_app(cfg, db)
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    print(f"mi_raft escuchando en http://{host}:{port} (db: {cfg.server.db})")
    uvicorn.run(app, host=host, port=port, log_level="info")


def cmd_post(args) -> None:
    url = _server_url(args)
    body = {
        "text": args.text,
        "author": args.author,
        "thread_id": args.thread,
    }
    out = _http_json("POST", f"{url}/channels/{args.channel.lstrip('#')}/messages", body, key=_api_key(args))
    print(f"Mensaje {out['id']} publicado; runs encendidos: {out['routed_runs']}")


def cmd_messages(args) -> None:
    url = _server_url(args)
    out = _http_json("GET", f"{url}/channels/{args.channel.lstrip('#')}/messages?limit={args.limit}", key=_api_key(args))
    for m in out:
        time = (m.get("created_at") or "")[11:16]
        thread = f"  hilo {m['thread_id']}" if m.get("thread_id") else ""
        print(f"[{m['id']} {time}] {m['author_id']}{thread}: {m['text']}")
    if not out:
        print("(canal vacío)")


def cmd_team(args) -> None:
    import secrets

    sub = args.team_cmd
    if sub == "list":
        from pathlib import Path as _P

        teams = sorted(_P("teams").glob("*/raft.yaml"))
        for t in teams:
            try:
                cfg = load_config(t)
                print(f"{cfg.team:<20} puerto {cfg.server.port:<6} config: {t}")
            except Exception as exc:
                print(f"{t}: config inválida ({exc})")
        if not teams:
            print("(sin equipos; crea uno con: miraft team create <nombre>)")
        return

    name = args.name
    if not name.replace("-", "").isalnum():
        raise SystemExit("Nombre de equipo inválido (usa letras, números y guiones)")
    root = Path("teams") / name
    if root.exists():
        raise SystemExit(f"{root} ya existe")

    used_ports = set()
    for cfg_path in Path("teams").glob("*/raft.yaml"):
        try:
            used_ports.add(load_config(cfg_path).server.port)
        except Exception:
            pass

    import socket

    def port_free(port: int) -> bool:
        if port in used_ports:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    port = 8501
    while not port_free(port):
        port += 1
    key = secrets.token_hex(12)

    config_text = f"""team: {name}
server:
  host: 127.0.0.1
  port: {port}
  db: teams/{name}/data/raft.db
  api_keys:
    - {key}

# Organigrama: conexiones permitidas agente -> agente (handoffs).
# Vacío o ausente = todos pueden hablar con todos.
# org:
#   onboarding: [dev, revisor]
#   dev: [revisor]

workspace: teams/{name}/workspace

defaults:
  max_concurrent: 1
  timeout_s: 600

agents:
  - name: onboarding
    runtime: claude
    work_dir: teams/{name}/workspace
    memory_file: teams/{name}/data/memory/onboarding.md
    permissions:
      mode: dontAsk
    instructions: |
      Eres el agente de onboarding del equipo "{name}" en mi_raft.
      Tu trabajo: acompañar a la persona que acaba de crear este equipo.
      Guíala para: 1) dar de alta agentes con su rol (pídete ayuda si no sabe),
      2) crear canales de trabajo, 3) repartir la primera task.
      Sé breve, práctico y en español. Nunca hagas trabajo de otros roles.

channels:
  - name: general
    topic: Canal general del equipo {name}
  - name: onboarding
    topic: Acompañamiento y puesta en marcha
"""
    (root / "data" / "memory").mkdir(parents=True, exist_ok=True)
    (root / "raft.yaml").write_text(config_text)
    ws = root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "README.md").write_text(
        f"# Workspace del equipo {name}\n\n"
        "Directorio compartido por los agentes del equipo para trabajar y entregar resultados.\n"
    )
    import subprocess

    try:
        subprocess.run(["git", "init", "-q"], cwd=ws, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        print("aviso: git no disponible; el workspace no queda como repo")
    print(f"Equipo '{name}' creado en {root}/")
    print(f"  Puerto:  {port}")
    print(f"  API key: {key}")
    print(f"  Workspace compartido: {ws}/ (repo git)")
    print("Arranca el equipo con:")
    print(f"  miraft serve --config {root}/raft.yaml")
    print("Túnel desde tu PC:")
    print(f"  ssh -N -L {port}:127.0.0.1:{port} apc@<host>")
    print(f"UI: http://127.0.0.1:{port} (te pedirá la API key)")


def cmd_search(args) -> None:
    url = _server_url(args)
    out = _http_json(
        "GET",
        f"{url}/search?q={urllib.parse.quote(args.query)}&limit={args.limit}",
        key=_api_key(args),
    )
    for m in out:
        time_ = (m.get("created_at") or "")[11:16]
        thread = f" hilo {m['thread_id']}" if m.get("thread_id") else ""
        print(f"[#{m['channel_id']} {m['id']} {time_}] {m['author_id']}{thread}: {m['text'][:120]}")
    if not out:
        print("(sin resultados)")


def cmd_agents(args) -> None:
    out = _http_json("GET", f"{_server_url(args)}/agents", key=_api_key(args))
    for a in out:
        print(f"{a['id']} (runtime={a['runtime']}, work_dir={a['work_dir']})")


def cmd_channels(args) -> None:
    out = _http_json("GET", f"{_server_url(args)}/channels", key=_api_key(args))
    for c in out:
        print(f"#{c['id']}{(' — ' + c['topic']) if c['topic'] else ''}")


def _print_task(t: dict) -> None:
    assignee = ""
    if t.get("assignee_id"):
        assignee = f" → {t['assignee_type']}:{t['assignee_id']}"
    print(
        f"[#{t['id']}] {t['status']}{assignee} — {t['title']}"
        + (f" (hilo {t['thread_id']})" if t.get("thread_id") else "")
    )


def cmd_task(args) -> None:
    url = _server_url(args)
    sub = args.task_cmd
    if sub == "create":
        body = {
            "title": args.title,
            "description": args.description or "",
            "channel": args.channel,
            "assignee": args.assignee,
        }
        out = _http_json("POST", f"{url}/tasks", body, key=_api_key(args))
        print(f"Task {out['id']} creada (assignee: {out['assignee'] or '—'})")
    elif sub == "list":
        out = _http_json("GET", f"{url}/tasks" + (f"?status={args.status}" if args.status else ""), key=_api_key(args))
        for t in out:
            _print_task(t)
        if not out:
            print("(sin tasks)")
    elif sub == "show":
        t = _http_json("GET", f"{url}/tasks/{args.id}", key=_api_key(args))
        _print_task(t)
        if t.get("description"):
            print(t["description"])
        if t.get("result_text"):
            print(f"Resultado: {t['result_text']}")
    elif sub == "claim":
        out = _http_json("POST", f"{url}/tasks/{args.id}/claim", {"agent": args.agent}, key=_api_key(args))
        print(f"Task {args.id} reclamada por {args.agent} (mensaje {out['message_id']})")
    elif sub == "review":
        _http_json("POST", f"{url}/tasks/{args.id}/review", {}, key=_api_key(args))
        print(f"Task {args.id} in_review")
    elif sub == "done":
        _http_json("POST", f"{url}/tasks/{args.id}/done", {"result": args.result}, key=_api_key(args))
        print(f"Task {args.id} done")
    elif sub == "cancel":
        _http_json("POST", f"{url}/tasks/{args.id}/cancel", {}, key=_api_key(args))
        print(f"Task {args.id} cancelled")
    elif sub == "comment":
        out = _http_json(
            "POST", f"{url}/tasks/{args.id}/comment", {"text": args.text, "author": args.author}, key=_api_key(args)
        )
        print(f"Comentario {out['id']} publicado; runs encendidos: {out['routed_runs']}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="miraft", description="CLI de mi_raft"
    )
    parser.add_argument("--config", default="raft.yaml", help="ruta a raft.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="genera raft.yaml de ejemplo")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="arranca servidor + runner")
    p.add_argument("--config", default=argparse.SUPPRESS, help="ruta a raft.yaml")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("post", help="publica un mensaje en un canal")
    p.add_argument("channel")
    p.add_argument("text")
    p.add_argument("--author", default="humano")
    p.add_argument("--thread", type=int, default=None)
    p.add_argument("--url", default=None, help="URL del server (si no, se lee de raft.yaml)")
    p.add_argument("--config", default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("messages", help="lee un canal")
    p.add_argument("channel")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--url", default=None)
    p.add_argument("--config", default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("agents", help="lista agentes")
    p.add_argument("--url", default=None)
    p.add_argument("--config", default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("channels", help="lista canales")
    p.add_argument("--url", default=None)
    p.add_argument("--config", default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("task", help="gestión de tasks")
    p.add_argument("--url", default=None)
    p.add_argument("--config", default=argparse.SUPPRESS)
    task_sub = p.add_subparsers(dest="task_cmd", required=True)

    p2 = task_sub.add_parser("create", help="crea una task")
    p2.add_argument("title")
    p2.add_argument("-d", "--description", default=None)
    p2.add_argument("-c", "--channel", default=None)
    p2.add_argument("-a", "--assignee", default=None, help="agente asignado (opcional)")
    p2.set_defaults(func=cmd_task)

    p2 = task_sub.add_parser("list", help="lista tasks")
    p2.add_argument(
        "--status", default=None,
        choices=["todo", "in_progress", "in_review", "done", "cancelled"],
    )
    p2.set_defaults(func=cmd_task)

    p2 = task_sub.add_parser("show", help="detalle de una task")
    p2.add_argument("id", type=int)
    p2.set_defaults(func=cmd_task)

    p2 = task_sub.add_parser("claim", help="un agente reclama una task open")
    p2.add_argument("id", type=int)
    p2.add_argument("--agent", required=True)
    p2.set_defaults(func=cmd_task)

    p2 = task_sub.add_parser("done", help="marca una task como done")
    p2.add_argument("id", type=int)
    p2.add_argument("-r", "--result", default=None)
    p2.set_defaults(func=cmd_task)

    p2 = task_sub.add_parser("review", help="marca una task in_progress como in_review")
    p2.add_argument("id", type=int)
    p2.set_defaults(func=cmd_task)

    p2 = task_sub.add_parser("cancel", help="cancela una task")
    p2.add_argument("id", type=int)
    p2.set_defaults(func=cmd_task)

    p2 = task_sub.add_parser("comment", help="comenta una task (menciones despiertan agentes)")
    p2.add_argument("id", type=int)
    p2.add_argument("text")
    p2.add_argument("--author", default="humano")
    p2.set_defaults(func=cmd_task)

    p = sub.add_parser("search", help="busca mensajes en todo el workspace")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--url", default=None)
    p.add_argument("--config", default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("team", help="gestión de equipos")    p.add_argument("--url", default=None)
    team_sub = p.add_subparsers(dest="team_cmd", required=True)
    p2 = team_sub.add_parser("create", help="crea un equipo aislado (server propio)")
    p2.add_argument("name")
    p2.set_defaults(func=cmd_team)
    p2 = team_sub.add_parser("list", help="lista los equipos")
    p2.set_defaults(func=cmd_team)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
