"""Translation of the network model into abstract OpenFlow rules.

A Rule is controller-agnostic: `match` uses the keyword names of Ryu's
OFPMatch, so the Ryu app can install it with `parser.OFPMatch(**rule.match)`.

Cookie layout (64 bit, only the low 16 bits are used):

    bits 15..8  queue id of the slice (0 for best effort)
    bits  7..0  rule kind: 0x01 = ingress rule (the first switch a flow
                meets, used to measure slice throughput), 0x00 = transit,
                0xBE = best-effort routing

so `cookie & SLICE_MASK == queue << 8` selects every rule of a slice.
"""

from collections import namedtuple

ETH_IP = 0x0800
ETH_ARP = 0x0806
IP_PROTO = {"icmp": 1, "tcp": 6, "udp": 17}

PRIO_BEST_EFFORT = 10
PRIO_SLICE_ANY = 100
PRIO_SLICE_PROTO = 200
PRIO_SLICE_PORT = 300

KIND_TRANSIT = 0x00
KIND_INGRESS = 0x01
KIND_BEST_EFFORT = 0xBE
SLICE_MASK = 0xFF00
FULL_MASK = 0xFFFFFFFFFFFFFFFF

Rule = namedtuple("Rule", "switch priority match out_port queue cookie")


def slice_cookie(queue, kind=KIND_TRANSIT):
    return (queue << 8) | kind


def best_effort_rules(net):
    """Destination based least-cost routing for IPv4 and ARP.

    ARP requests are routed towards the target IP instead of being flooded,
    which keeps the looped topology free of broadcast storms."""
    rules = []
    cookie = slice_cookie(0, KIND_BEST_EFFORT)
    for host in net.hosts.values():
        hops = net.best_effort_next_hops(host.switch)
        for sw in net.switches:
            if sw == host.switch:
                out = net.port[(sw, host.name)]
            else:
                out = net.port[(sw, hops[sw])]
            queue = 0 if net.is_uplink(sw, out) else None
            rules.append(Rule(sw, PRIO_BEST_EFFORT,
                              {"eth_type": ETH_IP, "ipv4_dst": host.ip}, out, queue, cookie))
            rules.append(Rule(sw, PRIO_BEST_EFFORT,
                              {"eth_type": ETH_ARP, "arp_tpa": host.ip}, out, None, cookie))
    return rules


def _traffic_match(sl, src_ip, dst_ip, forward):
    match = {"eth_type": ETH_IP, "ipv4_src": src_ip, "ipv4_dst": dst_ip}
    if sl.proto == "any":
        return match, PRIO_SLICE_ANY
    match["ip_proto"] = IP_PROTO[sl.proto]
    if sl.port is None:
        return match, PRIO_SLICE_PROTO
    # the server listens on `port`: it is the destination port on the way
    # to the server and the source port on the way back
    field = "%s_%s" % (sl.proto, "dst" if forward else "src")
    match[field] = int(sl.port)
    return match, PRIO_SLICE_PORT


def _directional_rules(net, sl, src, dst, path, forward):
    rules = []
    for i, sw in enumerate(path):
        if i + 1 < len(path):
            out = net.port[(sw, path[i + 1])]
        else:
            out = net.port[(sw, dst.name)]
        match, prio = _traffic_match(sl, src.ip, dst.ip, forward)
        kind = KIND_INGRESS if i == 0 else KIND_TRANSIT
        queue = sl.queue if net.is_uplink(sw, out) else None
        rules.append(Rule(sw, prio, match, out, queue, slice_cookie(sl.queue, kind)))
    return rules


def slice_rules(net, name):
    """Bidirectional rules pinning the slice traffic to its path and queue."""
    sl = net.slices[name]
    rules = []
    for c in sl.clients:
        for s in sl.servers:
            client, server = net.hosts[c], net.hosts[s]
            rules += _directional_rules(net, sl, client, server, sl.path, True)
            rules += _directional_rules(net, sl, server, client, list(reversed(sl.path)), False)
    return rules


def rules_for_switch(rules, switch):
    return [r for r in rules if r.switch == switch]
