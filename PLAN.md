# PLAN — mi_raft

## Current State
- **Activo ahora**: post-F5 — sprints 1-3, telemetría, herramientas web, control de proceso (escalación/watchdog) y M1-M6 están hechos y operativos. 77 tests. Equipo ejemplo `romanticas` (sello KDP) en vivo en :8504.
- **Bloqueado**: sandboxing real inactivo en esta máquina (bwrap no instalado y sudo requiere contraseña) — el código está listo, solo `sudo apt install bubblewrap`. CI real pendiente de token gh con scope `workflow`.
- **Siguiente paso**: del backlog — push notifications (etapa 2), runner concurrente ya activo, membresía por canal si el coste del equipo sigue alto, multi-máquina (daemon).
- **PLAN al día**: F0-F5 abajo + F6-F9 con el trabajo posterior. Detalle de decisiones/incidentes en MEMORY.md.

## F0 — Investigación ✅ (2026-09-14)
- [x] Referencias estudiadas (multica, raft-external-agents, omnigent); headless de los 3 CLIs verificado.
- [x] Diseño decidido y registrado en MEMORY.md y CLAUDE.md.

## F1 — Core MVP (en curso)
Criterio de éxito: con `raft.yaml` que define 3 agentes (claude-code, opencode, pi) y canal `#demo`:
1. `raft post '#demo' "@opencode escribe un README de este proyecto"` → opencode corre headless en su workdir y publica su respuesta como mensaje propio en el canal.
2. Si la respuesta de opencode menciona a otro agente (`@claude-code revisa esto`), ese agente se despierta y responde en el mismo thread.
3. Todo visible con `raft messages '#demo'`, sin tocar código para reconfigurar roles/runtimes.

