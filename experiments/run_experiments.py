#!/usr/bin/env python3
"""Automated measurements of the slicing behaviour.

Builds the network, then drives three experiments through the REST API while
iperf/ping run on the emulated hosts. Every experiment switches a slice on
and off in the middle of the traffic, so a single run shows the
"before / during / after" behaviour.

  E1  capacity guarantee  video UDP 15 Mbit/s vs a 20 Mbit/s best-effort flood
  E2  latency             ping h2->h5 while a bulk TCP transfer fills path A
  E3  admission+steering  backup slice rejected, then admitted and measured

Results (CSV + JSON + Markdown summary, plots if matplotlib is installed)
are written to results/<timestamp>/.

Run with:  ./run.sh --experiments      (controller must be running)
"""

import argparse
import csv
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mininet.log import info, setLogLevel  # noqa: E402

from network.topology import start, stop  # noqa: E402
from slicing import load_network  # noqa: E402
from slicing.client import ApiError, SliceClient  # noqa: E402

PHASE = 10  # seconds per phase: off -> on -> off

PING_RE = re.compile(r"\[(\d+\.\d+)\].*time=([\d.]+) ms")
INTERVAL_RE = re.compile(r"^([\d.]+)-([\d.]+)$")


class Run(object):
    """Bookkeeping for background processes started on Mininet hosts."""

    def __init__(self, net, outdir):
        self.net, self.outdir, self.pids = net, outdir, []

    def bg(self, host, cmd, log="/dev/null"):
        pid = self.net[host].cmd("stdbuf -oL %s > %s 2>&1 & echo $!" % (cmd, log)).strip().split()[-1]
        self.pids.append((host, pid))
        return pid

    def stop_all(self):
        for host, pid in self.pids:
            self.net[host].cmd("kill -INT %s 2>/dev/null" % pid)
        time.sleep(1)
        for host, pid in self.pids:
            self.net[host].cmd("kill -9 %s 2>/dev/null" % pid)
        self.pids = []

    def path(self, name):
        return os.path.join(self.outdir, name)


def parse_iperf(path, offset):
    """Per-second receiver throughput from an iperf2 CSV (-y C) report."""
    points = []
    if not os.path.exists(path):
        return points
    with open(path) as fh:
        for row in csv.reader(fh):
            if len(row) < 9:
                continue
            m = INTERVAL_RE.match(row[6])
            if not m:
                continue
            a, b = float(m.group(1)), float(m.group(2))
            if b - a > 1.5:  # final summary line
                continue
            points.append((round(offset + a, 1), float(row[8]) / 1e6))
    return points


def parse_ping(path, t0):
    points = []
    with open(path) as fh:
        for line in fh:
            m = PING_RE.search(line)
            if m:
                points.append((round(float(m.group(1)) - t0, 2), float(m.group(2))))
    return points


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else float("nan")


def phase_means(points, events):
    """Average a time series inside each phase (skipping 2 s of transient)."""
    bounds = [0.0] + [t for t, _ in events] + [3 * PHASE]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        out.append(round(mean(v for t, v in points if a + 2 <= t < b), 2))
    return out


def toggle(client, name, on, t0, events, label):
    (client.activate if on else client.deactivate)(name)
    events.append((round(time.time() - t0, 1), label))
    info("    t=%4.1fs  %s\n" % (events[-1][0], label))


def experiment_guarantee(run, client, hosts):
    info("*** E1: capacity guarantee of the video slice\n")
    client.reset()
    h4 = hosts["h4"].ip
    run.bg("h4", "iperf -s -u -p 5001 -i 1 -y C", run.path("e1_video_srv.csv"))
    run.bg("h4", "iperf -s -u -p 5002 -i 1 -y C", run.path("e1_flood_srv.csv"))
    time.sleep(1)
    t0 = time.time()
    run.bg("h1", "iperf -c %s -u -p 5001 -b 15M -t %d" % (h4, 3 * PHASE))
    run.bg("h3", "iperf -c %s -u -p 5002 -b 20M -t %d" % (h4, 3 * PHASE))
    events = []
    time.sleep(PHASE)
    toggle(client, "video", True, t0, events, "video slice ON")
    time.sleep(PHASE)
    toggle(client, "video", False, t0, events, "video slice OFF")
    time.sleep(PHASE + 2)
    run.stop_all()
    series = {"video (h1->h4, 15 Mbit/s offered)": parse_iperf(run.path("e1_video_srv.csv"), 0),
              "best-effort flood (h3->h4, 20 Mbit/s offered)": parse_iperf(run.path("e1_flood_srv.csv"), 0)}
    return {"events": events, "series": series, "unit": "Mbit/s",
            "phases": {k: phase_means(v, events) for k, v in series.items()}}


def experiment_latency(run, client, hosts):
    info("*** E2: latency of the control slice under load\n")
    client.reset()
    run.bg("h4", "iperf -s -p 5003")
    time.sleep(1)
    run.bg("h3", "iperf -c %s -p 5003 -t %d" % (hosts["h4"].ip, 3 * PHASE + 2))
    time.sleep(2)  # let TCP fill the bottleneck queue
    t0 = time.time()
    run.bg("h2", "ping -D -i 0.2 -w %d %s" % (3 * PHASE, hosts["h5"].ip), run.path("e2_ping.txt"))
    events = []
    time.sleep(PHASE)
    toggle(client, "control", True, t0, events, "control slice ON")
    time.sleep(PHASE)
    toggle(client, "control", False, t0, events, "control slice OFF")
    time.sleep(PHASE + 1)
    run.stop_all()
    series = {"RTT h2->h5": parse_ping(run.path("e2_ping.txt"), t0)}
    return {"events": events, "series": series, "unit": "ms",
            "phases": {k: phase_means(v, events) for k, v in series.items()}}


