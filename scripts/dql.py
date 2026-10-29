"""Minimal client for the Grail DQL Query API (POST /platform/storage/query/v1/query:execute, then query:poll).

Used by tests/test_e2e.py and scripts/e2e-azure.sh to check that metrics arrived in Dynatrace. Stdlib only.

Configuration (environment or the git-ignored .env.local):
  DT_TOKEN              token with scope storage:metrics:read (sent as Bearer)
  DT_QUERY_ENVIRONMENT  apps URL, e.g. https://abc12345.apps.dynatrace.com; derived from DT_ENVIRONMENT if unset

CLI:  python scripts/dql.py "<DQL>" [--field NAME]   # prints records as JSON lines, or just one field
"""
import json, os, sys, time, urllib.error, urllib.request

API = "/platform/storage/query/v1"


def apps_url(classic_url: str) -> str:
    """https://x.live.dynatrace.com -> https://x.apps.dynatrace.com (same for sprint/dev environments)."""
    u = classic_url.rstrip("/")
    for old, new in ((".sprint.dynatracelabs.com", ".sprint.apps.dynatracelabs.com"),
                     (".dev.dynatracelabs.com", ".dev.apps.dynatracelabs.com"),
                     (".live.dynatrace.com", ".apps.dynatrace.com")):
        u = u.replace(old, new)
    return u


def _call(method, url, token, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"DQL API {e.code} for {method} {url.split('?')[0]}: {e.read().decode(errors='replace')[:500]}") from None


def query(dql: str, base_url: str = None, token: str = None, timeout_s: int = 120):
    """Run a DQL query and return its records (list of dicts)."""
    token = token or os.environ.get("DT_TOKEN")
    base_url = (base_url or os.environ.get("DT_QUERY_ENVIRONMENT")
                or apps_url(os.environ.get("DT_ENVIRONMENT", ""))).rstrip("/")
    if not token or not base_url:
        raise RuntimeError("set DT_TOKEN (and DT_QUERY_ENVIRONMENT or DT_ENVIRONMENT)")
    _, resp = _call("POST", base_url + API + "/query:execute", token,
                    {"query": dql, "requestTimeoutMilliseconds": 30000, "maxResultRecords": 1000})
    deadline = time.time() + timeout_s
    while resp.get("state") not in ("SUCCEEDED", "FAILED", "CANCELLED"):
        if time.time() > deadline:
            raise RuntimeError("DQL query did not finish in time")
        _, resp = _call("GET", f"{base_url}{API}/query:poll?request-token={urllib.request.quote(resp['requestToken'], safe='')}"
                               "&request-timeout-milliseconds=30000", token)
    if resp["state"] != "SUCCEEDED":
        raise RuntimeError(f"DQL query {resp['state']}: {json.dumps(resp)[:500]}")
    return resp["result"]["records"]


if __name__ == "__main__":
    args = sys.argv[1:]
    field = args[args.index("--field") + 1] if "--field" in args else None
    for rec in query(args[0]):
        print(rec.get(field, "") if field else json.dumps(rec))
