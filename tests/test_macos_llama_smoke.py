"""Opt-in macOS + Apple Silicon llama.cpp integration smoke test.

Skipped by default. Enable only when all gates pass:

  - platform is macOS (darwin)
  - architecture is arm64
  - llama-server is discoverable
  - ACCURETTA_TEST_GGUF=/absolute/path/model.gguf (existing file)

Run (Mac contributor):

  export ACCURETTA_TEST_GGUF=/absolute/path/to/model.gguf
  # optional: export ACCURETTA_LLAMA_BIN=/opt/homebrew/bin/llama-server
  # optional: export ACCURETTA_TEST_LLAMA_TIMEOUT=180
  python3 -m unittest tests.test_macos_llama_smoke -v

Default CI / discover without the env var: this module skips (no model load).
"""

from __future__ import annotations

import builtins
import json
import os
import platform
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _TESTS_DIR)

import bridge
from macos_fakes import free_localhost_port


def _gguf_env() -> str:
    return (os.environ.get("ACCURETTA_TEST_GGUF") or "").strip()


def _smoke_skip_reason() -> str | None:
    if sys.platform != "darwin":
        return "opt-in llama smoke requires macOS"
    if platform.machine().lower() not in ("arm64", "aarch64"):
        return "opt-in llama smoke requires arm64"
    gguf = _gguf_env()
    if not gguf:
        return (
            "opt-in llama smoke disabled — set ACCURETTA_TEST_GGUF to an "
            "absolute path of a .gguf model to enable"
        )
    if not os.path.isabs(gguf):
        return "ACCURETTA_TEST_GGUF must be an absolute path"
    if not Path(gguf).is_file():
        return "ACCURETTA_TEST_GGUF does not point to an existing file"
    # Resolve binary without probing --version (cheap).
    bin_path = bridge.find_llama_bin()
    if not bin_path:
        return (
            "llama-server not found — install via Homebrew or set "
            "ACCURETTA_LLAMA_BIN"
        )
    return None


_SKIP = _smoke_skip_reason()


def _redact(text: str, secret: str) -> str:
    if not text or not secret:
        return text
    out = text.replace(secret, "<GGUF>")
    try:
        parent = str(Path(secret).parent)
        if parent and parent not in (".", "/"):
            out = out.replace(parent, "<GGUF_DIR>")
    except Exception:
        pass
    return out


def _pid_gone(pid: int | None) -> bool:
    if not pid:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _rss_mb(pid: int | None) -> float | None:
    if not pid:
        return None
    try:
        import subprocess
        r = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0:
            return None
        kb = int((r.stdout or "").strip().split()[0])
        return round(kb / 1024.0, 1)
    except Exception:
        return None


