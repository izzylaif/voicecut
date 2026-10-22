"""A stand-in for Ollama's HTTP API in tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeOllama:
    """Enough of Ollama's HTTP API for the client, the server and the editor."""

    def __init__(self) -> None:
        self.models: dict[str, dict[str, Any]] = {
            "qwen3:0.6b": {
                "name": "qwen3:0.6b",
                "model": "qwen3:0.6b",
                "size": 522_653_767,
                "details": {"format": "gguf"},
                "capabilities": ["completion", "thinking"],
            }
        }
        self.requests: list[tuple[str, Any]] = []
        self.pull_gate = threading.Event()
        self.pull_gate.set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, value: Any) -> None:
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _stream(self, chunks: Iterator[dict[str, Any]]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(json.dumps(chunk).encode() + b"\n")
                    self.wfile.flush()

            def do_GET(self) -> None:
                if self.path == "/api/version":
                    self._send(200, {"version": "0.40.8"})
                elif self.path == "/api/tags":
                    self._send(200, {"models": list(fake.models.values())})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append((self.path, body))
                if self.path == "/api/chat":
                    self._stream(
                        iter(
                            [
                                {"message": {"thinking": "Compare the takes."}},
                                {"message": {"content": '{"ok":'}},
                                {"message": {"content": "true}"}},
                                {
                                    "done": True,
                                    "done_reason": "stop",
                                    "prompt_eval_count": 12,
                                    "eval_count": 5,
                                },
                            ]
                        )
                    )
                elif self.path == "/api/pull":
                    if body["model"] == "nothing:1b":
                        self._stream(
                            iter(
                                [
                                    {"status": "pulling manifest"},
                                    {
                                        "error": "pull model manifest: file does not exist"
                                    },
                                ]
                            )
                        )
                        return

                    def pulling() -> Iterator[dict[str, Any]]:
                        yield {"status": "pulling manifest"}
                        for completed in (0, 50, 100):
                            fake.pull_gate.wait(5)
                            yield {
                                "status": "pulling abc",
                                "digest": "sha256:abc",
                                "total": 100,
                                "completed": completed,
                            }
                        fake.models[body["model"]] = {
                            "name": body["model"],
                            "model": body["model"],
                            "size": 100,
                            "details": {"format": "gguf"},
                            "capabilities": ["completion"],
                        }
                        yield {"status": "success"}

                    self._stream(pulling())
                elif self.path == "/api/show":
                    if body["model"] in fake.models:
                        self._send(200, {"capabilities": ["completion"]})
                    else:
                        self._send(404, {"error": f"model '{body['model']}' not found"})
                else:
                    self._send(404, {"error": "not found"})

            def do_DELETE(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.models.pop(body["model"], None)
                self._send(200, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
