"""Persistent, bounded analysis kernels (Python/R) for explicit workflows.

Boundary: a kernel is created by an explicit user action (CLI or Web) and is
project-scoped. The LLM never receives a generic code-execution action; the
kernel backs registered analysis tools and interactive workbench use only.
Code runs with the permissions of the target-agent user, so deployments should
place the service inside a container or the configured remote execution
profile. Sessions persist interpreter state (imports, variables, cwd) between
executions and are reaped when idle.
"""
from __future__ import annotations

import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .settings import Settings


class KernelLanguage(str, Enum):
    PYTHON = "python"
    R = "r"


class KernelStatus(str, Enum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    STOPPED = "stopped"
    FAILED = "failed"


class KernelError(RuntimeError):
    """Base class for kernel control-plane failures."""


class KernelDisabledError(KernelError):
    pass


class KernelNotConfiguredError(KernelError):
    pass


class KernelForbiddenError(KernelError):
    pass


class KernelConfigError(KernelError):
    pass


class KernelNotFoundError(KernelError):
    pass


class KernelUnavailableError(KernelError):
    pass


class KernelTimeoutError(KernelError):
    pass


_PY_BOOTSTRAP = r'''
import contextlib
import io
import json
import sys
import traceback

ns = {"__name__": "__kernel__"}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            sys.stdout.write(json.dumps({
                "seq": None, "ok": False, "error": "ProtocolError",
                "message": str(exc), "stdout": "", "stderr": "",
            }, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        seq = req.get("seq")
        code = req.get("code", "")
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                exec(compile(code, "<kernel>", "exec"), ns)
                payload = {
                    "seq": seq, "ok": True, "result": None,
                    "stdout": out.getvalue(), "stderr": err.getvalue(),
                    "error": None, "message": None, "traceback": None,
                }
                if "__kernel_result__" in ns:
                    payload["result"] = ns.pop("__kernel_result__")
            except BaseException as exc:
                payload = {
                    "seq": seq, "ok": False, "result": None,
                    "stdout": out.getvalue(), "stderr": err.getvalue(),
                    "error": type(exc).__name__, "message": str(exc),
                    "traceback": traceback.format_exc(),
                }
        try:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except TypeError:
            payload["result"] = "<unserializable>"
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
'''

_R_BOOTSTRAP = r'''
suppressMessages(library(jsonlite))
ns <- new.env(parent = globalenv())
con <- file("stdin", open = "r")
repeat {
  line <- readLines(con, n = 1, warn = FALSE)
  if (length(line) == 0 || nchar(trimws(line)) == 0) break
  req <- tryCatch(fromJSON(line, simplifyVector = FALSE), error = function(e) NULL)
  if (is.null(req)) {
    cat(toJSON(list(seq = NA, ok = FALSE, error = "ProtocolError",
                    message = "invalid JSON", stdout = ""), auto_unbox = TRUE), "\n", sep = "")
    flush(stdout())
    next
  }
  seqid <- req$seq
  code <- req$code
  stdout <- ""
  result <- NULL
  ok <- FALSE
  error <- ""
  message <- ""
  tryCatch({
    captured <- capture.output(eval(parse(text = code), envir = ns))
    stdout <- paste(captured, collapse = "\n")
    if (exists("__kernel_result__", envir = ns, inherits = FALSE)) {
      result <- get("__kernel_result__", envir = ns)
      rm("__kernel_result__", envir = ns)
    }
    ok <- TRUE
  }, error = function(e) {
    error <<- class(e)[1]
    message <<- conditionMessage(e)
  })
  payload <- tryCatch(
    toJSON(list(seq = seqid, ok = ok, result = result, stdout = stdout,
                error = error, message = message), auto_unbox = TRUE,
           null = "null", force = TRUE),
    error = function(e) toJSON(list(seq = seqid, ok = FALSE, error = "SerializationError",
                                    message = conditionMessage(e), stdout = stdout),
                               auto_unbox = TRUE)
  )
  cat(payload, "\n", sep = "")
  flush(stdout())
}
close(con)
'''


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    if value is None:
        return "", False
    if len(value) <= limit:
        return value, False
    head = limit // 2
    tail = limit - head - len("\n...[truncated by target-agent kernel]...\n")
    return f"{value[:head]}\n...[truncated by target-agent kernel]...\n{value[-max(tail, 0):]}", True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class KernelExecResult:
    kernel_id: str
    seq: int
    ok: bool
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    message: str | None = None
    traceback: str | None = None
    duration_ms: int = 0
    output_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kernel_id": self.kernel_id,
            "seq": self.seq,
            "ok": self.ok,
            "result": self.result,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "message": self.message,
            "traceback": self.traceback,
            "duration_ms": self.duration_ms,
            "output_truncated": self.output_truncated,
        }


