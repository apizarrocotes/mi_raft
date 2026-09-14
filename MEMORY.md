# MEMORY — mi_raft

## 2026-09-14 — Sesión 0: investigación y diseño
- Raft (raft.build) NO es open source; su core es cerrado. Referencias públicas estudiadas: `botiverse/multica` (Go, plataforma de agentes gestionados), `botiverse/raft-external-agents` (protocolo de wake de Raft), `omnigent-ai/omnigent` (abstracción de harnesses). Clones en `/tmp/opencode/refs/` (temporal, se pierde al reiniciar).
- **Decisión**: construir core propio fino en vez de adoptar framework (AutoGen/CrewAI/LangGraph no dan el modelo canales+tasks+identidades configurable, y pesan más que el problema). Alternativa descartada: partir de multica (Go, con capas SaaS que no aplican).
- **Decisiones de diseño** (justificadas por los repos):
  - SQLite+WAL con colas en la propia DB y claim atómico (multica usa Postgres solo por multi-tenancy).
  - Patrón wake de Raft: wakes sin contenido (solo IDs), extracción del cuerpo vía server; wake at-least-once, actividad at-most-once, coalescencia de ráfagas (DebouncedWakeNotifier).
  - Sesión + workdir persistidos por (agente, thread) para reanudar CLIs (migración 020 de multica).
  - Interfaz Executor con eventos + capacidades declaradas (omnigent/harness_capabilities.py) para añadir runtimes sin tocar el router.
- **Headless verificado con llamadas reales** (por subagente, sesiones reanudadas OK):
  - claude: `claude -p "P" --output-format json --permission-mode dontAsk` / resume `--resume <id>`
  - opencode: `opencode run "P" --format json --auto` / resume `-s <id>`; existe `opencode serve` para F2+
  - pi: `pi -p --mode json` / resume `-c` o `--session-id <id>`; limitar tools con `-t read,grep,...`
- **Elecciones del usuario**: API/CLI primero (UI web después), Python, una máquina (daemon multi-máquina aplazado), runtimes configurables: claude code, opencode, pi.
- Entorno: Python 3.11.2, sin `uv`; claude 2.1.270, opencode 1.18.21, pi 0.85.1 instalados.
- **2026-09-14 — lección**: el usuario tiene raft.build instalado (daemon `raft-computer` v1.0.5 en ~/.local/bin, instalado vía `curl -fsSL https://cdn.raft.build/computer/install.sh | sh`; estado en ~/.slock; servicio normalmente parado). El instalador oficial SOLO crea `raft-computer` — no clobberizamos nada — pero el CLI oficial de raft.build se llama `raft`, así que renombramos el nuestro a `miraft` (pyproject console script). Regla: antes de elegir nombre de binario, `type -a <nombre>` y reservar los nombres del ecosistema que imitamos. Al matar procesos del server usar patrones que no se auto-matching (pkill -f "[r]aft serve" en llamada separada).
- Validación end-to-end F1 superada: post con @opencode → creó hello.py en el sandbox, lo ejecutó, mencionó a @claude-code → claude verificó y cerró la tarea. Handoff agente→agente funcionando con sesión persistente (thread 1).

## 2026-09-14 — Sesión 2: F2 (tasks, memoria, runtime persistente)
- **`opencode serve` exige suscriptor**: POST /prompt sin un stream SSE abierto (global `/event`) queda encolado para siempre. El endpoint por-sesión (`/session/{sid}/event`) cuelga sin cabeceras. Patrón validado: abrir SSE global → POST prompt → leer eventos filtrando `data.sessionID` hasta `session.next.step.ended`. Campos: `text.ended→data.text`, `delta→data.delta`, `step.ended→data.finish` ("stop"/"error").
- **`opencode run --attach` a medio funcionar** en 1.18.21: solo emite step_start y sale; el turno continúa server-side. No usar.
- Primer turno tras arrancar `opencode serve` puede fallar con 401 PAID_MODEL_AUTH_REQUIRED (auth no cargada aún) → el runtime reintenta una vez con sesión nueva. Funciona.
- Sesiones de opencode sobreviven al reinicio del serve (storage global) → el `session_id` de la DB se reanuda aunque el server haya muerto.
- **Bug mordido (2ª vez)**: modelos Pydantic definidos dentro de `create_app` + `from __future__ import annotations` → FastAPI no resuelve la anotación → campos a query params → 422. Regla en CLAUDE.md: siempre a nivel de módulo.
- `~` sin expandir en `subprocess.Popen(cwd=...)` del runtime serve → FileNotFoundError; los runtimes que sobreescriben run_turn deben expandir/validar work_dir como el BaseRuntime.
- opencode con `--auto` creó ficheros en la RAÍZ del repo pese a work_dir=sandbox (interpretó "proyecto" como repo root). Pendiente F3: sandboxing real por agente.
- opencode-serve arranca sin contraseña (su propio warning) — localhost only.
- El CLI oficial `raft` de raft.build NO estaba instalado en ~/.local/bin (su instalador solo crea `raft-computer`); renombramos el nuestro a `miraft` de todos modos para no pisar su ecosistema.

