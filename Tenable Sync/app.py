import os
import json
import time
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import keyring
import requests
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# ----------------------------
# Config
# ----------------------------
APP_NAME = "TSync"
DEFAULT_TENABLE_BASE = "https://cloud.tenable.com"

DATA_DIR = r"C:\TSync"
DB_PATH = os.path.join(DATA_DIR, "tsync.db")

# Tenable keyring
KEYRING_SERVICE_TENABLE = "TSync-Tenable"
KEY_ACCESS = "access_key"
KEY_SECRET = "secret_key"
KEY_BASEURL = "base_url"

# JumpCloud keyring (2 accounts)
KEYRING_SERVICE_JC = "TSync-JumpCloud"
JC1_APIKEY = "jc1_api_key"
JC1_ORGID = "jc1_org_id"
JC2_APIKEY = "jc2_api_key"
JC2_ORGID = "jc2_org_id"
JC_BASE_URL = "https://console.jumpcloud.com"

# Friendly labels (requested)
JC1_LABEL = "Account1"
JC2_LABEL = "Account2"

# Tenable tag constants (NEW)
ORG_TAG_CATEGORY = "Org"
ORG_TAG_ACCOUNT1_VALUE = "Account1"
ORG_TAG_ACCOUNT2_VALUE = "Account2"

# ----------------------------
# App
# ----------------------------
app = FastAPI(title="TSync - Tenable + JumpCloud Asset Sync")
templates = Jinja2Templates(directory="templates")

# ----------------------------
# DB helpers
# ----------------------------
def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)

