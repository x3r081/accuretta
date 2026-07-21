"""Supervise a ``codex app-server`` child process (stdio JSON-RPC).

Newly written for Accuretta. Never uses shell=True.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from typing import Callable, List, Optional

from .rpc_client import CodexRpcClient, CodexRpcError, redact_stderr_line

log = logging.getLogger("accuretta.codex.process")

LineHandler = Callable[[str], None]


class CodexProcessError(Exception):
    pass


class CodexAppServerProcess:
    def __init__(
        self,
        executable: str,
        *,
        args: Optional[List[str]] = None,
        env: Optional[dict] = None,
        startup_timeout: float = 15.0,
    ):
        self.executable = executable
        self.args = list(args or ["app-server"])
        self.env = env
        self.startup_timeout = startup_timeout
        self._proc: Optional[subprocess.Popen] = None
        self._rpc: Optional[CodexRpcClient] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ready = False
        self._lock = threading.RLock()
        self._on_notification: Optional[Callable[[str, object], None]] = None
        self._exit_code: Optional[int] = None
        # PID of the owned child only — never used to signal unrelated Codex CLIs.
        self._owned_pid: Optional[int] = None

    @property
    def ready(self) -> bool:
        return self._ready and self._proc is not None and self._proc.poll() is None

    @property
    def rpc(self) -> CodexRpcClient:
        if self._rpc is None:
            raise CodexProcessError("Codex app-server is not running")
        return self._rpc

    def set_notification_handler(self, handler: Callable[[str, object], None]) -> None:
        self._on_notification = handler

    def start(self) -> None:
        with self._lock:
            if self.ready:
                return
            self._stop.clear()
            cmd = [self.executable] + self.args
            popen_kwargs = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "bufsize": 0,
            }
            if os.name == "nt":
                # Avoid console window flash on Windows.
                create = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if create:
                    popen_kwargs["creationflags"] = create
            else:
                popen_kwargs["start_new_session"] = True
            env = os.environ.copy()
            if self.env:
                env.update(self.env)
            try:
                self._proc = subprocess.Popen(cmd, env=env, shell=False, **popen_kwargs)
            except Exception as exc:
                raise CodexProcessError(
                    f"Codex app-server failed to start ({type(exc).__name__})"
                ) from None
            self._owned_pid = self._proc.pid

            def _write_line(line: str) -> None:
                proc = self._proc
                if proc is None or proc.stdin is None:
                    raise CodexRpcError("Codex stdin closed")
                data = (line + "\n").encode("utf-8")
                proc.stdin.write(data)
                proc.stdin.flush()

            self._rpc = CodexRpcClient(
                write_line=_write_line,
                on_notification=self._dispatch_notification,
            )
            self._stdout_thread = threading.Thread(
                target=self._read_stdout, name="codex-stdout", daemon=True
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr, name="codex-stderr", daemon=True
            )
            self._stdout_thread.start()
            self._stderr_thread.start()

            # Handshake
            try:
                self._rpc.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "accuretta",
                            "title": "Accuretta",
                            "version": "0.1.0",
                        },
                        "capabilities": {
                            # Stay on stable surface — no experimentalApi.
                            "optOutNotificationMethods": [],
                        },
                    },
                    timeout=self.startup_timeout,
                )
                self._rpc.notify("initialized", {})
            except Exception as exc:
                self.terminate()
                raise CodexProcessError(
                    f"Codex protocol initialization failed ({type(exc).__name__})"
                ) from None
            if self._proc.poll() is not None:
                self.terminate()
                raise CodexProcessError("Codex app-server exited during startup")
            self._ready = True

    def _dispatch_notification(self, method: str, params) -> None:
        # Run handlers off the stdout reader thread so they may safely issue
        # blocking RPC requests (e.g. account/read after login completed).
        handler = self._on_notification
        if handler is None:
            return

        def _run() -> None:
            try:
                handler(method, params)
            except Exception as exc:
                log.warning("codex notification handler failed: %s", type(exc).__name__)

        threading.Thread(target=_run, name="codex-notify", daemon=True).start()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while not self._stop.is_set():
                line = proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                if self._rpc is not None:
                    self._rpc.feed_line(text)
        finally:
            self._exit_code = proc.poll()
            if self._rpc is not None:
                self._rpc.close()
            self._ready = False

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while not self._stop.is_set():
                line = proc.stderr.readline()
                if not line:
                    break
                text = redact_stderr_line(line.decode("utf-8", errors="replace"))
                if text.strip():
                    log.info("codex stderr: %s", text.strip())
        except Exception:
            pass

    def terminate(self, *, grace_s: float = 2.0) -> None:
        with self._lock:
            self._stop.set()
            self._ready = False
            proc = self._proc
            rpc = self._rpc
            owned_pid = self._owned_pid
            self._rpc = None
            self._owned_pid = None
            if rpc is not None:
                try:
                    rpc.close()
                except Exception:
                    pass
            if proc is None:
                return
            try:
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                if proc.poll() is None:
                    # Prefer signaling the owned process group (Unix + start_new_session)
                    # so grandchildren exit. Never broadcast-kill by process name —
                    # only this owned PID / process group.
                    signaled = False
                    if os.name != "nt" and owned_pid:
                        try:
                            os.killpg(owned_pid, signal.SIGTERM)
                            signaled = True
                        except (ProcessLookupError, PermissionError, OSError):
                            signaled = False
                    if not signaled:
                        proc.terminate()
                    try:
                        proc.wait(timeout=grace_s)
                    except subprocess.TimeoutExpired:
                        if os.name != "nt" and owned_pid:
                            try:
                                os.killpg(owned_pid, signal.SIGKILL)
                            except (ProcessLookupError, PermissionError, OSError):
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                        else:
                            proc.kill()
                        try:
                            proc.wait(timeout=1.0)
                        except Exception:
                            pass
            finally:
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
                self._proc = None
            # Join readers briefly
            for thr in (self._stdout_thread, self._stderr_thread):
                if thr is not None and thr.is_alive():
                    thr.join(timeout=1.0)
            self._stdout_thread = None
            self._stderr_thread = None
