from __future__ import annotations

import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

from target_agent.kernel import (
    KernelDaemon, KernelForbiddenError, KernelManager, _DaemonHandler,
)
from target_agent.settings import Settings


def _settings(tmp_path, token=None):
    values = {
        "_env_file": None,
        "TARGET_AGENT_RUN_DIR": tmp_path / "runs",
        "RESEARCH_AGENT_PROJECT_DIR": tmp_path / "projects",
        "TARGET_AGENT_CACHE_DIR": tmp_path / "cache",
        "TARGET_AGENT_INPUT_ROOT": tmp_path / "input",
        "TARGET_AGENT_KERNEL_ENABLED": True,
    }
    if token is not None:
        values["TARGET_AGENT_KERNEL_TOKEN"] = token
    return Settings(**values)


class _DaemonServer:
    def __init__(self, tmp_path, token=None):
        from http.server import ThreadingHTTPServer

        self.settings = _settings(tmp_path, token=token)
        self.daemon = KernelDaemon(self.settings)
        _DaemonHandler.manager = self.daemon.manager
        _DaemonHandler.token = (
            self.settings.kernel_token.get_secret_value()
            if self.settings.kernel_token
            else None
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _DaemonHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, token=None, bearer=None, body=None):
        headers = {}
        data = None
        if token is not None:
            headers["X-Kernel-Token"] = token
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, json.loads(raw) if raw else {}


def test_daemon_rejects_requests_without_token(tmp_path):
    server = _DaemonServer(tmp_path, token="secret-token")
    try:
        status, payload = server.request("GET", "/api/kernels")
        assert status == 401
        assert payload["error"] == "unauthorized"
    finally:
        server.close()


def test_daemon_rejects_wrong_token(tmp_path):
    server = _DaemonServer(tmp_path, token="secret-token")
    try:
        status, _ = server.request("GET", "/api/kernels", token="wrong-token")
        assert status == 401
        status, _ = server.request("GET", "/api/kernels", bearer="wrong-token")
        assert status == 401
    finally:
        server.close()


def test_daemon_accepts_x_kernel_token_and_bearer(tmp_path):
    server = _DaemonServer(tmp_path, token="secret-token")
    try:
        status, payload = server.request("GET", "/api/kernels", token="secret-token")
        assert status == 200
        assert payload["kernels"] == []
        status, _ = server.request("GET", "/api/kernels", bearer="secret-token")
        assert status == 200
    finally:
        server.close()


def test_daemon_rejects_cwd_outside_whitelist_with_403(tmp_path):
    server = _DaemonServer(tmp_path, token="secret-token")
    outside = Path.home() / ".target-kernel-forbidden-cwd"
    try:
        status, payload = server.request(
            "POST",
            "/api/kernels",
            token="secret-token",
            body={"language": "python", "cwd": str(outside)},
        )
        assert status == 403
        assert payload["error"] == "KernelForbiddenError"
    finally:
        server.close()


def test_daemon_accepts_whitelisted_cwd(tmp_path):
    server = _DaemonServer(tmp_path, token="secret-token")
    try:
        status, payload = server.request(
            "POST",
            "/api/kernels",
            token="secret-token",
            body={"language": "python", "cwd": str(tmp_path)},
        )
        assert status == 201
        kernel_id = payload["kernel_id"]
        try:
            status, _ = server.request(
                "POST", f"/api/kernels/{kernel_id}/stop", token="secret-token"
            )
            assert status == 200
        finally:
            server.daemon.manager.stop_all()
    finally:
        server.close()


def test_kernel_manager_rejects_cwd_outside_whitelist(tmp_path):
    outside = Path.home() / ".target-kernel-forbidden-cwd"
    manager = KernelManager(_settings(tmp_path))
    with pytest.raises(KernelForbiddenError):
        manager.create(language="python", cwd=outside)


def test_daemon_without_token_configuration_stays_open(tmp_path):
    server = _DaemonServer(tmp_path, token=None)
    try:
        status, payload = server.request("GET", "/api/kernels")
        assert status == 200
        assert payload["kernels"] == []
    finally:
        server.close()
