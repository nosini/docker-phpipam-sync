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
import threading
import time
from typing import Optional

import docker
import requests

# ─── Configuration ────────────────────────────────────────────────────────────
PHPIPAM_URL      = os.getenv("PHPIPAM_URL",      "http://phpipam")
PHPIPAM_APP_ID   = os.getenv("PHPIPAM_APP_ID",   "docker")
PHPIPAM_TOKEN    = os.getenv("PHPIPAM_TOKEN",    "your_app_token_here")
LOG_LEVEL        = os.getenv("LOG_LEVEL",        "INFO")
# Identifies this Docker host in the ownership note. Defaults to the Docker
# engine's hostname; must be unique per host sharing one phpIPAM.
SYNC_INSTANCE_ID = os.getenv("SYNC_INSTANCE_ID", "")
# ──────────────────────────────────────────────────────────────────────────────

# Marker written by versions that did not record the owning host. Such records
# are re-adopted (and re-stamped with the per-host note) but never deleted
# unless re-adopted first.
LEGACY_NOTE = "Managed by docker-phpipam-sync"

SUBNET_CACHE_TTL       = 300   # seconds a subnet lookup (hit or miss) is reused
RETRY_INTERVAL         = 60    # seconds between pending-delete retries
PENDING_DELETE_MAX_AGE = 3600  # give up on a pending delete after this long

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("docker-phpipam")


class PhpIpamError(Exception):
    """Raised on transport failures, non-ok HTTP responses, unparseable bodies
    or an explicit success:false from phpIPAM.

    Distinct from a genuine "not found" (which the client reports as None), so
    callers can tell "phpIPAM was unreachable" apart from "record absent".
    """


def _norm_ip(ip: str) -> str:
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return ip