def db() -> sqlite3.Connection:
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with db() as conn:
        # Tenable assets (UNCHANGED)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS assets (
            id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)

        # Tenable sync runs (UNCHANGED)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            detail TEXT
        )
        """)

        # JumpCloud systems cache (UNCHANGED)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS jumpcloud_systems (
            account INTEGER NOT NULL,
            id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account, id)
        )
        """)

        # JumpCloud sync runs (UNCHANGED)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS jumpcloud_sync_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            detail TEXT
        )
        """)

        conn.commit()

@app.on_event("startup")
def _startup():
    init_db()

# ----------------------------
# Keyring helpers - Tenable (UNCHANGED behavior)
# ----------------------------
def get_tenable_creds() -> Tuple[Optional[str], Optional[str], str]:
    access = keyring.get_password(KEYRING_SERVICE_TENABLE, KEY_ACCESS)
    secret = keyring.get_password(KEYRING_SERVICE_TENABLE, KEY_SECRET)
    base_url = keyring.get_password(KEYRING_SERVICE_TENABLE, KEY_BASEURL) or DEFAULT_TENABLE_BASE
    return access, secret, base_url

def set_tenable_creds(access: str, secret: str, base_url: str) -> None:
    keyring.set_password(KEYRING_SERVICE_TENABLE, KEY_ACCESS, access.strip())
    keyring.set_password(KEYRING_SERVICE_TENABLE, KEY_SECRET, secret.strip())
    keyring.set_password(KEYRING_SERVICE_TENABLE, KEY_BASEURL, (base_url.strip() or DEFAULT_TENABLE_BASE))

def clear_tenable_creds() -> None:
    for user in (KEY_ACCESS, KEY_SECRET, KEY_BASEURL):
        try:
            keyring.delete_password(KEYRING_SERVICE_TENABLE, user)
        except keyring.errors.PasswordDeleteError:
            pass

def tenable_headers(access: str, secret: str) -> Dict[str, str]:
    return {
        "X-ApiKeys": f"accessKey={access}; secretKey={secret}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "TSync/1.0"
    }

# ----------------------------
# Keyring helpers - JumpCloud (UNCHANGED)
# ----------------------------
def get_jumpcloud_creds(account: int) -> Tuple[Optional[str], Optional[str]]:
    if account == 1:
        api_key = keyring.get_password(KEYRING_SERVICE_JC, JC1_APIKEY)
        org_id = keyring.get_password(KEYRING_SERVICE_JC, JC1_ORGID)
        return api_key, org_id
    if account == 2:
        api_key = keyring.get_password(KEYRING_SERVICE_JC, JC2_APIKEY)
        org_id = keyring.get_password(KEYRING_SERVICE_JC, JC2_ORGID)
        return api_key, org_id
    return None, None

def set_jumpcloud_creds(account: int, api_key: str, org_id: str) -> None:
    api_key = (api_key or "").strip()
    org_id = (org_id or "").strip()
    if account == 1:
        if api_key:
            keyring.set_password(KEYRING_SERVICE_JC, JC1_APIKEY, api_key)
        if org_id:
            keyring.set_password(KEYRING_SERVICE_JC, JC1_ORGID, org_id)
        return
    if account == 2:
        if api_key:
            keyring.set_password(KEYRING_SERVICE_JC, JC2_APIKEY, api_key)
        if org_id:
            keyring.set_password(KEYRING_SERVICE_JC, JC2_ORGID, org_id)
        return

def clear_jumpcloud_creds(account: Optional[int] = None) -> None:
    targets = []
    if account is None:
        targets = [JC1_APIKEY, JC1_ORGID, JC2_APIKEY, JC2_ORGID]
    elif account == 1:
        targets = [JC1_APIKEY, JC1_ORGID]
    elif account == 2:
        targets = [JC2_APIKEY, JC2_ORGID]

    for user in targets:
        try:
            keyring.delete_password(KEYRING_SERVICE_JC, user)
        except keyring.errors.PasswordDeleteError:
            pass

def jumpcloud_headers(api_key: str, org_id: Optional[str]) -> Dict[str, str]:
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "TSync/1.0",
        "x-api-key": api_key,
    }
    if org_id and org_id.strip():
        h["x-org-id"] = org_id.strip()
    return h

def mask_key(k: Optional[str]) -> str:
    if not k:
        return ""
    k = k.strip()
    if len(k) <= 6:
        return "*" * len(k)
    return f"{k[:2]}***{k[-4:]}"

# ----------------------------
# Normalization helpers (Tenable extraction - UNCHANGED)
# ----------------------------
def pick_first_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            s = pick_first_string(item)
            if s:
                return s
        return ""
    if isinstance(value, dict):
        for k in ("hostname", "fqdn", "name", "value", "id"):
            if k in value:
                s = pick_first_string(value.get(k))
                if s:
                    return s
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""

def join_list_strings(value: Any, limit: int = 3) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            s = pick_first_string(item)
            if s:
                out.append(s)
            if len(out) >= limit:
                break
        return ", ".join(out)
    if isinstance(value, dict):
        return pick_first_string(value)
    return pick_first_string(value)

def as_iso_if_epoch(v: Any) -> str:
    if isinstance(v, (int, float)):
        try:
            if v > 10_000_000_000:
                v = v / 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
        except Exception:
            return str(v)
    return ""

def find_first_by_keys_recursive(obj: Any, keys_lower: set, max_depth: int = 6, _depth: int = 0) -> Any:
    if _depth > max_depth:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in keys_lower:
                return v
        for _, v in obj.items():
            found = find_first_by_keys_recursive(v, keys_lower, max_depth=max_depth, _depth=_depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first_by_keys_recursive(item, keys_lower, max_depth=max_depth, _depth=_depth + 1)
            if found is not None:
                return found
    return None

# ----------------------------
# Tenable field extraction (UNCHANGED)
# ----------------------------
def extract_hostname(a: Dict[str, Any]) -> str:
    candidates: List[Any] = [
        a.get("hostname"),
        a.get("hostnames"),
        a.get("agent_name"),
        a.get("agent_names"),
        a.get("fqdn"),
        a.get("fqdns"),
        a.get("netbios_name"),
        a.get("netbios_names"),
        a.get("netbios"),
        a.get("computer_name"),
        a.get("name"),
        a.get("names"),
        a.get("display_name"),
    ]

    interfaces = a.get("interfaces")
    if isinstance(interfaces, list):
        for iface in interfaces:
            if isinstance(iface, dict):
                candidates.insert(0, iface.get("fqdn"))
                candidates.insert(0, iface.get("fqdns"))
                candidates.insert(0, iface.get("hostname"))
                candidates.insert(0, iface.get("hostnames"))
                candidates.insert(0, iface.get("name"))

    for c in candidates:
        s = pick_first_string(c)
        if s:
            return s
    return ""

def extract_os(a: Dict[str, Any]) -> str:
    candidates: List[Any] = [
        a.get("operating_system"),
        a.get("operating_systems"),
        a.get("os"),
        a.get("platform"),
        a.get("system_type"),
    ]

    src = a.get("sources")
    if isinstance(src, list):
        for s in src:
            if isinstance(s, dict):
                candidates.append(s.get("operating_system"))
                candidates.append(s.get("operating_systems"))
                candidates.append(s.get("os"))
                candidates.append(s.get("platform"))

    for c in candidates:
        s = join_list_strings(c, limit=3)
        if s:
            return s
    return ""

def extract_last_seen(a: Dict[str, Any]) -> str:
    direct_candidates: List[Any] = [
        a.get("last_seen"),
        a.get("last_seen_at"),
        a.get("last_seen_date"),
        a.get("last_seen_time"),
        a.get("last_seen_timestamp"),
        a.get("last_observed"),
        a.get("last_observed_at"),
        a.get("last_connect"),
        a.get("last_connection"),
        a.get("last_connected"),
        a.get("last_authenticated_scan"),
        a.get("last_unauthenticated_scan"),
        a.get("last_scan_time"),
        a.get("last_scan"),
        a.get("last_scanned"),
        a.get("last_found"),
        a.get("last_found_at"),
    ]

    agent = a.get("agent")
    if isinstance(agent, dict):
        direct_candidates.extend([
            agent.get("last_seen"),
            agent.get("last_connect"),
            agent.get("last_authenticated_scan"),
        ])

    interfaces = a.get("interfaces")
    if isinstance(interfaces, list):
        for iface in interfaces:
            if isinstance(iface, dict):
                direct_candidates.extend([
                    iface.get("last_seen"),
                    iface.get("last_seen_at"),
                    iface.get("last_observed"),
                    iface.get("last_observed_at"),
                ])

    for c in direct_candidates:
        iso = as_iso_if_epoch(c)
        if iso:
            return iso
        s = pick_first_string(c)
        if s:
            return s

    keys_lower = {
        "last_seen", "last_seen_at", "last_seen_date", "last_seen_time", "last_seen_timestamp",
        "last_observed", "last_observed_at",
        "last_connect", "last_connection", "last_connected",
        "last_scan", "last_scan_time", "last_scanned",
        "last_authenticated_scan", "last_unauthenticated_scan",
        "last_found", "last_found_at",
    }
    found = find_first_by_keys_recursive(a, keys_lower, max_depth=7)
    if found is not None:
        iso = as_iso_if_epoch(found)
        if iso:
            return iso
        s = pick_first_string(found)
        if s:
            return s

    return ""

def extract_tags(a: Dict[str, Any]) -> str:
    tags = a.get("tags") or a.get("tag") or a.get("labels")
    out: List[str] = []

    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str):
                if t.strip():
                    out.append(t.strip())
            elif isinstance(t, dict):
                k = t.get("key") or t.get("category") or t.get("name") or t.get("tag_key")
                v = t.get("value") or t.get("tag_value")
                if k and v:
                    out.append(f"{k}:{v}")
                elif v:
                    out.append(str(v))
                elif k:
                    out.append(str(k))

    elif isinstance(tags, dict):
        for k, v in tags.items():
            out.append(f"{k}:{v}")

    elif isinstance(tags, str):
        out.append(tags)

    out = [x for x in out if x.strip()]
    return ", ".join(out[:10])

def present_asset(a: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": a.get("id") or a.get("uuid") or a.get("asset_id") or "",
        "hostname": extract_hostname(a),
        "os": extract_os(a),
        "last_seen": extract_last_seen(a),
        "tags": extract_tags(a),
        "_raw": a,
    }

# ----------------------------
# Tenable Export Assets v2 flow (UNCHANGED)
# ----------------------------
def export_assets_all(access: str, secret: str, base_url: str, chunk_size: int = 5000) -> Dict[str, Any]:
    h = tenable_headers(access, secret)

    url_export = f"{base_url}/assets/v2/export"
    body = {"chunk_size": chunk_size}
    r = requests.post(url_export, headers=h, json=body, timeout=60)
    r.raise_for_status()
    export_uuid = r.json().get("export_uuid") or r.json().get("uuid") or r.json().get("id")
    if not export_uuid:
        raise RuntimeError(f"Unexpected export response (missing export_uuid): {r.text[:500]}")

    url_status = f"{base_url}/assets/export/{export_uuid}/status"
    chunks_available: List[int] = []
    status = "PROCESSING"

    for _ in range(300):
        rs = requests.get(url_status, headers=h, timeout=60)
        rs.raise_for_status()
        data = rs.json()

        status = data.get("status") or data.get("state") or status
        chunks_available = data.get("chunks_available") or data.get("chunks") or chunks_available

        if str(status).upper() in ("FINISHED", "COMPLETE", "COMPLETED"):
            break
        if str(status).upper() in ("CANCELLED", "CANCELED", "ERROR", "FAILED"):
            raise RuntimeError(f"Export failed with status={status}: {json.dumps(data)[:500]}")
        time.sleep(2)

    if str(status).upper() not in ("FINISHED", "COMPLETE", "COMPLETED"):
        raise RuntimeError(f"Timed out waiting for export to finish. Last status={status}")

    chunks_data: List[Dict[str, Any]] = []
    for chunk_id in chunks_available:
        url_chunk = f"{base_url}/assets/export/{export_uuid}/chunks/{chunk_id}"
        rc = requests.get(url_chunk, headers=h, timeout=120)
        rc.raise_for_status()
        chunk_payload = rc.json()

        assets = chunk_payload.get("assets") if isinstance(chunk_payload, dict) else None
        if assets is None and isinstance(chunk_payload, list):
            assets = chunk_payload
        if assets is None:
            assets = [chunk_payload]

        chunks_data.append({"chunk_id": chunk_id, "assets": assets})

    return {"export_uuid": export_uuid, "chunks": chunks_data}

def upsert_assets(assets: List[Dict[str, Any]]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with db() as conn:
        for a in assets:
            asset_id = a.get("id") or a.get("uuid") or a.get("asset_id")
            if not asset_id:
                continue
            conn.execute(
                "INSERT INTO assets (id, payload_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                (str(asset_id), json.dumps(a), now)
            )
            count += 1
        conn.commit()
    return count

def get_asset_count() -> int:
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM assets").fetchone()
        return int(row["c"])

def get_assets_page(offset: int, limit: int) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM assets ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        payload = json.loads(r["payload_json"])
        out.append(present_asset(payload))
    return out

# ----------------------------
# Local cache helpers (NEW for Tags page)
# ----------------------------
def get_all_assets_raw() -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT payload_json FROM assets").fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            out.append(json.loads(r["payload_json"]))
        except Exception:
            pass
    return out

# ----------------------------
# Sync timestamp helpers
# ----------------------------
def get_last_sync(table: str) -> dict:
    """Returns the most recent completed sync run info for the given table."""
    try:
        with db() as conn:
            row = conn.execute(
                f"SELECT finished_at, status, detail FROM {table} "
                f"WHERE status='SUCCESS' ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
        if row and row['finished_at']:
            return {
                "finished_at": row['finished_at'],
                "status": row['status'],
                "detail": row['detail'] or "",
            }
    except Exception:
        pass
    return {"finished_at": None, "status": None, "detail": ""}


def get_cache_age_hours(sync_info: dict) -> float | None:
    """Returns hours since last successful sync, or None if never synced."""
    ts = sync_info.get("finished_at")
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


# ----------------------------
# JumpCloud cache helpers (UNCHANGED)
# ----------------------------
def jc_upsert_systems(account: int, systems: List[Dict[str, Any]]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with db() as conn:
        for s in systems:
            sid = s.get("id") or s.get("_id") or s.get("systemId") or s.get("system_id")
            if not sid:
                continue
            conn.execute(
                "INSERT INTO jumpcloud_systems (account, id, payload_json, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(account, id) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                (int(account), str(sid), json.dumps(s), now)
            )
            count += 1
        conn.commit()
    return count

def jc_count(account: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM jumpcloud_systems WHERE account=?", (int(account),)).fetchone()
        return int(row["c"])

def jc_get_page_rows(account: int, offset: int, limit: int) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM jumpcloud_systems WHERE account=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (int(account), int(limit), int(offset))
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(json.loads(r["payload_json"]))
    return out


def jc_search_rows(q: str) -> List[Dict[str, Any]]:
    """Full-DB search across both accounts using SQLite LIKE on raw JSON payload."""
    pattern = f"%{q.lower()}%"
    with db() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM jumpcloud_systems WHERE LOWER(payload_json) LIKE ? ORDER BY account, updated_at DESC LIMIT 200",
            (pattern,)
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            out.append(json.loads(r["payload_json"]))
        except Exception:
            pass
    return out

def jc_get_all_raw(account: int) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT payload_json FROM jumpcloud_systems WHERE account=?", (int(account),)).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            out.append(json.loads(r["payload_json"]))
        except Exception:
            pass
    return out

# ----------------------------
# JumpCloud field extraction (UNCHANGED)
# ----------------------------
def jc_extract_id(s: Dict[str, Any]) -> str:
    return str(s.get("id") or s.get("_id") or s.get("systemId") or s.get("system_id") or "")

def jc_extract_hostname(s: Dict[str, Any]) -> str:
    for k in ("hostname", "displayName", "display_name", "name", "systemHostname", "system_hostname"):
        v = s.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    sys = s.get("system") if isinstance(s.get("system"), dict) else None
    if sys:
        for k in ("hostname", "displayName", "name"):
            v = sys.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""

def jc_extract_os(s: Dict[str, Any]) -> str:
    for k in ("os", "osFamily", "os_family", "platform", "version"):
        v = s.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    sys = s.get("system") if isinstance(s.get("system"), dict) else None
    if sys:
        for k in ("os", "osFamily", "platform"):
            v = sys.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""

def jc_extract_last_seen(s: Dict[str, Any]) -> str:
    candidates = [
        s.get("lastContact"),
        s.get("last_contact"),
        s.get("lastSeen"),
        s.get("last_seen"),
        s.get("lastConnected"),
        s.get("last_connected"),
        s.get("lastCheckIn"),
        s.get("last_check_in"),
        s.get("lastHeartbeat"),
        s.get("last_heartbeat"),
        s.get("lastLogin"),
        s.get("last_login"),
    ]
    for c in candidates:
        iso = as_iso_if_epoch(c)
        if iso:
            return iso
        if isinstance(c, str) and c.strip():
            return c.strip()

    sys = s.get("system") if isinstance(s.get("system"), dict) else None
    if sys:
        for k in ("lastContact", "lastSeen", "lastConnected", "lastCheckIn"):
            c = sys.get(k)
            iso = as_iso_if_epoch(c)
            if iso:
                return iso
            if isinstance(c, str) and c.strip():
                return c.strip()

    return ""

def jc_extract_tags(s: Dict[str, Any]) -> str:
    tags = s.get("tags") or s.get("tag") or s.get("labels")
    out: List[str] = []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str) and t.strip():
                out.append(t.strip())
            elif isinstance(t, dict):
                k = t.get("key") or t.get("name")
                v = t.get("value")
                if k and v:
                    out.append(f"{k}:{v}")
                elif k:
                    out.append(str(k))
                elif v:
                    out.append(str(v))
    elif isinstance(tags, dict):
        for k, v in tags.items():
            out.append(f"{k}:{v}")
    elif isinstance(tags, str) and tags.strip():
        out.append(tags.strip())

    return ", ".join(out[:10])

def present_jc_system(s: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": jc_extract_id(s),
        "hostname": jc_extract_hostname(s),
        "os": jc_extract_os(s),
        "last_seen": jc_extract_last_seen(s),
        "tags": jc_extract_tags(s),
        "_raw": s,
    }

# ----------------------------
# JumpCloud API pull (UNCHANGED)
# ----------------------------
def jc_fetch_all_systems(api_key: str, org_id: Optional[str]) -> List[Dict[str, Any]]:
    headers = jumpcloud_headers(api_key, org_id)
    limit = 100
    skip = 0
    all_items: List[Dict[str, Any]] = []

    while True:
        url = f"{JC_BASE_URL}/api/systems"
        params = {"limit": limit, "skip": skip}
        r = requests.get(url, headers=headers, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("results") or data.get("items") or []
        else:
            items = []

        if not items:
            break

        all_items.extend(items)
        skip += limit
        if skip > 500000:
            break

    return all_items

# ----------------------------
# Tenable tag helpers (NEW)
# ----------------------------
def hostname_key15(name: str) -> str:
    return (name or "").strip().lower()[:15]

def tenable_request_json(access: str, secret: str, base_url: str, method: str, path: str,
                         params: Optional[Dict[str, Any]] = None,
                         body: Optional[Dict[str, Any]] = None,
                         timeout: int = 60) -> Any:
    h = tenable_headers(access, secret)
    url = f"{base_url.rstrip('/')}{path}"
    m = method.upper().strip()

    if m == "GET":
        r = requests.get(url, headers=h, params=params, timeout=timeout)
    elif m == "POST":
        r = requests.post(url, headers=h, params=params, json=body, timeout=timeout)
    else:
        raise ValueError(f"Unsupported method: {method}")

    r.raise_for_status()
    if r.text:
        try:
            return r.json()
        except Exception:
            return r.text
    return None

def tenable_find_or_create_tag_value_uuid(access: str, secret: str, base_url: str, category_name: str, value: str) -> str:
    category_name = category_name.strip()
    value = value.strip()

    # Try find
    data = tenable_request_json(
        access, secret, base_url,
        "GET", "/tags/values",
        params={"f": f"category_name:eq:{category_name}"},
        timeout=60
    )

    values: List[Dict[str, Any]] = []
    if isinstance(data, list):
        values = data
    elif isinstance(data, dict):
        values = data.get("values") or data.get("tag_values") or data.get("items") or data.get("results") or []
        if not isinstance(values, list):
            values = []

    for tv in values:
        cat = tv.get("category_name") or ""
        val = tv.get("value") or ""
        if str(cat).strip().lower() == category_name.lower() and str(val).strip().lower() == value.lower():
            uuid = tv.get("uuid") or tv.get("id")
            if uuid:
                return str(uuid)

    # Create
    created = tenable_request_json(
        access, secret, base_url,
        "POST", "/tags/values",
        body={"category_name": category_name, "value": value},
        timeout=60
    )
    if isinstance(created, dict):
        uuid = created.get("uuid") or created.get("id")
        if uuid:
            return str(uuid)

    raise RuntimeError(f"Unable to find or create tag value uuid for {category_name}:{value}")

def tenable_assign_tags_to_assets(access: str, secret: str, base_url: str,
                                 asset_uuids: List[str], tag_value_uuids: List[str],
                                 action: str = "add") -> str:
    if not asset_uuids:
        return ""
    body = {"action": action, "assets": asset_uuids, "tags": tag_value_uuids}
    res = tenable_request_json(access, secret, base_url, "POST", "/tags/assets/assignments", body=body, timeout=120)
    if isinstance(res, dict):
        return str(res.get("job_uuid") or res.get("uuid") or res.get("id") or "")
    return ""

# ----------------------------
# Routes (UI)
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    page: int = 1,
    per_page: int = 10,
    jc_page: int = 1,
    jc_per_page: int = 10,
    jc_search: str = ""
):
    # Tenable (UNCHANGED)
    access, secret, base_url = get_tenable_creds()
    total = get_asset_count()

    per_page = int(per_page)
    if per_page not in (10, 50, 100, 200, 500):
        per_page = 10

    page = max(1, int(page))
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    assets = get_assets_page(offset=offset, limit=per_page)

    # JumpCloud (UNCHANGED)
    jc_per_page = int(jc_per_page)
    if jc_per_page not in (10, 50, 100, 200, 500):
        jc_per_page = 10
    jc_page_num = max(1, int(jc_page))

    jc1_key, jc1_org = get_jumpcloud_creds(1)
    jc2_key, jc2_org = get_jumpcloud_creds(2)

    jc1_total = jc_count(1)
    jc2_total = jc_count(2)
    jc_total = jc1_total + jc2_total

    jc_total_pages = max(1, (jc_total + jc_per_page - 1) // jc_per_page)
    if jc_page_num > jc_total_pages:
        jc_page_num = jc_total_pages
    jc_offset = (jc_page_num - 1) * jc_per_page

    jc_search = jc_search.strip()
    combined: List[Dict[str, Any]] = []

    if jc_search:
        # Server-side search across full DB
        raw_results = jc_search_rows(jc_search)
        for row in raw_results:
            acct = row.get("_account_hint", 0)
            p = present_jc_system(row)
            # Determine label by checking which account the row belongs to
            with db() as conn:
                r = conn.execute(
                    "SELECT account FROM jumpcloud_systems WHERE id=?",
                    (str(p["id"]),)
                ).fetchone()
            if r:
                p["_account"] = r["account"]
                p["_label"] = JC1_LABEL if r["account"] == 1 else JC2_LABEL
            else:
                p["_account"] = 0
                p["_label"] = "?"
            combined.append(p)
        jc_total_search = len(combined)
        jc_total_pages = max(1, (jc_total_search + jc_per_page - 1) // jc_per_page)
        if jc_page_num > jc_total_pages:
            jc_page_num = jc_total_pages
        jc_offset = (jc_page_num - 1) * jc_per_page
        combined_page = combined[jc_offset: jc_offset + jc_per_page]
    else:
        need = jc_offset + jc_per_page
        chunk = max(need, 500)
        jc1_rows = jc_get_page_rows(1, offset=0, limit=chunk)
        jc2_rows = jc_get_page_rows(2, offset=0, limit=chunk)
        for row in jc1_rows:
            p = present_jc_system(row)
            p["_account"] = 1
            p["_label"] = JC1_LABEL
            combined.append(p)
        for row in jc2_rows:
            p = present_jc_system(row)
            p["_account"] = 2
            p["_label"] = JC2_LABEL
            combined.append(p)
        combined_page = combined[jc_offset: jc_offset + jc_per_page]

    tenable_last_sync = get_last_sync("sync_runs")
    jc_last_sync      = get_last_sync("jumpcloud_sync_runs")
    tenable_age_hours = get_cache_age_hours(tenable_last_sync)
    jc_age_hours      = get_cache_age_hours(jc_last_sync)

    return templates.TemplateResponse("index.html", {
        "request": request,

        # Tenable
        "has_keys": bool(access and secret),
        "base_url": base_url,
        "assets_count": total,
        "assets": assets,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "tenable_last_sync": tenable_last_sync,
        "tenable_cache_stale": tenable_age_hours is None or tenable_age_hours > 24,

        # JumpCloud labels/counts
        "jc1_label": JC1_LABEL,
        "jc2_label": JC2_LABEL,
        "jc1_configured": bool(jc1_key),  # org may be optional
        "jc2_configured": bool(jc2_key),
        "jc_total_count": jc_total,
        "jc1_count": jc1_total,
        "jc2_count": jc2_total,

        "jc_page": jc_page_num,
        "jc_per_page": jc_per_page,
        "jc_total_pages": jc_total_pages,
        "jc_systems": combined_page,
        "jc_search": jc_search,
        "jc_last_sync": jc_last_sync,
        "jc_cache_stale": jc_age_hours is None or jc_age_hours > 24,
    })

@app.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request):
    # Tenable
    access, secret, base_url = get_tenable_creds()

    # JumpCloud
    jc1_key, jc1_org = get_jumpcloud_creds(1)
    jc2_key, jc2_org = get_jumpcloud_creds(2)

    return templates.TemplateResponse("settings.html", {
        "request": request,

        # Tenable
        "has_keys": bool(access and secret),
        "base_url": base_url,
        "masked_access": mask_key(access),
        "masked_secret": mask_key(secret),

        # JumpCloud + labels
        "jc1_label": JC1_LABEL,
        "jc2_label": JC2_LABEL,

        "jc1_has_key": bool(jc1_key),
        "jc1_org": jc1_org or "",
        "jc1_masked_key": mask_key(jc1_key),

        "jc2_has_key": bool(jc2_key),
        "jc2_org": jc2_org or "",
        "jc2_masked_key": mask_key(jc2_key),
    })

# --- Separate saves so JC doesn't require Tenable and vice-versa (UNCHANGED)
@app.post("/settings/tenable")
def settings_tenable_post(
    access_key: str = Form(...),
    secret_key: str = Form(...),
    base_url: str = Form(DEFAULT_TENABLE_BASE),
):
    set_tenable_creds(access_key, secret_key, base_url)
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/jumpcloud")
def settings_jumpcloud_post(
    jc1_api_key: str = Form(""),
    jc1_org_id: str = Form(""),
    jc2_api_key: str = Form(""),
    jc2_org_id: str = Form(""),
):
    if (jc1_api_key or "").strip() or (jc1_org_id or "").strip():
        set_jumpcloud_creds(1, jc1_api_key, jc1_org_id)
    if (jc2_api_key or "").strip() or (jc2_org_id or "").strip():
        set_jumpcloud_creds(2, jc2_api_key, jc2_org_id)
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/clear")
def settings_clear():
    clear_tenable_creds()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/clear_jumpcloud")
def settings_clear_jumpcloud():
    clear_jumpcloud_creds(None)
    return RedirectResponse(url="/settings", status_code=303)

# ----------------------------
# NEW: Tags page
# ----------------------------
@app.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request):
    access, secret, base_url = get_tenable_creds()
    jc_cached = (jc_count(1) + jc_count(2)) > 0
    tenable_last_sync = get_last_sync("sync_runs")
    jc_last_sync      = get_last_sync("jumpcloud_sync_runs")
    tenable_age_hours = get_cache_age_hours(tenable_last_sync)
    jc_age_hours      = get_cache_age_hours(jc_last_sync)

    return templates.TemplateResponse("tags.html", {
        "request": request,
        "tenable_has_keys": bool(access and secret),
        "jc_cached": bool(jc_cached),
        "base_url": base_url,
        "tenable_last_sync": tenable_last_sync,
        "jc_last_sync": jc_last_sync,
        "tenable_cache_stale": tenable_age_hours is None or tenable_age_hours > 24,
        "jc_cache_stale": jc_age_hours is None or jc_age_hours > 24,
    })

# ----------------------------
# NEW: Org tagging endpoint
# ----------------------------
@app.post("/api/tenable/tag-org")
def api_tenable_tag_org():
    access, secret, base_url = get_tenable_creds()
    if not access or not secret:
        return JSONResponse({"ok": False, "error": "Missing Tenable API keys. Go to /settings."}, status_code=400)

    jc1_total = jc_count(1)
    jc2_total = jc_count(2)
    if jc1_total == 0 and jc2_total == 0:
        return JSONResponse({"ok": False, "error": "No JumpCloud devices cached yet. Run JumpCloud sync first."}, status_code=400)

    try:
        # Build match-key sets from JC caches
        acct1_keys: set = set()
        acct2_keys: set = set()

        for s in jc_get_all_raw(1):
            k = hostname_key15(jc_extract_hostname(s))
            if k:
                acct1_keys.add(k)

        for s in jc_get_all_raw(2):
            k = hostname_key15(jc_extract_hostname(s))
            if k:
                acct2_keys.add(k)

        tenable_assets = get_all_assets_raw()

        acct1_assets: List[str] = []
        acct2_assets: List[str] = []
        both_assets: List[str] = []
        collisions = 0
        no_match = 0
        missing_hostname = 0

        for a in tenable_assets:
            asset_uuid = str(a.get("id") or a.get("uuid") or a.get("asset_id") or "").strip()
            if not asset_uuid:
                continue

            hn = extract_hostname(a)
            if not hn:
                missing_hostname += 1
                continue

            k = hostname_key15(hn)
            in_b = k in acct1_keys
            in_f = k in acct2_keys

            if in_b and in_f:
                collisions += 1
                both_assets.append(asset_uuid)
            elif in_b:
                acct1_assets.append(asset_uuid)
            elif in_f:
                acct2_assets.append(asset_uuid)
            else:
                no_match += 1

        # Ensure tag values exist in Tenable
        acct1_tag_uuid    = tenable_find_or_create_tag_value_uuid(access, secret, base_url, ORG_TAG_CATEGORY, ORG_TAG_ACCOUNT1_VALUE)
        acct2_tag_uuid = tenable_find_or_create_tag_value_uuid(access, secret, base_url, ORG_TAG_CATEGORY, ORG_TAG_ACCOUNT2_VALUE)
        both_tag_uuid      = tenable_find_or_create_tag_value_uuid(access, secret, base_url, ORG_TAG_CATEGORY, "Both Accounts")

        # Batch assignments
        def batches(lst: List[str], size: int) -> List[List[str]]:
            return [lst[i:i+size] for i in range(0, len(lst), size)]

        batch_size = 1000
        jobs_acct1: List[str] = []
        jobs_acct2: List[str] = []
        jobs_both: List[str] = []

        for b in batches(acct1_assets, batch_size):
            job = tenable_assign_tags_to_assets(access, secret, base_url, b, [acct1_tag_uuid], action="add")
            if job:
                jobs_acct1.append(job)

        for f in batches(acct2_assets, batch_size):
            job = tenable_assign_tags_to_assets(access, secret, base_url, f, [acct2_tag_uuid], action="add")
            if job:
                jobs_acct2.append(job)

        for bt in batches(both_assets, batch_size):
            job = tenable_assign_tags_to_assets(access, secret, base_url, bt, [both_tag_uuid], action="add")
            if job:
                jobs_both.append(job)

        return JSONResponse({
            "ok": True,
            "jc_counts": {"Account1": jc1_total, "Account2": jc2_total},
            "tenable_assets_total": len(tenable_assets),
            "match_counts": {
                "acct1_assets_to_tag": len(acct1_assets),
                "acct2_assets_to_tag": len(acct2_assets),
                "both_accounts_tagged": len(both_assets),
                "no_match": int(no_match),
                "missing_hostname": int(missing_hostname),
            },
            "tag_uuids": {[0m"Org:Account1": acct1_tag_uuid, "Org:Account2": acct2_tag_uuid, "Org:Both Accounts": both_tag_uuid},
            "jobs": {"Account1": jobs_acct1, "Account2": jobs_acct2, "Both Accounts": jobs_both},
            "note": "Tags applied via Tenable async jobs. 'Both Accounts' devices appear in both JC orgs — likely mid-migration. Review and re-tag once migration is complete."
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# ----------------------------
# Tag: Department + OS (live from Tenable)
# ----------------------------
import re as _re

_RE_CSD       = _re.compile(r"-csd",      _re.IGNORECASE)
_RE_SALES     = _re.compile(r"-sal",      _re.IGNORECASE)
_RE_SET       = _re.compile(r"-set",      _re.IGNORECASE)
_RE_WORKSPACE = _re.compile(r"^wsamzn-",  _re.IGNORECASE)
_RE_MAC       = _re.compile(r"mac|darwin",_re.IGNORECASE)
_RE_WINDOWS   = _re.compile(r"windows",   _re.IGNORECASE)

def _extract_agent_name(a: Dict[str, Any]) -> str:
    v = a.get("agent_name")
    if isinstance(v, str) and v.strip():
        return v.strip()
    v = a.get("agent_names")
    if isinstance(v, list):
        for item in v:
            if isinstance(item, str) and item.strip():
                return item.strip()
    agent = a.get("agent")
    if isinstance(agent, dict):
        for k in ("name", "agent_name", "hostname"):
            av = agent.get(k)
            if isinstance(av, str) and av.strip():
                return av.strip()
    return ""

def _extract_os(a: Dict[str, Any]) -> str:
    for k in ("operating_system", "operating_systems", "os", "platform"):
        v = a.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    return ""

@app.post("/api/tenable/tag-department")
def api_tenable_tag_department():
    """
    Tags all assets with OS:Mac / OS:Windows and Department:CSD/Sales/SET/Workspace/Corp.
    Fetches live from Tenable so no stale-cache risk. Macs get OS tag only.
    """
    access, secret, base_url = get_tenable_creds()
    if not access or not secret:
        return JSONResponse({"ok": False, "error": "Missing Tenable API keys. Go to /settings."}, status_code=400)

    try:
        # Fetch live from Tenable (same as assign-groups uses)
        tenable_assets = _tenable_fetch_all_assets_with_tags(access, secret, base_url)

        dept_buckets: Dict[str, List[str]] = {
            "CSD": [], "Sales": [], "SET": [], "Workspace": [], "Corp": [],
        }
        os_buckets: Dict[str, List[str]] = {"Mac": [], "Windows": []}
        skipped_no_id = 0
        skipped_mac   = 0

        for a in tenable_assets:
            asset_uuid = str(a.get("id") or a.get("uuid") or a.get("asset_id") or "").strip()
            if not asset_uuid:
                skipped_no_id += 1
                continue

            os_str = _extract_os(a)
            os_tag = None
            if _RE_MAC.search(os_str):
                os_tag = "Mac"
            elif _RE_WINDOWS.search(os_str):
                os_tag = "Windows"

            if os_tag:
                os_buckets[os_tag].append(asset_uuid)

            if os_tag == "Mac":
                skipped_mac += 1
                continue

            agent_name = _extract_agent_name(a)
            if _RE_CSD.search(agent_name):
                dept = "CSD"
            elif _RE_SALES.search(agent_name):
                dept = "Sales"
            elif _RE_SET.search(agent_name):
                dept = "SET"
            elif _RE_WORKSPACE.match(agent_name):
                dept = "Workspace"
            else:
                dept = "Corp"
            dept_buckets[dept].append(asset_uuid)

        dept_tag_uuids: Dict[str, str] = {}
        for dv in ("CSD", "Sales", "SET", "Workspace", "Corp"):
            dept_tag_uuids[dv] = tenable_find_or_create_tag_value_uuid(
                access, secret, base_url, "Department", dv
            )

        os_tag_uuids: Dict[str, str] = {}
        for ov in ("Mac", "Windows"):
            os_tag_uuids[ov] = tenable_find_or_create_tag_value_uuid(
                access, secret, base_url, "OS", ov
            )

        dept_jobs: Dict[str, List[str]] = {d: [] for d in dept_buckets}
        for dv, uuids in dept_buckets.items():
            for batch in [uuids[i:i+1000] for i in range(0, len(uuids), 1000)]:
                job = tenable_assign_tags_to_assets(access, secret, base_url, batch, [dept_tag_uuids[dv]], action="add")
                if job:
                    dept_jobs[dv].append(job)

        os_jobs: Dict[str, List[str]] = {o: [] for o in os_buckets}
        for ov, uuids in os_buckets.items():
            for batch in [uuids[i:i+1000] for i in range(0, len(uuids), 1000)]:
                job = tenable_assign_tags_to_assets(access, secret, base_url, batch, [os_tag_uuids[ov]], action="add")
                if job:
                    os_jobs[ov].append(job)

        return JSONResponse({
            "ok":                   True,
            "source":               "live",
            "tenable_assets_total": len(tenable_assets),
            "skipped_no_id":        skipped_no_id,
            "macs_os_tagged_only":  skipped_mac,
            "department_counts":    {f"Department:{k}": len(v) for k, v in dept_buckets.items()},
            "os_counts":            {f"OS:{k}": len(v) for k, v in os_buckets.items()},
            "department_jobs":      {f"Department:{k}": v for k, v in dept_jobs.items()},
            "os_jobs":              {f"OS:{k}": v for k, v in os_jobs.items()},
            "note": "Mac assets get OS:Mac only. Department tags are Windows-only. Fetched live from Tenable.",
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ----------------------------
# Tag: Device:Not in JC (weekly audit)
# ----------------------------
@app.post("/api/tenable/tag-not-in-jc")
def api_tenable_tag_not_in_jc():
    """
    Weekly audit tag. Finds all Tenable assets with no matching JumpCloud device
    (using the same hostname_key15 match as tag-org) and tags them Device:Not in JC.
    Self-contained — no import from app_routes.
    """
    access, secret, base_url = get_tenable_creds()
    if not access or not secret:
        return JSONResponse({"ok": False, "error": "Missing Tenable API keys. Go to /settings."}, status_code=400)

    jc1_total = jc_count(1)
    jc2_total = jc_count(2)
    if jc1_total == 0 and jc2_total == 0:
        return JSONResponse({"ok": False, "error": "No JumpCloud devices cached. Run JumpCloud sync first."}, status_code=400)

    try:
        # Build JumpCloud keyset from both accounts
        jc_keys: set = set()
        for acct in (1, 2):
            for s in jc_get_all_raw(acct):
                k = hostname_key15(jc_extract_hostname(s))
                if k:
                    jc_keys.add(k)

        # Find Tenable assets with no JumpCloud match
        tenable_assets = get_all_assets_raw()
        unmatched_uuids: List[str] = []
        skipped_no_id = 0
        skipped_no_hostname = 0

        for a in tenable_assets:
            asset_uuid = str(a.get("id") or a.get("uuid") or a.get("asset_id") or "").strip()
            if not asset_uuid:
                skipped_no_id += 1
                continue

            hn = extract_hostname(a)
            if not hn:
                skipped_no_hostname += 1
                continue

            if hostname_key15(hn) not in jc_keys:
                unmatched_uuids.append(asset_uuid)

        if not unmatched_uuids:
            return JSONResponse({
                "ok": True,
                "tagged": 0,
                "note": "No unmatched Tenable assets found — nothing to tag.",
            })

        # Ensure tag value exists and assign
        tag_uuid = tenable_find_or_create_tag_value_uuid(
            access, secret, base_url, "Device", "Not in JC"
        )

        jobs = []
        for batch in [unmatched_uuids[i:i+1000] for i in range(0, len(unmatched_uuids), 1000)]:
            job = tenable_assign_tags_to_assets(access, secret, base_url, batch, [tag_uuid], action="add")
            if job:
                jobs.append(job)

        return JSONResponse({
            "ok": True,
            "tenable_total": len(tenable_assets),
            "tagged": len(unmatched_uuids),
            "skipped_no_id": skipped_no_id,
            "skipped_no_hostname": skipped_no_hostname,
            "jobs": jobs,
            "note": f"Tagged {len(unmatched_uuids)} assets with Device:Not in JC.",
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# NEW: Agent group assignment endpoint
# ----------------------------

# Group name constants — must match exactly what's in Tenable
_GROUP_MAC              = "Mac"
_GROUP_CORP             = "Corp"
_GROUP_CSD              = "CSD"
_GROUP_SALES            = "Sales"
_GROUP_SET              = "SET"
_GROUP_WORKSPACE        = "WorkSpaces"
_GROUP_WIN_WORKSTATIONS = "Windows Workstations"

# Catch-all groups to remove from when a device lands in a specific group
# Key = specific group name, Value = catch-all to remove from
_CATCHALL_REMOVALS: Dict[str, str] = {
    _GROUP_CSD:       _GROUP_WIN_WORKSTATIONS,
    _GROUP_SALES:     _GROUP_WIN_WORKSTATIONS,
    _GROUP_SET:       _GROUP_WIN_WORKSTATIONS,
    _GROUP_WORKSPACE: _GROUP_WIN_WORKSTATIONS,
    _GROUP_CORP:      _GROUP_WIN_WORKSTATIONS,  # Corp has its own group, not Windows Workstations
    _GROUP_MAC:       _GROUP_WIN_WORKSTATIONS,  # Macs shouldn't be in Win Workstations
}


def _tenable_fetch_all_assets_with_tags(access: str, secret: str, base_url: str) -> List[Dict[str, Any]]:
    """Fetch all assets from Tenable API (not local cache) so tags are current."""
    h = tenable_headers(access, secret)
    url_export = f"{base_url}/assets/v2/export"
    body = {"chunk_size": 5000}
    r = requests.post(url_export, headers=h, json=body, timeout=60)
    r.raise_for_status()
    export_uuid = r.json().get("export_uuid") or r.json().get("uuid") or r.json().get("id")
    if not export_uuid:
        raise RuntimeError(f"Missing export_uuid in response: {r.text[:300]}")

    url_status = f"{base_url}/assets/export/{export_uuid}/status"
    status = "PROCESSING"
    chunks_available: List[int] = []
    for _ in range(300):
        rs = requests.get(url_status, headers=h, timeout=60)
        rs.raise_for_status()
        data = rs.json()
        status = data.get("status") or data.get("state") or status
        chunks_available = data.get("chunks_available") or data.get("chunks") or chunks_available
        if str(status).upper() in ("FINISHED", "COMPLETE", "COMPLETED"):
            break
        if str(status).upper() in ("CANCELLED", "CANCELED", "ERROR", "FAILED"):
            raise RuntimeError(f"Export failed: status={status}")
        time.sleep(2)

    if str(status).upper() not in ("FINISHED", "COMPLETE", "COMPLETED"):
        raise RuntimeError(f"Timed out waiting for export. Last status={status}")

    all_assets: List[Dict[str, Any]] = []
    for chunk_id in chunks_available:
        url_chunk = f"{base_url}/assets/export/{export_uuid}/chunks/{chunk_id}"
        rc = requests.get(url_chunk, headers=h, timeout=120)
        rc.raise_for_status()
        payload = rc.json()
        if isinstance(payload, list):
            all_assets.extend(payload)
        elif isinstance(payload, dict):
            items = payload.get("assets") or []
            if isinstance(items, list):
                all_assets.extend(items)
    return all_assets


def _tenable_get_scanner_id(access: str, secret: str, base_url: str) -> int:
    """
    Returns the ID of the first linked/managed scanner.
    Tenable cloud uses a real scanner ID — 'null' does not work for agent-groups.
    """
    h = tenable_headers(access, secret)
    url = f"{base_url}/scanners"
    r = requests.get(url, headers=h, timeout=60)
    r.raise_for_status()
    data = r.json()
    scanners = data.get("scanners") or (data if isinstance(data, list) else [])
    # Prefer linked/managed/on scanner
    for s in scanners:
        if s.get("linked") or s.get("status") == "on" or s.get("type") in ("managed", "local"):
            sid = s.get("id")
            if sid is not None:
                return int(sid)
    # Fall back: lowest numeric ID
    ids = [int(s["id"]) for s in scanners if s.get("id") is not None]
    if ids:
        return min(ids)
    raise RuntimeError(f"No scanners found. Response: {data}")


def _tenable_fetch_all_agents(access: str, secret: str, base_url: str, scanner_id: int) -> Dict[str, int]:
    """Returns {asset_uuid: agent_id} mapping."""
    h = tenable_headers(access, secret)
    mapping: Dict[str, int] = {}
    limit = 1000
    offset = 0
    while True:
        url = f"{base_url}/scanners/{scanner_id}/agents?limit={limit}&offset={offset}"
        r = requests.get(url, headers=h, timeout=60)
        r.raise_for_status()
        data = r.json()
        agents = data.get("agents") or []
        if not agents:
            break
        for a in agents:
            au = a.get("asset_uuid")
            aid = a.get("id")
            if au and aid is not None:
                mapping[str(au)] = int(aid)
        offset += limit
        if offset > 2_000_000:
            break
    return mapping


def _tenable_fetch_agent_groups(access: str, secret: str, base_url: str, scanner_id: int) -> Dict[str, int]:
    """Returns {group_name: group_id} mapping."""
    h = tenable_headers(access, secret)
    url = f"{base_url}/scanners/{scanner_id}/agent-groups"
    r = requests.get(url, headers=h, timeout=60)
    r.raise_for_status()
    data = r.json()
    groups = data.get("groups") or (data if isinstance(data, list) else [])
    return {str(g.get("name", "")).strip(): int(g["id"]) for g in groups if g.get("id") is not None and g.get("name")}


def _agent_bulk_action(access: str, secret: str, base_url: str, scanner_id: int, group_id: int, agent_ids: List[int], action: str) -> Any:
    """
    Bulk add/remove agents from a group.
    Tenable's bulk endpoint: POST /scanners/{id}/agent-groups/{gid}/agents/_bulk/add
    Body: {"items": [{"id": <agent_int_id>}, ...]}
    Returns a dict with job info, or raises with the full Tenable error message on failure.
    """
    if not agent_ids:
        return None
    h = tenable_headers(access, secret)
    url = f"{base_url.rstrip('/')}/scanners/{scanner_id}/agent-groups/{group_id}/agents/_bulk/{action}"
    body = {"items": [int(aid) for aid in agent_ids]}
    r = requests.post(url, headers=h, json=body, timeout=120)
    if not r.ok:
        raise RuntimeError(
            f"Bulk {action} failed [{r.status_code}] for scanner={scanner_id} group={group_id}: {r.text[:500]}"
        )
    if r.text:
        try:
            return r.json()
        except Exception:
            return r.text
    return {"ok": True, "count": len(agent_ids)}


def _parse_asset_tags(asset: Dict[str, Any]) -> set:
    """Extract set of 'Category:Value' tag strings from a Tenable asset."""
    tags_raw = asset.get("tags") or []
    out = set()
    if isinstance(tags_raw, list):
        for t in tags_raw:
            if isinstance(t, dict):
                k = (t.get("key") or t.get("category_name") or t.get("category") or "").strip()
                v = (t.get("value") or t.get("tag_value") or "").strip()
                if k and v:
                    out.add(f"{k}:{v}")
            elif isinstance(t, str) and t.strip():
                out.add(t.strip())
    elif isinstance(tags_raw, str) and tags_raw.strip():
        for part in tags_raw.split(","):
            if part.strip():
                out.add(part.strip())
    return out


def _classify_asset_to_group(tags: set) -> Optional[str]:
    """Determine which agent group an asset belongs to based on its tags."""
    # OS:Mac → Mac group (highest priority)
    if "OS:Mac" in tags:
        return _GROUP_MAC
    # Department tags → specific groups
    if "Department:CSD" in tags:
        return _GROUP_CSD
    if "Department:Sales" in tags:
        return _GROUP_SALES
    if "Department:SET" in tags:
        return _GROUP_SET
    if "Department:Workspace" in tags:
        return _GROUP_WORKSPACE
    if "Department:Corp" in tags:
        return _GROUP_CORP
    # OS:Windows with no dept tag → Windows Workstations
    if "OS:Windows" in tags:
        return _GROUP_WIN_WORKSTATIONS
    return None


@app.post("/api/tenable/assign-groups")
def api_tenable_assign_groups():
    access, secret, base_url = get_tenable_creds()
    if not access or not secret:
        return JSONResponse({"ok": False, "error": "Missing Tenable API keys. Go to /settings."}, status_code=400)

    try:
        # 0. Resolve real scanner ID (null does not work for agent-groups API)
        scanner_id = _tenable_get_scanner_id(access, secret, base_url)

        # 1. Fetch current assets with tags from Tenable API
        all_assets = _tenable_fetch_all_assets_with_tags(access, secret, base_url)

        # 2. Fetch agent groups (name -> id)
        group_map = _tenable_fetch_agent_groups(access, secret, base_url, scanner_id)
        missing_groups = [g for g in [
            _GROUP_MAC, _GROUP_CORP, _GROUP_CSD, _GROUP_SALES,
            _GROUP_SET, _GROUP_WORKSPACE, _GROUP_WIN_WORKSTATIONS
        ] if g not in group_map]
        if missing_groups:
            return JSONResponse({
                "ok": False,
                "error": f"The following agent groups were not found in Tenable: {missing_groups}. "
                         f"Please create them first. Found groups: {list(group_map.keys())}"
            }, status_code=400)

        # 3. Fetch agent map (asset_uuid -> agent_id)
        agent_map = _tenable_fetch_all_agents(access, secret, base_url, scanner_id)

        # 4. Classify each asset into a target group
        group_add_buckets: Dict[str, List[int]] = {g: [] for g in group_map}
        group_remove_buckets: Dict[str, List[int]] = {g: [] for g in group_map}

        skipped_no_agent = 0
        skipped_no_group = 0
        classified: Dict[str, int] = {}

        # Build catch-all removal map; support Mac Workstations if it exists
        catchall_removals = dict(_CATCHALL_REMOVALS)
        if "Mac Workstations" in group_map:
            catchall_removals[_GROUP_MAC] = "Mac Workstations"

        for asset in all_assets:
            asset_uuid = str(asset.get("id") or asset.get("uuid") or asset.get("asset_id") or "").strip()
            if not asset_uuid:
                continue

            agent_id = agent_map.get(asset_uuid)
            if agent_id is None:
                skipped_no_agent += 1
                continue

            tags = _parse_asset_tags(asset)
            target_group = _classify_asset_to_group(tags)

            if target_group is None:
                skipped_no_group += 1
                continue

            # Add to target group
            group_add_buckets[target_group].append(agent_id)
            classified[target_group] = classified.get(target_group, 0) + 1

            # Remove from catch-all if target is more specific
            catchall = catchall_removals.get(target_group)
            if catchall and catchall in group_remove_buckets:
                group_remove_buckets[catchall].append(agent_id)

        # 5. Execute bulk adds
        add_results: Dict[str, Any] = {}
        for group_name, agent_ids in group_add_buckets.items():
            if not agent_ids:
                continue
            gid = group_map[group_name]
            batch_jobs = []
            errors = []
            for batch in [agent_ids[i:i+500] for i in range(0, len(agent_ids), 500)]:
                try:
                    result = _agent_bulk_action(access, secret, base_url, scanner_id, gid, batch, "add")
                    if result:
                        batch_jobs.append(result)
                except Exception as be:
                    errors.append(str(be))
            add_results[group_name] = {"count": len(agent_ids), "jobs": batch_jobs, "errors": errors}

        # 6. Execute bulk removes from catch-alls
        remove_results: Dict[str, Any] = {}
        for group_name, agent_ids in group_remove_buckets.items():
            if not agent_ids:
                continue
            gid = group_map[group_name]
            batch_jobs = []
            errors = []
            for batch in [agent_ids[i:i+500] for i in range(0, len(agent_ids), 500)]:
                try:
                    result = _agent_bulk_action(access, secret, base_url, scanner_id, gid, batch, "remove")
                    if result:
                        batch_jobs.append(result)
                except Exception as be:
                    errors.append(str(be))
            remove_results[group_name] = {"count": len(agent_ids), "jobs": batch_jobs, "errors": errors}

        return JSONResponse({
            "ok": True,
            "scanner_id": scanner_id,
            "tenable_assets_total": len(all_assets),
            "agents_total": len(agent_map),
            "skipped_no_agent": skipped_no_agent,
            "skipped_no_tag_match": skipped_no_group,
            "classified": classified,
            "added_to_groups": add_results,
            "removed_from_catchalls": remove_results,
            "note": (
                "Macs -> Mac group (removed from Mac Workstations if present, else Windows Workstations). "
                "CSD/Sales/SET/Workspace -> dept group (removed from Windows Workstations). "
                "Corp -> Corp only. Windows with no dept tag -> Windows Workstations."
            ),
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ----------------------------
# Sync endpoints
# ----------------------------
@app.get("/api/debug/jc-live-search")
def api_debug_jc_live_search(q: str = "", account: int = 1):
    """
    Hits the JumpCloud API directly to search for a device by hostname fragment.
    Bypasses the local DB entirely — tells you if JC API is returning the device at all.
    Usage: /api/debug/jc-live-search?q=1-4l2zfo5ea&account=1
    """
    q = q.strip().lower()
    if not q:
        return JSONResponse({"error": "Pass ?q=partial_hostname"}, status_code=400)

    api_key  = keyring.get_password("tsync_jumpcloud", f"jc{account}_api_key")
    org_id   = keyring.get_password("tsync_jumpcloud", f"jc{account}_org_id")
    if not api_key:
        return JSONResponse({"error": f"No JumpCloud account {account} API key saved."}, status_code=400)

    headers = jumpcloud_headers(api_key, org_id)
    limit = 100
    skip = 0
    total_fetched = 0
    matches = []
    pages_checked = 0

    while True:
        url = f"{JC_BASE_URL}/api/systems"
        params = {"limit": limit, "skip": skip}
        r = requests.get(url, headers=headers, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()

        total_count = None
        if isinstance(data, dict):
            total_count = data.get("totalCount")
            items = data.get("results") or data.get("items") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        pages_checked += 1
        total_fetched += len(items)

        for s in items:
            hn = jc_extract_hostname(s)
            if q in (hn or "").lower():
                matches.append({
                    "id": str(s.get("id") or s.get("_id") or ""),
                    "hostname": hn,
                    "key15": hostname_key15(hn),
                    "fields": {k: s.get(k) for k in ("hostname", "displayName", "display_name", "name") if s.get(k)},
                })

        if not items:
            break
        skip += limit
        if skip > 500000:
            break

    return JSONResponse({
        "account": account,
        "query": q,
        "total_fetched_from_api": total_fetched,
        "pages_checked": pages_checked,
        "matches_found": len(matches),
        "matches": matches,
        "note": "If matches_found=0 the device is not being returned by the JumpCloud API for this account.",
    })


@app.get("/api/debug/hostname-match")
def api_debug_hostname_match(q: str = ""):
    """
    Debug endpoint: given a search string, shows hostname keys for matching
    Tenable assets and JumpCloud systems so you can see why a match is failing.
    Usage: /api/debug/hostname-match?q=1-4l2zfo5ea
    """
    q = q.strip().lower()
    if not q:
        return JSONResponse({"error": "Pass ?q=partial_hostname"}, status_code=400)

    tenable_matches = []
    for a in get_all_assets_raw():
        hn = extract_hostname(a)
        key = hostname_key15(hn)
        if q in (hn or "").lower() or q in key:
            tenable_matches.append({
                "id": str(a.get("id") or ""),
                "raw_hostname": hn,
                "key15": key,
            })

    jc_matches = []
    for acct in (1, 2):
        for s in jc_get_all_raw(acct):
            hn = jc_extract_hostname(s)
            key = hostname_key15(hn)
            if q in (hn or "").lower() or q in key:
                jc_matches.append({
                    "account": acct,
                    "id": str(s.get("id") or s.get("_id") or ""),
                    "raw_hostname": hn,
                    "key15": key,
                    "fields_present": [k for k in ("hostname", "displayName", "display_name", "name", "systemHostname") if s.get(k)],
                })

    # Also show what keys are in the JC keyset that are close
    jc_keyset = set()
    for acct in (1, 2):
        for s in jc_get_all_raw(acct):
            k = hostname_key15(jc_extract_hostname(s))
            if k:
                jc_keyset.add(k)

    return JSONResponse({
        "query": q,
        "tenable_matches": tenable_matches,
        "jc_matches": jc_matches,
        "would_match": any(
            t["key15"] in jc_keyset for t in tenable_matches
        ),
    })


@app.post("/api/db/clear-tenable")
def api_clear_tenable():
    """Wipes all Tenable assets and sync run history from the local DB."""
    try:
        with db() as conn:
            conn.execute("DELETE FROM assets")
            conn.execute("DELETE FROM sync_runs")
        return JSONResponse({"ok": True, "note": "Tenable assets and sync history cleared."})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/db/clear-jumpcloud")
def api_clear_jumpcloud():
    """Wipes all JumpCloud systems and sync run history from the local DB."""
    try:
        with db() as conn:
            conn.execute("DELETE FROM jumpcloud_systems")
            conn.execute("DELETE FROM jumpcloud_sync_runs")
        return JSONResponse({"ok": True, "note": "JumpCloud systems and sync history cleared."})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/sync")
def sync_now():
    access, secret, base_url = get_tenable_creds()
    if not access or not secret:
        return JSONResponse({"ok": False, "error": "Missing Tenable API keys. Go to /settings."}, status_code=400)

    started_at = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO sync_runs (started_at, status, detail) VALUES (?, ?, ?)",
            (started_at, "RUNNING", None)
        )
        run_id = cur.lastrowid
        conn.commit()

    try:
        result = export_assets_all(access, secret, base_url, chunk_size=5000)
        all_assets: List[Dict[str, Any]] = []
        for c in result["chunks"]:
            all_assets.extend(c["assets"])

        upserted = upsert_assets(all_assets)

        finished_at = datetime.now(timezone.utc).isoformat()
        with db() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at=?, status=?, detail=? WHERE run_id=?",
                (finished_at, "SUCCESS", f"export_uuid={result['export_uuid']}, upserted={upserted}", run_id)
            )
            conn.commit()

        return JSONResponse({"ok": True, "export_uuid": result["export_uuid"], "upserted": upserted})

    except Exception as e:
        finished_at = datetime.now(timezone.utc).isoformat()
        with db() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at=?, status=?, detail=? WHERE run_id=?",
                (finished_at, "FAILED", str(e)[:2000], run_id)
            )
            conn.commit()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/sync/jumpcloud")
def sync_jumpcloud():
    jc1_key, jc1_org = get_jumpcloud_creds(1)
    jc2_key, jc2_org = get_jumpcloud_creds(2)

    if not jc1_key and not jc2_key:
        return JSONResponse({"ok": False, "error": "Missing JumpCloud keys. Add them in /settings."}, status_code=400)

    started_at = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO jumpcloud_sync_runs (started_at, status, detail) VALUES (?, ?, ?)",
            (started_at, "RUNNING", None)
        )
        run_id = cur.lastrowid
        conn.commit()

    try:
        total_upserted = 0
        detail_parts = []

        if jc1_key:
            systems1 = jc_fetch_all_systems(jc1_key, jc1_org)
            up1 = jc_upsert_systems(1, systems1)
            total_upserted += up1
            detail_parts.append(f"{JC1_LABEL}={up1}")

        if jc2_key:
            systems2 = jc_fetch_all_systems(jc2_key, jc2_org)
            up2 = jc_upsert_systems(2, systems2)
            total_upserted += up2
            detail_parts.append(f"{JC2_LABEL}={up2}")

        finished_at = datetime.now(timezone.utc).isoformat()
        with db() as conn:
            conn.execute(
                "UPDATE jumpcloud_sync_runs SET finished_at=?, status=?, detail=? WHERE run_id=?",
                (finished_at, "SUCCESS", ", ".join(detail_parts), run_id)
            )
            conn.commit()

        return JSONResponse({"ok": True, "upserted": total_upserted, "detail": ", ".join(detail_parts)})

    except Exception as e:
        finished_at = datetime.now(timezone.utc).isoformat()
        with db() as conn:
            conn.execute(
                "UPDATE jumpcloud_sync_runs SET finished_at=?, status=?, detail=? WHERE run_id=?",
                (finished_at, "FAILED", str(e)[:2000], run_id)
            )
            conn.commit()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# ----------------------------
# APIs
# ----------------------------
@app.get("/api/assets")
def api_assets(page: int = 1, per_page: int = 10):
    total = get_asset_count()
    per_page = int(per_page)
    if per_page not in (10, 50, 100, 200, 500):
        per_page = 10
    page = max(1, int(page))
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    assets = get_assets_page(offset=offset, limit=per_page)
    return {"count": total, "page": page, "per_page": per_page, "total_pages": total_pages, "assets": assets}

@app.get("/api/jumpcloud")
def api_jumpcloud(account: int = 1, page: int = 1, per_page: int = 10):
    per_page = int(per_page)
    if per_page not in (10, 50, 100, 200, 500):
        per_page = 10
    page = max(1, int(page))

    total = jc_count(account)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    rows = jc_get_page_rows(account, offset=offset, limit=per_page)
    presented = []
    for r in rows:
        p = present_jc_system(r)
        p["_account"] = account
        p["_label"] = JC1_LABEL if account == 1 else JC2_LABEL
        presented.append(p)

    return {"account": account, "count": total, "page": page, "per_page": per_page, "total_pages": total_pages, "systems": presented}

@app.get("/api/health")
def api_health():
    access, secret, base_url = get_tenable_creds()
    jc1_key, _ = get_jumpcloud_creds(1)
    jc2_key, _ = get_jumpcloud_creds(2)
    return {
        "ok": True,
        "data_dir": DATA_DIR,
        "db_path": DB_PATH,
        "tenable_has_keys": bool(access and secret),
        "tenable_base_url": base_url,
        "jumpcloud_acct1_has_key": bool(jc1_key),
        "jumpcloud_acct2_has_key": bool(jc2_key),
    }

# Run with:
# uvicorn app:app --host 127.0.0.1 --port 8001

# ----------------------------
# Migration: Old Tenable account comparison
# ----------------------------

@app.get("/migration", response_class=HTMLResponse)
def migration_get(request: Request):
    return templates.TemplateResponse("migration.html", {"request": request})


@app.post("/api/migration/compare")
def api_migration_compare(
    old_access: str = Form(...),
    old_secret: str = Form(...),
):
    """
    Fetches assets live from the old Tenable account (credentials NOT saved),
    compares against local new-account DB by hostname key15.
    Returns three buckets: in_both, old_only, new_only.
    """
    old_access = old_access.strip()
    old_secret = old_secret.strip()
    if not old_access or not old_secret:
        return JSONResponse({"ok": False, "error": "Old Tenable access and secret keys are required."}, status_code=400)

    base_url = "https://cloud.tenable.com"

    try:
        # Fetch live from old account
        old_assets = _tenable_fetch_all_assets_with_tags(old_access, old_secret, base_url)

        # Build lookup from new account local DB
        new_assets = get_all_assets_raw()
        new_by_key: Dict[str, Dict] = {}
        for a in new_assets:
            hn = extract_hostname(a)
            k = hostname_key15(hn)
            if k:
                new_by_key[k] = {
                    "id": str(a.get("id") or ""),
                    "hostname": hn,
                }

        in_both  = []
        old_only = []

        for a in old_assets:
            hn  = extract_hostname(a)
            k   = hostname_key15(hn)
            uid = str(a.get("id") or a.get("uuid") or "").strip()
            tags = _parse_asset_tags(a)

            entry = {
                "id":       uid,
                "hostname": hn,
                "key15":    k,
                "tags":     sorted(tags),
            }

            if k and k in new_by_key:
                entry["new_id"] = new_by_key[k]["id"]
                in_both.append(entry)
            else:
                old_only.append(entry)

        # New only — in new DB but not in old account fetch
        old_keys = {hostname_key15(extract_hostname(a)) for a in old_assets if hostname_key15(extract_hostname(a))}
        new_only = [
            {"id": v["id"], "hostname": v["hostname"], "key15": k}
            for k, v in new_by_key.items()
            if k not in old_keys
        ]

        return JSONResponse({
            "ok": True,
            "old_total":   len(old_assets),
            "new_total":   len(new_assets),
            "in_both":     in_both,
            "old_only":    old_only,
            "new_only":    new_only,
            "summary": {
                "in_both_count":  len(in_both),
                "old_only_count": len(old_only),
                "new_only_count": len(new_only),
            },
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/migration/delete-from-old")
def api_migration_delete_from_old(request_body: dict):
    """
    Deletes selected assets from the OLD Tenable account.
    Credentials passed in request body — never saved.
    Body: { old_access, old_secret, asset_uuids: [...] }
    """
    old_access  = (request_body.get("old_access") or "").strip()
    old_secret  = (request_body.get("old_secret") or "").strip()
    asset_uuids = request_body.get("asset_uuids") or []

    if not old_access or not old_secret:
        return JSONResponse({"ok": False, "error": "Old Tenable credentials required."}, status_code=400)
    if not asset_uuids:
        return JSONResponse({"ok": False, "error": "No asset UUIDs provided."}, status_code=400)

    base_url = "https://cloud.tenable.com"
    headers  = tenable_headers(old_access, old_secret)

    try:
        import app_routes as _ar

        # Get agents from old account for unlink step
        agents = _ar.tenable_get_all_agents(base_url, headers)
        map_asset_to_agent: Dict[str, int] = {}
        for ag in agents:
            au = ag.get("asset_uuid")
            aid = ag.get("id")
            if au and aid is not None:
                map_asset_to_agent[str(au)] = int(aid)

        deleted = 0
        errors  = 0
        results = []

        for raw_uuid in asset_uuids:
            uid = (raw_uuid or "").strip()
            if not uid:
                continue

            res = {"asset_uuid": uid, "unlink": None, "delete": None}

            agent_id = map_asset_to_agent.get(uid)
            if agent_id is not None:
                ok_u, msg_u = _ar.tenable_unlink_agent_if_present(base_url, headers, agent_id)
                res["unlink"] = msg_u
            else:
                res["unlink"] = "no_agent"

            ok_d, msg_d = _ar.tenable_hard_delete_asset(base_url, headers, uid)
            res["delete"] = msg_d
            if ok_d:
                deleted += 1
            else:
                errors += 1

            results.append(res)

        return JSONResponse({
            "ok":      True,
            "deleted": deleted,
            "errors":  errors,
            "results": results,
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# Load additional routes (diff page, delete)
import app_routes  # noqa: F401, E402
