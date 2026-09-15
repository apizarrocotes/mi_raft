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

## Noche 2026-09-14/15 — Sprints 1-3 del backlog (run autónomo)
- **S1**: costes/tokens capturados de los 3 runtimes (claude json / opencode step_finish / pi message_end) → columnas en run + GET /usage + panel en Equipo; budget_usd por agente (claude --max-budget-usd); estados de agente reales (idle/working/error) — el set de status en _fail_run; markdown con marked+DOMPurify vendored en static/vendor (sin build step); editar agentes en UI; CI aplazado como docs/ci.yml.example (el token gh sin scope `workflow` no puede pushear workflows — `gh auth refresh -s workflow` lo arregla).
- **S2**: búsqueda FTS5 con tokenizador unicode61 sin diacríticos (trigger + backfill en init); /search + miraft search + overlay ⌘K con salto y resaltado; Activity (type=status) con mensajes de sistema en done/cancel de tasks; no-leídos + toasts + Notification API con mute; DMs (channel.type=dm, id dm-<agente>, members_json) — /channels excluye dms, /dms las lista.
- **S3**: tasks v2 — rebuild de tabla por CHECK constraint (SQLite no puede ampliar un CHECK: detectar sql viejo y copiar a task_v2); subtasks con parent_task_id + subtask_count; desglose por agente (asyncio.run dentro del endpoint sync del threadpool); runner concurrente (4 inflight, max_concurrent por agente ya lo garantiza el claim); sandbox bwrap con perfil conservador (ro-bind /, tmpfs HOME, credenciales ~/.claude ~/.config ~/.local read-only, workdir escribible) — bwrap NO instalado aquí y sudo pide contraseña: queda listo para cuando se instale.
- **Lección técnica repetida (3 veces)**: editar con oldString que termina en "\n" fusiona la línea siguiente — causó 3 SyntaxError (server.py, __main__.py). Al hacer edits de línea única, incluir la línea siguiente en el oldString y replicarla en el newString.
- **Regla reforzada**: modelos Pydantic SIEMPRE a nivel de módulo (mordida nº 3 con BreakdownIn dentro de create_app).
- Los tests de @mención multi-agente con route_message insertan mensajes en el canal demo del tmp — sin efectos colaterales; todo el suite (53) corre en <8s offline.

## 2026-09-15 — Telemetría de ejecución (run_message)
- **Refactor clave de BaseRuntime.run_turn**: de `communicate()` a pump de stdout línea a línea con `asyncio.timeout` (3.11), stdin feed concurrente como task, stderr concurrente. Sin esto no hay telemetría en vivo ni cancelación limpia.
- **claude pasa a `--output-format stream-json --verbose`** (verbose es obligatorio en -p con stream-json): eventos `assistant.message.content[]` con blocks `tool_use` (name/input) y `user` con `tool_result`; el resultado final llega en el evento `type:"result"` (último). parse_output mantiene compat con el JSON único antiguo.
- opencode: los tool calls llegan como evento con `part.type == "tool"` (tool: bash, callID, state.input) — parseo defensivo por contener "tool" en el type. pi: blocks con type que contiene "tool" (defensivo, no verificado con tool real). opencode-serve: solo tipos session.next.*; tool events si aparecen.
- **sink pattern**: runner pasa `event_sink` a run_turn; los fakes de tests necesitan `event_sink=None` en su firma (si no, TypeError). Cada runtime emite {type: tool_use|tool_result|text|step, tool, payload}; payload truncado a 4096 chars en DB.
- Verificado real: run de opencode con bash captura tool_use con el comando completo, steps y texto.

## 2026-09-15 — Herramientas web para agentes (análisis Raft + implementación)
- **Modelo de tools en Raft**: vienen del RUNTIME, no de la plataforma ("The runtime determines what tools the agent has access to"). Raft conecta 9 harnesses (Claude Code, Codex, Antigravity, Kimi, Copilot, Cursor, Gemini CLI, OpenCode, Pi). Nuestro modelo espejo: las tools son las del CLI + las que el server expone.
- **Superficie verificada localmente**: claude tiene WebSearch/WebFetch nativos (en -p con dontAsk hay que permitirlos: --allowedTools WebSearch WebFetch — ahora por defecto si web_search); opencode NO tiene búsqueda nativa (solo webfetch en su API, no flag CLI); pi con allowlist -t (bash opcional). → **Desigualdad resuelta con capa propia del server**: /tools/search + /tools/fetch abiertos (sin auth, para curl de agentes), inyectados en el prompt de todos los runtimes locales.
- **DDG antibot**: html.duckduckgo.com da 202+challenge a urllib SIEMPRE y a curl en peticiones repetidas (rate limit por IP). Solución: lite.duckduckgo.com/lite/ por POST (funciona) con fallback a html GET + caché 300s por query. Si se degrada, config con provider brave + brave_key (API de pago con tier gratis).
- /tools/fetch bloquea localhost/loopback/link-local (SSRF) y devuelve texto plano (scripts/estilos fuera, cap 100KB).
- Verificado E2E: opencode buscó la última versión de Python vía curl a /tools/search con su bash y citó fuentes correctas (3.14.7, agosto 2026).

