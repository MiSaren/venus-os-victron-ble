#!/usr/bin/env python3
"""Stand-in for the vesmart helper (python3 -m victronble.vesmart) in the
integration test: a charger whose night mode lives in a JSON file.

    fake_vesmart.py --state-file FILE --mac MAC --op read|night-on|night-off --pins-file FILE
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from victronble import pins  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--mac", required=True)
    parser.add_argument("--op", required=True, choices=("read", "night-on", "night-off"))
    parser.add_argument("--pins-file", required=True)
    args = parser.parse_args()
    result = {"mac": args.mac, "op": args.op, "ok": False}
    time.sleep(0.2)  # a session takes a while
    try:
        with open(args.state_file) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {"mode": 0, "sessions": []}
    state["sessions"].append(args.op)
    if pins.load(args.pins_file).get(args.mac) != state.get("pin", "123456"):
        result["error"] = "pairing failed: wrong PIN?"
    else:
        if args.op != "read":
            state["mode"] = 2 if args.op == "night-on" else 0
        night = state["mode"] == 2
        result.update(ok=True, mode=state["mode"], night=night, max_current=15.0 if night else 30.0, state=5)
    with open(args.state_file, "w") as f:
        json.dump(state, f)
    sys.stderr.write("fake vesmart: %s %s\n" % (args.mac, args.op))
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
