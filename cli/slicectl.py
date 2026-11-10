#!/usr/bin/env python3
"""Command line client for the slicing controller.

    slicectl.py list
    slicectl.py on video          slicectl.py off video
    slicectl.py reset
    slicectl.py stats [--watch]
    slicectl.py status
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicing.client import DEFAULT_URL, ApiError, SliceClient  # noqa: E402


def cmd_list(client, _args):
    print("%-4s %-8s %-6s %-20s %-10s %5s  %s" % ("", "SLICE", "TYPE", "TRAFFIC", "PATH", "SHARE", "ENDPOINTS"))
    for s in client.slices():
        print("%-4s %-8s %-6s %-20s %-10s %4g%%  %s <-> %s" % (
            "ON" if s["active"] else "-", s["name"], s["type"], s["traffic"],
            "-".join(s["path"]), s["share"], ",".join(s["clients"]), ",".join(s["servers"])))


def cmd_on(client, args):
    for name in args.names:
        print(client.activate(name)["message"])


def cmd_off(client, args):
    for name in args.names:
        print(client.deactivate(name)["message"])


def cmd_reset(client, _args):
    print(client.reset()["message"])


def cmd_stats(client, args):
    while True:
        data = client.stats()
        line = "  ".join("%s %6.2f" % (n, v) for n, v in sorted(data["slices"].items()))
        links = "  ".join("%s %3.0f%%" % (l, v["utilisation"]) for l, v in sorted(data["links"].items()))
        print("%s | slices Mbit/s: %s | links: %s" % (time.strftime("%H:%M:%S"), line, links))
        if not args.watch:
            return
        time.sleep(1)


def cmd_status(client, _args):
    st = client.status()
    missing = sorted(set(st["expected_switches"]) - set(st["switches"]))
    print("uptime %.0fs, switches connected: %s%s" % (
        st["uptime"], ", ".join(st["switches"]) or "none",
        ("  (missing: %s)" % ", ".join(missing)) if missing else ""))
    print("active slices: %s" % (", ".join(st["active"]) or "none"))
    for ev in st["events"][-10:]:
        print("  %s %s %s" % (time.strftime("%H:%M:%S", time.localtime(ev["t"])),
                              "!" if ev["level"] == "error" else " ", ev["msg"]))


def main():
    parser = argparse.ArgumentParser(description="On-demand slice control")
    parser.add_argument("--api", default=DEFAULT_URL, help="controller REST endpoint")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="show slice catalogue and state")
    for name in ("on", "off"):
        p = sub.add_parser(name, help="%s one or more slices" % ("activate" if name == "on" else "deactivate"))
        p.add_argument("names", nargs="+")
    sub.add_parser("reset", help="deactivate every slice")
    p = sub.add_parser("stats", help="per-slice throughput and link utilisation")
    p.add_argument("--watch", action="store_true", help="refresh every second")
    sub.add_parser("status", help="controller status and recent events")
    args = parser.parse_args()

    handlers = {"list": cmd_list, "on": cmd_on, "off": cmd_off, "reset": cmd_reset,
                "stats": cmd_stats, "status": cmd_status}
    client = SliceClient(args.api)
    try:
        handlers.get(args.command or "list")(client, args)
    except ApiError as err:
        sys.exit("error: %s" % err)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