## 2026-09-15 — Incidente timeout + recuperación del pipeline (romanticas)
- **Fallo**: trendwatcher (opencode) hizo 54 operaciones legítimas de research y chocó el timeout de 600s. Detección OK por diseño (run failed, agente error, system message). El pipeline quedó esperando (ningún run detrás).
- **2 errores MÍOS que rompieron opencode**: (1) maté PIDs `opencode` a ciegas creyendo que eran huérfanos de mi_raft — uno era la **propia sesión del usuario** (se rompió varias veces); (2) maté un `opencode run` que era un run VIVO del server (#24, "código -9"). **Regla permanente**: antes de matar cualquier proceso, verificar ascendencia en /proc — solo matar si llega a un `miraft serve`; jamás `opencode` sueltos (la sesión del usuario es un `opencode` sin más). Mi sesión actual: ancestro de mis comandos bash.
- **kill_tree mejorado** en BaseRuntime: killpg + barrido recursivo de descendientes vía /proc/*/stat (los nietos con setsid sobreviven al killpg). Comprometido.
- **Patrón anti-timeout que funcionó**: turno incremental (máx 5 búsquedas, guardar informe parcial SIEMPRE, avisar en canal para continuar) + timeout_s 900 para research. El informe v1 quedó en mercado/ con fuentes y pendientes marcados, y el agente preguntó al humano una decisión de producto (¿español o bilingüe?).
- pgrep en este host a veces cuelga el shell — inspeccionar procesos leyendo /proc directamente con python.

## 2026-09-15 — Control de proceso (pregunta clave del usuario)
- Pregunta: "¿cómo garantizan estos sistemas que el proceso se mantiene y no se para?" Respuesta implementada (3 piezas, in-process, sin cron):
  1. **Escalación a supervisor** (`escalate_to` en raft.yaml): fallos de run, handoffs bloqueados por org y runs atascados publican mensaje con @mención al supervisor y se ENRUTAN como runs — el pipeline nunca se para en silencio. Guardias: el supervisor no se auto-escala a sí mismo, máx 3 escalaciones por hilo (anti-bucle).
  2. **Watchdog** (watchdog_loop cada 60s): termina runs 'running' que superan timeout+gracia 120s y escala.
  3. **Ligazón con tasks**: la escalación indica la task afectada del hilo (task_for_thread).
- **Bug de LIKE mordido**: las escalaciones empiezan con @mención (texto = "@jefa ⚠️...") → `text LIKE '⚠️%'` no matcheaba nunca y el contador anti-bucle no contaba. Fix: escalate() normaliza el formato (⚠️ siempre primero, luego mención). Lección: cuando un formato de mensaje es contracto (prefijo buscable), construirlo en UN solo sitio (escalate), nunca en los callers.
- El supervisor sigue siendo quien decide (reintentar, reasignar, cerrar) — control de loop cerrado, no autonomía infinita. Cron/autopilots siguen excluidos.

## 2026-09-15 — Equipo ejemplo: sello "Tinta Ardiente" (romance picante KDP)
- `teams/romanticas/` — puerto 8504, key en su raft.yaml (local, gitignored). 7 agentes (onboarding + 6), 6 canales por lane, 12 aristas de org con la jefa-editorial como hub.
- Pipeline diseñado: mercado (trendwatcher, opencode+web) → biblia → outline → borradores (novelista, opencode) → edición (editor-fino, claude) → beta (beta-lectora, claude) → paquete KDP (kdp-manager, claude). Escala de picante 1-5 objetivo 4 con límites KDP explícitos en instrucciones. Workspace: manuscrito/ (+ediciones/), biblia/, mercado/, publicacion/.
- **Lección de diseño org**: no puse arista editor-fino→jefa (sus reportes van por canales) y el bloqueo automático funcionó en vivo; los agentes lo entendieron y se adaptaron ("espero señal en #manuscrito"). Lección: las aristas son para HANDOFFS de trabajo; los reportes de estado fluyen por canales. Si un lane necesita devolver trabajo al hub, añadir arista explícita.
- **Lección CONFIRMADA por fallo real (2026-09-15)**: el novelista terminó la biblia, intentó reportar a la jefa y el org lo bloqueó 2 veces → pipeline parado. Fix: TODO lane necesita arista de retorno al hub (novelista→jefa, editor-fino→jefa añadidas; 14 aristas). Modelo correcto: hub delega a todos, todos reportan al hub, lane-a-lane solo según pipeline. La recuperación se hizo con mensaje humano en el hilo mencionando a la jefa (contexto del hilo intacto).
- El kickoff dejó la jefa esperando "luz verde del humano" para lanzar Fase 0 — buen comportamiento de seguridad del equipo; el usuario decide cuándo arrancar el pipeline real (consume múltiples runs con búsqueda web incluida).
