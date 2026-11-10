import itertools
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flowsim import Packet, walk  # noqa: E402
from slicing import ConfigError, Network, load_network  # noqa: E402
from slicing.rules import (KIND_INGRESS, SLICE_MASK, best_effort_rules,  # noqa: E402
                           slice_cookie, slice_rules)


def specs():
    with open(os.path.join(ROOT, "config", "network.json")) as fh:
        net = json.load(fh)
    with open(os.path.join(ROOT, "config", "slices.json")) as fh:
        sl = json.load(fh)
    return net, sl


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.net = load_network()

    def test_ports_are_unique_per_switch(self):
        by_switch = {}
        for (sw, _n), port in self.net.port.items():
            by_switch.setdefault(sw, []).append(port)
        for sw, ports in by_switch.items():
            self.assertEqual(sorted(ports), list(range(1, len(ports) + 1)), sw)

    def test_uplinks_only_on_edge(self):
        names = {(sw, n) for sw, n, _p, _l in self.net.uplinks()}
        self.assertEqual(names, {("s1", "s2"), ("s1", "s3"), ("s4", "s2"),
                                 ("s4", "s3"), ("s5", "s3")})

    def test_best_effort_prefers_cheap_path(self):
        self.assertEqual(self.net.best_effort_path("s1", "s4"), ["s1", "s2", "s4"])
        self.assertEqual(self.net.best_effort_path("s4", "s5"), ["s4", "s3", "s5"])

    def test_queue_plan(self):
        plan = self.net.queue_plan()
        s1_to_s3 = plan[("s1", self.net.port[("s1", "s3")])]
        owners = {q[3]: q for q in s1_to_s3["queues"]}
        self.assertEqual(set(owners), {"best-effort", "control", "iot", "backup"})
        self.assertEqual(owners["control"][1:3], (3000000, 3000000))
        self.assertEqual(owners["best-effort"][1:3], (1000000, 10000000))
        s1_to_s2 = plan[("s1", self.net.port[("s1", "s2")])]
        self.assertEqual({q[3] for q in s1_to_s2["queues"]}, {"best-effort", "video"})

    def test_admission_control(self):
        self.assertEqual(self.net.admission_check([], "video"), [])
        self.assertEqual(self.net.admission_check(["control"], "iot"), [])
        conflicts = self.net.admission_check(["control", "iot"], "backup")
        self.assertEqual(len(conflicts), 1)
        self.assertIn("B1", conflicts[0])
        self.assertEqual(self.net.admission_check(["control"], "backup"), [])


class ValidationTest(unittest.TestCase):
    def broken(self, mutate):
        net, sl = specs()
        mutate(net, sl)
        with self.assertRaises(ConfigError) as ctx:
            Network(net, sl)
        return "\n".join(ctx.exception.problems)

    def test_path_must_follow_links(self):
        msg = self.broken(lambda n, s: s["video"].__setitem__("path", ["s1", "s4"]))
        self.assertIn("no link between s1 and s4", msg)

    def test_client_must_sit_on_first_switch(self):
        msg = self.broken(lambda n, s: s["video"]["endpoints"].__setitem__("clients", ["h6"]))
        self.assertIn("client h6", msg)

    def test_duplicate_queue(self):
        msg = self.broken(lambda n, s: s["iot"].__setitem__("queue", 1))
        self.assertIn("queue 1 already used", msg)

    def test_share_limit(self):
        msg = self.broken(lambda n, s: s["video"].__setitem__("share", 95))
        self.assertIn("share must be", msg)

    def test_port_needs_transport_protocol(self):
        msg = self.broken(lambda n, s: s["control"]["traffic"].__setitem__("port", 80))
        self.assertIn("requires proto tcp or udp", msg)

    def test_hosts_only_on_edge(self):
        msg = self.broken(lambda n, s: n["hosts"]["h6"].__setitem__("switch", "s3"))
        self.assertIn("edge switches", msg)


