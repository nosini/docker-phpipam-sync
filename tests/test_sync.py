"""Scenario tests for docker_phpipam_sync against in-memory fakes of the Docker
client and the phpIPAM REST API. Needs no Docker engine and no phpIPAM.

Run from the repository root:  python -m unittest -v
"""

import json
import logging
import types
import unittest
from unittest import mock

import docker

import docker_phpipam_sync as m

logging.disable(logging.CRITICAL)

NOTE = f"{m.LEGACY_NOTE} (hostA)"
OTHER_HOST_NOTE = f"{m.LEGACY_NOTE} (hostB)"


# ─── fake phpIPAM ─────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, code, body, raw=None):
        self.status_code = code
        self.ok = 200 <= code < 300
        self._body = body
        self.text = raw if raw is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakePhpIpam:
    """Stands in for requests.Session; routes by method and path."""

    def __init__(self):
        self.subnets = {"1": ("172.20.0.0", 16), "2": ("172.21.0.0", 16), "3": ("172.17.0.0", 16)}
        self.addrs: dict[str, dict] = {}
        self.next_id = 100
        self.down = False            # transport failure
        self.html = False            # 200 with a non-JSON body (proxy/SSO page)
        self.refuse_delete = False   # 200 with success:false

    def request(self, method, url, json=None, timeout=None):
        if self.down:
            raise m.requests.ConnectionError("phpIPAM down")
        if self.html:
            return FakeResponse(200, None, "<html>login</html>")
        p = url.split("/api/docker/", 1)[1].strip("/").split("/")

        if method == "GET" and p[:2] == ["addresses", "search"]:
            return self._list([dict(a, id=i) for i, a in self.addrs.items() if a["ip"] == p[2]])
        if method == "GET" and p == ["subnets"]:
            return self._list([{"id": k, "subnet": s, "mask": mask}
                               for k, (s, mask) in self.subnets.items()])
        if method == "GET" and p[:2] == ["subnets", "cidr"]:
            cidr = f"{p[2]}/{p[3]}"
            return self._list([{"id": k} for k, (s, mask) in self.subnets.items()
                               if f"{s}/{mask}" == cidr])
        if method == "GET" and p[0] == "subnets" and p[2:] == ["addresses"]:
            return self._list([dict(a, id=i) for i, a in self.addrs.items() if a["subnetId"] == p[1]])
        if method == "POST" and p == ["addresses"]:
            addr_id = str(self.next_id)
            self.next_id += 1
            self.addrs[addr_id] = dict(json)
            return FakeResponse(201, {"success": True, "id": addr_id})
        if method == "PATCH" and p[0] == "addresses":
            self.addrs[p[1]].update(json)
            return FakeResponse(200, {"success": True})
        if method == "DELETE" and p[0] == "addresses":
            if self.refuse_delete:
                return FakeResponse(200, {"success": False, "message": "locked"})
            if self.addrs.pop(p[1], None) is None:
                return FakeResponse(404, {"success": False})
            return FakeResponse(200, {"success": True})
        raise AssertionError(f"unexpected {method} {url}")

    @staticmethod
    def _list(data):
        # phpIPAM answers empty results with 404 + success:false.
        if not data:
            return FakeResponse(404, {"success": False})
        return FakeResponse(200, {"success": True, "data": data})


# ─── fake Docker ──────────────────────────────────────────────────────────────
class FakeContainer:
    def __init__(self, cid, name, nets, image="nginx:latest"):
        self.id, self.name = cid, name
        self.attrs = {
            "Config": {"Image": image},
            "Image": "sha256:" + "a" * 64,
            "NetworkSettings": {"Networks": {
                n: {"IPAddress": ip, "GlobalIPv6Address": ""} for n, ip in nets.items()
            }},
        }

    @property
    def image(self):
        # Behave like a container whose image has been removed.
        raise docker.errors.ImageNotFound("image removed")