## 2026-09-14 — Sesión 3: F3 (auth, webhooks, agentes externos, sandbox)
- **API keys**: `server.api_keys` en raft.yaml; vacío = API abierta (dev localhost). Con keys, TODO exige Bearer o X-mi-raft-key salvo /health. El CLI lee la primera clave del config automáticamente.
- **Agente externo validado en producción**: runtime `external` + `wake_url`. El wake lleva SOLO IDs (patrón Raft: {eventId, agentId, channel, threadId, messageId}); la respuesta llega publicando un mensaje con `author` = nombre registrado (el server lo marca author_type=agent). El runner hace polling de esa respuesta hasta timeout_s. ¡No mutar el AgentConfig en tests esperando que execute_run lo vea: lo recarga de la DB!
- **Detalle de opencode que explica el "escape" del sandbox**: opencode descubre el proyecto subiendo hasta el .git — un work_dir dentro de otro repo hace que trate la raíz del repo como proyecto. Fix: el sandbox es repo git propio (cmd_init lo hace; data/sandbox ya migrado). No es aislamiento real: --auto sigue pudiendo escribir fuera si se le ocurre; para trabajo serio → bwrap/landlock (F4).
- SQLite: el mismo objeto Database se usa desde threads del server y del runner; escrituras solo vía db.tx() (lock). Las lecturas directas conn.execute son seguras en un proceso.
- El server de opencode del agente opencode-server (puerto 14610) es un proceso huérfano persistente: sobrevive reinicios de miraft serve. Si cambia la config de puertos, matarlo a mano (pkill -f "opencode serve --port <N>").

## 2026-09-14 — Sesión 4: F4 (UI web)
- **UI = un solo index.html vanilla** servido por FastAPI (`/`): sin build step, sin node. Convención dura en CLAUDE.md: nunca introducir build step al core. El JS inline se valida con `node --check` tras extraerlo (regex sobre el HTML).
- **SSE**: `GET /events?last_id=N` con semántica `id > N` (no "desde ahora": un last_id mayor que cualquier id existente NO recibe nada nuevo salvo ids mayores — ojo en tests). Auth por query param `api_key` porque EventSource no soporta headers.
- **TestClient + SSE = cuelgue**: nunca GET a un endpoint de stream infinito en tests; solo el caso 401 (fallo antes de streamear) o clientes reales con timeout/lectura acotada (regla en CLAUDE.md).
- Parseo SSE manual: el bloque empieza con `id: N`, NO con `event:` — un `startswith("event:")` descarta todo (mordido en el test E2E). EventSource del navegador lo maneja nativo.
- pyproject: package-data incluye `static/*.html` (reinstalar editable tras añadir assets).

## 2026-09-14 — Sesión 5: F5 (equipos, organigrama, workspace)
- **Modelo de equipos**: un equipo = un server aislado (propio puerto/DB/api_key/workspace), como los "servers" de Raft. NO multi-tenancy en una DB (estaba en la lista no-copiar de multica y complica todo). `teams/<nombre>/raft.yaml` + `miraft serve --config ...`.
- **Puerto 8501 estaba ocupado por una app Streamlit** del sistema: el asignador de puertos de `team create` ahora hace bind-test real sobre 127.0.0.1, no solo compara con otros equipos.
- **Alta dinámica**: agentes/canales creados por API viven SOLO en la agent/channel tables (el YAML siembra, la DB conserva lo dinámico; sync_config con INSERT OR REPLACE no borra lo DB-only). work_dir por defecto de un agente nuevo = `workspace` del equipo (cfg.workspace).
- **Organigrama**: tabla org_edge, regenerada desde cfg.org en cada boot (DELETE + INSERT). Vacía = todo permitido. Enforcement solo para handoffs agente→agente; los humanos pueden mencionar a cualquiera. El bloqueo deja system message en el hilo.
- **Bienvenida onboarding**: en create_app, si existe #general y la DB de mensajes está vacía → mensaje @onboarding → run real del agente. Fire en cualquier server nuevo con #general.
- **argparse**: para que `--config` funcione tanto global como por subcomando, el duplicado en el subparser lleva `default=argparse.SUPPRESS` (si no, el default del subparser pisa el global). prog corregido a "miraft".
- Los agentes dinámicos NO pueden ser runtime `external` por API (necesitan wake_url en YAML) — validación en POST /agents.
- El agente `dev` dinámico respetó el workspace (opencode + repo git propio del workspace ayudan a fijar el "proyecto").

## 2026-09-14 — Sesión 6: memoria para agentes dinámicos
- **Hueco cerrado**: los agentes creados por API/UI nacían SIN memoria. Ahora POST /agents deriva `memory_file` por defecto del directorio de la DB (`<dir-db>/memory/<nombre>.md`) y crea el fichero con cabecera. `PUT /agents/{id}/memory` es retroactivo: si el agente no tenía memoria, se la asigna.
- API de memoria: GET/PUT `/agents/{id}/memory` (contenido del fichero); la UI (pestaña Equipo, clic en un agente) lo edita.
- **Lección de tests**: los tests que crean agentes dinámicos DEBEN fijar `cfg.server.db` al tmp — con el default (`data/raft.db` relativo a CWD) derivan memoria hacia `data/memory/` REAL y pueden pisar la memoria de agentes productivos (lo detectamos con alpha.md/nuevo-agente.md).
- La plantilla de team scaffold ya daba memoria al onboarding; ahora cualquier agente nuevo de cualquier equipo la tiene sin configurar nada.
