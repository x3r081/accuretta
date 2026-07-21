"""Controlled fakes for macOS acceptance / lifecycle tests.

No network, no Homebrew installs, no real GGUF models. All artifacts stay
under caller-provided temporary directories.

ACCURETTA_FAKE_LLAMA_MODE values:
  ready          — serve /v1/models immediately
  delayed_ready  — sleep ACCURETTA_FAKE_LLAMA_DELAY (default 1.5s) then ready
  hang           — never bind / never answer
  exit_soon      — exit before listening
  flood_stdout   — print many lines then serve
  flood_stderr   — same (merged into stdout by bridge spawn)
  exit_during_gen — ready; on chat send one chunk then exit
  endless_stream — ready; chat SSE never ends (slow chunks)
"""

from __future__ import annotations

import json
import os
import socket
import stat
import textwrap
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable


FAKE_LLAMA_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    \"\"\"Fake llama-server for Accuretta lifecycle / acceptance tests.\"\"\"
    import json
    import os
    import sys
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer

    def parse_port(argv):
        port = 8080
        i = 0
        while i < len(argv):
            if argv[i] == "--port" and i + 1 < len(argv):
                try:
                    port = int(argv[i + 1])
                except ValueError:
                    pass
                i += 2
                continue
            i += 1
        return port

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/v1/models"):
                body = json.dumps({"data": [{"id": "fake-model"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            mode = os.environ.get("ACCURETTA_FAKE_LLAMA_MODE", "ready")
            if self.path.startswith("/v1/chat/completions"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if mode == "exit_during_gen":
                    self.wfile.write(
                        b'data: {"choices":[{"delta":{"content":"hi"}}]}\\n\\n'
                    )
                    self.wfile.flush()
                    os._exit(99)
                if mode == "endless_stream":
                    n = 0
                    while True:
                        n += 1
                        chunk = (
                            'data: {"choices":[{"delta":{"content":"x%d"}}]}\\n\\n'
                            % n
                        ).encode("utf-8")
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        time.sleep(0.05)
                # normal short completion
                self.wfile.write(
                    b'data: {"choices":[{"delta":{"content":"ok"}}]}\\n\\n'
                )
                self.wfile.write(b"data: [DONE]\\n\\n")
                return
            self.send_response(404)
            self.end_headers()

    def main():
        mode = os.environ.get("ACCURETTA_FAKE_LLAMA_MODE", "ready")
        if mode == "hang":
            time.sleep(86400)
            return
        if mode == "exit_soon":
            sys.exit(42)
        if mode in ("flood_stdout", "flood_stderr"):
            for i in range(5000):
                print(f"flood-line-{i}-" + ("Z" * 200), flush=True)
        if mode == "delayed_ready":
            try:
                delay = float(os.environ.get("ACCURETTA_FAKE_LLAMA_DELAY") or "1.5")
            except Exception:
                delay = 1.5
            time.sleep(max(0.0, delay))
        port = parse_port(sys.argv[1:])
        HTTPServer(("127.0.0.1", port), Handler).serve_forever()

    if __name__ == "__main__":
        main()
    """
)


def write_executable(path: Path, content: str) -> str:
    """Write an executable script under path; return absolute path string."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path.resolve())


def write_fake_llama(root: Path, name: str = "llama-server",
                     script: str | None = None) -> str:
    return write_executable(root / name, script or FAKE_LLAMA_SCRIPT)


def write_stub_model(root: Path, name: str = "stub.gguf") -> str:
    """Tiny stand-in so safe_exists / start() see a model file (not a real GGUF)."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF\x00fake-acceptance-stub\n")
    return str(path.resolve())


def free_localhost_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def occupy_localhost_port() -> tuple[socket.socket, int]:
    """Bind and keep a port occupied. Caller must close the socket."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, int(s.getsockname()[1])


class LocalModelsServer:
    """Tiny localhost HTTP server that answers /v1/models (for port-busy tests)."""

    def __init__(self, port: int | None = None):
        self.port = port or free_localhost_port()
        self._httpd: HTTPServer | None = None
        self._thread = None

    def start(self) -> int:
        import threading

        port = self.port

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/v1/models"):
                    body = b'{"data":[{"id":"occupant"}]}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

        self._httpd = HTTPServer(("127.0.0.1", port), H)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def sse_bytes(*payloads: object) -> list[bytes]:
    """Build SSE frame bytes for mocked llama_post_stream iterators."""
    out: list[bytes] = []
    for p in payloads:
        if p is None:
            out.append(b"\n")
        elif isinstance(p, bytes):
            out.append(p)
        elif p == "[DONE]":
            out.append(b"data: [DONE]\n\n")
        else:
            out.append(("data: " + json.dumps(p) + "\n\n").encode("utf-8"))
    return out


def make_stream_iter(chunks: list[bytes], *, die_after: int | None = None,
                     exc_factory: Callable[[], BaseException] | None = None):
    """Return a callable suitable for mock.patch(..., side_effect=...)."""

    def _open(*_a, **_k):
        class _Resp:
            def __iter__(self_inner):
                for i, c in enumerate(chunks):
                    if die_after is not None and i >= die_after:
                        raise (exc_factory or ConnectionResetError)("peer closed")
                    yield c

            def close(self_inner):
                pass

        return _Resp()

    return _open


def apple_sysctl(total_gb: int, chip: str = "Apple M4 Pro") -> dict[str, str]:
    return {
        "machdep.cpu.brand_string": chip,
        "hw.memsize": str(int(total_gb) * 1024 ** 3),
        "hw.optional.arm64": "1",
    }


MTL_LIST = """Available devices:
  BLAS: Accelerate (0 MiB, 0 MiB free)
  MTL0: Apple M4 Pro (18186 MiB, 18185 MiB free)
"""
