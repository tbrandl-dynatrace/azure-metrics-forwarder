"""Run the Function's `poll` handler once, like one tick of the timer trigger, against real Azure and Dynatrace.

Azure auth is DefaultAzureCredential, i.e. your `az login`. Used by scripts/e2e-poller.sh.
  python scripts/poll_once.py <subscription> <resource group> <resource type> <metric>[,<metric>...]
"""
import json, logging, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
logging.basicConfig(level=logging.INFO, format="%(message)s")

subscription, resource_group, resource_type, metrics = sys.argv[1:5]
os.environ["AZMON_CONFIG"] = json.dumps([{"subscription": subscription, "resourceGroup": resource_group,
                                          "resourceType": resource_type, "metrics": metrics.split(",")}])

import function_app   # reads DT_ENVIRONMENT / DT_TOKEN / AZMON_CONFIG at import time

function_app.poll._function.get_user_function()(None)
