"""Drives the Ryu app against fake datapaths and replays the FlowMods it sends
into per-switch tables, then checks forwarding with the table walker."""

import json
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_ryu  # noqa: E402

WSGI = fake_ryu.install()

from controller import slice_controller as sc  # noqa: E402
from flowsim import Packet, walk  # noqa: E402
from slicing.rules import Rule  # noqa: E402


def replay(app):
    """Apply every FlowMod sent so far (adds and cookie-masked deletes)."""
    rules = []
    for name, dp in app.datapaths.items():
        table = []
        for msg in dp.sent:
            if not isinstance(msg, fake_ryu.OFPFlowMod):
                continue
            if msg.kw.get("command") == fake_ryu.ofproto.OFPFC_DELETE:
                mask, cookie = msg.cookie_mask, msg.cookie
                table = [r for r in table if (r.cookie & mask) != (cookie & mask)]
                continue
            actions = msg.instructions[0].actions if msg.instructions else []
            out = [a.port for a in actions if isinstance(a, fake_ryu.OFPActionOutput)]
            queue = [a.queue_id for a in actions if isinstance(a, fake_ryu.OFPActionSetQueue)]
            if not out:
                continue  # table-miss drop
            table.append(Rule(name, msg.priority, msg.match.fields, out[0],
                              queue[0] if queue else None, msg.cookie))
        rules += table
    return rules


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.app = sc.SliceController(wsgi=WSGI())
        for sw in self.app.net.switches.values():
            dp = fake_ryu.FakeDatapath(sw.dpid)
            ev = types.SimpleNamespace(msg=types.SimpleNamespace(datapath=dp))
            self.app.switch_features_handler(ev)

    def path(self, *pkt):
        p, q, got = walk(self.app.net, replay(self.app), Packet(*pkt))
        self.assertEqual(got, pkt[1])
        return p, q

    def test_ryu_internal_attributes_untouched(self):
        # Ryu delivers OpenFlow events through self.events.put(): an app that
        # reuses that name breaks every switch connection
        self.assertTrue(hasattr(self.app.events, "put"))
        self.assertTrue(self.app.is_active)

    def test_lifecycle(self):
        self.assertEqual(self.path("h2", "h5")[0], ["s1", "s2", "s4"])
        self.app.activate("control")
        path, queues = self.path("h2", "h5")
        self.assertEqual(path, ["s1", "s3", "s4"])
        self.assertIn(("s1", 2), queues)
        self.app.deactivate("control")
        self.assertEqual(self.path("h2", "h5")[0], ["s1", "s2", "s4"])

    def test_deactivate_only_removes_own_rules(self):
        self.app.activate("video")
        self.app.activate("backup")
        self.app.deactivate("video")
        self.assertEqual(self.path("h1", "h4", "tcp", 40000, 5201)[0], ["s1", "s3", "s4"])
        _p, q = self.path("h1", "h4", "udp", 40000, 5001)
        self.assertIn(("s1", 0), q)

    def test_admission_rejected(self):
        self.app.activate("control")
        self.app.activate("iot")
        with self.assertRaises(ValueError):
            self.app.activate("backup")
        self.assertEqual(self.app.active, ["control", "iot"])

    def test_reconnect_reinstalls_active_slices(self):
        self.app.activate("iot")
        dp = fake_ryu.FakeDatapath(1)
        self.app.switch_features_handler(types.SimpleNamespace(msg=types.SimpleNamespace(datapath=dp)))
        path, queues = self.path("h3", "h6")
        self.assertEqual(path, ["s1", "s3", "s5"])
        self.assertIn(("s1", 3), queues)

    def test_slice_throughput_from_flow_stats(self):
        self.app.activate("video")
        dp = self.app.datapaths["s1"]

        def stats(byte_count, now):
            body = [types.SimpleNamespace(
                cookie=(1 << 8) | 1, byte_count=byte_count,
                match=fake_ryu.OFPMatch(eth_type=0x800, ipv4_src="10.0.0.1",
                                        ipv4_dst="10.0.0.4", ip_proto=17))]
            sc.time.time = lambda: now
            self.app.flow_stats_handler(types.SimpleNamespace(
                msg=types.SimpleNamespace(datapath=dp, body=body)))

        real_time = sc.time.time
        try:
            stats(0, 100.0)
            stats(1500000, 101.0)  # 1.5 MB in one second = 12 Mbit/s
        finally:
            sc.time.time = real_time
        self.assertAlmostEqual(self.app.slice_rate["video"], 12.0)

    def test_rest_api(self):
        api = sc.SliceRestApi(None, None, {sc.API_INSTANCE: self.app})
        body = json.loads(api.get_slices(None).body)
        self.assertEqual([s["name"] for s in body["slices"]], ["video", "control", "iot", "backup"])
        self.assertEqual(api.post_activate(None, name="nope").status, 404)
        self.assertEqual(api.post_activate(None, name="control").status, 200)
        api.post_activate(None, name="iot")
        resp = api.post_activate(None, name="backup")
        self.assertEqual(resp.status, 409)
        self.assertIn("B1", json.loads(resp.body)["error"])
        topo = json.loads(api.get_topology(None).body)
        self.assertEqual(len(topo["links"]), 5)


if __name__ == "__main__":
    unittest.main()
