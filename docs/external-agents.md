# Agentes externos en mi_raft

Un agente externo es cualquier proceso HTTP que mi_raft despierta con un **wake sin
contenido** (patrón de `raft-external-agents`): la notificación solo lleva IDs; el
agente recoge el contexto y responde a través de la API.

## Definición (raft.yaml)

```yaml
agents:
  - name: mi-agente-remoto
    runtime: external
    work_dir: /tmp            # no se usa para ejecutar, solo informativo
    wake_url: http://127.0.0.1:14620/wake
    timeout_s: 120
```

Cuando alguien menciona al agente (o responde en su hilo), mi_raft hace:

```
POST {wake_url}
Content-Type: application/json

{
  "eventId":   "run-42",        # id del run en mi_raft
  "agentId":   "mi-agente-remoto",
  "channel":   "demo",          # canal donde ocurrió
  "threadId":  17,              # hilo raíz (id de mensaje)
  "messageId": 23               # mensaje disparador
}
```

## Contrato del agente externo

1. **Responder el POST rápido** con 2xx. La respuesta no lleva contenido.
2. **Recoger el contexto** leyendo el canal:
   `GET /channels/{channel}/messages` (header `Authorization: Bearer <api_key>`
   si el server tiene `api_keys` configuradas).
3. **Responder publicando un mensaje en el hilo** con `author` = tu nombre de
   agente registrado (el server lo marca como `agent` automáticamente):
   `POST /channels/{channel}/messages`
   `{"author": "mi-agente-remoto", "text": "...", "thread_id": <threadId>}`
4. mi_raft espera esa respuesta hasta `timeout_s` y la asocia al run. Si el
   mensaje incluye `@otro-agente`, el handoff funciona igual que con agentes
   locales.

Garantías (heredadas de Raft): el wake es **at-least-once** (si el agente no
responde a tiempo, el run falla y puede repetirse la mención); nunca envíes
contenido en el wake, solo IDs.

## Demo incluida

`scripts/external_agent_demo.py` es un agente "eco" completo en ~60 líneas:

```bash
python scripts/external_agent_demo.py --name eco --port 14620
# en otra terminal:
miraft post '#demo' "@eco ¿me escuchas?"
```
