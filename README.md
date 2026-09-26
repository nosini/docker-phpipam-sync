# docker-phpipam-sync

A daemon that watches the Docker socket and automatically registers/removes
container IPs in phpIPAM via its REST API.

## What it does

| Event | Action |
|---|---|
| Container **starts** | Registers all container IPs in the matching phpIPAM subnet |
| Container **stops/dies** | Removes IPs from phpIPAM — unless another running container still holds the same IP |
| **Daemon starts** | Syncs all currently running containers first, then removes its own records whose container is gone |

---

## phpIPAM setup

1. Go to **Administration → API** in phpIPAM.
2. Create a new API application:
   - App ID: `docker` (or whatever you set in `PHPIPAM_APP_ID`)
   - App security: **SSL with App code token**
   - App permissions: **Read/Write**
3. Copy the generated **App code** — that is your `PHPIPAM_TOKEN`.
4. Make sure the subnets you want IPs registered in already exist in phpIPAM.
   The daemon matches Docker network CIDRs to phpIPAM subnets automatically.

---

## Running with Docker Compose (recommended)

```bash
# 1. Edit docker-compose.yml and fill in your phpIPAM URL, app ID, and token
# 2. Build and start
docker compose up -d --build

# View logs
docker compose logs -f
```

---

## Running directly with Python

```bash
pip install -r requirements.txt

export PHPIPAM_URL="http://192.168.1.10"
export PHPIPAM_APP_ID="docker"
export PHPIPAM_TOKEN="your_static_token"

python docker_phpipam_sync.py
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `PHPIPAM_URL` | `http://phpipam` | Base URL of your phpIPAM instance |
| `PHPIPAM_APP_ID` | `docker` | API application ID |
| `PHPIPAM_TOKEN` | — | Static app token from phpIPAM |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose, `WARNING` for quiet |
| `SYNC_INSTANCE_ID` | Docker engine hostname | Identifies this host in the note of each record it owns. Must be unique per Docker host when several hosts share one phpIPAM |

## Record ownership

Every record the daemon creates gets the note
`Managed by docker-phpipam-sync (<SYNC_INSTANCE_ID>)`. The daemon only ever
deletes records carrying its own host's note, so several Docker hosts can
share a phpIPAM instance. Records written by older versions (note without the
host part) are re-adopted and re-stamped when a running container holds their
IP; stale ones must be removed by hand.

---

## Subnet matching

The daemon tries to find the right phpIPAM subnet in two steps:

1. Exact CIDR match — e.g. Docker network `172.20.0.0/16` → phpIPAM subnet `172.20.0.0/16`
2. If no exact match, most-specific subnet that *contains* the IP is used.

If no subnet is found for a network, a warning is logged and that IP is skipped.
