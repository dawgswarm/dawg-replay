"""Фейковый Telegram Bot API для тестов: /bot<токен>/<метод>, очередь обновлений, журнал вызовов.

Пример:
    with FakeTelegram() as tg:
        bot = Bot(TOKEN, tg.url)
        tg.push_message("/start code", chat_id=42)
        bot.get_updates(timeout=1)
"""

from __future__ import annotations

import json
import os
import threading
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = "123456789:AAH-fake_token_for_tests_0123456789"
CHAT_ID = 4242


class FakeTelegram:
    def __init__(self, token: str = TOKEN, username: str = "replay_test_bot"):
        self.token = token
        self.username = username
        self.calls: list[tuple[str, dict[str, Any]]] = []  # (метод, параметры) кроме getUpdates
        self.updates: list[dict[str, Any]] = []
        self.fail: dict[str, tuple[int, str]] = {}  # метод -> (error_code, description)
        self.documents: list[dict[str, Any]] = []
        self.on_get_updates = None  # вызывается при каждом getUpdates (для сценариев)
        self.polls = 0
        self._update_id = 100
        self._message_id = 500
        self._cond = threading.Condition()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _params(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                ctype = self.headers.get("Content-Type", "")
                if ctype.startswith("multipart/form-data"):
                    msg = BytesParser(policy=email_policy).parsebytes(
                        f"Content-Type: {ctype}\r\n\r\n".encode() + raw)
                    out: dict[str, Any] = {}
                    for part in msg.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        if part.get_filename():
                            out[name] = {"filename": part.get_filename(), "content": part.get_payload(decode=True)}
                        else:
                            out[name] = part.get_payload(decode=True).decode("utf-8").strip()
                    return out
                return json.loads(raw) if raw else {}

            def do_POST(self) -> None:
                parts = self.path.strip("/").split("/")
                if len(parts) != 2 or not parts[0].startswith("bot"):
                    self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})
                    return
                token, method = parts[0][3:], parts[1]
                params = self._params()
                if token != fake.token:
                    self._send(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                    return
                if method in fake.fail:
                    code, desc = fake.fail[method]
                    self._send(max(code, 400), {"ok": False, "error_code": code, "description": desc})
                    return
                if method == "getUpdates":
                    self._send(200, {"ok": True, "result": fake._get_updates(params)})
                    return
                fake.calls.append((method, params))
                self._send(200, {"ok": True, "result": fake._result(method, params)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # --- ответы ---

    def _next_message_id(self) -> int:
        with self._cond:
            self._message_id += 1
            return self._message_id

    def _result(self, method: str, params: dict[str, Any]) -> Any:
        if method == "getMe":
            return {"id": int(self.token.split(":")[0]), "is_bot": True, "first_name": "Replay Test", "username": self.username}
        if method in ("sendMessage", "sendDocument"):
            if method == "sendDocument":
                self.documents.append(params)
            return {"message_id": self._next_message_id(), "chat": {"id": params.get("chat_id")}, "date": 0}
        return True

    def _get_updates(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        offset = params.get("offset")
        timeout = float(params.get("timeout") or 0)
        with self._cond:
            self.polls += 1
            if offset is not None:
                self.updates = [u for u in self.updates if u["update_id"] >= int(offset)]
        if self.on_get_updates is not None:
            self.on_get_updates(self)
        with self._cond:
            if not self.updates and timeout:
                self._cond.wait(min(timeout, 2.0))
            return list(self.updates)

    # --- сценарии ---

    def push(self, update: dict[str, Any]) -> None:
        with self._cond:
            self._update_id += 1
            self.updates.append({"update_id": self._update_id, **update})
            self._cond.notify_all()

    def push_message(self, text: str, chat_id: int = CHAT_ID, chat_type: str = "private",
                     first_name: str = "Иван", username: str | None = "ivan") -> None:
        sender = {"id": chat_id, "is_bot": False, "first_name": first_name}
        if username:
            sender["username"] = username
        self.push({"message": {"message_id": self._next_message_id(), "date": 0, "text": text,
                               "chat": {"id": chat_id, "type": chat_type}, "from": sender}})

    def sent(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]

    def start(self) -> FakeTelegram:
        # Системный прокси не должен перехватывать запросы к локальному фейку (httpx читает NO_PROXY).
        for var in ("NO_PROXY", "no_proxy"):
            items = [x for x in os.environ.get(var, "").split(",") if x]
            if "127.0.0.1" not in items:
                os.environ[var] = ",".join([*items, "127.0.0.1", "localhost"])
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> FakeTelegram:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
