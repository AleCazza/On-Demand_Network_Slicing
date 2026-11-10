"""Network and slice model.

Everything that both the data plane (Mininet) and the control plane (Ryu)
must agree on is derived here from the two JSON templates:

  * switch port numbering (so no MAC/port table is ever written by hand),
  * which ports are edge uplinks (where slice queues live),
  * best-effort routing (least-cost paths),
  * per-port queue plan and admission control for slices.

The module has no dependency on Ryu or Mininet, so it can be unit tested
on any Python >= 3.6 machine.
"""

import heapq
import json
import os
from collections import OrderedDict

PROTOCOLS = ("any", "tcp", "udp", "icmp")
MAX_QUEUE_ID = 7


class ConfigError(ValueError):
    """Raised when a template is inconsistent. Collects every problem found."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("invalid configuration:\n  - " + "\n  - ".join(self.problems))


class Switch(object):
    def __init__(self, name, dpid, role, pos=None):
        self.name = name
        self.dpid = int(dpid)
        self.role = role
        self.pos = pos or [0, 0]

    @property
    def is_edge(self):
        return self.role == "edge"


class Host(object):
    def __init__(self, name, switch, ip, role="", pos=None):
        self.name = name
        self.switch = switch
        self.ip = ip
        self.role = role
        self.pos = pos or [0, 0]

    @property
    def mac(self):
        last = int(self.ip.split(".")[-1])
        return "00:00:00:00:00:%02x" % last


class Link(object):
    def __init__(self, link_id, a, b, bw, delay, cost=1):
        self.id = link_id
        self.a = a
        self.b = b
        self.bw = float(bw)        # Mbit/s
        self.delay = float(delay)  # ms, applied on the core side of the link
        self.cost = float(cost)

    @property
    def ends(self):
        return (self.a, self.b)

    def other(self, node):
        return self.b if node == self.a else self.a


class Slice(object):
    def __init__(self, name, spec):
        self.name = name
        self.label = spec.get("label", name)
        self.type = spec.get("type", "")
        self.queue = spec.get("queue")
        self.color = spec.get("color", "#888888")
        endpoints = spec.get("endpoints", {})
        self.clients = list(endpoints.get("clients", []))
        self.servers = list(endpoints.get("servers", []))
        traffic = spec.get("traffic", {})
        self.proto = traffic.get("proto", "any")
        self.port = traffic.get("port")
        self.path = list(spec.get("path", []))
        self.share = spec.get("share")

    def describe_traffic(self):
        if self.proto == "any":
            return "all IP traffic"
        if self.port:
            return "%s port %d" % (self.proto.upper(), self.port)
        return self.proto.upper()

    def to_dict(self):
        return OrderedDict([
            ("name", self.name), ("label", self.label), ("type", self.type),
            ("queue", self.queue), ("color", self.color),
            ("clients", self.clients), ("servers", self.servers),
            ("proto", self.proto), ("port", self.port),
            ("traffic", self.describe_traffic()),
            ("path", self.path), ("share", self.share),
        ])


class Network(object):
    """Validated view of the infrastructure plus the slice catalogue."""

    def __init__(self, net_spec, slice_spec):
        self.controller = net_spec.get("controller", {"ip": "127.0.0.1", "port": 6633})
        self.be_min_share = float(net_spec.get("best_effort", {}).get("min_share", 10))

        self.switches = OrderedDict(
            (name, Switch(name, s["dpid"], s.get("role", "core"), s.get("pos")))
            for name, s in net_spec["switches"].items())
        self.hosts = OrderedDict(
            (name, Host(name, h["switch"], h["ip"], h.get("role", ""), h.get("pos")))
            for name, h in net_spec["hosts"].items())
        self.links = [Link(l["id"], l["ends"][0], l["ends"][1], l["bw"],
                           l.get("delay", 0), l.get("cost", 1))
                      for l in net_spec["links"]]
        self.slices = OrderedDict(
            (name, Slice(name, spec)) for name, spec in slice_spec.items()
            if not name.startswith("_"))

        self._validate_infrastructure()
        self._assign_ports()
        self._validate_slices()

    # ------------------------------------------------------------------ build

    def _validate_infrastructure(self):
        problems = []
        dpids = [s.dpid for s in self.switches.values()]
        if len(set(dpids)) != len(dpids):
            problems.append("switch dpids must be unique")
        for sw in self.switches.values():
            if sw.role not in ("edge", "core"):
                problems.append("switch %s: role must be 'edge' or 'core'" % sw.name)
        ips = [h.ip for h in self.hosts.values()]
        if len(set(ips)) != len(ips):
            problems.append("host IP addresses must be unique")
        for h in self.hosts.values():
            if h.switch not in self.switches:
                problems.append("host %s: unknown switch %s" % (h.name, h.switch))
            elif not self.switches[h.switch].is_edge:
                problems.append("host %s: hosts may only attach to edge switches" % h.name)
        seen = set()
        for link in self.links:
            for end in link.ends:
                if end not in self.switches:
                    problems.append("link %s: unknown switch %s" % (link.id, end))
            key = frozenset(link.ends)
            if key in seen:
                problems.append("link %s: duplicate link between %s" % (link.id, "-".join(link.ends)))
            seen.add(key)
            if link.bw <= 0:
                problems.append("link %s: bw must be positive" % link.id)
        if problems:
            raise ConfigError(problems)

    def _assign_ports(self):
        """Number switch ports deterministically: switch links first (in file
        order), then hosts (in file order). Mininet is told to use exactly these
        numbers, so interface sX-ethN is always OpenFlow port N."""
        next_port = dict((name, 1) for name in self.switches)
        self.port = {}  # (switch, neighbour) -> port on switch
        for link in self.links:
            for node in link.ends:
                self.port[(node, link.other(node))] = next_port[node]
                next_port[node] += 1
        for host in self.hosts.values():
            self.port[(host.switch, host.name)] = next_port[host.switch]
            next_port[host.switch] += 1

        self.neighbours = dict((name, []) for name in self.switches)
        self._link_by_ends = {}
        for link in self.links:
            self.neighbours[link.a].append(link.b)
            self.neighbours[link.b].append(link.a)
            self._link_by_ends[frozenset(link.ends)] = link

    def _validate_slices(self):
        problems = []
        queues = {}
        limit = 100.0 - self.be_min_share
        for sl in self.slices.values():
            where = "slice %s" % sl.name
            if not isinstance(sl.queue, int) or not 1 <= sl.queue <= MAX_QUEUE_ID:
                problems.append("%s: queue must be an integer in 1..%d" % (where, MAX_QUEUE_ID))
            elif sl.queue in queues:
                problems.append("%s: queue %d already used by %s" % (where, sl.queue, queues[sl.queue]))
            else:
                queues[sl.queue] = sl.name
            if not isinstance(sl.share, (int, float)) or not 0 < sl.share <= limit:
                problems.append("%s: share must be in (0, %g] percent" % (where, limit))
            if sl.proto not in PROTOCOLS:
                problems.append("%s: proto must be one of %s" % (where, ", ".join(PROTOCOLS)))
            if sl.port is not None and sl.proto not in ("tcp", "udp"):
                problems.append("%s: a port requires proto tcp or udp" % where)
            if len(sl.path) < 2:
                problems.append("%s: path needs at least two switches" % where)
                continue
            for a, b in zip(sl.path, sl.path[1:]):
                if frozenset((a, b)) not in self._link_by_ends:
                    problems.append("%s: no link between %s and %s" % (where, a, b))
            if len(set(sl.path)) != len(sl.path):
                problems.append("%s: path must not contain loops" % where)
            if not sl.clients or not sl.servers:
                problems.append("%s: needs at least one client and one server" % where)
            for h in sl.clients:
                if h not in self.hosts or self.hosts[h].switch != sl.path[0]:
                    problems.append("%s: client %s must be attached to %s" % (where, h, sl.path[0]))
            for h in sl.servers:
                if h not in self.hosts or self.hosts[h].switch != sl.path[-1]:
                    problems.append("%s: server %s must be attached to %s" % (where, h, sl.path[-1]))
        if problems:
            raise ConfigError(problems)

    # ---------------------------------------------------------------- queries

    def link(self, a, b):
        return self._link_by_ends[frozenset((a, b))]

    def switch_by_dpid(self, dpid):
        for sw in self.switches.values():
            if sw.dpid == dpid:
                return sw
        return None

    def host_by_ip(self, ip):
        for h in self.hosts.values():
            if h.ip == ip:
                return h
        return None

    def is_uplink(self, switch, port):
        """True for an edge-switch port facing another switch: these ports
        carry the HTB queues that enforce slice capacity."""
        if not self.switches[switch].is_edge:
            return False
        return any(self.port.get((switch, n)) == port for n in self.neighbours[switch])

    def uplinks(self):
        """Yield (switch, neighbour, port, link) for every edge uplink."""
        for sw in self.switches.values():
            if not sw.is_edge:
                continue
            for n in self.neighbours[sw.name]:
                yield sw.name, n, self.port[(sw.name, n)], self.link(sw.name, n)

    def core_ports(self):
        """Yield (switch, neighbour, port, link) for every core-switch port
        facing another switch: netem emulates capacity and delay there."""
        for sw in self.switches.values():
            if sw.is_edge:
                continue
            for n in self.neighbours[sw.name]:
                yield sw.name, n, self.port[(sw.name, n)], self.link(sw.name, n)

    def best_effort_next_hops(self, dst_switch):
        """Least-cost next hop from every switch towards dst_switch (Dijkstra
        run from the destination; ties broken by switch name for determinism)."""
        dist = {dst_switch: 0.0}
        next_hop = {}
        heap = [(0.0, dst_switch)]
        while heap:
            d, node = heapq.heappop(heap)
            if d > dist.get(node, float("inf")):
                continue
            for n in sorted(self.neighbours[node]):
                nd = d + self.link(node, n).cost
                if nd < dist.get(n, float("inf")):
                    dist[n] = nd
                    next_hop[n] = node
                    heapq.heappush(heap, (nd, n))
        return next_hop

    def best_effort_path(self, src_switch, dst_switch):
        hops = self.best_effort_next_hops(dst_switch)
        path = [src_switch]
        while path[-1] != dst_switch:
            path.append(hops[path[-1]])
        return path

    # ------------------------------------------------------- capacity / queues

    def slice_links(self, sl):
        return [self.link(a, b) for a, b in zip(sl.path, sl.path[1:])]

    def queue_plan(self):
        """For every edge uplink return the HTB configuration:
        {(switch, port): {"link": Link, "queues": [(qid, min_bps, max_bps, owner)]}}

        Queue 0 is best effort (guaranteed min_share, may borrow up to the full
        link). Each slice crossing the link gets a queue with min = max = its
        share, i.e. a guaranteed and capped portion of the link."""
        plan = OrderedDict()
        for sw, _n, port, link in self.uplinks():
            cap = link.bw * 1e6
            queues = [(0, int(cap * self.be_min_share / 100.0), int(cap), "best-effort")]
            for sl in self.slices.values():
                if link in self.slice_links(sl):
                    rate = int(cap * sl.share / 100.0)
                    queues.append((sl.queue, rate, rate, sl.name))
            plan[(sw, port)] = {"link": link, "queues": queues}
        return plan

    def link_load(self, active):
        """Percentage of every link reserved by the given set of slice names."""
        load = OrderedDict((l.id, 0.0) for l in self.links)
        for name in active:
            sl = self.slices[name]
            for link in self.slice_links(sl):
                load[link.id] += sl.share
        return load

    def admission_check(self, active, candidate):
        """Return a list of human readable conflicts that prevent `candidate`
        from being activated on top of `active` (empty list = admitted)."""
        limit = 100.0 - self.be_min_share
        load = self.link_load(set(active) | {candidate})
        conflicts = []
        for link in self.slice_links(self.slices[candidate]):
            if load[link.id] > limit + 1e-9:
                users = [n for n in active if link in self.slice_links(self.slices[n])]
                conflicts.append(
                    "link %s (%s) would be %g%% reserved (max %g%%), already used by %s"
                    % (link.id, "-".join(link.ends), load[link.id], limit, ", ".join(users)))
        return conflicts

    # ----------------------------------------------------------------- export

    def topology_dict(self):
        return OrderedDict([
            ("switches", [OrderedDict([("name", s.name), ("dpid", s.dpid), ("role", s.role),
                                       ("pos", s.pos)]) for s in self.switches.values()]),
            ("hosts", [OrderedDict([("name", h.name), ("ip", h.ip), ("switch", h.switch),
                                    ("role", h.role), ("pos", h.pos)]) for h in self.hosts.values()]),
            ("links", [OrderedDict([("id", l.id), ("ends", list(l.ends)), ("bw", l.bw),
                                    ("delay", l.delay)]) for l in self.links]),
            ("best_effort_min_share", self.be_min_share),
        ])


def _read_json(path):
    with open(path) as fh:
        return json.load(fh, object_pairs_hook=OrderedDict)


def default_config_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")


def load_network(config_dir=None):
    config_dir = config_dir or os.environ.get("SLICING_CONFIG") or default_config_dir()
    return Network(_read_json(os.path.join(config_dir, "network.json")),
                   _read_json(os.path.join(config_dir, "slices.json")))
