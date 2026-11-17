"""Run the Function's `poll` handler once, like one tick of the timer trigger, against real Azure and Dynatrace.

Resources are found by tag (default tag name `dynatrace-metrics`, override with AZMON_TAG), restricted to the given
subscription. Azure auth is DefaultAzureCredential, i.e. your `az login`. Used by scripts/e2e-poller.sh.
  python scripts/poll_once.py <subscription>
"""
import logging, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
logging.basicConfig(level=logging.INFO, format="%(message)s")

os.environ.setdefault("AZMON_TAG", "dynatrace-metrics")
os.environ["AZMON_SUBSCRIPTIONS"] = sys.argv[1]

import function_app   # reads DT_ENVIRONMENT / DT_TOKEN / AZMON_* at import time

function_app.poll._function.get_user_function()(None)
