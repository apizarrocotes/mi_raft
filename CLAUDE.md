# mi_raft — Contrato de trabajo

## Qué es este proyecto
Orquestador multi-agente self-hosted estilo raft.build. Python 3.11, SQLite+WAL, una máquina.
Agentes = runtimes CLI headless (claude code, opencode, pi) definidos en YAML.
UI web desde F4: UN solo index.html vanilla servido por FastAPI — NUNCA añadir build step ni node al core.

## Decisiones que NO se revisan sin discusión
- **SQLite + WAL** (no Postgres): una máquina, colas en la misma DB (claim atómico UPDATE...RETURNING).
- **Patrón wake de Raft**: los eventos de despertar no transportan contenido; el runner lee el cuerpo de la DB. Wake = at-least-once; telemetría de actividad = at-most-once, fire-and-forget.
- **Abstracción de runtime desde el día 1** (patrón Executor de omnigent): el router nunca sabe qué CLI corre debajo. Añadir un runtime = un archivo en `runtimes/`.
- **Sesión persistente por par (agente, thread)**: se guarda `session_id` + `work_dir` y se reanuda con --resume/-s/-c. Nunca re-prompt del contexto.
- **Menciones estructuradas** `[@Nombre](mention://agent/<id>)` generadas por el server; matching de texto plano @Nombre solo en entrada humana.
- **Continuidad de hilo**: respuesta humana en un thread sin menciones despierta al último agente participante (multica lo hace con el assignee).
- **Memoria del agente = fichero** (`memory_file` en YAML): se inyecta en el prompt; el agente lo edita con sus tools. Nada de DB vectorial en F2.
- **No copiar de multica**: squads, autopilots/cron, multi-workspace, auth multiusuario, Redis, cloud runtimes.

## Convenciones de código
- Python 3.11 stdlib + mínimo deps: fastapi, uvicorn, pyyaml, httpx. CLI con argparse (sin typer/click).
- Sin typechecker configurado aún; usar type hints igualmente.
- Config = `raft.yaml` (ver `config.example.yaml`). Todo lo configurable vive ahí, nunca en código.
- Un runtime de agente = un módulo en `src/mi_raft/runtimes/` que implementa `BaseRuntime`.
- DB en `data/raft.db` (gitignored). Logs del server a stdout.

## Comandos
- `miraft serve` — arranca servidor + runner (o `python -m mi_raft serve` desde `src/`)
- `miraft team create <n>` / `team list` — equipos: cada uno es un server aislado (puerto/DB/clave/workspace propios)
- `miraft post <#canal> <texto>` / `messages <#canal>` / `agents` / `channels` / `init`
- `--config` funciona global y por subcomando (argparse.SUPPRESS en el duplicado)
- Tests: `python -m pytest tests/` (si pytest no está: scripts de humo en `scripts/`)
- NUNCA usar `raft` como nombre de binario: es el CLI oficial de raft.build (en esta máquina solo está `raft-computer`, su daemon; el nombre `raft` queda reservado para su ecosistema).

## Riesgos conocidos
- CLIs de agentes pueden colgarse: todo subprocess lleva timeout configurable y kill.
- `--auto` (opencode) y `--permission-mode` (claude) son potentes: los permisos por agente se definen en YAML, nunca hardcode. En la demo, opencode con `--auto` escribió FUERA de su sandbox (raíz del repo): sandbox = repo git propio (miraft init lo hace); para aislamiento real → bwrap/landlock.
- `opencode serve` arranca sin contraseña (warning del propio binario): aceptable en localhost monousuario; no exponer nunca fuera de 127.0.0.1. La API key viaja como query param en /events (limitación de EventSource): solo para localhost.
- El primer turno contra un `opencode serve` recién arrancado puede 401 (auth aún no cargada): el runtime reintenta una vez por eso.
- Pydantic models de FastAPI SIEMPRE a nivel de módulo: dentro de create_app, `from __future__ import annotations` rompe la resolución y los campos van a query params (bug ya mordido dos veces).
- NUNCA hacer GET de un endpoint SSE desde tests de TestClient: el stream infinito cuelga el cliente (probar solo el 401, o el flujo con cliente HTTP de lectura acotada).