Tareas:
- [x] Scaffold: pyproject + estructura src/mi_raft/
- [x] Schema YAML + validación (config.py)
- [x] SQLite schema: agent, channel, message (author polimórfico, thread, type), run (cola con session_id/work_dir), run_message (actividad)
- [x] Server FastAPI: API REST (post/list messages, agents, channels) + runner background
- [x] BaseRuntime + adaptadores claude/opencode/pi (timeout, kill, resume, parse JSON)
- [x] Router: menciones → cola → coalescencia → ejecución → respuesta al canal → handoffs
- [x] CLI: init/serve/post/messages/agents/channels (renombrado a `miraft` — el nombre `raft` es del CLI oficial de raft.build)
- [x] Demo end-to-end: @opencode creó hello.py y ejecutó; handoff a @claude-code que verificó y cerró (thread 1 de #demo)

## F2 — Tasks y memoria (pendiente)
- Tasks con estados (queued→running→done), claim atómico, asignación polimórfica.
- Memoria persistente por agente (resumen de threads, ficheros de memoria estilo COMP).
- Threads profundos + notificaciones (get pinged).
- `opencode serve` como runtime persistente (proceso vivo, no CLI por mensaje).

## F2 — Tasks y memoria ✅ (2026-09-14)
Criterios validados en `#demo`:
1. Task: `miraft task create ... -a opencode` → auto-claim → agente creó el fichero y respondió en el hilo → `miraft task done 1`.
2. Memoria: opencode escribió `data/memory/opencode.md` (regla pedida + aprendizajes propios); en un hilo NUEVO respondió desde memoria sin tocar ficheros.
3. Runtime persistente: agente `opencode-server` (opencode-serve) — server en :14610 vivo tras los turnos, sesión reanudada en el hilo (recordó "SERVIDOR_VIVO").
4. Continuidad de hilo: respuesta humana sin mención en un hilo despierta al último agente participante.

Tareas:
- [x] Tabla task (open/claimed/done/cancelled) + claim atómico UPDATE...RETURNING
- [x] API REST tasks (create/list/show/claim/done/cancel/comment) + CLI `miraft task`
- [x] Asignación de task = mensaje en canal con @mención (reusa routing/threads/coalescencia)
- [x] `memory_file` por agente: inyectado en prompt, editado por el agente con sus tools
- [x] Runtime `opencode-serve`: servidor persistente por agente (puerto por config o derivado), turno vía POST /prompt + SSE global /event, reintentos, health-check y respawn
- [x] Reset de runs huérfanos al reiniciar el server
- [x] 18 tests unitarios; hallazgos: opencode serve solo ejecuta turnos con suscriptor SSE activo; primer turno puede 401 (auth no cargada) → retry

## F3 — Integración externa ✅ (2026-09-14)
Criterios validados:
1. API keys: `server.api_keys` en raft.yaml → toda la API exige Bearer/X-mi-raft-key (401 sin clave, 200 con ella); el CLI lee la clave de config; sin keys, API abierta (dev).
2. Webhook: `curl -H 'Authorization: Bearer ...' POST /channels/#canal` dispara menciones → agentes. Cero código extra: es el mismo endpoint de mensajes.
3. Agente externo: agente `eco` (runtime external) en otro proceso recibió wake sin contenido (solo IDs), leyó el canal por API y respondió en el hilo como agente. Contrato en docs/external-agents.md; demo en scripts/external_agent_demo.py.
4. Sandboxing básico: sandbox como repo git propio (opencode ya no toma la raíz del repo como proyecto) + instrucción de límites en el prompt de cada turno.

Tareas:
- [x] API keys opcionales (dependency FastAPI en todas las rutas salvo /health)
- [x] CLI con auth transparente desde config
- [x] Runtime `external`: wake POST sin contenido + polling de respuesta en el hilo (timeout por agente)
- [x] Los agentes externos postean con `author: <nombre-registrado>` y el server los marca como `agent`
- [x] docs/external-agents.md + scripts/external_agent_demo.py
- [x] cmd_init: git init del sandbox + README; boundary en build_prompt (excepto external)
- [x] 23 tests. Descartado: records/reminders (cron está en la lista "no copiar de multica")

## F4 — UI web ✅ primera entrega (2026-09-14)
- Un solo `src/mi_raft/static/index.html` (vanilla, sin build step ni node) servido por FastAPI en `/`.
- Chat: canales en sidebar, timeline con hilos anidados, composer con Enter/Shift+Enter, sugerencias de @mención, responder-en-hilo, autores con localStorage.
- Tasks: crear (con asignación a agente), done/cancel, badges de estado; refresco cada 5s.
- Agentes en sidebar con runtime; salud de conexión por SSE.
- Backend: `GET /` + `GET /events` (SSE, `?last_id=` y auth por query param — limitación de EventSource); `db.list_messages_since`.
- Validado: HTML 200, JS checked con node --check, SSE entregando mensajes en vivo (E2E con cliente de lectura acotada).
- Pendiente UI: deep-links, archivo histórico, drag de tasks, notificaciones de escritorio.

## F5 — Equipos, organigrama, workspace ✅ (2026-09-14)
Criterios validados (equipo `beta`):
1. `miraft team create beta` → scaffold aislado (puerto libre real vía bind-check, api_key aleatoria, workspace repo git, agente onboarding + #general/#onboarding). Al primer arranque, el onboarding responde en #general con la guía.
2. Alta dinámica: `POST /channels` y `POST /agents` crean canal #dev y agente `dev` (work_dir por defecto = workspace del equipo); el agente creado ejecutó su primera task y dejó el entregable en el workspace.
3. Organigrama: `org:` en YAML (onboarding→[dev], dev→[]); onboarding→dev permitido, dev→onboarding bloqueado con system message en el hilo. Grafo SVG en la pestaña Equipo.
4. Workspace compartido: pestaña Workspace con árbol + vista de fichero (endpoints /workspace/tree y /workspace/file con confinamiento de rutas).

Tareas:
- [x] `miraft team create/list` (puertos con test de bind real; 8501 estaba ocupado por Streamlit)
- [x] Bienvenida de primer arranque (mensaje @onboarding si DB vacía y existe #general)
- [x] API dinámica: POST /channels, POST /agents (valida runtime/único), PATCH /agents/{id}, GET /org, GET /meta
- [x] Org edges persistidos en DB (tabla org_edge); enforcement en route_message (solo agent→agent; humanos sin restricción)
- [x] UI: pestañas Workspace y Equipo, botón + canal, alta de agentes, grafo SVG
- [x] 30 tests
- [x] Fixes: --config en subcomandos (argparse.SUPPRESS), prog=miraft, asignador de puertos con bind real

## Sprints 1-3 del BACKLOG ✅ (noche 2026-09-14/15)
- **S1**: costes/tokens capturados de los 3 runtimes (columnas en run + GET /usage + panel Equipo); `budget_usd` por agente (claude `--max-budget-usd`); estados reales de agente (idle/working/error); markdown con marked+DOMPurify vendored (sin build step); editar agentes en UI; CI como `docs/ci.yml.example` (falta token gh con scope `workflow`).
- **S2**: búsqueda FTS5 (unicode61 sin diacríticos, trigger + backfill); /search + `miraft search` + overlay ⌘K; Activity (type=status); no-leídos + toasts + Notification API con mute; DMs (`channel.type=dm`, id `dm-<agente>`).
- **S3**: tasks v2 (rebuild de tabla por CHECK: detectar sql viejo → task_v2; subtasks con `parent_task_id`; desglose por agente); runner concurrente (4-6 inflight); sandbox bwrap (perfil conservador, listo pero inactivo sin bwrap).
- 77 tests. Lección reforzada: modelos Pydantic siempre a nivel de módulo (mordida nº 3).

## F6 — Telemetría de ejecución + herramientas web ✅ (2026-09-15)
- **run_message**: streaming de stdout por línea (pump con `asyncio.timeout`, stdin/stderr concurrentes); claude `--output-format stream-json --verbose`; parseo defensivo en opencode/pi/serve; sink con seq por run. Vista Actividad (runs por agente + timeline de operaciones); `miraft runs` / `miraft run <id>`.
- **Herramientas web propias**: `/tools/search` (DDG lite + fallback + caché) y `/tools/fetch` (anti-SSRF, texto plano, cap 100KB), abiertos sin auth para curl de agentes e inyectados en el prompt de todos los runtimes locales. Resuelve la desigualdad de tools nativas entre CLIs.
- Fix crítico: límite de 64KB por línea en StreamReader → `limit=64MB` (los JSON gigantes rompían TODOS los agentes).

## F7 — Control de proceso ✅ (2026-09-15)
- **Escalación a supervisor** (`escalate_to`): fallos de run, handoffs bloqueados y runs atascados publican mensaje al supervisor y se enrutan como runs. Guardias: sin auto-escalación, máx 3 por hilo.
- **Watchdog** (cada 60s) termina runs colgados (timeout + gracia) y escala.
- **Bypass de supervisor**: `escalate_to` nunca se le bloquea un handoff (vía de emergencia); la delegación normal sí respeta el org.
- **kill_tree** recursivo vía `/proc` (los nietos con setsid sobreviven a killpg).

## F8 — Org refinado + M1-M6 ✅ (2026-09-15)
- **M1**: grafo de comunicación inyectado en el prompt (delega/reporta/fuera de líneas/no-acuses).
- **M3**: continuidad de hilo solo en DMs — en canales multi-agente, solo las menciones despiertan.
- **M4**: `task_deps` + claim que salta hilos con tasks bloqueadas. **M5**: `phase` en tasks.
- **M6**: editor-fino/beta-lectora/kdp-manager → pi + nan/deepseek-v4-flash (384K salida, 1M contexto; elimina `reason=length`).
- **Catálogo de proveedores/modelos**: `providers.provider` por agente + `GET /models` (caché 300s, ejecuta los CLIs) + validación en PATCH.
- Auditoría previa: 34/49 eventos eran handoffs bloqueados; ack-storms = ~80% del coste → M1-M6 atacan comunicación, precedencia y stale snapshots.

## F9 — Equipo ejemplo `romanticas` ✅ (2026-09-15)
- `teams/romanticas/` (:8504): 7 agentes, 6 canales por lane, 14 aristas con la jefa-editorial como hub. Pipeline: mercado → biblia → outline → borradores → edición → beta → paquete KDP.
- **Lección org confirmada**: todo lane necesita arista de retorno al hub (los reportes de estado fluyen por canales; las aristas son para handoffs de trabajo).

## F10 — Futuro (explícitamente aplazado)
- Daemon multi-máquina, push notifications reales (service worker + VAPID), membresía por canal (si el coste sigue alto).
- Sandboxing fuerte real (bwrap/landlock) — código listo, falta instalar bwrap.
- UI: kanban drag&drop, archivo histórico, PWA móvil.