class PhpIpamClient:
    def __init__(self, base_url: str, app_id: str, token: str, note: str):
        self.base = f"{base_url.rstrip('/')}/api/{app_id}"
        # Marker written into every record this daemon owns. Only records
        # carrying this exact note may ever be deleted by us.
        self.note = note
        self.session = requests.Session()
        self.session.headers.update({
            "token": token,
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Optional[dict]:
        """Return the decoded JSON body, or None on HTTP 404."""
        try:
            r = self.session.request(method, f"{self.base}{path}", json=payload, timeout=10)
        except requests.RequestException as e:
            log.error(f"{method} {path} failed: {e}")
            raise PhpIpamError(str(e)) from e
        if r.status_code == 404: return None
        if not r.ok:
            self._log_error_body(method, path, r)
            raise PhpIpamError(f"{method} {path} → HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            # e.g. a reverse proxy / SSO page answering 200 with HTML.
            log.error(f"{method} {path} → HTTP {r.status_code}, non-JSON body: {r.text[:300]}")
            raise PhpIpamError(f"{method} {path} → non-JSON response") from e
        if not isinstance(body, dict):
            raise PhpIpamError(f"{method} {path} → unexpected response: {str(body)[:300]}")
        return body

    def _get(self, path: str) -> Optional[dict | list]:
        # phpIPAM returns 404 with success:false for empty search results.
        body = self._request("GET", path)
        return body.get("data") if body and body.get("success") else None

    def _write(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        body = self._request(method, path, payload)
        if body is None:
            raise PhpIpamError(f"{method} {path} → HTTP 404")
        if not body.get("success"):
            raise PhpIpamError(f"{method} {path} → {body.get('message') or body}")
        return body

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
        PhpIpamError if phpIPAM is unreachable or rejects the request."""
        # Truncate description to 64 chars for phpIPAM DB compatibility
        final_desc = (description[:61] + "...") if len(description) > 64 else description

        for rec in self.find_address(ip) or []:
            # Only records in the SAME subnet are relevant — the same RFC1918 IP
            # may legitimately be documented in another subnet.
            if str(rec.get("subnetId")) != str(subnet_id) or not rec.get("id"):
                continue
            addr_id = str(rec["id"])
            if rec.get("note") in (self.note, LEGACY_NOTE):
                # Ours already (e.g. leftover from a previous daemon run) → re-adopt,
                # refreshing the fields since the IP may now belong to another container.
                log.info(f"    {ip} already managed by us (id={addr_id}), re-adopting.")
                update = {"hostname": hostname, "description": final_desc, "note": self.note}
                if any(rec.get(k) != v for k, v in update.items()):
                    try:
                        self._write("PATCH", f"/addresses/{addr_id}/", update)
                    except PhpIpamError as e:
                        log.warning(f"    Could not update {ip} (id={addr_id}): {e}")
                return addr_id
            # Someone else owns it — never track/delete it.
            log.info(f"    {ip} exists in subnet {subnet_id} but is not managed by us "
                     f"(id={addr_id}), leaving it alone.")
            return None

        # Successful POST /addresses/ is {"success":true,"id":"65",...} — no "data".
        body = self._write("POST", "/addresses/", {
            "subnetId":    subnet_id,
            "ip":          ip,
            "hostname":    hostname,
            "description": final_desc,
            "note":        self.note,
        })
        addr_id = body.get("id")
        if addr_id is None:
            raise PhpIpamError(f"POST /addresses/ for {ip} returned no id")
        log.info(f"    ✔ Registered {ip} ({hostname}) → subnet {subnet_id} (id={addr_id})")
        return str(addr_id)

    def delete_address(self, ip: str, addr_id: str) -> None:
        """Delete an address unambiguously by its phpIPAM id. Already gone
        counts as success. Raises PhpIpamError on any failure."""
        path = f"/addresses/{addr_id}/"
        body = self._request("DELETE", path)
        # 404 (already gone) is success from our point of view.
        if body is not None and not body.get("success"):
            raise PhpIpamError(f"DELETE {path} → {body.get('message') or body}")
        log.info(f"    ✔ Removed {ip} (id={addr_id}) from phpIPAM")

    def find_subnet_by_cidr(self, cidr: str) -> Optional[str]:
        results = self._get(f"/subnets/cidr/{cidr}/")
        if isinstance(results, list) and results and results[0].get("id"):
            return str(results[0]["id"])
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
                    best_id = str(subnet["id"])
                    best_prefix = net.prefixlen
            except Exception:
                continue
        return best_id

    def managed_addresses(self) -> list[dict]:
        """Every record carrying our note, across all subnets (one request per
        subnet)."""
        found: list[dict] = []
        for subnet in self._get("/subnets/") or []:
            if not subnet.get("id"): continue
            for rec in self._get(f"/subnets/{subnet['id']}/addresses/") or []:
                if rec.get("note") == self.note and rec.get("id") and rec.get("ip"):
                    found.append(rec)
        return found


class DockerPhpIpamDaemon:
    def __init__(self):
        self.docker = docker.from_env()
        instance    = SYNC_INSTANCE_ID or self.docker.info()["Name"]
        self.ipam   = PhpIpamClient(PHPIPAM_URL, PHPIPAM_APP_ID, PHPIPAM_TOKEN,
                                    note=f"{LEGACY_NOTE} ({instance})")
        # container_id → list of {"ip": str, "addr_id": str} records we own.
        self.tracked: dict[str, list[dict]] = {}
        # Deletions that failed (phpIPAM unreachable/refused); retried on a timer.
        self.pending_deletes: list[dict] = []
        # Cache key: Docker CIDR containing the IP (or the IP itself if none)
        # → (subnet id, monotonic time of lookup).
        self._subnet_cache: dict[str, tuple[Optional[str], float]] = {}
        # Docker event time to resume from after the event stream breaks.
        self._since: Optional[int] = None
        # Serialises event handling with the background retry thread.
        self._lock = threading.Lock()
        threading.Thread(target=self._retry_loop, name="pending-deletes", daemon=True).start()

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
            configs = net.attrs.get("IPAM", {}).get("Config") or []
            return [c.get("Subnet") for c in configs if c.get("Subnet")]
        except Exception:
            return []

    def _resolve_subnet(self, network_name: str, ip: str) -> Optional[str]:
        ip_obj = ipaddress.ip_address(ip)
        # strict=False tolerates Docker networks created with host bits set.
        cidr = next((c for c in self._network_cidrs(network_name)
                     if ip_obj in ipaddress.ip_network(c, strict=False)), None)
        # Keyed on the CIDR, not the network name: a network recreated under the
        # same name (e.g. `compose down && up`) may get a different range.
        cache_key = cidr or ip
        cached = self._subnet_cache.get(cache_key)
        if cached and time.monotonic() - cached[1] < SUBNET_CACHE_TTL:
            return cached[0]

        try:
            # 1. Try exact CIDR match for this specific IP version.
            sid = self.ipam.find_subnet_by_cidr(cidr) if cidr else None
            # 2. Fallback: Search all phpIPAM subnets for best fit
            if not sid:
                sid = self.ipam.find_subnet_for_ip(ip)
        except PhpIpamError as e:
            # phpIPAM was unreachable — do NOT cache, so the next event retries.
            log.warning(f"  phpIPAM unreachable resolving subnet for {network_name} ({ip}): {e}")
            return None

        # Only genuine results (incl. a real not-found) are cached, and only for
        # a while, so subnets added to phpIPAM later are picked up.
        self._subnet_cache[cache_key] = (sid, time.monotonic())
        if not sid:
            log.warning(f"  No phpIPAM subnet found for {network_name} ({ip})")
        return sid

    def on_start(self, container_id: str):
        try:
            c = self.docker.containers.get(container_id)
        except Exception: return

        name  = c.name
        # Config.Image is the reference the container was created from; unlike
        # c.image it needs no lookup, so it works even if the image was removed.
        image = c.attrs.get("Config", {}).get("Image") or c.attrs.get("Image", "")[:19]
        log.info(f"▶ Container started: {name} ({container_id[:12]})")

        # Merge with what is already tracked: on_start may run twice for one
        # container (initial sweep + replayed start event), and a transient
        # failure the second time must not drop ids registered the first time.
        tracked = self.tracked.setdefault(container_id, [])
        known = {e["addr_id"] for e in tracked}
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
            if addr_id and addr_id not in known:
                tracked.append({"ip": ip, "addr_id": addr_id})
                known.add(addr_id)

    def _discover_owned(self, container_id: str) -> list[dict]:
        """Best-effort fallback for containers we have no tracking for (e.g. the
        daemon restarted after they started). Search by IP and keep only records
        we own in the subnet of the container's network. After 'die' the inspect
        usually shows no IPs, so this commonly returns []; _reconcile covers the
        rest on the next restart."""
        found: list[dict] = []
        for net_name, ip in self._container_ips(container_id):
            sid = self._resolve_subnet(net_name, ip)
            if not sid: continue
            try:
                results = self.ipam.find_address(ip)
            except PhpIpamError as e:
                log.warning(f"  phpIPAM error searching {ip}: {e}")
                continue
            for rec in results or []:
                if (rec.get("note") == self.ipam.note and rec.get("id")
                        and str(rec.get("subnetId")) == str(sid)):
                    found.append({"ip": ip, "addr_id": str(rec["id"])})
        return found

    def _ips_in_use(self) -> set[str]:
        """IPs currently held by any running container. ignore_removed keeps a
        container removed mid-iteration (e.g. --rm) from crashing us."""
        in_use: set[str] = set()
        for c in self.docker.containers.list(ignore_removed=True):
            for net in c.attrs["NetworkSettings"]["Networks"].values():
                for ip in (net.get("IPAddress"), net.get("GlobalIPv6Address")):
                    if ip: in_use.add(_norm_ip(ip))
        return in_use

    def _queue_delete(self, ip: str, addr_id: str):
        if any(e["addr_id"] == addr_id for e in self.pending_deletes): return
        self.pending_deletes.append({"ip": ip, "addr_id": addr_id, "queued": time.monotonic()})

    def _delete(self, ip: str, addr_id: str):
        try:
            self.ipam.delete_address(ip, addr_id)
        except PhpIpamError as e:
            # phpIPAM is down or refused — requeue so the address does not leak.
            log.warning(f"  phpIPAM error deleting {ip} (id={addr_id}): {e}; will retry.")
            self._queue_delete(ip, addr_id)

    def on_stop(self, container_id: str):
        entries = self.tracked.pop(container_id, None)
        if entries is None:
            entries = self._discover_owned(container_id)
        if not entries: return

        ips = [e["ip"] for e in entries]
        log.info(f"■ Container stopped: {container_id[:12]}  IPs: {', '.join(ips)}")
        try:
            in_use = self._ips_in_use()
        except Exception as e:
            log.warning(f"  Could not list containers ({e}); deferring deletion.")
            for entry in entries:
                self._queue_delete(entry["ip"], entry["addr_id"])
            return
        for entry in entries:
            ip, addr_id = entry["ip"], entry["addr_id"]
            # Simple check: is IP still in use by any other container?
            if _norm_ip(ip) in in_use:
                log.info(f"  {ip} still in use, skipping deletion.")
                continue
            self._delete(ip, addr_id)

    def _retry_pending_deletes(self):
        """Re-attempt deletions that failed earlier; drops an entry once it has
        been pending for PENDING_DELETE_MAX_AGE."""
        if not self.pending_deletes: return
        try:
            in_use = self._ips_in_use()
        except Exception as e:
            log.warning(f"  Could not list containers ({e}); retrying deletes later.")
            return
        still: list[dict] = []
        for entry in self.pending_deletes:
            ip, addr_id = entry["ip"], entry["addr_id"]
            if _norm_ip(ip) in in_use:
                # IP got recycled to another container that now owns the record.
                continue
            try:
                self.ipam.delete_address(ip, addr_id)
            except PhpIpamError as e:
                if time.monotonic() - entry["queued"] >= PENDING_DELETE_MAX_AGE:
                    log.error(f"  ✗ Gave up deleting {ip} (id={addr_id}) after "
                              f"{PENDING_DELETE_MAX_AGE}s; address leaked.")
                else:
                    log.warning(f"  Still failing to delete {ip} (id={addr_id}): {e}; will retry.")
                    still.append(entry)
        self.pending_deletes = still

    def _retry_loop(self):
        while True:
            time.sleep(RETRY_INTERVAL)
            with self._lock:
                try:
                    self._retry_pending_deletes()
                except Exception:
                    log.exception("Error retrying pending deletes")

    def _reconcile(self):
        """Delete our records whose container is gone, e.g. because it exited
        while the daemon was down."""
        try:
            records = self.ipam.managed_addresses()
            in_use = self._ips_in_use()
        except Exception as e:
            log.warning(f"  Could not reconcile stale records: {e}")
            return
        tracked_ids = {e["addr_id"] for entries in self.tracked.values() for e in entries}
        for rec in records:
            ip, addr_id = rec["ip"], str(rec["id"])
            if addr_id in tracked_ids or _norm_ip(ip) in in_use:
                continue
            log.info(f"■ Stale record {ip} (id={addr_id}): no running container holds it.")
            self._delete(ip, addr_id)

    def _handle(self, action: Optional[str], container_id: str):
        # One bad event must not take down processing for every container.
        try:
            if action == "start": self.on_start(container_id)
            elif action == "die": self.on_stop(container_id)
        except Exception:
            log.exception(f"Error handling '{action}' for {container_id[:12]}")

    def run(self):
        log.info("=" * 60)
        log.info("docker-phpipam-sync starting (v4 + v6 support)")
        log.info("=" * 60)
        # After a restart, resume from the last event seen so Docker replays
        # anything that happened in the gap. On first start, capture the time
        # BEFORE the (slow) initial sweep so Docker replays any start/die we
        # miss while it runs.
        if self._since is None:
            self._since = int(time.time())
        since = self._since
        with self._lock:
            for c in self.docker.containers.list(ignore_removed=True):
                self._handle("start", c.id)
            self._reconcile()

        for event in self.docker.events(
            decode=True,
            since=since,
            filters={"type": "container", "event": ["start", "die"]},
        ):
            # status/id are deprecated aliases of Action/Actor.ID.
            action = event.get("Action") or event.get("status")
            cid = (event.get("Actor") or {}).get("ID") or event.get("id", "")
            # React ONLY to 'die': it fires exactly once per container exit
            # (stop/kill/crash alike), unlike 'kill'/'stop' which fire early.
            with self._lock:
                self._handle(action, cid)
            self._since = event.get("time", self._since)

if __name__ == "__main__":
    signal.signal(signal.SIGINT,  lambda s, f: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    # The daemon instance is kept across restarts so tracked records, pending
    # deletes and the event resume point survive a crash.
    daemon = None
    while True:
        try:
            daemon = daemon or DockerPhpIpamDaemon()
            daemon.run()
            log.warning("Docker event stream ended. Restarting in 10s...")
        except Exception as exc:
            log.exception(f"Daemon crashed: {exc}. Restarting in 10s...")
        time.sleep(10)
