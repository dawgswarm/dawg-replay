"""Фейковый API браузеров DAWG для тестов: POST/GET/DELETE /api/v1/browsers и готовность браузера."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "test-key-0123456789"


class FakeDawg:
    def __init__(self, api_key: str = KEY):
        self.api_key = api_key
        self.sessions: dict[str, dict] = {}
        self.ready_delay = 0.0  # сколько секунд браузер «стартует» после POST
        self.post_delay = 0.0  # задержка ответа на POST (браузер создаётся до задержки)
        self.limit_429 = 0  # сколько POST подряд получат 429
        self.retry_after = 1
        self.post_status: int | None = None  # принудительный код ответа на POST
        self.delete_fails = False
        self.requests: list[tuple[str, str, object]] = []
        self.created_at: dict[str, float] = {}
        self._n = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                data = json.dumps(body if body is not None else {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                return json.loads(raw) if raw else None

            def _auth(self):
                if self.headers.get("X-API-Key") != fake.api_key:
                    self._send(401, {"error": "bad key"})
                    return False
                return True

            def do_POST(self):
                body = self._body()
                fake.requests.append(("POST", self.path, body))
                if not self._auth():
                    return
                if self.path != "/api/v1/browsers":
                    return self._send(404)
                if fake.post_status:
                    return self._send(fake.post_status, {"error": "forced"})
                with fake._lock:
                    if fake.limit_429 > 0:
                        fake.limit_429 -= 1
                        return self._send(429, {"detail": {"retry_after_seconds": fake.retry_after}})
                    fake._n += 1
                    sid, bid = str(uuid.uuid4()), f"browser-{fake._n}"
                    fake.sessions[sid] = {
                        "id": sid,
                        "browserInstanceId": bid,
                        "status": "ACTIVE",
                        "startedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    }
                    fake.created_at[bid] = time.monotonic()
                if fake.post_delay:
                    time.sleep(fake.post_delay)
                self._send(201, {"success": True, "data": {"sessionId": sid, "browserId": bid, "ephemeralToken": "eph-tok"}})

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                if not self._auth():
                    return
                if self.path == "/api/v1/browsers":
                    return self._send(200, {"success": True, "data": list(fake.sessions.values())})
                parts = self.path.strip("/").split("/")
                if len(parts) == 5 and parts[:2] == ["dawg", "baas"] and parts[3:] == ["json", "version"]:
                    bid = parts[2]
                    t = fake.created_at.get(bid)
                    if t is None or time.monotonic() - t < fake.ready_delay:
                        return self._send(502, {"error": "starting"})
                    return self._send(200, {"webSocketDebuggerUrl": f"ws://127.0.0.1/devtools/browser/{bid}"})
                self._send(404)

            def do_DELETE(self):
                fake.requests.append(("DELETE", self.path, None))
                if not self._auth():
                    return
                sid = self.path.rsplit("/", 1)[-1]
                if fake.delete_fails:
                    return self._send(500, {"error": "boom"})
                if fake.sessions.pop(sid, None) is None:
                    return self._send(404, {"success": False, "error": "Browser session not found"})
                self._send(200, {"success": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def start(self) -> FakeDawg:
        # Системный прокси не должен перехватывать запросы к локальному фейку (httpx и requests читают NO_PROXY).
        for var in ("NO_PROXY", "no_proxy"):
            items = [x for x in os.environ.get(var, "").split(",") if x]
            if "127.0.0.1" not in items:
                os.environ[var] = ",".join([*items, "127.0.0.1", "localhost"])
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add_foreign_session(self, started_at: str) -> str:
        """Сессия этого ключа, созданная не нашим запуском (dawg-mcp и т.п.)."""
        sid = str(uuid.uuid4())
        self.sessions[sid] = {"id": sid, "browserInstanceId": "foreign", "status": "ACTIVE", "startedAt": started_at}
        return sid

    def count(self, method: str, prefix: str = "") -> int:
        return sum(1 for m, p, _ in self.requests if m == method and p.startswith(prefix))
