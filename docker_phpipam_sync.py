#!/usr/bin/env python3
"""
docker-phpipam-sync.py
Daemon that watches Docker events and syncs container IPs (v4 & v6) to phpIPAM.
"""

import ipaddress
import logging
import os
import signal
import sys
import time
from typing import Optional

import docker
import requests

# ─── Configuration ────────────────────────────────────────────────────────────
PHPIPAM_URL    = os.getenv("PHPIPAM_URL",    "http://phpipam")
PHPIPAM_APP_ID = os.getenv("PHPIPAM_APP_ID", "docker")
PHPIPAM_TOKEN  = os.getenv("PHPIPAM_TOKEN",  "your_app_token_here")
LOG_LEVEL      = os.getenv("LOG_LEVEL",      "INFO")
# ──────────────────────────────────────────────────────────────────────────────

# Marker written into every phpIPAM record this daemon owns. Only records
# carrying this exact note may ever be deleted by us.
MANAGED_NOTE = "Managed by docker-phpipam-sync"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("docker-phpipam")


class PhpIpamError(Exception):
    """Raised on transport failures or non-ok HTTP responses from phpIPAM.

    Distinct from a genuine "not found" (which the client reports as None), so
    callers can tell "phpIPAM was unreachable" apart from "record absent".
    """


class PhpIpamClient:
    def __init__(self, base_url: str, app_id: str, token: str):
        self.base = f"{base_url.rstrip('/')}/api/{app_id}"
        self.session = requests.Session()
        self.session.headers.update({
            "token": token,
            "Content-Type": "application/json",
        })

    def _get(self, path: str) -> Optional[dict | list]:
        try:
            r = self.session.get(f"{self.base}{path}", timeout=10)
        except requests.RequestException as e:
            log.error(f"GET {path} failed: {e}")
            raise PhpIpamError(str(e)) from e
        # phpIPAM returns 404 with success:false for empty search results.
        if r.status_code == 404: return None
        if not r.ok:
            self._log_error_body("GET", path, r)
            raise PhpIpamError(f"GET {path} → HTTP {r.status_code}")
        data = r.json()
        return data.get("data") if data.get("success") else None

    def _post(self, path: str, payload: dict) -> dict:
        try:
            r = self.session.post(f"{self.base}{path}", json=payload, timeout=10)
        except requests.RequestException as e:
            log.error(f"POST {path} failed: {e}")
            raise PhpIpamError(str(e)) from e
        if not r.ok:
            self._log_error_body("POST", path, r)
            raise PhpIpamError(f"POST {path} → HTTP {r.status_code}")
        # Successful POST /addresses/ is {"success":true,"id":"65",...} — no "data".
        return r.json()

    def _delete(self, path: str) -> bool:
        try:
            r = self.session.delete(f"{self.base}{path}", timeout=10)
        except requests.RequestException as e:
            log.error(f"DELETE {path} failed: {e}")
            raise PhpIpamError(str(e)) from e
        # Already gone is success from our point of view.
        if r.status_code == 404: return True
        if not r.ok:
            self._log_error_body("DELETE", path, r)
            raise PhpIpamError(f"DELETE {path} → HTTP {r.status_code}")
        return bool(r.json().get("success"))

    def _log_error_body(self, method: str, path: str, r) -> None:
        try:
            body = r.json()
            phpipam_msg = body.get("message") or body.get("data") or body
            log.error(f"{method} {path} → HTTP {r.status_code}: {phpipam_msg}")
        except Exception:
            log.error(f"{method} {path} → HTTP {r.status_code}: {r.text[:300]}")

    def find_address(self, ip: str) -> Optional[list[dict]]:
        """Global exact-IP search. Returns ALL matching records (across every
        subnet) or None. Callers must filter by subnet / note themselves."""
        results = self._get(f"/addresses/search/{ip}/")
        if isinstance(results, list) and results:
            return results
        return None

    def create_address(self, ip: str, hostname: str, description: str, subnet_id: str) -> Optional[str]:
        """Create (or re-adopt) the address record for `ip` in `subnet_id`.

        Returns the phpIPAM address id (str) on success/adoption, or None if a
        record already exists in this subnet that we do not manage. Raises
        PhpIpamError if phpIPAM is unreachable."""
        for rec in self.find_address(ip) or []:
            # Only records in the SAME subnet are relevant — the same RFC1918 IP
            # may legitimately be documented in another subnet.
            if str(rec.get("subnetId")) != str(subnet_id):
                continue
            if rec.get("note") == MANAGED_NOTE:
                # Ours already (e.g. leftover from a previous daemon run) → re-adopt.
                log.info(f"    {ip} already managed by us (id={rec['id']}), re-adopting.")
                return rec["id"]
            # Someone else owns it — never track/delete it.
            log.info(f"    {ip} exists in subnet {subnet_id} but is not managed by us "
                     f"(id={rec['id']}), leaving it alone.")
            return None

        # Truncate description to 64 chars for phpIPAM DB compatibility
        final_desc = (description[:61] + "...") if len(description) > 64 else description

        body = self._post("/addresses/", {
            "subnetId":    subnet_id,
            "ip":          ip,
            "hostname":    hostname,
            "description": final_desc,
            "note":        MANAGED_NOTE,
        })
        if body.get("success"):
            addr_id = body.get("id")
            log.info(f"    ✔ Registered {ip} ({hostname}) → subnet {subnet_id} (id={addr_id})")
            return addr_id
        return None

    def delete_address(self, ip: str, addr_id: str) -> bool:
        """Delete an address unambiguously by its phpIPAM id. Raises
        PhpIpamError if phpIPAM is unreachable."""
        if self._delete(f"/addresses/{addr_id}/"):
            log.info(f"    ✔ Removed {ip} (id={addr_id}) from phpIPAM")
            return True
        return False

    def find_subnet_by_cidr(self, cidr: str) -> Optional[str]:
        results = self._get(f"/subnets/cidr/{cidr}/")
        if isinstance(results, list) and results:
            return results[0]["id"]
        return None

    def find_subnet_for_ip(self, ip: str) -> Optional[str]:
        all_subnets = self._get("/subnets/")
        if not all_subnets: return None

        ip_obj = ipaddress.ip_address(ip)
        best_id, best_prefix = None, -1
        for subnet in all_subnets:
            try:
                # Handle both v4 and v6 subnet masks from phpIPAM
                net = ipaddress.ip_network(f"{subnet['subnet']}/{subnet['mask']}", strict=False)
                if ip_obj in net and net.prefixlen > best_prefix:
                    best_id = subnet["id"]
                    best_prefix = net.prefixlen
            except Exception:
                continue
        return best_id