def _wait_until(pred, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


@unittest.skipIf(_SKIP is not None, _SKIP or "disabled")
class MacOSLlamaCppSmokeTest(unittest.TestCase):
    """Real Metal/CPU llama-server + GGUF smoke (opt-in only)."""

    def setUp(self):
        self.model_path = _gguf_env()
        self.assertTrue(os.path.isabs(self.model_path))
        self.assertTrue(Path(self.model_path).is_file())

        self.llama_bin = bridge.find_llama_bin()
        self.assertTrue(self.llama_bin)

        self.timeout_s = float(os.environ.get("ACCURETTA_TEST_LLAMA_TIMEOUT") or 180)
        self.port = free_localhost_port()
        self._orig_llama = bridge.LLAMA
        bridge.LLAMA = f"http://127.0.0.1:{self.port}"
        self.addCleanup(self._restore_llama)

        self._tmpdir = tempfile.TemporaryDirectory(prefix="accuretta-llama-smoke-")
        self.addCleanup(self._tmpdir.cleanup)
        data = Path(self._tmpdir.name) / "data"
        data.mkdir()
        settings_path = data / "settings.json"

        probed = bridge.probe_llama_devices(self.llama_bin, timeout=3.0)
        self.metal_build = bool(probed.get("metal_llama_build"))
        self.devices = probed.get("devices") or []
        self.ngl = 99 if self.metal_build else 0

        settings_path.write_text(json.dumps({
            "spec_strategy": "off",
            "enable_speculative": False,
            "flash_attn": True,
            "num_gpu": self.ngl,
            "num_ctx": 4096,
            "num_batch": 512,
            "n_ubatch": 256,
            "n_parallel": 1,
            "watchdog_enabled": False,
            "model": "local",
            "enable_thinking": False,
            "llama_bin": self.llama_bin,
        }), encoding="utf-8")
        self._orig_settings = bridge.SETTINGS_FILE
        bridge.SETTINGS_FILE = settings_path
        self.addCleanup(lambda: setattr(bridge, "SETTINGS_FILE", self._orig_settings))

        self.proc = bridge.LlamaProcess()
        self.addCleanup(self._cleanup_proc)
        self.peak_rss_mb: float | None = None
        self.child_pid: int | None = None

    def _restore_llama(self):
        bridge.LLAMA = self._orig_llama

    def _cleanup_proc(self):
        try:
            self.proc.stop_permanent(timeout=5.0)
            self.proc.shutdown_watchdog()
        except Exception:
            pass
        bridge._METAL_RUNTIME_SELECTED = False
        bridge._HW_SPECS_CACHE = None

    def _redacting_print(self, *args, **kwargs):
        sep = kwargs.get("sep", " ")
        text = sep.join(str(a) for a in args)
        text = _redact(text, self.model_path)
        kwargs = dict(kwargs)
        file = kwargs.pop("file", sys.stdout)
        end = kwargs.pop("end", "\n")
        flush = kwargs.pop("flush", False)
        # Drop other print kwargs we don't forward.
        file.write(text + end)
        if flush:
            try:
                file.flush()
            except Exception:
                pass

    def _sample_rss(self):
        mb = _rss_mb(self.child_pid)
        if mb is None:
            return
        if self.peak_rss_mb is None or mb > self.peak_rss_mb:
            self.peak_rss_mb = mb

    def test_llama_server_chat_smoke(self):
        version = bridge.probe_llama_version(self.llama_bin, timeout=2.0)
        arch = platform.machine()

        t_start = time.perf_counter()
        with mock.patch.object(builtins, "print", side_effect=self._redacting_print):
            res = self.proc.start(
                self.model_path,
                wait=False,
                wait_seconds=int(self.timeout_s),
                port_override=self.port,
                ctx_override=4096,
                ngl_override=self.ngl,
            )
        self.assertTrue(res.get("ok"), msg=_redact(str(res), self.model_path))
        self.child_pid = res.get("pid")
        self.assertIsNotNone(self.child_pid)

        def _ready() -> bool:
            self._sample_rss()
            return bridge.llama_ping(
                timeout=0.5, base_url=f"http://127.0.0.1:{self.port}"
            )

        ready = _wait_until(_ready, timeout=self.timeout_s, interval=0.25)
        startup_s = round(time.perf_counter() - t_start, 3)
        self.assertTrue(
            ready,
            f"llama-server not ready within {self.timeout_s:.0f}s "
            f"(startup waited {startup_s}s)",
        )
        self._sample_rss()

        # Direct completion (timings / non-empty content).
        t_chat = time.perf_counter()
        completion = bridge.llama_post(
            "/v1/chat/completions",
            {
                "model": "local",
                "messages": [
                    {"role": "user", "content": "Reply with exactly: pong"},
                ],
                "stream": False,
                "temperature": 0.0,
                "max_tokens": 32,
            },
            base=f"http://127.0.0.1:{self.port}",
            timeout=min(120.0, self.timeout_s),
        )
        chat_wall_s = round(time.perf_counter() - t_chat, 3)
        self._sample_rss()

        choices = completion.get("choices") or []
        self.assertTrue(choices, msg="empty choices from llama-server")
        content = ((choices[0].get("message") or {}).get("content") or "").strip()
        self.assertTrue(content, msg="empty completion content")

        timings = completion.get("timings") or {}
        usage = completion.get("usage") or {}
        prompt_ms = timings.get("prompt_ms")
        predicted_ms = timings.get("predicted_ms")
        predicted_n = timings.get("predicted_n") or usage.get("completion_tokens")
        prompt_n = timings.get("prompt_n") or usage.get("prompt_tokens")
        tok_per_s = timings.get("predicted_per_second")
        if tok_per_s is None and predicted_ms and predicted_n:
            try:
                tok_per_s = float(predicted_n) / (float(predicted_ms) / 1000.0)
            except Exception:
                tok_per_s = None

        # Bridge consume path (SSE via run_chat_turn).
        events: list[dict] = []

        def emit(ev: dict):
            events.append(ev)

        with mock.patch.object(bridge, "_llama_props_ctx", return_value=4096), \
             mock.patch.object(builtins, "print", side_effect=self._redacting_print):
            turn = bridge.run_chat_turn(
                "macos-llama-smoke",
                [{"role": "user", "content": "Reply with exactly: ok"}],
                use_tools=False,
                emit=emit,
                native_tools=False,
            )
        self._sample_rss()

        deltas = "".join(
            e.get("content", "") for e in events if e.get("type") == "delta"
        ).strip()
        self.assertTrue(
            deltas or (isinstance(turn, dict) and (turn.get("content") or "").strip()),
            msg="bridge run_chat_turn produced no content",
        )
        self.assertFalse(
            any(e.get("type") == "error" for e in events),
            msg=f"bridge emitted error: {events}",
        )

        metal_active = bool(bridge._METAL_RUNTIME_SELECTED and self.metal_build)
        # Backend evidence from probe + optional log hints (redacted).
        log_blob = "\n".join(self.proc.read_log(tail=200).get("lines") or [])
        log_blob = _redact(log_blob, self.model_path)
        log_mentions_metal = ("MTL" in log_blob) or ("metal" in log_blob.lower())

        diagnostics = {
            "llama_version": version,
            "architecture": arch,
            "llama_bin_basename": Path(self.llama_bin).name,
            "backend": {
                "metal_llama_build": self.metal_build,
                "devices": [
                    {
                        "id": d.get("id"),
                        "backend": d.get("backend"),
                        "name": d.get("name"),
                        "memory_mib": d.get("memory_mib"),
                    }
                    for d in self.devices
                ],
                "log_mentions_metal": log_mentions_metal,
            },
            "metal_offload_active": metal_active,
            "ngl_requested": self.ngl,
            "startup_s": startup_s,
            "chat_wall_s": chat_wall_s,
            "prompt_ms": prompt_ms,
            "prompt_tokens": prompt_n,
            "predicted_ms": predicted_ms,
            "predicted_tokens": predicted_n,
            "generation_tok_per_s": (
                round(float(tok_per_s), 3) if tok_per_s is not None else None
            ),
            "peak_rss_mb": self.peak_rss_mb,
            "direct_reply_chars": len(content),
            "bridge_delta_chars": len(deltas),
            "model_configured": True,
        }
        # Intentionally no model path in diagnostics / stdout.
        print(
            "[macos-llama-smoke] diagnostics:",
            json.dumps(diagnostics, ensure_ascii=False),
            flush=True,
        )

        # Stop and ensure no child remains.
        pid = self.child_pid
        self.proc.stop_permanent(timeout=5.0)
        self.assertFalse(self.proc.is_running())
        self.assertTrue(
            _wait_until(lambda: _pid_gone(pid), timeout=5.0, interval=0.05),
            f"llama-server child pid {pid} still alive after stop",
        )
        self.assertFalse(
            bridge.llama_ping(timeout=0.3, base_url=f"http://127.0.0.1:{self.port}"),
            "port still answering /v1/models after stop",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