class ForwardingTest(unittest.TestCase):
    """Walk packets through the generated flow tables."""

    def setUp(self):
        self.net = load_network()
        self.be = best_effort_rules(self.net)

    def tables(self, *active):
        rules = list(self.be)
        for name in active:
            rules += slice_rules(self.net, name)
        return rules

    def test_full_connectivity_without_slices(self):
        rules = self.tables()
        for a, b in itertools.permutations(self.net.hosts, 2):
            for arp in (False, True):
                _p, _q, got = walk(self.net, rules, Packet(a, b, arp=arp))
                self.assertEqual(got, b, "%s -> %s arp=%s" % (a, b, arp))

    def test_full_connectivity_with_every_admissible_combination(self):
        names = list(self.net.slices)
        for k in range(len(names) + 1):
            for combo in itertools.combinations(names, k):
                if any(self.net.admission_check([c for c in combo if c != n], n) for n in combo):
                    continue
                rules = self.tables(*combo)
                for a, b in itertools.permutations(self.net.hosts, 2):
                    for proto, port in (("icmp", None), ("tcp", 5201), ("udp", 5001)):
                        pkt = Packet(a, b, proto, 40000, port)
                        _p, _q, got = walk(self.net, rules, pkt)
                        self.assertEqual(got, b, "%s %s->%s %s" % (combo, a, b, proto))

    def test_video_slice_pins_udp_on_upper_path_queue_1(self):
        rules = self.tables("video")
        path, queues, _ = walk(self.net, rules, Packet("h1", "h4", "udp", 40000, 5001))
        self.assertEqual(path, ["s1", "s2", "s4"])
        self.assertIn(("s1", 1), queues)
        path, queues, _ = walk(self.net, rules, Packet("h4", "h1", "udp", 5001, 40000))
        self.assertIn(("s4", 1), queues)
        # TCP between the same hosts is not part of the slice
        _p, queues, _ = walk(self.net, rules, Packet("h1", "h4", "tcp", 40000, 5001))
        self.assertIn(("s1", 0), queues)

    def test_control_slice_moves_traffic_to_low_latency_path(self):
        before, _, _ = walk(self.net, self.tables(), Packet("h2", "h5"))
        after, queues, _ = walk(self.net, self.tables("control"), Packet("h2", "h5"))
        self.assertEqual(before, ["s1", "s2", "s4"])
        self.assertEqual(after, ["s1", "s3", "s4"])
        self.assertIn(("s1", 2), queues)

    def test_backup_slice_matches_only_its_port(self):
        rules = self.tables("video", "backup")
        path, queues, _ = walk(self.net, rules, Packet("h1", "h4", "tcp", 40000, 5201))
        self.assertEqual(path, ["s1", "s3", "s4"])
        self.assertIn(("s1", 4), queues)
        path, _q, _ = walk(self.net, rules, Packet("h4", "h1", "tcp", 5201, 40000))
        self.assertEqual(path, ["s4", "s3", "s1"])
        path, _q, _ = walk(self.net, rules, Packet("h1", "h4", "tcp", 40000, 22))
        self.assertEqual(path, ["s1", "s2", "s4"])

    def test_queues_only_on_uplinks(self):
        for name in self.net.slices:
            for r in slice_rules(self.net, name):
                self.assertEqual(r.queue is not None, self.net.is_uplink(r.switch, r.out_port))

    def test_cookies_select_whole_slice(self):
        for name, sl in self.net.slices.items():
            rules = slice_rules(self.net, name)
            self.assertTrue(all(r.cookie & SLICE_MASK == slice_cookie(sl.queue) for r in rules))
            ingress = [r for r in rules if r.cookie & 0xFF == KIND_INGRESS]
            # one measuring rule per direction per client/server pair
            self.assertEqual(len(ingress), 2 * len(sl.clients) * len(sl.servers))


if __name__ == "__main__":
    unittest.main()