@dataclass
class KernelInfo:
    kernel_id: str
    language: str
    cwd: str
    status: KernelStatus
    pid: int | None = None
    created_at: str = field(default_factory=_utc_now)
    last_used_at: str = field(default_factory=_utc_now)
    exec_count: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kernel_id": self.kernel_id,
            "language": self.language,
            "cwd": self.cwd,
            "status": self.status.value,
            "pid": self.pid,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "exec_count": self.exec_count,
            "error": self.error,
        }


class _PythonSession:
    language = KernelLanguage.PYTHON

    def __init__(self, *, cwd: Path, python_bin: str, max_output_chars: int):
        self.cwd = cwd
        self.python_bin = python_bin or sys.executable
        self.max_output_chars = max_output_chars
        self.proc: subprocess.Popen[str] | None = None
        self._seq = 0
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._stderr_thread: threading.Thread | None = None
        self._info: KernelInfo | None = None

    def start(self) -> KernelInfo:
        self.proc = subprocess.Popen(
            [self.python_bin, "-u", "-c", _PY_BOOTSTRAP],
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="kernel-stderr"
        )
        self._stderr_thread.start()
        self._info = KernelInfo(
            kernel_id="", language="python", cwd=str(self.cwd),
            status=KernelStatus.READY, pid=self.proc.pid,
        )
        return self._info

    def _drain_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        try:
            for line in self.proc.stderr:
                self._stderr_tail.append(line)
        except (OSError, ValueError):
            pass

    def _stderr_summary(self) -> str:
        return "".join(self._stderr_tail)[-2000:]

    def execute(self, code: str, timeout: float, max_output_chars: int) -> KernelExecResult:
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise KernelUnavailableError("kernel process is not running")
        if self.proc.poll() is not None:
            self._mark_failed("kernel process exited before execution")
            raise KernelUnavailableError(f"kernel process exited: {self._stderr_summary()}")
        self._seq += 1
        seq = self._seq
        started = time.monotonic()
        self.proc.stdin.write(json.dumps({"seq": seq, "code": code}, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        line = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._mark_failed(f"execution exceeded {timeout:g}s; the session was terminated")
                self.stop()
                raise KernelTimeoutError(f"execution exceeded {timeout:g}s; the kernel session was terminated")
            try:
                line = self.proc.stdout.readline()
            except (OSError, ValueError) as exc:
                self._mark_failed(str(exc))
                raise KernelUnavailableError(f"kernel stdout closed: {exc}") from exc
            if line == "":
                self._mark_failed("kernel process exited during execution")
                raise KernelUnavailableError(f"kernel process exited during execution: {self._stderr_summary()}")
            if line.strip():
                if time.monotonic() > deadline:
                    self._mark_failed(f"execution exceeded {timeout:g}s; the session was terminated")
                    self.stop()
                    raise KernelTimeoutError(f"execution exceeded {timeout:g}s; the kernel session was terminated")
                break
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            self._mark_failed("protocol violation")
            raise KernelUnavailableError(f"kernel returned a non-JSON response: {line[:200]!r}") from exc
        if payload.get("seq") != seq:
            self._mark_failed("protocol sequence mismatch")
            raise KernelUnavailableError("kernel protocol sequence mismatch")
        stdout, stdout_truncated = _truncate_text(payload.get("stdout") or "", max_output_chars)
        stderr, stderr_truncated = _truncate_text(payload.get("stderr") or "", max_output_chars)
        traceback, tb_truncated = _truncate_text(payload.get("traceback") or "", max_output_chars)
        return KernelExecResult(
            kernel_id=self._info.kernel_id if self._info else "",
            seq=seq,
            ok=bool(payload.get("ok")),
            result=payload.get("result"),
            stdout=stdout,
            stderr=stderr,
            error=payload.get("error"),
            message=payload.get("message"),
            traceback=traceback or None,
            duration_ms=duration_ms,
            output_truncated=stdout_truncated or stderr_truncated or tb_truncated,
        )

    def _mark_failed(self, reason: str) -> None:
        if self._info is not None:
            self._info.status = KernelStatus.FAILED
            self._info.error = reason

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
            except (OSError, ValueError):
                pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
        if self._info is not None and self._info.status != KernelStatus.FAILED:
            self._info.status = KernelStatus.STOPPED
        self.proc = None


class _RSession:
    language = KernelLanguage.R

    def __init__(self, *, cwd: Path, r_bin: str):
        self.cwd = cwd
        self.r_bin = r_bin
        self.proc: subprocess.Popen[str] | None = None
        self._seq = 0
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._stderr_thread: threading.Thread | None = None
        self._info: KernelInfo | None = None

    def start(self) -> KernelInfo:
        self.proc = subprocess.Popen(
            [self.r_bin, "--vanilla", "-e", _R_BOOTSTRAP],
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="kernel-r-stderr"
        )
        self._stderr_thread.start()
        time.sleep(0.05)
        if self.proc.poll() is not None:
            self._info = KernelInfo(
                kernel_id="", language="r", cwd=str(self.cwd),
                status=KernelStatus.FAILED, pid=None,
                error="Rscript exited at startup; jsonlite is required",
            )
            return self._info
        self._info = KernelInfo(
            kernel_id="", language="r", cwd=str(self.cwd),
            status=KernelStatus.READY, pid=self.proc.pid,
        )
        return self._info

    def _drain_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        try:
            for line in self.proc.stderr:
                self._stderr_tail.append(line)
        except (OSError, ValueError):
            pass

    def _stderr_summary(self) -> str:
        return "".join(self._stderr_tail)[-2000:]

    def execute(self, code: str, timeout: float, max_output_chars: int) -> KernelExecResult:
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise KernelUnavailableError("R kernel process is not running")
        if self.proc.poll() is not None:
            self._mark_failed("R kernel exited before execution")
            raise KernelUnavailableError(f"R kernel process exited: {self._stderr_summary()}")
        self._seq += 1
        seq = self._seq
        started = time.monotonic()
        self.proc.stdin.write(json.dumps({"seq": seq, "code": code}, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        line = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._mark_failed(f"R execution exceeded {timeout:g}s; the session was terminated")
                self.stop()
                raise KernelTimeoutError(f"R execution exceeded {timeout:g}s; the kernel session was terminated")
            try:
                line = self.proc.stdout.readline()
            except (OSError, ValueError) as exc:
                self._mark_failed(str(exc))
                raise KernelUnavailableError(f"R kernel stdout closed: {exc}") from exc
            if line == "":
                self._mark_failed("R kernel process exited during execution")
                raise KernelUnavailableError(f"R kernel process exited during execution: {self._stderr_summary()}")
            if line.strip():
                if time.monotonic() > deadline:
                    self._mark_failed(f"R execution exceeded {timeout:g}s; the session was terminated")
                    self.stop()
                    raise KernelTimeoutError(f"R execution exceeded {timeout:g}s; the kernel session was terminated")
                break
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            self._mark_failed("protocol violation")
            raise KernelUnavailableError(f"R kernel returned a non-JSON response: {line[:200]!r}") from exc
        stdout, stdout_truncated = _truncate_text(payload.get("stdout") or "", max_output_chars)
        stderr, stderr_truncated = _truncate_text(payload.get("stderr") or "", max_output_chars)
        traceback, tb_truncated = _truncate_text(payload.get("traceback") or "", max_output_chars)
        return KernelExecResult(
            kernel_id=self._info.kernel_id if self._info else "",
            seq=seq,
            ok=bool(payload.get("ok")),
            result=payload.get("result"),
            stdout=stdout,
            stderr=stderr,
            error=payload.get("error"),
            message=payload.get("message"),
            traceback=traceback or None,
            duration_ms=duration_ms,
            output_truncated=stdout_truncated or stderr_truncated or tb_truncated,
        )

    def _mark_failed(self, reason: str) -> None:
        if self._info is not None:
            self._info.status = KernelStatus.FAILED
            self._info.error = reason

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
            except (OSError, ValueError):
                pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
        if self._info is not None and self._info.status != KernelStatus.FAILED:
            self._info.status = KernelStatus.STOPPED
        self.proc = None


class KernelManager:
    """Thread-safe registry of explicitly created persistent kernels."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._sessions: dict[str, Any] = {}
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self._settings.kernel_enabled

    def capabilities(self) -> dict[str, Any]:
        rscript = shutil.which(self._settings.kernel_r_bin or "Rscript")
        return {
            "enabled": self.enabled,
            "backends": {
                "python": True,
                "r": bool(rscript),
            },
            "limits": {
                "idle_timeout_seconds": self._settings.kernel_idle_timeout_seconds,
                "exec_timeout_seconds": self._settings.kernel_exec_timeout_seconds,
                "max_output_chars": self._settings.kernel_max_output_chars,
                "max_code_chars": self._settings.kernel_max_code_chars,
            },
        }

    def _new_id(self) -> str:
        return f"kernel-{uuid.uuid4().hex[:20]}"

    def _allowed_cwd_roots(self) -> list[Path]:
        roots = [
            self._settings.projects_dir,
            self._settings.runs_dir,
            self._settings.cache_dir,
            self._settings.input_root,
        ]
        resolved = [root.expanduser().resolve() for root in roots]
        resolved.append(Path(tempfile.gettempdir()).resolve())
        return resolved

    def _is_allowed_cwd(self, path: Path) -> bool:
        return any(path == root or root in path.parents for root in self._allowed_cwd_roots())

    def create(self, language: str = "python", cwd: str | Path | None = None) -> KernelInfo:
        if not self.enabled:
            raise KernelDisabledError("kernel execution is disabled by TARGET_AGENT_KERNEL_ENABLED=false")
        lang = KernelLanguage(language.lower())
        workdir = Path(cwd or self._settings.projects_dir).expanduser().resolve()
        # Reject out-of-whitelist cwds before creating anything, so a hostile
        # request cannot cause directories to be created outside managed roots.
        if not self._is_allowed_cwd(workdir):
            raise KernelForbiddenError("kernel cwd must be inside a managed data directory")
        if cwd is None:
            # Default projects dir is a managed data directory: create it on demand.
            workdir.mkdir(parents=True, exist_ok=True)
        if not workdir.is_dir():
            raise KernelConfigError(f"kernel cwd is not a directory: {workdir}")
        kernel_id = self._new_id()
        if lang == KernelLanguage.PYTHON:
            session = _PythonSession(
                cwd=workdir,
                python_bin=self._settings.kernel_python_bin,
                max_output_chars=self._settings.kernel_max_output_chars,
            )
        else:
            r_bin = shutil.which(self._settings.kernel_r_bin or "Rscript")
            if not r_bin:
                raise KernelNotConfiguredError(
                    "Rscript was not found; install R and jsonlite, or set TARGET_AGENT_KERNEL_R"
                )
            session = _RSession(cwd=workdir, r_bin=r_bin)
        info = session.start()
        if info.status == KernelStatus.FAILED:
            info.kernel_id = kernel_id
            info.error = info.error or "kernel failed to start"
            self._sessions[kernel_id] = session
            return info
        info.kernel_id = kernel_id
        self._sessions[kernel_id] = session
        return info

    def _require(self, kernel_id: str) -> Any:
        session = self._sessions.get(kernel_id)
        if session is None:
            raise KernelNotFoundError(f"kernel not found: {kernel_id}")
        return session

    def execute(
        self, kernel_id: str, code: str, *, timeout: float | None = None
    ) -> KernelExecResult:
        with self._lock:
            session = self._require(kernel_id)
            if not isinstance(code, str) or not code.strip():
                raise KernelConfigError("kernel code must be a non-empty string")
            if len(code) > self._settings.kernel_max_code_chars:
                raise KernelConfigError(
                    f"kernel code exceeds {self._settings.kernel_max_code_chars} characters"
                )
            info = session._info
            if info is None or info.status in {KernelStatus.STOPPED, KernelStatus.FAILED}:
                raise KernelUnavailableError(f"kernel is not ready: {info.status.value if info else 'unknown'}")
            info.last_used_at = _utc_now()
        exec_timeout = timeout if timeout is not None else self._settings.kernel_exec_timeout_seconds
        result = session.execute(code, exec_timeout, self._settings.kernel_max_output_chars)
        with self._lock:
            info = session._info
            if info is not None:
                info.exec_count += 1
                info.last_used_at = _utc_now()
        return result

    def get(self, kernel_id: str) -> KernelInfo:
        with self._lock:
            session = self._require(kernel_id)
            self._maybe_reap_locked()
            return session._info

    def list(self) -> list[KernelInfo]:
        with self._lock:
            self._maybe_reap_locked()
            return [session._info for session in self._sessions.values()]

    def stop(self, kernel_id: str) -> KernelInfo:
        with self._lock:
            session = self._require(kernel_id)
            session.stop()
            info = session._info
            self._sessions.pop(kernel_id, None)
            return info

    def stop_all(self) -> int:
        with self._lock:
            count = len(self._sessions)
            for session in self._sessions.values():
                session.stop()
            self._sessions.clear()
            return count

    def reap_idle(self) -> int:
        with self._lock:
            return self._maybe_reap_locked()

    def _maybe_reap_locked(self) -> int:
        idle_seconds = self._settings.kernel_idle_timeout_seconds
        if idle_seconds <= 0:
            deadline = None
        else:
            deadline = time.time() - idle_seconds
        stopped: list[str] = []
        for kernel_id, session in list(self._sessions.items()):
            info = session._info
            if info is None:
                continue
            if info.status in {KernelStatus.STOPPED, KernelStatus.FAILED}:
                stopped.append(kernel_id)
                continue
            if deadline is not None:
                last = info.last_used_at
                try:
                    last_epoch = datetime.fromisoformat(last).timestamp()
                except ValueError:
                    last_epoch = time.monotonic()
                if last_epoch < deadline:
                    session.stop()
                    stopped.append(kernel_id)
        for kernel_id in stopped:
            self._sessions.pop(kernel_id, None)
        return len(stopped)



class _DaemonHandler(BaseHTTPRequestHandler):
    """JSON kernel daemon routes shared by CLI subprocesses."""

    manager: KernelManager | None = None
    token: str | None = None

    def _authorized(self) -> bool:
        if not self.token:
            return True
        presented = self.headers.get("X-Kernel-Token") or ""
        if not presented:
            header = self.headers.get("Authorization", "")
            scheme, _, value = header.partition(" ")
            presented = value.strip() if scheme.lower() == "bearer" else ""
        return bool(presented) and hmac.compare_digest(
            presented.encode("utf-8"), self.token.encode("utf-8")
        )

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def do_GET(self) -> None:
        if not self._authorized():
            self._send(401, {"error": "unauthorized", "detail": "kernel token required"})
            return
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        if self.path == "/api/kernels":
            self._send(200, {
                "kernels": [info.to_dict() for info in self.manager.list()],
                "capabilities": self.manager.capabilities(),
            })
            return
        prefix = "/api/kernels/"
        if self.path.startswith(prefix):
            kernel_id = self.path[len(prefix):].strip("/")
            try:
                self._send(200, self.manager.get(kernel_id).to_dict())
            except KernelNotFoundError as exc:
                self._send(404, {"error": exc.__class__.__name__, "detail": str(exc)})
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(401, {"error": "unauthorized", "detail": "kernel token required"})
            return
        body = self._read_json()
        if self.path == "/api/kernels":
            try:
                info = self.manager.create(
                    language=str(body.get("language") or "python"),
                    cwd=body.get("cwd"),
                )
            except (KernelDisabledError, KernelNotConfiguredError, KernelConfigError) as exc:
                self._send(400, {"error": exc.__class__.__name__, "detail": str(exc)})
                return
            except KernelForbiddenError as exc:
                self._send(403, {"error": exc.__class__.__name__, "detail": str(exc)})
                return
            self._send(201, info.to_dict())
            return
        if self.path == "/api/kernels/stop-all":
            self._send(200, {"stopped": self.manager.stop_all()})
            return
        prefix = "/api/kernels/"
        if self.path.startswith(prefix):
            parts = self.path[len(prefix):].strip("/").split("/")
            if len(parts) == 2 and parts[1] == "exec":
                kernel_id = parts[0]
                code = body.get("code")
                if not isinstance(code, str) or not code.strip():
                    self._send(400, {"error": "invalid_code", "detail": "code must be a non-empty string"})
                    return
                timeout = body.get("timeout")
                try:
                    result = self.manager.execute(kernel_id, code, timeout=timeout)
                except KernelNotFoundError as exc:
                    self._send(404, {"error": exc.__class__.__name__, "detail": str(exc)})
                except KernelUnavailableError as exc:
                    self._send(409, {"error": exc.__class__.__name__, "detail": str(exc)})
                except KernelTimeoutError as exc:
                    self._send(408, {"error": exc.__class__.__name__, "detail": str(exc)})
                except KernelConfigError as exc:
                    self._send(400, {"error": exc.__class__.__name__, "detail": str(exc)})
                else:
                    self._send(200, result.to_dict())
                return
            if len(parts) == 2 and parts[1] == "stop":
                kernel_id = parts[0]
                try:
                    self._send(200, self.manager.stop(kernel_id).to_dict())
                except KernelNotFoundError as exc:
                    self._send(404, {"error": exc.__class__.__name__, "detail": str(exc)})
                return
        self._send(404, {"error": "not_found"})


class KernelDaemon:
    """Localhost JSON daemon that keeps kernels alive across CLI processes."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.manager = KernelManager(settings)

    def run(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        from http.server import ThreadingHTTPServer

        _DaemonHandler.manager = self.manager
        _DaemonHandler.token = (
            self.settings.kernel_token.get_secret_value() if self.settings.kernel_token else None
        )
        server = ThreadingHTTPServer((host, port or self.settings.kernel_port), _DaemonHandler)
        server.daemon_threads = True
        try:
            server.serve_forever()
        finally:
            self.manager.stop_all()
            server.server_close()


def daemon_base_url(settings: Settings) -> str:
    return f"http://127.0.0.1:{settings.kernel_port}"


def daemon_health(base_url: str, timeout: float = 0.5, token: str | None = None) -> bool:
    headers = {"X-Kernel-Token": token} if token else {}
    try:
        request = urllib.request.Request(f"{base_url}/healthz", headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def ensure_kernel_daemon(settings: Settings) -> str:
    """Return a healthy daemon base URL, starting a detached daemon if needed."""
    base_url = daemon_base_url(settings)
    token = settings.kernel_token.get_secret_value() if settings.kernel_token else None
    if daemon_health(base_url, token=token):
        return base_url
    log_path = settings.cache_dir / "kernel-daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    with log_path.open("ab") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "target_agent.cli", "kernel", "serve",
             "--port", str(settings.kernel_port)],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
            creationflags=creationflags,
        )
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if daemon_health(base_url, token=token):
            return base_url
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    raise KernelUnavailableError(
        f"kernel daemon did not become healthy; inspect {log_path}"
    )


class KernelDaemonClient:
    """CLI-side client for the localhost kernel daemon."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = ensure_kernel_daemon(settings)

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None,
              timeout: float = 90.0) -> tuple[int, dict[str, Any]]:
        data = None
        headers = {}
        token = self.settings.kernel_token
        if token is not None:
            secret = token.get_secret_value()
            if secret:
                headers["X-Kernel-Token"] = secret
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"error": "http_error", "detail": raw}

    def start(self, language: str = "python", cwd: str | Path | None = None) -> dict[str, Any]:
        status, payload = self._call("POST", "/api/kernels", {
            "language": language, "cwd": str(cwd) if cwd else None,
        }, timeout=30.0)
        if status != 201:
            raise KernelUnavailableError(f"{payload.get('error', 'error')}: {payload.get('detail', '')}")
        return payload

    def execute(self, kernel_id: str, code: str, timeout: float | None = None) -> dict[str, Any]:
        status, payload = self._call(
            "POST", f"/api/kernels/{kernel_id}/exec", {"code": code, "timeout": timeout},
        )
        if status == 408:
            raise KernelTimeoutError(str(payload.get("detail", "kernel execution timed out")))
        if status != 200:
            raise KernelUnavailableError(f"{payload.get('error', 'error')}: {payload.get('detail', '')}")
        return payload

    def status(self, kernel_id: str | None = None) -> dict[str, Any]:
        path = f"/api/kernels/{kernel_id}" if kernel_id else "/api/kernels"
        status, payload = self._call("GET", path, timeout=10.0)
        if status != 200:
            raise KernelNotFoundError(str(payload.get("detail", "kernel not found")))
        return payload

    def stop(self, kernel_id: str) -> dict[str, Any]:
        status, payload = self._call("POST", f"/api/kernels/{kernel_id}/stop", timeout=10.0)
        if status != 200:
            raise KernelNotFoundError(str(payload.get("detail", "kernel not found")))
        return payload

    def stop_all(self) -> int:
        status, payload = self._call("POST", "/api/kernels/stop-all", timeout=10.0)
        if status != 200:
            raise KernelUnavailableError("failed to stop kernels")
        return int(payload.get("stopped", 0))


__all__ = [
    "KernelConfigError", "KernelDisabledError", "KernelError", "KernelExecResult",
    "KernelForbiddenError", "KernelInfo", "KernelLanguage", "KernelManager",
    "KernelNotConfiguredError",
    "KernelNotFoundError", "KernelStatus", "KernelTimeoutError", "KernelUnavailableError",
    "KernelDaemon", "KernelDaemonClient", "daemon_base_url", "daemon_health",
    "ensure_kernel_daemon",
]
