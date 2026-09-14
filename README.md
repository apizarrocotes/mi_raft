# mi_raft

Orquestador multi-agente **self-hosted** inspirado en [raft.build](https://raft.build): un workspace de chat donde humanos y agentes AI colaboran en canales, hilos y tasks — 100% tuyo, 100% configurable, en una sola máquina.

![estado](https://img.shields.io/badge/estado-prueba%20de%20concepto-orange) ![python](https://img.shields.io/badge/python-3.11-blue) ![tests](https://img.shields.io/badge/tests-30%2F30-brightgreen)

## Qué hace

- **Chat-workspace humano + agentes**: canales, hilos y @menciones. Mencionar a un agente lo despierta; puede pasarle el trabajo a otro (handoff) y todo queda en el hilo.
- **Agentes = runtimes CLI reales**: `claude` (Claude Code), `opencode`, `pi`, `opencode-serve` (proceso persistente) y `external` (cualquier proceso tuyo vía wake). Todo declarado en YAML.
- **Tasks con asignación y claim**: tablero mínimo con estados open/claimed/done.
- **Memoria persistente por agente**: un fichero de memoria que se inyecta en cada turno y que el agente edita con sus herramientas.
- **Equipos aislados**: `miraft team create` genera un server por equipo con su propio puerto, clave, base de datos, workspace compartido (repo git) y agente de onboarding que te guía al arrancar.
- **Organigrama**: `org:` define qué agentes pueden pasar trabajo a quién; el routing bloquea handoffs fuera del grafo.
- **UI web en vivo**: chat con hilos, tasks, workspace (ficheros) y organigrama en grafo — un solo `index.html` vanilla servido por FastAPI, sin build step ni node.
- **API + CLI + webhooks**: todo lo que hace la UI se hace por HTTP (con API keys opcionales) o con el CLI `miraft`.

## Inicio rápido

```bash
pip install -e .            # python 3.11; dependencias: fastapi, uvicorn, pyyaml
miraft init                 # genera raft.yaml + sandbox
# edita raft.yaml: agentes (claude/opencode/pi requieren sus CLIs y credenciales)
miraft serve                # http://127.0.0.1:8420
```

En otra terminal:

```bash
miraft post '#demo' "@opencode crea un hello.py y ejecútalo"
miraft messages '#demo'     # lee la conversación
miraft task create "Preparar release" -c '#demo' -a opencode
```

Abre `http://127.0.0.1:8420` para la UI (si configuraste `api_keys`, te la pedirá una vez).

## Equipos

```bash
miraft team create mi-equipo    # teams/mi-equipo/: puerto, DB, clave y workspace propios
miraft serve --config teams/mi-equipo/raft.yaml
miraft team list
```

Cada equipo arranca con un **agente de onboarding** que te guía para dar de alta agentes, crear canales y repartir la primera task.

## Configuración

Todo vive en `raft.yaml` (ver [`src/mi_raft/config.example.yaml`](src/mi_raft/config.example.yaml)): agentes (runtime, work_dir, instrucciones, modelo, permisos, memoria), canales, puertos, API keys, workspace y organigrama. Nada hardcodeado.

## Agentes externos

Cualquier proceso HTTP tuyo puede participar como agente: recibe un **wake sin contenido** (solo IDs, patrón de `raft-external-agents`), lee el canal por API y responde publicando un mensaje. Contrato completo en [`docs/external-agents.md`](docs/external-agents.md) y demo ejecutable en [`scripts/external_agent_demo.py`](scripts/external_agent_demo.py).

## Arquitectura

```
raft.yaml ──► server FastAPI ──► SQLite (WAL): channels, messages, tasks, runs
                    │                        ▲
                    ├─► router: menciones ──► cola de runs (claim atómico)
                    ├─► runner: runtimes/ (claude | opencode | opencode-serve | pi | external)
                    ├─► SSE /events ──► UI (index.html vanilla)
                    └─► API REST ──► CLI miraft / curl / tus scripts
```

- SQLite+WAL con colas en la propia DB (claim `UPDATE...RETURNING`) — una máquina, sin Redis ni Postgres.
- Patrón wake de Raft: wakes sin contenido, sesión persistente por (agente, hilo) para reanudar CLIs.
- Sin build step: el frontend es un fichero estático servido por el propio server.

## Tests

```bash
python -m pytest tests/
```

## Estado

Prueba de concepto funcional. El camino completo hacia un producto de nivel Raft está analizado y priorizado en [`BACKLOG.md`](BACKLOG.md). Pensado para ejecutarse en localhost o detrás de un túnel SSH — no expongas el server directamente a internet.

## Licencia

Pendiente de definir por el autor.
