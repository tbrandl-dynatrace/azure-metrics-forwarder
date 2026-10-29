import importlib, json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_env_local():
    """Read DT_ENVIRONMENT / DT_TOKEN from the git-ignored .env.local (already-set variables win)."""
    path = os.path.join(ROOT, ".env.local")
    if not os.path.isfile(path):
        return
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_env_local()


class FakeEvent:
    """Stands in for func.EventHubEvent: the Function only calls get_body()."""

    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def get_body(self):
        return self._body


class MockDynatrace:
    """Local HTTP server that behaves like POST /api/v2/metrics/ingest and records what it receives."""

    def __init__(self):
        self.requests = []          # [{"path", "headers", "lines"}]
        self.status = 202           # status returned for every request

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                outer.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "lines": body.split("\n") if body else []})
                payload = json.dumps({"linesOk": len(outer.requests[-1]["lines"]), "linesInvalid": 0}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def lines(self):
        return [line for r in self.requests for line in r["lines"]]

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def dynatrace():
    mock = MockDynatrace()
    yield mock
    mock.stop()


@pytest.fixture
def load_function(monkeypatch):
    """Import function_app fresh with the given environment (it reads its config at import time)
    and return the plain handler, i.e. what the Azure Functions host would invoke for a batch of events."""

    def _load(dt_environment, dt_token="test-token", location="westeurope"):
        monkeypatch.setenv("DT_ENVIRONMENT", dt_environment)
        monkeypatch.setenv("DT_TOKEN", dt_token)
        monkeypatch.setenv("AZURE_LOCATION", location)
        monkeypatch.delenv("REGION_NAME", raising=False)
        sys.modules.pop("function_app", None)
        module = importlib.import_module("function_app")
        return module.run._function.get_user_function()

    return _load
