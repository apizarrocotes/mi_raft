# BACKLOG — camino al nivel de Raft y similares

Análisis de brechas (2026-09-14) entre mi_raft y las plataformas de referencia:
[Raft](https://docs.raft.build) (docs de divide-the-work, search, notifications, pricing) y
[multica](https://github.com/botiverse/multica) (repositorio abierto, arquitectura estudiada).

Prioridades: **P0** = núcleo del modelo Raft que aún no tenemos · **P1** = operatividad seria · **P2** = pulido/escala.
Esfuerzo: **S** < 1 sesión · **M** 1-2 sesiones · **L** > 2 sesiones.

---

## P0 — Núcleo del modelo de equipo

### 1. Búsqueda global (⌘K) — `M`
Raft: búsqueda de mensajes/hilos/tasks con salto al mensaje en contexto, filtros por canal/autor/fecha, y "tus agentes también buscan".
- SQLite **FTS5** sobre `message` (+ task title/description) con trigger de indexado.
- `GET /search?q=&channel=&author=`; overlay en UI con resultados → click abre el hilo en contexto.
- Los agentes la usan vía endpoint (patrón documentado en su prompt: "para recuperar contexto, consulta /search").
- Hoy: **no existe forma de recuperar nada** salvo scroll.

### 2. Notificaciones + Activity — `M` (etapa 1) / `L` (push real)
Raft: baseline silencioso; te pings DMs, canales a los que te unes, hilos que sigues y menciones; **Activity** para el ruido; mute global.
- Etapa 1 (M): membresía de canal (`channel_member`), "siguiendo" hilos, badge de no-leídos en sidebar, notificación in-app + `Notification` API del navegador (pestaña abierta) disparada por el SSE existente.
- Etapa 2 (L): Push real con service worker + VAPID (funciona con pestaña cerrada).
- Pestaña Activity: progreso de tasks y "chatter" de agentes fuera del chat.

### 3. Tasks v2 (tablero por canal) — `L`
Raft: mensaje→task (Convert to Task / "As Task" en composer), tablero por canal, un owner a la vez, estados **todo → in progress → in review → done**, subtasks paralelos y fases para dependencias, agente propone el desglose y humano lo aprueba.
- Nos falta casi todo salvo claim básico y estados open/claimed/done.
- Añadir: `parent_task_id` (subtasks), estado `in_review`, conversión mensaje→task desde UI, vista tablero (kanban simple) junto al chat, endpoint de desglose por agente (prompt→JSON de subtasks → alta masiva).

### 4. Markdown y código en mensajes — `M`
Los agentes responden en markdown; la UI lo pinta plano (se pierden tablas, listas, código).
- Vendored `marked.min.js` + `highlight.min.js` en `static/` (sigue sin build step, ficheros locales).
- Render con sanitización (`DOMPurify` vendored o escape previo + renderer propio básico).

### 5. Mensajes directos (DMs) — `M`
Raft tiene DMs humano↔agente (siempre notifican). Modelo propuesto: canal `type=dm` con 2 miembros (humano+agente), listado en sidebar, misma maquinaria de runs.

### 6. Observabilidad de costes y tokens — `S` ⚡ quick win
Los runtimes **ya devuelven** coste/tokens (claude: `total_cost_usd`/`usage`; opencode: `step_finish.tokens`; pi: `cost` en message_end) y los **descartamos**.
- Columnas `cost_usd`, `tokens_in`, `tokens_out` en `run`; capturarlas en cada runtime.
- `GET /usage?by=agent|day`; mini-panel en pestaña Equipo; presupuesto opcional por agente en YAML (`budget_usd` → `--max-budget-usd` en claude).

### 7. Telemetría de ejecución (run messages) — `L`
multica registra cada paso del agente (`task_message`: tool, input/output secuenciados); Raft lo llama observabilidad básica.
- Tabla `run_message(run_id, seq, type, tool, payload_json)` alimentada por los formatos streaming (`claude --output-format stream-json`, eventos de opencode/pi).
- Vista "qué está haciendo ahora" por run + reproducción de la ejecución.

---

## P1 — Operatividad seria

### 8. Estados reales de agente — `S`
La columna `agent.status` existe y no se usa. Actualizar en claim (working), fallo (error), fin (idle); punto de estado vivo en la sidebar.

### 9. Runner concurrente real — `M`
El loop es secuencial global: `max_concurrent` solo se respeta contra la DB, nunca hay 2 runs a la vez. Un `asyncio.gather` con semaphore por agente lo activa de verdad.

### 10. Sandboxing fuerte por agente — `M-L`
`--auto`/`dontAsk` pueden escribir fuera del work_dir. Ejecutar cada runtime dentro de `bwrap`/`landlock` con permisos por agente en YAML (`sandbox: {readonly: [...], writable: [workspace]}`).

### 11. Outgoing webhooks — `M`
Suscripciones a eventos (`message.created`, `task.done`, `run.failed` → POST a URL con firma HMAC). Hoy solo tenemos webhooks entrantes.

### 12. Adjuntos en mensajes — `M`
Upload por API y composer (multipart), guardado en `<workspace>/uploads/`, vista de imagen/pdf en UI. Raft da cuotas de upload; nosotros: límite configurable.

### 13. Edición de agentes desde la UI — `S`
El backend ya soporta `PATCH /agents/{id}` (instructions/model/permissions/memory). Falta el formulario en pestaña Equipo.

### 14. Retención y backup — `S-M`
Backup programado de SQLite (`.backup`), retención configurable de mensajes, comando `miraft backup`.

### 15. Onboarding más rico — `M`
Detectar CLIs instalados y credenciales al crear equipo, proponer primer setup, tour guiado en la UI.

### 16. Skills y MCP por agente — `M`
Superficiar en YAML lo que los CLIs ya soportan: `skills: [...]`, `mcp_servers: {...}` (claude/opencode/pi los cargan de su config; pasarlas como flags/ENV del run).

### 17. UX de timeline — `M`
Deep-links (`#canal/123`), paginación/scroll infinito (hoy límite 200), no-leídos por canal, pin de mensajes.

### 18. Multi-máquina (daemon) — `L`
Raft ejecuta agentes en tus ordenadores vía daemon. Nuestro protocolo ya es HTTP: un `miraftd` que claim-ea runs de un server y ejecute runtimes remotos, con cola en el server central. Requiere mTLS o token por daemon.

### 19. Despliegue: Docker + systemd + CI — `M`
GitHub Actions (pytest en cada push), imagen Docker/compose (una línea para auto-hospedar), units de systemd para `miraft serve` y equipos.

---

## P2 — Pulido y escala

- **Catch-up / digest**: resumen diario de actividad generado por un agente (Raft "Catch up in one place").
- **Kanban drag&drop** con fases visibles; **reacciones** emoji; **pins**; mensajes editables/borrables.
- **PWA móvil** (Raft on every device): manifest + service worker; la UI ya es responsive-ish.
- **Memoria de equipo** (knowledge base compartida además de la memoria individual): fichero por canal/proyecto que los agentes consultan.
- **Integración GitHub**: crear task desde issue, abrir PR desde task, publicar artifacts (como `raft-artifact-share-action`).
- **Temas claro/oscuro**, i18n EN/ES.
- **Audit log** y métricas Prometheus (`/metrics`).
- **Presupuestos duros** por agente/equipo con corte automático.

---

## Excluido por diseño (revisar solo si el uso real lo exige)

Estas decisiones están en CLAUDE.md; cambiarlas requiere discusión:

| Excluido | Motivo | Señal para revisitar |
|---|---|---|
| Multi-usuario con auth/roles | Monousuario + api_keys es suficiente en un host propio | Si se invita a personas reales |
| Multi-workspace en una DB | Equipos = servers aislados; más simple y seguro | Si la gestión de N servers duele |
| Squads, autopilots/cron | En la lista "no copiar de multica" | Si aparece un caso recurrente sin cron |
| Raft Apps / marketplace / Login-with | Plataforma de producto, no orquestación | Nunca, salvo giro de producto |

---

## Roadmap sugerido (por sprints)

1. **Sprint 1 (quick wins)**: 6 observabilidad de costes · 8 estados de agente · 4 markdown · 13 editar agentes · 19 CI
2. **Sprint 2 (núcleo social)**: 1 búsqueda · 2 notificaciones etapa 1 · 5 DMs
3. **Sprint 3 (trabajo serio)**: 3 tasks v2 · 9 runner concurrente · 10 sandboxing · 11 outgoing webhooks
4. **Después**: 7 telemetría · 18 multi-máquina · 19 docker/systemd · resto de P2

---

*Fuentes: docs.raft.build (welcome, divide-the-work, search-your-raft, get-pinged-when-it-matters, pricing), análisis del repo multica (migraciones SQL, daemon, protocolo de wake) y raft-external-agents, realizados el 2026-09-14.*
