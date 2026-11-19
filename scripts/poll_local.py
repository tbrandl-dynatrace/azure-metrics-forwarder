"""Run the poller on this machine, continuously, and send the metrics into Dynatrace (e.g. for a live demo).

It runs the same `poll` handler as the deployed timer function, once at the start of every minute, with your own
`az login` (DefaultAzureCredential). Stop with Ctrl-C.

  python scripts/poll_local.py                              # resources tagged dynatrace-nearrealtime, all your subscriptions
  python scripts/poll_local.py --tag demo --subscriptions <id>[,<id>]
  python scripts/poll_local.py --once                       # a single poll, then exit

DT_ENVIRONMENT / DT_TOKEN come from the environment or the git-ignored .env.local (see .env.example).
Don't run it while the deployed Function polls the same resources: counters would be sent twice.
"""
import argparse, logging, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_env_local():
    path = os.path.join(ROOT, ".env.local")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        k, _, v = line.strip().partition("=")
        if k and not k.startswith("#") and v and not os.environ.get(k.strip()):
            os.environ[k.strip()] = v.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", default=os.environ.get("AZMON_TAG", "dynatrace-nearrealtime"), help="tag name that selects resources")
    ap.add_argument("--subscriptions", help="comma-separated subscription ids (default: all you can read)")
    ap.add_argument("--delay-minutes", help="how old the polled minute is (default 2)")
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    a = ap.parse_args()

    load_env_local()
    for k in ("DT_ENVIRONMENT", "DT_TOKEN"):
        if not os.environ.get(k):
            sys.exit(f"{k} is not set (environment or .env.local)")
    os.environ["AZMON_TAG"] = a.tag
    if a.subscriptions:
        os.environ["AZMON_SUBSCRIPTIONS"] = a.subscriptions
    if a.delay_minutes:
        os.environ["AZMON_DELAY_MINUTES"] = a.delay_minutes
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("azure").setLevel(logging.WARNING)          # quiet the SDK's request logging

    import function_app
    poll = function_app.poll._function.get_user_function()
    print(f"polling resources tagged '{a.tag}' every minute into {os.environ['DT_ENVIRONMENT']} (Ctrl-C to stop)")
    while True:
        try:
            poll(None)
        except Exception as e:                                      # keep going: one bad minute must not end the demo
            logging.error("poll failed: %s", str(e)[:400])
        if a.once:
            return
        time.sleep(60 - time.time() % 60 + 0.5)                     # next full minute, like the timer trigger


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("stopped")
