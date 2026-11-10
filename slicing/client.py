"""Minimal client for the controller northbound API (standard library only)."""

import json
import os

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = os.environ.get("SLICING_API", "http://127.0.0.1:8080")


class ApiError(Exception):
    pass


class SliceClient(object):
    def __init__(self, base_url=DEFAULT_URL, timeout=5):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method, path):
        req = Request(self.base + path, method=method,
                      headers={"Content-Type": "application/json"},
                      data=b"{}" if method == "POST" else None)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except HTTPError as err:
            try:
                body = json.loads(err.read().decode())
            except ValueError:
                body = {"error": str(err)}
            raise ApiError(body.get("error", str(err)))
        except URLError as err:
            raise ApiError("controller not reachable at %s (%s)" % (self.base, err.reason))

    def slices(self):
        return self._call("GET", "/api/slices")["slices"]

    def activate(self, name):
        return self._call("POST", "/api/slices/%s/activate" % name)

    def deactivate(self, name):
        return self._call("POST", "/api/slices/%s/deactivate" % name)

    def reset(self):
        return self._call("POST", "/api/reset")

    def stats(self):
        return self._call("GET", "/api/stats")

    def status(self):
        return self._call("GET", "/api/status")
