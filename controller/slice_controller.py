"""Control plane: Ryu application for on-demand network slicing.

Responsibilities
  * proactive best-effort routing for every host (no packet-in, no flooding)
  * activation / deactivation of slices at runtime, with admission control
  * northbound REST API and static web GUI (port 8080)
  * throughput monitoring per slice (flow stats) and per link (port stats)

Run with:  ryu-manager controller/slice_controller.py   (see run.sh)
"""

import json
import os
import sys
import time
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ryu.app.wsgi import ControllerBase, Response, WSGIApplication, route  # noqa: E402
from ryu.base import app_manager  # noqa: E402
from ryu.controller import ofp_event  # noqa: E402
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,  # noqa: E402
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub  # noqa: E402
from ryu.ofproto import ofproto_v1_3  # noqa: E402
from webob.static import DirectoryApp  # noqa: E402

from slicing import load_network  # noqa: E402
from slicing.rules import (KIND_INGRESS, SLICE_MASK, best_effort_rules,  # noqa: E402
                           rules_for_switch, slice_cookie, slice_rules)

API_INSTANCE = "slicing_app"
GUI_DIR = os.path.join(ROOT, "gui")
POLL_INTERVAL = 1.0


class RateMeter(object):
    """Turns monotonically increasing byte counters into Mbit/s."""

    def __init__(self):
        self._last = {}

    def update(self, key, byte_count, now):
        prev = self._last.get(key)
        self._last[key] = (byte_count, now)
        if prev is None or byte_count < prev[0] or now <= prev[1]:
            return 0.0
        return (byte_count - prev[0]) * 8.0 / (now - prev[1]) / 1e6

    def forget(self, predicate):
        for key in [k for k in self._last if predicate(k)]:
            del self._last[key]


