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

## 2026-09-15 — Diseño org refinado (corrección del usuario)
- **Modelo definitivo del hub**: la jefa delega SOLO a los heads de lane (novelista-lead, editor-fino, trendwatcher, kdp-manager, beta-lectora) — NO directamente a las escritoras. La escritura se reparte vía lead. La lista de delegación define los lanes normales.
- **Bypass de supervisor (código)**: `escalate_to` nunca se le bloquea un handoff (route_message) — es la vía de EMERGENCIA (escalaciones de fallos, micro-fixes urgentes), distinta de la delegación normal. Test: supervisor menciona sin arista → run creado; agente normal sin arista → bloqueado.
- **Edición en vivo del org**: las aristas viven en la tabla org_edge (reconstruidas del yaml SOLO en boot) — se pueden editar por SQL sin reinicio y el route_message las lee en vivo. El yaml es el source of truth para el próximo boot.
- Incidente que lo destapó: la jefa mencionó a novelista-a/b directamente (micro-fix L64) → bloqueado 2 veces → ella misma lo diagnosticó ("lane muda, probable flag interno del runner post-pausa") — modelo mental erróneo; el M1 prompt injection evita que los agentes inventen teorías de infraestructura.

## 2026-09-15 — Catálogo de proveedores/modelos (petición: CLI + proveedor + LLM configurables)
- **Los CLIs exponen su catálogo**: `opencode models` (69 combos "provider/model"), `pi --list-models` (tabla provider/model/context), claude NO tiene listado (lista curada de Anthropic en catalog.py). catalog.py los llama con caché TTL 300s.
- **agents.provider** (columna + YAML + PATCH): opencode combina provider/model en `-m provider/model` (si model no trae "/"); pi usa `--provider` + `--model` separados; claude ignora provider (solo anthropic). PATCH valida el modelo contra el catálogo (422 si no existe).
- La PRIMERA llamada a GET /models tarda varios segundos (ejecuta los CLIs) — luego caché 300s. En curl con -m corto parece colgado; usar margen.
- En romanticas, trendwatcher y novelista tienen provider nan + glm5.3-flash explícito (antes era el default implícito de opencode).
- Los agentes del equipo publican por la API con curl (lo aprendieron solos leyendo raft.yaml) — el prompt ahora les enseña a firmar con su author; mensajes anónimos #50/#55 re-atribuidos a novelista en la DB.