def experiment_admission(run, client, hosts):
    info("*** E3: admission control and per-service steering\n")
    client.reset()
    log = []

    def attempt(action, name):
        try:
            msg = (client.activate if action == "on" else client.deactivate)(name)["message"]
            log.append({"request": "%s %s" % (action, name), "accepted": True, "message": msg})
        except ApiError as err:
            log.append({"request": "%s %s" % (action, name), "accepted": False, "message": str(err)})
        info("    %-12s -> %s\n" % (log[-1]["request"], "accepted" if log[-1]["accepted"] else "REJECTED"))

    attempt("on", "control")
    attempt("on", "iot")
    attempt("on", "backup")   # B1 would exceed its budget
    attempt("off", "iot")
    attempt("on", "backup")   # now it fits

    h4 = hosts["h4"].ip
    run.bg("h4", "iperf -s -p 5201 -i 1 -y C", run.path("e3_backup_srv.csv"))
    run.bg("h4", "iperf -s -p 5003 -i 1 -y C", run.path("e3_other_srv.csv"))
    time.sleep(1)
    run.bg("h1", "iperf -c %s -p 5201 -t %d" % (h4, PHASE))
    run.bg("h1", "iperf -c %s -p 5003 -t %d" % (h4, PHASE))
    time.sleep(PHASE + 2)
    run.stop_all()
    client.reset()
    flows = {
        "TCP :5201 backup slice (path s1-s3-s4, 40% of 10 Mbit/s)": parse_iperf(run.path("e3_backup_srv.csv"), 0),
        "TCP :5003 best effort (path s1-s2-s4, 20 Mbit/s)": parse_iperf(run.path("e3_other_srv.csv"), 0),
    }
    return {"requests": log, "unit": "Mbit/s",
            "throughput": {k: round(mean(v for t, v in pts if t >= 2), 2) for k, pts in flows.items()},
            "series": flows}


def write_summary(outdir, results):
    with open(os.path.join(outdir, "results.json"), "w") as fh:
        json.dump(results, fh, indent=2)
    lines = ["# Experiment results", "", "Generated %s" % time.strftime("%Y-%m-%d %H:%M"), ""]
    lines += ["## E1 - capacity guarantee (Mbit/s received)", "",
              "| flow | slice off | slice on | slice off again |", "|---|---|---|---|"]
    for k, v in results["E1"]["phases"].items():
        lines.append("| %s | %s | %s | %s |" % ((k,) + tuple(v)))
    lines += ["", "## E2 - control slice latency (mean RTT, ms)", "",
              "| measure | slice off | slice on | slice off again |", "|---|---|---|---|"]
    for k, v in results["E2"]["phases"].items():
        lines.append("| %s | %s | %s | %s |" % ((k,) + tuple(v)))
    lines += ["", "## E3 - admission control", "", "| request | result | controller message |", "|---|---|---|"]
    for r in results["E3"]["requests"]:
        lines.append("| %s | %s | %s |" % (r["request"], "accepted" if r["accepted"] else "**rejected**", r["message"]))
    lines += ["", "| flow h1->h4 | mean throughput (Mbit/s) |", "|---|---|"]
    for k, v in results["E3"]["throughput"].items():
        lines.append("| %s | %s |" % (k, v))
    with open(os.path.join(outdir, "summary.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Automated slicing experiments")
    parser.add_argument("--config", help="directory with network.json and slices.json")
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--out", default=os.path.join(ROOT, "results"))
    args = parser.parse_args()

    setLogLevel("info")
    model = load_network(args.config)
    outdir = os.path.join(args.out, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(outdir)
    client = SliceClient(args.api)
    net = start(model)
    run = Run(net, outdir)
    try:
        time.sleep(2)
        info("*** Connectivity check\n")
        loss = net.pingAll(timeout="1")
        results = {"pingall_loss": loss,
                   "E1": experiment_guarantee(run, client, model.hosts),
                   "E2": experiment_latency(run, client, model.hosts),
                   "E3": experiment_admission(run, client, model.hosts)}
        write_summary(outdir, results)
        info("*** Results written to %s\n" % outdir)
    finally:
        run.stop_all()
        stop(net)
    try:
        from experiments.plot_results import plot_all
        plot_all(outdir)
        info("*** Plots saved in %s\n" % outdir)
    except ImportError:
        info("*** matplotlib not available: run experiments/plot_results.py %s on any machine\n" % outdir)
    give_back(args.out)


def give_back(path):
    """Files are created as root under sudo: hand them back to the real user."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not (uid and gid):
        return
    for base, _dirs, files in os.walk(path):
        for name in [base] + [os.path.join(base, f) for f in files]:
            os.chown(name, int(uid), int(gid))


if __name__ == "__main__":
    main()
