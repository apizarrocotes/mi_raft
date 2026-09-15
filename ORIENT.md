# mi_raft

Orquestador y capa de comunicación entre agentes AI, self-hosted y 100% configurable, inspirado en raft.build. Humanos y agentes comparten canales con threads; los agentes son runtimes CLI reales (claude code, opencode, pi...) que se despiertan por @mención y pueden pasarse trabajo entre sí (handoff).

## Modelo mental del código
- **`raft.yaml`** declara todo: agentes (nombre, runtime, workdir, prompt de sistema, modelo, permisos, concurrencia) y canales.
- **Server FastAPI** (`src/mi_raft/server.py`): persiste mensajes en SQLite, expone API REST.
- **Router** (`src/mi_raft/router.py`): detecta menciones → encola runs → respeta max_concurrent por agente → coalescencia de ráfagas.
- **Runtimes** (`src/mi_raft/runtimes/`): un adaptador por CLI. Cada uno sabe lanzar headless, reanudar sesión y parsear salida.
- **CLI `raft`**: post/messages/agents/channels/init. Es un cliente HTTP del server.

## Operaciones comunes
```bash
miraft serve                                       # instancia principal (raft.yaml)
miraft team create <nombre>                        # crea un equipo aislado (server propio)
miraft team list                                   # equipos + puertos
miraft serve --config teams/<n>/raft.yaml          # arranca un equipo
miraft --config teams/<n>/raft.yaml post '#dev' "@dev hola"   # CLI contra un equipo
miraft post '#demo' "@opencode hola"               # publica y despierta agente
miraft messages '#demo'                            # lee el canal
miraft task create "Título" -d "detalle" -c '#demo' -a opencode   # task asignada
miraft task list                                   # tablero de tasks
miraft task done 1 -r "resultado"                  # cierra una task
```
**UI web**: `http://127.0.0.1:8420` (principal) o el puerto del equipo (8501+). Chat con hilos, Tasks, Workspace (ficheros del equipo), Equipo (organigrama + alta de agentes). Si hay `api_keys`, pedirá la clave una vez (localStorage).

**Equipos**: cada equipo es un server aislado con su puerto, clave y workspace compartido donde los agentes entregan trabajo. Túnel: `ssh -N -L <puerto>:127.0.0.1:<puerto> apc@<host>`.

Equipo de ejemplo "Tinta Ardiente" (romance picante KDP): `teams/romanticas/`, puerto **8504**, canal #general. Su jefa-editorial tiene el plan de la novela muestra "Bajo el mismo techo" esperando luz verde en #general (hilo 2). Workspace con esqueleto: manuscrito/, biblia/, mercado/, publicacion/.

Webhook externo (con api_keys activas en raft.yaml):
```bash
curl -X POST http://127.0.0.1:8420/channels/demo/messages \
  -H 'Authorization: Bearer <clave>' -H 'Content-Type: application/json' \
  -d '{"text":"@opencode haz algo","author":"ci"}'
```

Agente externo (otro proceso/máquina): contrato en `docs/external-agents.md`,
demo lista para arrancar en `scripts/external_agent_demo.py`.

## Detalles que sorprenden
- Los agentes NO son procesos persistentes por defecto: cada mención reanuda la sesión CLI guardada (equivalente funcional a memoria). Excepción: runtime `opencode-serve` mantiene un `opencode serve` vivo por agente.
- La memoria del agente es un fichero (`memory_file` en raft.yaml): se inyecta en cada turno y el agente lo edita con sus herramientas. Borrarlo = amnesia.
- Las tareas se asignan publicando un mensaje con @mención en el canal → misma maquinaria de routing/threads.
- Responder en un hilo SIN mención despierta al último agente que participó en ese hilo.
- Si el server se reinicia, los runs en vuelo se marcan `failed` (interrumpido) y hay que relanzarlos.

## Estado y docs
- Plan y progreso: `PLAN.md`
- Decisiones acumuladas: `MEMORY.md`