## 2026-09-15 — Auditoría de errores + M1-M6 (análisis pedido por el usuario)
- **Hallazgos del análisis** (canal + actividad): 34/49 eventos = handoffs bloqueados por org (agentes no conocían el grafo a priori); ack-storms = el 80% del coste (5 agentes en un canal, cada reply despierta a otros; acuses "sin acción" con turnos claude de 2-15 min); stale snapshots (agentes deciden sobre estado viejo de hilos); kdp despierto sin poder trabajar (falta de precedencia); 15 fallos de run.
- **M1**: grafo de comunicación inyectado en el prompt (delega a / reporta a / fuera de líneas por canal sin mención / no-acuses).
- **M3**: continuidad de hilo SOLO en DMs — en canales multi-agente solo las menciones despiertan. Test actualizado (continuidad en canal normal = 0 runs; en DM = 1).
- **M4**: task_deps + claim que salta hilos con tasks bloqueadas. CUIDADO: el claim puede devolver None aunque haya runs queued si su thread está bloqueado — los tests de continuidad deben cerrar sus runs (finish_run) antes de reclamar el siguiente (max_concurrent=1 bloqueaba el test, no el código).
- **M5**: phase en tasks (texto libre; fases del sello: mercado/biblia/borrador/edicion/publicacion).
- **M6**: editor-fino/beta-lectora/kdp-manager → pi + nan/deepseek-v4-flash (384K salida, elimina reason=length; 1M contexto). La jefa se queda en claude. Sesiones claude previas no migran — la memoria por fichero es el transporte de contexto. Primer run pi verificado en vivo (#308, 20 ops, tool events capturados).
- **Cosmético pendiente**: el parse_line de pi captura el prompt del usuario como eventos text (op #1/#3) — filtrar role=user en el futuro.
- **Estado**: server romanticas REINICIADO y operativo con todo M1-M6. Si el coste sigue siendo alto, el siguiente escalón es membresía por canal (Raft-style: los agentes solo escuchan sus lanes).

## 2026-09-15 — El bug del timeline vacío (resuelto)
- **Síntoma**: ni los mensajes del humano ni los de los agentes aparecían en la web (pero estaban en la DB y los agentes los recibían).
- **Causa raíz**: al servir /vendor (marked v12), renderText pasó a la rama markdown que llamaba `marked.setOptions(...)` — **inexistente como método del singleton en marked v12** → CADA render() lanzaba excepción → la timeline jamás se pintaba. Antes se veía todo porque /vendor daba 404 y caía en texto plano (el vendor 404 enmascaraba este bug).
- **Fix**: renderText blindado — capability check (marked.parse + DOMPurify.sanitize), opciones INLINE en parse (no setOptions), try/catch con fallback a texto plano. Lección: un fallback silencioso (vendor 404 → texto plano) enmascaró el bug durante horas; al arreglar el 404 se activó. Siempre testear la rama "buena" de un fallback.
- **Diagnóstico sin X**: el chromium headless no arranca sin libatk/libcups (no sudo) — el testing de UI se hace por análisis estático + node --check + simulación con vm de node (con sandbox correcto: no inyectar globalThis manual).
- **Limpieza pedida por el usuario**: 559 mensajes de #manuscrito borrados (canal reiniciado). La biblia, outline, ediciones y el tablero espejo sobreviven en el workspace (biblia/estado-manuscrito.md).

## 2026-09-15 tarde — Límite de línea + cuota claude de la jefa
- **Fallo masivo "Separator is not found / chunk longer than limit"**: el StreamReader de asyncio limita las líneas a 64KB — los JSON de pi/claude-stream con outputs de tools grandes las revientan y caían TODOS los agentes. Fix: `limit=64MB` en create_subprocess_exec (base.py). Verificado: novelista-a completó post-fix.
- **Cuota claude de la jefa**: "You've hit your session limit · resets 8:10pm" (api_error_status 429) — el result event llega AUNQUE exit≠0. diagnose_failure hook en BaseRuntime; claude extrae result.text → escalaciones/mensajes limpios ("claude error API 429: ..."). Red herring: "sdk_opt_in_required" es solo fast_mode_disabled_reason, NO el problema.
- **Proceso**: jefa parada hasta las 20:10 (cuota). 10 runs pausados con motivo. El equipo sigue en lanes pi/opencode (cuota nan aparte). Al reactivar: basta una mención a la jefa cuando el usuario quiera.
- Lección: al aparecer fallos en cascada de TODOS los agentes a la vez, buscar límites de infraestructura compartida (buffer sizes, cuotas) antes de causas por agente.

## 2026-09-15 tarde II — Servidor zombie y la web sin actualizar
- **La web "no actualizaba" porque un server VIEJO seguía dueño del puerto 8504**: los logs mostraban "address already in use" — los reinicios fallaban al bindear y el proceso antiguo (código sin los fixes del día) seguía sirviendo. SIGTERM a uvicorn puede quedar en gracia indefinida si está atascado → verificar SIEMPRE con `ss -lptn sport = :<puerto>` QUÉ PID es dueño, y SIGKILL si es un zombie. Tras reiniciar, validar que el puerto lo tiene el PID nuevo (ficha /vendor o /health + versión).
- **REGRA ABSOLUTA tras incidente**: jamás matar por patrones genéricos ("uvicorn", "python") — en esta máquina conviven servicios del usuario (growatt :8010, CRM, streamlits 8501/8502). Solo matar procesos verificados como propios por ascendencia/cmdline exacta. Maté por error growatt y el CRM (el usuario perdonó el CRM; growatt tenía watchdog y se re-levantó solo).
- Los procesos con setsid sobreviven killpg → _kill_tree con barrido de descendientes vía /proc (ya implementado).
- index.html ahora se sirve con Cache-Control: no-cache (el navegador cacheaba HTML viejo tras los restarts).

## 2026-09-15 — Equipo ejemplo: sello "Tinta Ardiente" (romance picante KDP)
- `teams/romanticas/` — puerto 8504, key en su raft.yaml (local, gitignored). 7 agentes (onboarding + 6), 6 canales por lane, 12 aristas de org con la jefa-editorial como hub.
- Pipeline diseñado: mercado (trendwatcher, opencode+web) → biblia → outline → borradores (novelista, opencode) → edición (editor-fino, claude) → beta (beta-lectora, claude) → paquete KDP (kdp-manager, claude). Escala de picante 1-5 objetivo 4 con límites KDP explícitos en instrucciones. Workspace: manuscrito/ (+ediciones/), biblia/, mercado/, publicacion/.
- **Lección de diseño org**: no puse arista editor-fino→jefa (sus reportes van por canales) y el bloqueo automático funcionó en vivo; los agentes lo entendieron y se adaptaron ("espero señal en #manuscrito"). Lección: las aristas son para HANDOFFS de trabajo; los reportes de estado fluyen por canales. Si un lane necesita devolver trabajo al hub, añadir arista explícita.
- **Lección CONFIRMADA por fallo real (2026-09-15)**: el novelista terminó la biblia, intentó reportar a la jefa y el org lo bloqueó 2 veces → pipeline parado. Fix: TODO lane necesita arista de retorno al hub (novelista→jefa, editor-fino→jefa añadidas; 14 aristas). Modelo correcto: hub delega a todos, todos reportan al hub, lane-a-lane solo según pipeline. La recuperación se hizo con mensaje humano en el hilo mencionando a la jefa (contexto del hilo intacto).
- El kickoff dejó la jefa esperando "luz verde del humano" para lanzar Fase 0 — buen comportamiento de seguridad del equipo; el usuario decide cuándo arrancar el pipeline real (consume múltiples runs con búsqueda web incluida).

## 2026-09-16 — Timeout del lane de escritura (novelistas)
- **Síntoma**: `novelista-a` con "enmudecimiento" intermitente — runs que mueren a los 900s exactos (run #457; novelista-lead #454) y la cola serial los relanza → estado `error`.
- **Datos**: los runs buenos del lane tardan 80–640s (novelista-a/b) y hasta 776s (lead); 900s (el default del equipo) no dejaba margen para los capítulos largos.
- **Fix**: `timeout_s: 1800` explícito para novelista-lead, novelista-a y novelista-b en `teams/romanticas/raft.yaml` (no tracked, gitignored). Aplicado con reinicio de `miraft serve` (SIGTERM a uvicorn se quedó colgado → SIGKILL, ya documentado) — nuevo PID y sync_config dejó los 3 en 1800 + status `idle`.
- Nota: `novelista-a` apunta a `memory_file: .../memory/novelista.md` (sin sufijo `-a`); es su memoria acumulada real, no renombrar sin migrar el fichero.

## 2026-09-16 — "pi no devolvió respuesta de asistente": contexto desbordado + error silenciado
- **Síntoma**: jefa-editorial (pi) falla en 3-4s con `pi no devolvió respuesta de asistente: {"type":"session"...}`. Run #479, hilo 642.
- **Causa raíz (doble)**: (1) `build_prompt` inyecta el fichero de memoria ENTERO cada turno; la memoria de jefa-editorial pesa **173 KB** (editor-fino 98 KB, beta-lectora 76 KB) y, con la sesión reanudada encima, desborda el contexto de `nan/qwen3.6` (262K) → el proveedor devuelve **400 Invalid request**. El JSONL de pi lo registra (`"stopReason":"error"`, `errorMessage:"400: ..."`), pero (2) `PiRuntime.parse_output` solo mira si hay texto → error críptico que oculta la causa. Se ve idéntico en runs #452 (hilo 628) y en editor-fino/beta-lectora/kdp-manager.
- **Fix**: `memory_max_chars` configurable por agente (default 24000, `config.py`/`db.py`/`raft.yaml`); `build_prompt` recorta la memoria inyectada conservando inicio+fin y deja aviso para que el agente la consolide (el fichero completo sigue en disco). `PiRuntime` ahora propaga `stopReason=error`/`errorMessage`. Tests: `test_prompt_truncates_oversized_memory`, `test_pi_provider_error_is_surfaced` (79 pasan).
- **Verificado en copia de la DB real**: prompt de jefa-editorial 203.919 → 54.351 chars.
- **Pendiente**: reiniciar `miraft serve` del equipo para cargar el código nuevo (la migración añade la columna sola). Requiere OK del usuario (SIGTERM puede colgarse). Las sesiones pi ya creadas siguen enormes: al reintentar, pi compacta; las memorias gigantes deben consolidarse.
- **Distinto**: `novelista-a`/opencode `razón=length` NO es esto — su sesión gastó ~60.000 chars solo en *reasoning* sin emitir texto ni tool; el diagnóstico limpio ya existía. Reducir el prompt ayuda, pero el modelo sobre-piensa.

## 2026-09-16 (tarde) — novelista-a "no escribe": reasoning runaway de qwen3.8-flash
- **Revisión pedida por el humano** (jefa lo contaba en DM): confirmado por disco y sesiones de opencode. `manuscrito/cap09-las-cuentas.md` congelado en 13.511 B / mtime 15-Sep 19:06, cabecera `~(cw)` sin cerrar.
- **Qué pasó run a run**: #457 (timeout 900s) SÍ escribió bloques 1-3, pero luego se puso a podar sobre `/tmp/opencode/cap09-body.md` (fuera del workspace) y expiró sin copiar de vuelta → de ahí existe el fichero a medias. #465, #478 y #482 no escribieron nada.
- **Causa raíz**: `qwen3.8-flash` (nan) gasta TODO el presupuesto de salida en *reasoning* oculto y muere con `finish=length` sin emitir texto/tool. #478 = 59.991 chars de reasoning; #482 = 59.988 (techo determinista ~60K). El brief quirúrgico denso de la lead (#700) lo dispara.
- **Dos bugs de mi_raft que lo enmascaraban** (`runtimes/opencode.py:parse_output`): (a) devolvía `done` con cualquier texto intermedio aunque el ÚLTIMO paso muriese por `length` → #482 se marcó done y publicó "Leo por tramos:" como respuesta; (b) los tokens se sobreescribían paso a paso y el último (`length`, 0) dejaba `tokens_in/out=0`, de ahí el "cero tokens procesados" de la jefa.
- **Fix aplicado**: (1) `extra_args: ["--variant","minimal"]` en novelista-a/b/lead del `teams/romanticas/raft.yaml` — probado: opencode+nan/qwen3.8-flash acepta `--variant minimal` y el reasoning bajó a 15 tokens; (2) `parse_output` ahora falla si el último `finish=length` aunque haya texto (lo incluye como "Texto parcial") y acumula tokens/coste entre pasos; (3) instrucciones de novelista-a/b: PROHIBIDO `/tmp`, escribir en `manuscrito/` con write/edit desde el inicio. Tests: 80 (`test_length_with_text_still_errors`, `test_opencode_accumulates_tokens_across_steps`).
- **Sesiones reseteadas** (`session_id=NULL`, con backup): antes jefa-editorial 642/628/640, editor-fino 512/359, beta-lectora 512; ahora también novelista-a hilo 642 (run #482, sesión envenenada con el reasoning de 60K). Server reiniciado (PID nuevo; SIGTERM vuelve a colgarse → SIGKILL verificado por puerto).
- **Lección**: un run no debe marcarse `done` si el último paso del agente murió por límite de salida; y elegir variante de razonamiento (`minimal`) es palanca de config, no de prompt, para modelos que sobre-piensan.

## 2026-09-16 (tarde II) — Web: autorefresco; pi: tokens en cero
- **Web**: `index.html` ya tenía SSE (`/events`) para mensajes nuevos, pero nada refrescaba salud/agentes/actividad ni reabría el stream si moría. Añadido `tick()` cada 5s: ping `/health`, reabre `EventSource` si `readyState===2`, recarga `/agents`, y en tab chat hace *fallback* de merge de mensajes nuevos (solo re-render si cambió, sin duplicar vía `S.seen`); en tasks/activity refresca salvo si hay detalle de run abierto. Verificado `node --check` del JS inline. No requiere reinicio (index.html se sirve de disco con no-cache; basta recargar el navegador).
- **pi sin tokens**: NO era parseo de mi_raft. El paquete `pi-nan-provider` borra `stream_options` por defecto (`supportsUsageInStreaming:false` en su catálogo) porque el esquema de NaN no documenta usage en streaming → `message.usage` llega a ceros (542/542 mensajes pi revisados tenían 0). Opt-in por modelo en `~/.pi/agent/models.json` con `providers.nan.modelOverrides.<modelo>.compat.supportsUsageInStreaming:true` (qwen3.6, qwen3.8-flash, deepseek-v4-flash, glm5.3-flash, mimo-v2.5, gemma4). Probado: pi+nan/qwen3.6 ahora devuelve `usage.input=933, output=24`; `PiRuntime.parse_output` ya leía esas claves. Test añadido `test_pi_parse_stream_usage_shape` (81 pasan). No requiere reinicio (pi lee models.json al arrancar cada run).
- Nota: `~/.pi/agent/models.json` es config global de pi, fuera del repo; si el gateway algún día deja de mandar usage, quitar el override.
- **Consumo (corrección)**: `reasoning` YA está dentro de `output` (pi-ai: `output=completion_tokens`, `reasoning=completion_tokens_details.reasoning_tokens`); NO sumarlo. Lo que faltaba era la entrada: pi separa `input` (prompt sin caché) de `cacheRead`/`cacheWrite`, y `PiRuntime` solo guardaba `input`. Ahora `tokens_in = input + cacheRead + cacheWrite` (= prompt_tokens) para contar consumo real; test `test_pi_parse_stream_usage_shape`. Pendiente reiniciar el server para cargarlo (había 2 runs de novelista-lead activos en el hilo 710 al aplicarlo).
- **Concurrencia por (agente, hilo)**: `claim_next_run` limitaba `max_concurrent` por agente pero NO por hilo; `coalesce_run` solo fusiona con runs `queued`, no `running`. Resultado visto en vivo: novelista-lead (`max_concurrent: 2`) con #487 (trigger #710) y #489 (trigger #713) corriendo en paralelo sobre el MISMO hilo. No es correcto: mismo prompt/sesión, respuestas duplicadas, ediciones concurrentes. `max_concurrent>1` es para lanes/hilos distintos. Fix: condición `NOT EXISTS (running del mismo agent_id y thread_id)` en el claim. Test `test_claim_serializes_same_agent_thread` (82 pasan).

## 2026-09-16 (noche) — Migración de los novelistas: opencode → pi
- **Motivo**: novelista-a volvió a morir por `razón=length` (run #493, hilo 725, cap10): con `--variant minimal` activo, el último paso gastó **59.987 chars de reasoning** y 0 texto/tool. A/B en el gateway confirma que `minimal` sí baja el reasoning (default 1.338 vs minimal 0 chars en prueba), pero NO es tope duro; en briefs densos el modelo (qwen3.8-flash) sigue desbocándose. Es estocástico: el reenvío (#495) completó cap10 (`cap10-la-visita-cancelada.md`, ~12.6 KB).
- **Cambio**: `novelista-lead`, `novelista-a` y `novelista-b` pasan de `runtime: opencode` a `runtime: pi`, `model: qwen3.6` (el de jefa, el pi más estable aquí), `permissions.tools: [read,grep,find,ls,bash,write,edit]` y `extra_args: ["--thinking","minimal"]` (pi: off|minimal|low|medium|high|xhigh|max). `max_concurrent`/`timeout_s` sin cambios.
- **Sesiones**: reseteados 41 `session_id` de runs `done` con prefijo `ses_` (formato opencode) de los 3 agentes; pi no reanuda ids de opencode. Backup `/tmp/opencode/raft.db.bak-20260916-192528`. Server reiniciado (PID nuevo; SIGTERM volvió a colgarse → SIGKILL; el run #498 que arrancó con la config vieja se mató también).
- Lección: `--variant minimal` (opencode) y `--thinking minimal` (pi) reducen pero no acotan el reasoning; si el modelo insiste en desbordarse, cambiar de CLI/modelo es la palanca fiable.
- **Validación (run #499, msg #738/#739)**: nudge humano a novelista-a en hilo 725 → `done` en pi/qwen3.6, leyó cap10, verificó 1.850 palabras + sha256, y por primera vez **`tokens_in=18534, tokens_out=250`** registrados (fix de usage pi confirmado end-to-end). Detalle: #495 seguía `running` durante el reset, así que al terminar dejó su `ses_…` y pi lo adoptó como id de sesión; segundo reset (2 runs) lo limpió.
