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


class MockAzure:
    """Local stand-in for Azure Resource Graph, the metric definitions API and the regional Azure Monitor Metrics Batch endpoint."""

    def __init__(self):
        self.resources = []         # [{"id", "location"}]
        self.batch_calls = []       # [{"region", "subscription", "params", "resourceids"}]
        self.arm_calls = 0
        self.graph_rows = []        # Azure Resource Graph result rows: {id, type, location, subscriptionId, v}
        self.graph_calls = []       # request bodies sent to Resource Graph
        self.definitions = {}       # lower-case resource type -> metricDefinitions "value" list
        self.definition_calls = []
        self.batch_response = lambda call: {"values": []}

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, payload, status=200):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if "/metricDefinitions" in self.path:                # <resource id>/providers/Microsoft.Insights/metricDefinitions
                    outer.definition_calls.append(self.path)
                    rtype = self.path.lower().split("/providers/", 1)[1].split("/providers/microsoft.insights")[0]
                    for key, defs in outer.definitions.items():
                        if rtype.startswith(key):
                            return self._send({"value": defs})
                    return self._send({"value": []})
                outer.arm_calls += 1
                self._send({"value": outer.resources})

            def do_POST(self):
                from urllib.parse import urlparse, parse_qs
                u = urlparse(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if u.path.startswith("/providers/Microsoft.ResourceGraph"):
                    outer.graph_calls.append(body)
                    return self._send({"data": outer.graph_rows})
                parts = u.path.strip("/").split("/")                 # <region>/subscriptions/<sub>/metrics:getBatch
                call = {"region": parts[0], "subscription": parts[2],
                        "params": {k: v[0] for k, v in parse_qs(u.query).items()}, "resourceids": body["resourceids"],
                        "auth": self.headers.get("Authorization")}
                outer.batch_calls.append(call)
                answer = outer.batch_response(call)
                if isinstance(answer, tuple):                         # (status, body): simulate an Azure error
                    return self._send(answer[1], answer[0])
                self._send(answer)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def azure():
    mock = MockAzure()
    yield mock
    mock.stop()


@pytest.fixture
def load_poller(monkeypatch, azure):
    """Import function_app / azmon fresh, pointed at the mock Azure and the given Dynatrace URL; returns poll()."""

    def _load(dt_environment, tag="dynatrace-metrics", delay=None, subscriptions=None):
        monkeypatch.setenv("DT_ENVIRONMENT", dt_environment)
        monkeypatch.setenv("DT_TOKEN", "test-token")
        monkeypatch.setenv("AZMON_ARM_URL", azure.url)
        monkeypatch.setenv("AZMON_METRICS_URL", azure.url + "/{region}")
        if delay is not None:
            monkeypatch.setenv("AZMON_DELAY_MINUTES", str(delay))
        if tag is None:
            monkeypatch.delenv("AZMON_TAG", raising=False)
        else:
            monkeypatch.setenv("AZMON_TAG", tag)
        if subscriptions is None:
            monkeypatch.delenv("AZMON_SUBSCRIPTIONS", raising=False)
        else:
            monkeypatch.setenv("AZMON_SUBSCRIPTIONS", ",".join(subscriptions))
        for m in ("function_app", "azmon"):
            sys.modules.pop(m, None)
        azmon = importlib.import_module("azmon")
        monkeypatch.setattr(azmon, "get_token", lambda scope: "fake-token-for-" + scope.split("/")[2])
        module = importlib.import_module("function_app")
        return module.poll._function.get_user_function()

    return _load
