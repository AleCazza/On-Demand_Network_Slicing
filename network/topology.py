#!/usr/bin/env python3
"""Data plane: builds the emulated network from config/network.json.

Two kinds of switches are provisioned differently, mirroring the split
between provider-edge and core routers in an operator network:

  * core switches (s2, s3): every port towards another switch gets a netem
    qdisc that emulates the link capacity and its propagation delay;
  * edge switches (s1, s4, s5): every uplink gets an OVS linux-htb QoS with
    one queue for best effort plus one queue per slice crossing that link.
    The controller steers slice flows into those queues with set_queue.

Run with:  sudo python3 network/topology.py   (controller must be running)
"""

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mininet.cli import CLI  # noqa: E402
from mininet.log import error, info, setLogLevel  # noqa: E402
from mininet.net import Mininet  # noqa: E402
from mininet.node import OVSKernelSwitch, RemoteController  # noqa: E402
from mininet.topo import Topo  # noqa: E402

from slicing import load_network  # noqa: E402
from slicing.client import ApiError, SliceClient  # noqa: E402


class SlicingTopo(Topo):
    """Mininet topology generated from the shared model. Port numbers are
    passed explicitly so that sX-ethN is OpenFlow port N on every run."""

    def build(self, model):
        for sw in model.switches.values():
            self.addSwitch(sw.name, dpid="%016x" % sw.dpid, protocols="OpenFlow13")
        for host in model.hosts.values():
            self.addHost(host.name, ip=host.ip + "/24", mac=host.mac)
        for link in model.links:
            self.addLink(link.a, link.b,
                         port1=model.port[(link.a, link.b)],
                         port2=model.port[(link.b, link.a)])
        for host in model.hosts.values():
            self.addLink(host.name, host.switch,
                         port1=0, port2=model.port[(host.switch, host.name)])


def intf(switch, port):
    return "%s-eth%d" % (switch, port)


def run(cmd):
    """Run a provisioning command, failing loudly: a silently missing queue
    would make every slice measurement meaningless."""
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            universal_newlines=True)
    if result.returncode != 0:
        raise RuntimeError("command failed: %s\n%s" % (" ".join(cmd), result.stdout))
    return result.stdout


def clear_qos_records():
    run(["ovs-vsctl", "--all", "destroy", "qos", "--", "--all", "destroy", "queue"])


def provision_core(model):
    for sw, _n, port, link in model.core_ports():
        dev = intf(sw, port)
        run(["tc", "qdisc", "replace", "dev", dev, "root", "netem",
             "delay", "%gms" % link.delay, "rate", "%gmbit" % link.bw, "limit", "1000"])
        info("  %-8s netem  %5g Mbit/s  %4g ms   (link %s)\n" % (dev, link.bw, link.delay, link.id))


def provision_edge(model):
    for (sw, port), entry in model.queue_plan().items():
        dev = intf(sw, port)
        cap = int(entry["link"].bw * 1e6)
        cmd = ["ovs-vsctl", "--", "set", "port", dev, "qos=@qos",
               "--", "--id=@qos", "create", "qos", "type=linux-htb",
               "other-config:max-rate=%d" % cap]
        cmd += ["queues:%d=@q%d" % (qid, qid) for qid, _mn, _mx, _o in entry["queues"]]
        for qid, min_rate, max_rate, _owner in entry["queues"]:
            cmd += ["--", "--id=@q%d" % qid, "create", "queue",
                    "other-config:min-rate=%d" % min_rate,
                    "other-config:max-rate=%d" % max_rate]
        run(cmd)
        desc = ", ".join("q%d %s %.1f-%.1f" % (q, o, mn / 1e6, mx / 1e6)
                         for q, mn, mx, o in entry["queues"])
        info("  %-8s htb    %5g Mbit/s  [%s]\n" % (dev, cap / 1e6, desc))


class SlicingCLI(CLI):
    """Mininet CLI with an extra `slice` command talking to the controller."""

    prompt = "slicing> "

    def __init__(self, net, client, **kwargs):
        self.client = client
        CLI.__init__(self, net, **kwargs)

    def do_slice(self, line):
        """slice list | slice on NAME | slice off NAME | slice reset | slice stats"""
        args = line.split()
        try:
            if not args or args[0] == "list":
                for s in self.client.slices():
                    state = "ON " if s["active"] else "off"
                    print("  [%s] %-8s %-6s %-22s %s  %s%%  %s<->%s" % (
                        state, s["name"], s["type"], s["traffic"], "-".join(s["path"]),
                        s["share"], ",".join(s["clients"]), ",".join(s["servers"])))
            elif args[0] in ("on", "off") and len(args) == 2:
                call = self.client.activate if args[0] == "on" else self.client.deactivate
                print("  " + call(args[1])["message"])
            elif args[0] == "reset":
                print("  " + self.client.reset()["message"])
            elif args[0] == "stats":
                for name, mbps in sorted(self.client.stats()["slices"].items()):
                    print("  %-8s %6.2f Mbit/s" % (name, mbps))
            else:
                print(self.do_slice.__doc__)
        except ApiError as err:
            print("  error: %s" % err)


def build(model):
    ctrl = model.controller
    net = Mininet(topo=SlicingTopo(model), switch=OVSKernelSwitch, controller=None,
                  autoSetMacs=False, autoStaticArp=True)
    net.addController(RemoteController("c0", ip=ctrl["ip"], port=int(ctrl["port"])))
    return net


def start(model):
    if os.geteuid() != 0:
        sys.exit("Mininet needs root: run with sudo")
    clear_qos_records()
    net = build(model)
    net.start()
    info("*** Provisioning core links (capacity + delay)\n")
    provision_core(model)
    info("*** Provisioning edge uplinks (slice queues)\n")
    provision_edge(model)
    info("*** Waiting for switches to connect to the controller\n")
    if hasattr(net, "waitConnected") and not net.waitConnected(timeout=15):
        error("!!! some switches did not connect: is the controller running?\n")
    return net


def stop(net):
    net.stop()
    try:
        clear_qos_records()
    except RuntimeError as err:
        error("%s\n" % err)


def main():
    parser = argparse.ArgumentParser(description="On-demand slicing data plane")
    parser.add_argument("--config", help="directory with network.json and slices.json")
    parser.add_argument("--api", default="http://127.0.0.1:8080", help="controller REST API")
    args = parser.parse_args()

    setLogLevel("info")
    model = load_network(args.config)
    net = start(model)
    info("*** Ready. GUI: %s   |   try: slice list, slice on video, pingall\n" % args.api)
    try:
        SlicingCLI(net, SliceClient(args.api))
    finally:
        stop(net)


if __name__ == "__main__":
    main()