class FakeDocker:
    def __init__(self):
        self.running: dict[str, FakeContainer] = {}
        self.nets = {"proj_default": ["172.20.0.0/16"], "bridge": ["172.17.0.0/16"]}
        self.queued_events: list = []
        self.list_fails = False
        self.last_since = None
        self.containers = types.SimpleNamespace(get=self._get, list=self._list)
        self.networks = types.SimpleNamespace(get=lambda name: types.SimpleNamespace(
            attrs={"IPAM": {"Config": [{"Subnet": c} for c in self.nets[name]]}}))

    def _get(self, cid):
        if cid not in self.running:
            raise docker.errors.NotFound(cid)
        return self.running[cid]

    def _list(self, ignore_removed=False):
        if self.list_fails:
            raise docker.errors.APIError("docker unavailable")
        return list(self.running.values())

    def info(self):
        return {"Name": "hostA"}

    def events(self, decode, since, filters):
        self.last_since = since
        while self.queued_events:
            e = self.queued_events.pop(0)
            if isinstance(e, Exception):
                raise e
            yield e


def event(action, cid, t=1000):
    # Current engines only guarantee Action/Actor; status/id are deprecated.
    return {"Action": action, "Actor": {"ID": cid}, "time": t}


class DaemonTestCase(unittest.TestCase):
    def setUp(self):
        self.docker = FakeDocker()
        self.ipam = FakePhpIpam()
        patches = [
            mock.patch.object(m.docker, "from_env", return_value=self.docker),
            # No background retry thread; tests call _retry_pending_deletes directly.
            mock.patch.object(m.threading, "Thread"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.daemon = m.DockerPhpIpamDaemon()
        self.daemon.ipam.session = self.ipam

    def start(self, cid, name, nets):
        self.docker.running[cid] = FakeContainer(cid, name, nets)
        self.daemon.on_start(cid)

    def stop(self, cid):
        del self.docker.running[cid]
        self.daemon.on_stop(cid)


class TestLifecycle(DaemonTestCase):
    def test_register_and_remove_via_events_with_removed_image(self):
        self.docker.running["c1"] = FakeContainer("c1", "web", {"proj_default": "172.20.0.5"})
        self.docker.queued_events = [event("start", "c1")]
        self.daemon.run()
        (rec,) = self.ipam.addrs.values()
        self.assertEqual(rec["description"], "Docker: web [nginx:latest] on proj_default")
        self.assertEqual(rec["note"], NOTE)

        del self.docker.running["c1"]
        self.docker.queued_events = [event("die", "c1")]
        self.daemon.run()
        self.assertEqual(self.ipam.addrs, {})

    def test_repeated_start_merges_tracking(self):
        self.start("c1", "web", {"proj_default": "172.20.0.5"})
        self.ipam.down = True
        self.daemon.on_start("c1")
        self.ipam.down = False
        self.assertEqual(self.daemon.tracked["c1"], [{"ip": "172.20.0.5", "addr_id": "100"}])
        self.daemon.on_start("c1")
        self.assertEqual(len(self.daemon.tracked["c1"]), 1)

    def test_readopt_refreshes_fields_and_legacy_note(self):
        self.ipam.addrs["60"] = {"ip": "172.20.0.5", "subnetId": "1", "hostname": "web",
                                 "description": "Docker: web", "note": m.LEGACY_NOTE}
        self.start("c1", "db", {"proj_default": "172.20.0.5"})
        rec = self.ipam.addrs["60"]
        self.assertEqual(rec["hostname"], "db")
        self.assertIn("db", rec["description"])
        self.assertEqual(rec["note"], NOTE)
        self.assertEqual(self.daemon.tracked["c1"], [{"ip": "172.20.0.5", "addr_id": "60"}])

    def test_unmanaged_record_is_left_alone(self):
        self.ipam.addrs["61"] = {"ip": "172.20.0.5", "subnetId": "1", "note": "manual"}
        self.start("c1", "web", {"proj_default": "172.20.0.5"})
        self.assertEqual(self.daemon.tracked["c1"], [])
        self.stop("c1")
        self.assertIn("61", self.ipam.addrs)


class TestSubnetResolution(DaemonTestCase):
    def test_network_recreated_with_new_cidr(self):
        self.start("c1", "web", {"proj_default": "172.20.0.5"})
        self.docker.nets["proj_default"] = ["172.21.0.0/16"]
        self.start("c2", "web", {"proj_default": "172.21.0.5"})
        self.assertEqual(self.ipam.addrs["101"]["subnetId"], "2")

    def test_negative_cache_expires(self):
        del self.ipam.subnets["1"]
        self.start("c1", "web", {"proj_default": "172.20.0.5"})
        self.assertEqual(self.ipam.addrs, {})
        self.ipam.subnets["1"] = ("172.20.0.0", 16)
        with mock.patch.object(m.time, "monotonic", return_value=m.time.monotonic() + m.SUBNET_CACHE_TTL):
            self.daemon.on_start("c1")
        self.assertEqual(len(self.ipam.addrs), 1)


class TestFailureHandling(DaemonTestCase):
    def test_non_json_response_does_not_crash(self):
        self.ipam.html = True
        self.docker.running["c1"] = FakeContainer("c1", "web", {"proj_default": "172.20.0.5"})
        self.docker.queued_events = [event("start", "c1")]
        self.daemon.run()  # must not raise
        self.assertEqual(self.ipam.addrs, {})

    def test_state_survives_crash_and_resumes_from_last_event(self):
        self.docker.running["c1"] = FakeContainer("c1", "web", {"proj_default": "172.20.0.5"})
        self.docker.queued_events = [event("start", "c1", t=1234), RuntimeError("stream broke")]
        with self.assertRaises(RuntimeError):
            self.daemon.run()
        self.ipam.down = True
        self.stop("c1")
        self.assertEqual(len(self.daemon.pending_deletes), 1)

        self.ipam.down = False
        self.daemon.run()
        self.assertEqual(self.docker.last_since, 1234)
        self.daemon._retry_pending_deletes()
        self.assertEqual(self.ipam.addrs, {})
        self.assertEqual(self.daemon.pending_deletes, [])

    def test_pending_delete_gives_up_by_age_not_attempts(self):
        self.start("c1", "web", {"proj_default": "172.20.0.5"})
        self.ipam.down = True
        self.stop("c1")
        for _ in range(10):
            self.daemon._retry_pending_deletes()
        self.assertEqual(len(self.daemon.pending_deletes), 1)
        self.daemon.pending_deletes[0]["queued"] -= m.PENDING_DELETE_MAX_AGE
        self.daemon._retry_pending_deletes()
        self.assertEqual(self.daemon.pending_deletes, [])

    def test_refused_delete_is_retried(self):
        self.start("c1", "web", {"proj_default": "172.20.0.5"})
        self.ipam.refuse_delete = True
        self.stop("c1")
        self.assertEqual(len(self.daemon.pending_deletes), 1)
        self.ipam.refuse_delete = False
        self.daemon._retry_pending_deletes()
        self.assertEqual(self.ipam.addrs, {})

    def test_docker_error_defers_deletion(self):
        self.start("c1", "web", {"proj_default": "172.20.0.5"})
        del self.docker.running["c1"]
        self.docker.list_fails = True
        self.daemon._handle("die", "c1")
        self.assertEqual(len(self.daemon.pending_deletes), 1)
        self.assertIn("100", self.ipam.addrs)


class TestOwnership(DaemonTestCase):
    def test_reconcile_removes_only_own_orphans(self):
        self.ipam.addrs = {
            "50": {"ip": "172.20.0.9", "subnetId": "1", "note": NOTE},
            "51": {"ip": "172.20.0.10", "subnetId": "1", "note": OTHER_HOST_NOTE},
            "52": {"ip": "172.20.0.11", "subnetId": "1", "note": "manual"},
        }
        self.docker.running["c1"] = FakeContainer("c1", "web", {"proj_default": "172.20.0.5"})
        self.daemon.run()
        self.assertEqual(set(self.ipam.addrs), {"51", "52", "100"})

    def test_discovery_ignores_other_subnet_and_other_host(self):
        self.ipam.addrs = {
            "70": {"ip": "172.17.0.2", "subnetId": "3", "note": OTHER_HOST_NOTE},
            "71": {"ip": "172.17.0.2", "subnetId": "9", "note": NOTE},
        }
        self.docker.running["c1"] = FakeContainer("c1", "x", {"bridge": "172.17.0.2"})
        self.assertEqual(self.daemon._discover_owned("c1"), [])
        self.ipam.addrs["72"] = {"ip": "172.17.0.2", "subnetId": "3", "note": NOTE}
        self.assertEqual(self.daemon._discover_owned("c1"), [{"ip": "172.17.0.2", "addr_id": "72"}])


if __name__ == "__main__":
    unittest.main()
