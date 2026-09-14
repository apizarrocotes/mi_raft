"""Agente externo de demostración para mi_raft: un eco que responde en el hilo.

Uso:
    python scripts/external_agent_demo.py --name eco --port 14620 \
        [--server http://127.0.0.1:8420] [--key CLAVE]

Contrato: recibe POST /wake con IDs, lee el canal y responde con author = name.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(name: str, server_url: str, key: str | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _api(self, method, path, payload=None):
            data = json.dumps(payload).encode() if payload is not None else None
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            req = urllib.request.Request(
                f"{server_url}{path}", data=data, method=method, headers=headers
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())

        def do_POST(self):
            if self.path != "/wake":
                self._json({"ok": False}, 404)
                return
            length = int(self.headers.get("Content-Length", 0))
            wake = json.loads(self.rfile.read(length))
            messages = self._api("GET", f"/channels/{wake['channel']}/messages")
            trigger = next(
                (m for m in messages if m["id"] == wake["messageId"]), None
            )
            if trigger is None:
                self._json({"ok": False, "error": "mensaje no encontrado"}, 200)
                return
            reply = f"[eco] {trigger['author_id']} dijo: {trigger['text'][:140]}"
            self._api(
                "POST",
                f"/channels/{wake['channel']}/messages",
                {
                    "author": name,
                    "text": reply,
                    "thread_id": wake["threadId"],
                },
            )
            self._json({"ok": True})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="agente externo eco para mi_raft")
    parser.add_argument("--name", default="eco")
    parser.add_argument("--port", type=int, default=14620)
    parser.add_argument("--server", default="http://127.0.0.1:8420")
    parser.add_argument("--key", default=None)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(
        args.name, args.server.rstrip("/"), args.key
    ))
    print(f"agente externo '{args.name}' escuchando en http://127.0.0.1:{args.port}/wake")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