class SliceController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(SliceController, self).__init__(*args, **kwargs)
        self.net = load_network()
        self.best_effort = best_effort_rules(self.net)
        self.slice_rules = dict((name, slice_rules(self.net, name)) for name in self.net.slices)
        self.active = []            # activation order is kept for the GUI
        self.datapaths = {}         # switch name -> datapath
        self.event_log = deque(maxlen=60)  # note: RyuApp already owns self.events
        self.started = time.time()

        self.flow_meter = RateMeter()
        self.port_meter = RateMeter()
        self.slice_rate = dict((name, 0.0) for name in self.net.slices)
        self._ingress_rate = {}     # (switch, cookie, src, dst) -> Mbit/s
        self.port_rate = {}         # (switch, port) -> Mbit/s transmitted

        self._queue_owner = dict((sl.queue, name) for name, sl in self.net.slices.items())
        self._port_peer = dict(((sw, port), peer) for (sw, peer), port in self.net.port.items())

        kwargs["wsgi"].register(SliceRestApi, {API_INSTANCE: self})
        self.monitor = hub.spawn(self._monitor_loop)
        self.log_event("controller started with %d slice templates: %s"
                       % (len(self.net.slices), ", ".join(self.net.slices)))

    # ------------------------------------------------------------- helpers

    def log_event(self, message, level="info"):
        self.event_log.append({"t": time.time(), "level": level, "msg": message})
        getattr(self.logger, "warning" if level == "error" else "info")(message)

    def _flow_mod(self, dp, rule):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        actions = []
        if rule.queue is not None:
            actions.append(parser.OFPActionSetQueue(rule.queue))
        actions.append(parser.OFPActionOutput(rule.out_port))
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, cookie=rule.cookie, priority=rule.priority,
            match=parser.OFPMatch(**rule.match), instructions=inst,
            idle_timeout=0, hard_timeout=0))

    def _delete_by_cookie(self, dp, cookie, mask):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, cookie=cookie, cookie_mask=mask, table_id=ofp.OFPTT_ALL,
            command=ofp.OFPFC_DELETE, out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
            match=parser.OFPMatch()))

    def _install(self, rules):
        # downstream switches first, so a flow never reaches a switch that
        # does not know the slice yet
        for rule in reversed(rules):
            dp = self.datapaths.get(rule.switch)
            if dp is not None:
                self._flow_mod(dp, rule)

    # ------------------------------------------------------ slice lifecycle

    def slice_state(self, name):
        sl = self.net.slices[name]
        data = sl.to_dict()
        data["active"] = name in self.active
        data["throughput"] = round(self.slice_rate.get(name, 0.0), 3)
        data["links"] = [l.id for l in self.net.slice_links(sl)]
        data["capacity_mbps"] = round(min(l.bw for l in self.net.slice_links(sl)) * sl.share / 100.0, 2)
        return data

    def activate(self, name):
        if name not in self.net.slices:
            raise KeyError(name)
        if name in self.active:
            return "slice %s is already active" % name
        conflicts = self.net.admission_check(self.active, name)
        if conflicts:
            msg = "slice %s rejected: %s" % (name, "; ".join(conflicts))
            self.log_event(msg, "error")
            raise ValueError(msg)
        self.active.append(name)
        self._install(self.slice_rules[name])
        msg = "slice %s activated (%s, path %s, %g%% of each link)" % (
            name, self.net.slices[name].describe_traffic(),
            "-".join(self.net.slices[name].path), self.net.slices[name].share)
        self.log_event(msg)
        return msg

    def deactivate(self, name):
        if name not in self.net.slices:
            raise KeyError(name)
        if name not in self.active:
            return "slice %s is already inactive" % name
        self.active.remove(name)
        cookie = slice_cookie(self.net.slices[name].queue)
        for dp in self.datapaths.values():
            self._delete_by_cookie(dp, cookie, SLICE_MASK)
        self.slice_rate[name] = 0.0
        msg = "slice %s deactivated, its traffic falls back to best effort" % name
        self.log_event(msg)
        return msg

    def reset(self):
        for name in list(self.active):
            self.deactivate(name)
        return "all slices deactivated"

    # ------------------------------------------------------- OpenFlow events

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        sw = self.net.switch_by_dpid(dp.id)
        if sw is None:
            self.log_event("unknown switch dpid=%016x ignored" % dp.id, "error")
            return
        self.datapaths[sw.name] = dp
        ofp = dp.ofproto
        self._delete_by_cookie(dp, 0, 0)  # start from an empty table
        # table-miss: drop (no packet-in, the controller is fully proactive)
        dp.send_msg(dp.ofproto_parser.OFPFlowMod(
            datapath=dp, priority=0, match=dp.ofproto_parser.OFPMatch(), instructions=[],
            table_id=0, command=ofp.OFPFC_ADD))
        rules = rules_for_switch(self.best_effort, sw.name)
        for name in self.active:
            rules += rules_for_switch(self.slice_rules[name], sw.name)
        self._install(rules)
        self.log_event("switch %s connected (%s), %d rules installed" % (sw.name, sw.role, len(rules)))

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state != DEAD_DISPATCHER or dp.id is None:
            return
        sw = self.net.switch_by_dpid(dp.id)
        if sw is not None and self.datapaths.get(sw.name) is dp:
            del self.datapaths[sw.name]
            self.flow_meter.forget(lambda k: k[0] == sw.name)
            self.port_meter.forget(lambda k: k[0] == sw.name)
            self.log_event("switch %s disconnected" % sw.name, "error")

    @set_ev_cls(ofp_event.EventOFPErrorMsg, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_handler(self, ev):
        msg = ev.msg
        sw = self.net.switch_by_dpid(msg.datapath.id)
        self.log_event("OpenFlow error from %s: type=%d code=%d"
                       % (sw.name if sw else msg.datapath.id, msg.type, msg.code), "error")

    # ------------------------------------------------------------ monitoring

    def _monitor_loop(self):
        while True:
            for dp in list(self.datapaths.values()):
                ofp, parser = dp.ofproto, dp.ofproto_parser
                # only ingress rules: each slice packet is counted exactly once
                dp.send_msg(parser.OFPFlowStatsRequest(
                    dp, 0, ofp.OFPTT_ALL, ofp.OFPP_ANY, ofp.OFPG_ANY,
                    KIND_INGRESS, 0xFF, parser.OFPMatch()))
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
            hub.sleep(POLL_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_handler(self, ev):
        sw = self.net.switch_by_dpid(ev.msg.datapath.id)
        if sw is None:
            return
        now = time.time()
        seen = set()
        for stat in ev.msg.body:
            fields = dict(stat.match.items())
            key = (sw.name, stat.cookie, fields.get("ipv4_src"), fields.get("ipv4_dst"))
            seen.add(key)
            self._ingress_rate[key] = self.flow_meter.update(key, stat.byte_count, now)
        for key in [k for k in self._ingress_rate if k[0] == sw.name and k not in seen]:
            del self._ingress_rate[key]
        totals = dict((name, 0.0) for name in self.net.slices)
        for (_sw, cookie, _src, _dst), rate in self._ingress_rate.items():
            owner = self._queue_owner.get((cookie & SLICE_MASK) >> 8)
            if owner in self.active:
                totals[owner] += rate
        self.slice_rate = totals

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_handler(self, ev):
        sw = self.net.switch_by_dpid(ev.msg.datapath.id)
        if sw is None:
            return
        now = time.time()
        for stat in ev.msg.body:
            key = (sw.name, stat.port_no)
            if key in self._port_peer:
                self.port_rate[key] = self.port_meter.update(key, stat.tx_bytes, now)

    def link_stats(self):
        """Per link Mbit/s in both directions, keyed by link id."""
        out = {}
        for link in self.net.links:
            a, b = link.ends
            ab = self.port_rate.get((a, self.net.port[(a, b)]), 0.0)
            ba = self.port_rate.get((b, self.net.port[(b, a)]), 0.0)
            out[link.id] = {"forward": round(ab, 3), "reverse": round(ba, 3),
                            "utilisation": round(100.0 * max(ab, ba) / link.bw, 1)}
        return out


# --------------------------------------------------------------------- REST

def json_response(data, status=200):
    return Response(status=status, content_type="application/json", charset="utf-8",
                    body=json.dumps(data).encode("utf-8"))


class SliceRestApi(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(SliceRestApi, self).__init__(req, link, data, **config)
        self.app = data[API_INSTANCE]
        self.static = DirectoryApp(GUI_DIR)

    @route("slicing", "/api/topology", methods=["GET"])
    def get_topology(self, req, **kwargs):
        return json_response(self.app.net.topology_dict())

    @route("slicing", "/api/slices", methods=["GET"])
    def get_slices(self, req, **kwargs):
        app = self.app
        return json_response({
            "slices": [app.slice_state(n) for n in app.net.slices],
            "active": list(app.active),
            "link_reservation": app.net.link_load(app.active),
        })

    @route("slicing", "/api/slices/{name}/activate", methods=["POST"])
    def post_activate(self, req, **kwargs):
        return self._lifecycle(self.app.activate, kwargs["name"])

    @route("slicing", "/api/slices/{name}/deactivate", methods=["POST"])
    def post_deactivate(self, req, **kwargs):
        return self._lifecycle(self.app.deactivate, kwargs["name"])

    @route("slicing", "/api/reset", methods=["POST"])
    def post_reset(self, req, **kwargs):
        return json_response({"message": self.app.reset(), "active": []})

    @route("slicing", "/api/stats", methods=["GET"])
    def get_stats(self, req, **kwargs):
        app = self.app
        return json_response({
            "time": time.time(),
            "slices": dict((n, round(r, 3)) for n, r in app.slice_rate.items()),
            "links": app.link_stats(),
        })

    @route("slicing", "/api/status", methods=["GET"])
    def get_status(self, req, **kwargs):
        app = self.app
        return json_response({
            "uptime": round(time.time() - app.started, 1),
            "switches": sorted(app.datapaths),
            "expected_switches": list(app.net.switches),
            "active": list(app.active),
            "events": list(app.event_log),
        })

    @route("gui", "/{filename:[^/]*}", methods=["GET"])
    def gui(self, req, **kwargs):
        req.path_info = kwargs.get("filename") or "index.html"
        return self.static(req)

    def _lifecycle(self, action, name):
        try:
            message = action(name)
        except KeyError:
            return json_response({"error": "unknown slice '%s'" % name}, 404)
        except ValueError as err:
            return json_response({"error": str(err)}, 409)
        return json_response({"message": message, "active": list(self.app.active)})