class DockerPhpIpamDaemon:
    def __init__(self):
        self.docker = docker.from_env()
        self.ipam   = PhpIpamClient(PHPIPAM_URL, PHPIPAM_APP_ID, PHPIPAM_TOKEN)
        # container_id → list of {"ip": str, "addr_id": str} records we own.
        self.tracked: dict[str, list[dict]] = {}
        # Deletions that failed because phpIPAM was unreachable; retried later.
        self.pending_deletes: list[dict] = []
        # Cache key: (network_name, ip_version)
        self._subnet_cache: dict[tuple[str, int], Optional[str]] = {}

    def _container_ips(self, container_id: str) -> list[tuple[str, str]]:
        """Return list of (network_name, ip) for all v4 and v6 addresses."""
        result = []
        try:
            c = self.docker.containers.get(container_id)
            for net_name, info in c.attrs["NetworkSettings"]["Networks"].items():
                v4 = info.get("IPAddress")
                v6 = info.get("GlobalIPv6Address")
                if v4: result.append((net_name, v4))
                if v6: result.append((net_name, v6))
        except Exception as e:
            log.debug(f"Could not inspect {container_id[:12]}: {e}")
        return result

    def _network_cidrs(self, network_name: str) -> list[str]:
        """Return all subnets (v4/v6) associated with a Docker network."""
        try:
            net = self.docker.networks.get(network_name)
            configs = net.attrs.get("IPAM", {}).get("Config", [])
            return [c.get("Subnet") for c in configs if c.get("Subnet")]
        except Exception:
            return []

    def _resolve_subnet(self, network_name: str, ip: str) -> Optional[str]:
        ip_obj = ipaddress.ip_address(ip)
        cache_key = (network_name, ip_obj.version)

        if cache_key in self._subnet_cache:
            return self._subnet_cache[cache_key]

        sid = None
        cidrs = self._network_cidrs(network_name)
        try:
            # 1. Try exact CIDR match for this specific IP version. strict=False
            #    tolerates Docker networks created with host bits set.
            for cidr in cidrs:
                if ip_obj in ipaddress.ip_network(cidr, strict=False):
                    sid = self.ipam.find_subnet_by_cidr(cidr)
                    break

            # 2. Fallback: Search all phpIPAM subnets for best fit
            if not sid:
                sid = self.ipam.find_subnet_for_ip(ip)
        except PhpIpamError as e:
            # phpIPAM was unreachable — do NOT cache, so the next event retries.
            log.warning(f"  phpIPAM unreachable resolving subnet for {network_name} ({ip}): {e}")
            return None

        # Only genuine results (incl. a real not-found) are cached.
        self._subnet_cache[cache_key] = sid
        if not sid:
            log.warning(f"  No phpIPAM subnet found for {network_name} ({ip})")
        return sid

    def on_start(self, container_id: str):
        try:
            c = self.docker.containers.get(container_id)
        except Exception: return

        name  = c.name
        image = c.image.tags[0] if c.image.tags else c.image.short_id
        log.info(f"▶ Container started: {name} ({container_id[:12]})")

        registered: list[dict] = []
        for net_name, ip in self._container_ips(container_id):
            log.info(f"  Network '{net_name}' → {ip}")
            sid = self._resolve_subnet(net_name, ip)
            if not sid: continue

            desc = f"Docker: {name} [{image}] on {net_name}"
            try:
                addr_id = self.ipam.create_address(ip, name, desc, sid)
            except PhpIpamError as e:
                log.warning(f"  phpIPAM error registering {ip}: {e}")
                continue
            if addr_id:
                registered.append({"ip": ip, "addr_id": addr_id})

        self.tracked[container_id] = registered

    def _discover_owned(self, container_id: str) -> list[dict]:
        """Best-effort fallback for containers we have no tracking for (e.g. the
        daemon restarted after they started). Search by IP and keep only records
        we actually own. After 'die' the inspect usually shows no IPs, so this
        commonly returns []."""
        found: list[dict] = []
        for _net, ip in self._container_ips(container_id):
            try:
                results = self.ipam.find_address(ip)
            except PhpIpamError as e:
                log.warning(f"  phpIPAM error searching {ip}: {e}")
                continue
            for rec in results or []:
                if rec.get("note") == MANAGED_NOTE:
                    found.append({"ip": ip, "addr_id": rec["id"]})
        return found

    def _ip_in_use(self, ip: str) -> bool:
        """Is `ip` currently held by any running container? ignore_removed keeps
        a container removed mid-iteration (e.g. --rm) from crashing us."""
        for c in self.docker.containers.list(ignore_removed=True):
            for net in c.attrs["NetworkSettings"]["Networks"].values():
                if ip in (net.get("IPAddress"), net.get("GlobalIPv6Address")):
                    return True
        return False

    def on_stop(self, container_id: str):
        entries = self.tracked.pop(container_id, None)
        if entries is None:
            entries = self._discover_owned(container_id)
        if not entries: return

        ips = [e["ip"] for e in entries]
        log.info(f"■ Container stopped: {container_id[:12]}  IPs: {', '.join(ips)}")
        for entry in entries:
            ip, addr_id = entry["ip"], entry["addr_id"]
            # Simple check: is IP still in use by any other container?
            if self._ip_in_use(ip):
                log.info(f"  {ip} still in use, skipping deletion.")
                continue
            try:
                self.ipam.delete_address(ip, addr_id)
            except PhpIpamError as e:
                # phpIPAM is down — requeue so the address does not leak.
                log.warning(f"  phpIPAM error deleting {ip} (id={addr_id}): {e}; will retry.")
                self.pending_deletes.append({"ip": ip, "addr_id": addr_id, "attempts": 1})

    def _retry_pending_deletes(self):
        """Re-attempt deletions that failed while phpIPAM was unreachable. Runs
        on each incoming event; drops an entry after ~5 attempts."""
        if not self.pending_deletes: return
        still: list[dict] = []
        for entry in self.pending_deletes:
            ip, addr_id = entry["ip"], entry["addr_id"]
            if self._ip_in_use(ip):
                # IP got recycled to another container that now owns the record.
                continue
            try:
                self.ipam.delete_address(ip, addr_id)
            except PhpIpamError as e:
                entry["attempts"] += 1
                if entry["attempts"] >= 5:
                    log.error(f"  ✗ Gave up deleting {ip} (id={addr_id}) after "
                              f"{entry['attempts']} attempts; address leaked.")
                else:
                    log.warning(f"  phpIPAM still unreachable deleting {ip} (id={addr_id}) "
                                f"(attempt {entry['attempts']}): {e}; will retry.")
                    still.append(entry)
        self.pending_deletes = still

    def run(self):
        log.info("=" * 60)
        log.info("docker-phpipam-sync starting (v4 + v6 support)")
        log.info("=" * 60)
        # Capture the timestamp BEFORE the (slow) initial sweep so Docker replays
        # any start/die we miss while it runs.
        since = int(time.time())
        for c in self.docker.containers.list(ignore_removed=True):
            self.on_start(c.id)

        for event in self.docker.events(
            decode=True,
            since=since,
            filters={"type": "container", "event": ["start", "die"]},
        ):
            self._retry_pending_deletes()
            status, cid = event.get("status"), event.get("id", "")
            # React ONLY to 'die': it fires exactly once per container exit
            # (stop/kill/crash alike), unlike 'kill'/'stop' which fire early.
            if status == "start": self.on_start(cid)
            elif status == "die": self.on_stop(cid)

if __name__ == "__main__":
    signal.signal(signal.SIGINT,  lambda s, f: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    while True:
        try:
            DockerPhpIpamDaemon().run()
        except Exception as exc:
            log.error(f"Daemon crashed: {exc}. Restarting in 10s...")
            time.sleep(10)
