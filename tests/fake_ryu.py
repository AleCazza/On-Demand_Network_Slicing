"""Minimal stand-ins for the parts of Ryu/WebOb used by the controller, so the
controller logic can be exercised without an OpenFlow switch. The fake
OFPMatch enforces the OXM prerequisites that Open vSwitch checks."""

import sys
import types

PREREQ = {
    "ipv4_src": ("eth_type", 0x0800), "ipv4_dst": ("eth_type", 0x0800),
    "ip_proto": ("eth_type", 0x0800), "arp_tpa": ("eth_type", 0x0806),
    "tcp_src": ("ip_proto", 6), "tcp_dst": ("ip_proto", 6),
    "udp_src": ("ip_proto", 17), "udp_dst": ("ip_proto", 17),
}
KNOWN = set(PREREQ) | {"eth_type", "in_port"}


class OFPMatch(object):
    def __init__(self, **fields):
        for k, v in fields.items():
            if k not in KNOWN:
                raise TypeError("unknown OXM field %s" % k)
            if k in PREREQ:
                need, value = PREREQ[k]
                if fields.get(need) != value:
                    raise ValueError("OFPBMC_BAD_PREREQ: %s needs %s=%s" % (k, need, value))
        self.fields = dict(fields)

    def items(self):
        return self.fields.items()


class _Msg(object):
    def __init__(self, *args, **kwargs):
        self.args, self.__dict__["kw"] = args, kwargs
        self.__dict__.update(kwargs)


class OFPFlowMod(_Msg):
    pass


class OFPActionOutput(_Msg):
    def __init__(self, port, max_len=0):
        super().__init__(port=port)


class OFPActionSetQueue(_Msg):
    def __init__(self, queue_id):
        super().__init__(queue_id=queue_id)


class OFPInstructionActions(_Msg):
    def __init__(self, type_, actions):
        super().__init__(type_=type_, actions=actions)


class OFPFlowStatsRequest(_Msg):
    pass


class OFPPortStatsRequest(_Msg):
    pass


parser = types.SimpleNamespace(**{c.__name__: c for c in (
    OFPMatch, OFPFlowMod, OFPActionOutput, OFPActionSetQueue, OFPInstructionActions,
    OFPFlowStatsRequest, OFPPortStatsRequest)})

ofproto = types.SimpleNamespace(
    OFP_VERSION=4, OFPIT_APPLY_ACTIONS=4, OFPTT_ALL=0xff, OFPP_ANY=0xffffffff,
    OFPG_ANY=0xffffffff, OFPFC_ADD=0, OFPFC_DELETE=3, OFPP_CONTROLLER=0xfffffffd,
    OFPCML_NO_BUFFER=0xffff)


class FakeDatapath(object):
    def __init__(self, dpid):
        self.id = dpid
        self.ofproto = ofproto
        self.ofproto_parser = parser
        self.sent = []

    def send_msg(self, msg):
        self.sent.append(msg)


def install():
    """Register fake modules under the real import names."""
    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod

    class _Queue(object):
        def put(self, item):
            pass

    class RyuApp(object):
        """Sets the same internal attributes as ryu.base.app_manager.RyuApp,
        so a subclass that overwrites one of them is caught by the tests."""

        RESERVED = ("name", "event_handlers", "observers", "threads", "main_thread",
                    "events", "_events_sem", "is_active", "logger", "CONF")

        def __init__(self, *args, **kwargs):
            import logging
            self.name = type(self).__name__
            self.event_handlers, self.observers, self.threads = {}, {}, []
            self.main_thread, self._events_sem, self.is_active, self.CONF = None, None, True, None
            self.events = _Queue()
            self.logger = logging.getLogger("fake-ryu")

    class ControllerBase(object):
        def __init__(self, req, link, data, **config):
            pass

    class Response(object):
        def __init__(self, status=200, content_type=None, charset=None, body=b""):
            self.status, self.content_type, self.body = status, content_type, body

    class WSGIApplication(object):
        def __init__(self):
            self.registered = []

        def register(self, cls, data):
            self.registered.append((cls, data))

    def route(name, path, methods=None, requirements=None):
        def deco(fn):
            fn.route = (path, methods)
            return fn
        return deco

    def set_ev_cls(ev, dispatchers=None):
        def deco(fn):
            return fn
        return deco

    events = dict((n, type(n, (), {})) for n in (
        "EventOFPSwitchFeatures", "EventOFPStateChange", "EventOFPErrorMsg",
        "EventOFPFlowStatsReply", "EventOFPPortStatsReply"))

    module("ryu")
    module("ryu.app")
    module("ryu.app.wsgi", ControllerBase=ControllerBase, Response=Response,
           WSGIApplication=WSGIApplication, route=route)
    module("ryu.base")
    module("ryu.base.app_manager", RyuApp=RyuApp)
    module("ryu.controller")
    module("ryu.controller.ofp_event", **events)
    module("ryu.controller.handler", CONFIG_DISPATCHER="config", MAIN_DISPATCHER="main",
           DEAD_DISPATCHER="dead", set_ev_cls=set_ev_cls)
    module("ryu.lib")
    module("ryu.lib.hub", spawn=lambda fn, *a: None, sleep=lambda s: None)
    module("ryu.ofproto")
    module("ryu.ofproto.ofproto_v1_3", OFP_VERSION=4)
    module("webob")
    module("webob.static", DirectoryApp=lambda path: (lambda req: ("static", req.path_info)))
    return WSGIApplication
