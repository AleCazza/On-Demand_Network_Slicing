"""Tiny OpenFlow table walker used by the tests.

It applies the abstract rules produced by slicing.rules hop by hop, exactly
like a switch would (highest priority match wins), and reports the path and
the egress queues a packet goes through.
"""

from slicing.rules import ETH_ARP, ETH_IP, IP_PROTO


class Packet(object):
    def __init__(self, src, dst, proto="icmp", sport=None, dport=None, arp=False):
        self.src, self.dst = src, dst
        self.proto, self.sport, self.dport, self.arp = proto, sport, dport, arp

    def fields(self, net):
        src, dst = net.hosts[self.src], net.hosts[self.dst]
        if self.arp:
            return {"eth_type": ETH_ARP, "arp_tpa": dst.ip}
        f = {"eth_type": ETH_IP, "ipv4_src": src.ip, "ipv4_dst": dst.ip,
             "ip_proto": IP_PROTO[self.proto]}
        if self.proto in ("tcp", "udp"):
            f["%s_src" % self.proto] = self.sport
            f["%s_dst" % self.proto] = self.dport
        return f


def _matches(match, fields):
    return all(fields.get(k) == v for k, v in match.items())


def walk(net, rules, packet, max_hops=16):
    """Return (switch path, [(switch, queue)], delivered_to or None)."""
    fields = packet.fields(net)
    owner = {}
    for (node, neigh), port in net.port.items():
        owner[(node, port)] = neigh
    sw = net.hosts[packet.src].switch
    path, queues = [], []
    for _ in range(max_hops):
        path.append(sw)
        table = [r for r in rules if r.switch == sw and _matches(r.match, fields)]
        if not table:
            return path, queues, None
        rule = max(table, key=lambda r: r.priority)
        if len([r for r in table if r.priority == rule.priority]) > 1:
            raise AssertionError("ambiguous rules on %s for %r" % (sw, fields))
        queues.append((sw, rule.queue))
        nxt = owner[(sw, rule.out_port)]
        if nxt in net.hosts:
            return path, queues, nxt
        sw = nxt
    raise AssertionError("forwarding loop: %s" % path)
