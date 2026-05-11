import json
import re
from datetime import datetime
from typing import Any, Dict, List, Tuple, Set

import requests
from fastapi import Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import app as core

templates = Jinja2Templates(directory="templates")


# ----------------------------
# Date helpers
# ----------------------------
def parse_last_seen(v: str) -> datetime:
    # Missing/invalid treated as very old -> show first
    if not v:
        return datetime.min
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return datetime.min


# ----------------------------
# Matching helpers
# ----------------------------
def _first_label(name: str) -> str:
    if not name:
        return ""
    n = name.strip()
    if not n:
        return ""
    return n.split(".")[0]


def key_variants(name: str) -> Set[str]:
    """
    Build multiple 15-char match keys to reduce false mismatches.
    """
    if not name:
        return set()

    base = _first_label(name).strip().lower()
    if not base:
        return set()

    raw15 = base[:15]
    cleaned = re.sub(r"[^a-z0-9\-_]", "", base)[:15]
    nodash = re.sub(r"[-_]", "", cleaned)[:15]
    nosep = re.sub(r"[^a-z0-9]", "", base)[:15]

    return {k for k in (raw15, cleaned, nodash, nosep) if k}


def jc_candidate_names(raw: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []

    for k in ("hostname", "displayName", "display_name", "name", "systemHostname", "system_hostname"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())

    system = raw.get("system")
    if isinstance(system, dict):
        for k in ("hostname", "displayName", "name"):
            v = system.get(k)
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())

    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def tenable_candidate_names(asset_presented: Dict[str, Any]) -> List[str]:
    raw = asset_presented.get("_raw") or {}
    candidates: List[str] = []

    hn = asset_presented.get("hostname")
    if isinstance(hn, str) and hn.strip():
        candidates.append(hn.strip())

    for k in (
        "hostname", "hostnames",
        "fqdn", "fqdns",
        "netbios_name", "netbios_names",
        "agent_name", "agent_names",
        "computer_name",
        "name", "names",
        "display_name",
    ):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item.strip():
                    candidates.append(item.strip())

    interfaces = raw.get("interfaces")
    if isinstance(interfaces, list):
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            for k in ("hostname", "hostnames", "fqdn", "fqdns", "name"):
                v = iface.get(k)
                if isinstance(v, str) and v.strip():
                    candidates.append(v.strip())
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, str) and item.strip():
                            candidates.append(item.strip())

    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def has_tenable_tag(asset_presented: Dict[str, Any], wanted: str) -> bool:
    wanted_n = (wanted or "").strip().lower()
    if not wanted_n:
        return False

    tags = (asset_presented.get("tags") or "").strip().lower()
    if not tags:
        return False

    tags = re.sub(r"\s+", " ", tags)
    wanted_n = re.sub(r"\s+", " ", wanted_n)
    return wanted_n in tags


# ----------------------------
# Loaders
# ----------------------------
def load_all_tenable_assets() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with core.db() as conn:
        rows = conn.execute("SELECT payload_json FROM assets").fetchall()
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
            out.append(core.present_asset(payload))  # includes _raw
        except Exception:
            continue
    return out


def load_all_jumpcloud_systems() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with core.db() as conn:
        rows = conn.execute("SELECT account, payload_json FROM jumpcloud_systems").fetchall()

    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
            presented = core.present_jc_system(payload)
            acct = int(r["account"])
            presented["_account"] = acct
            presented["_label"] = core.JC1_LABEL if acct == 1 else core.JC2_LABEL
            presented["_raw"] = payload
            out.append(presented)
        except Exception:
            continue

    return out


# ----------------------------
# Keyset build + matching
# ----------------------------
def build_jumpcloud_keyset_and_examples(
    jc_systems: List[Dict[str, Any]]
) -> Tuple[Set[str], Dict[str, str], Dict[int, int]]:
    keys: Set[str] = set()
    key_example: Dict[str, str] = {}
    acct_counts = {1: 0, 2: 0}

    for s in jc_systems:
        raw = s.get("_raw", {})
        names = jc_candidate_names(raw)

        best_label = names[0] if names else (s.get("hostname") or "").strip()

        for name in names or ([best_label] if best_label else []):
            for k in key_variants(name):
                keys.add(k)
                if k not in key_example and best_label:
                    key_example[k] = best_label

        acct = int(s.get("_account", 0))
        if acct in acct_counts:
            acct_counts[acct] += 1

    return keys, key_example, acct_counts


def build_tenable_keyset_and_examples(
    tenable_assets: List[Dict[str, Any]]
) -> Tuple[Set[str], Dict[str, str]]:
    """
    For reverse diff (JC not in Tenable), build a Tenable keyset.
    """
    keys: Set[str] = set()
    key_example: Dict[str, str] = {}

    for a in tenable_assets:
        candidates = tenable_candidate_names(a)
        best = candidates[0] if candidates else (a.get("hostname") or "")

        for name in candidates or ([best] if best else []):
            for k in key_variants(name):
                keys.add(k)
                if k not in key_example and best:
                    key_example[k] = best

    return keys, key_example


def find_possible_jc_match(tenable_names: List[str], jc_key_example: Dict[str, str]) -> str:
    for name in tenable_names:
        for k in key_variants(name):
            ex = jc_key_example.get(k)
            if ex:
                return ex
    return ""


def tenable_not_in_jumpcloud(
    tenable_assets: List[Dict[str, Any]],
    jc_keyset: Set[str],
    jc_key_example: Dict[str, str],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    for a in tenable_assets:
        candidates = tenable_candidate_names(a)
        all_keys: Set[str] = set()
        for name in candidates:
            all_keys |= key_variants(name)

        if not all_keys:
            a["_match_key"] = ""
            a["_tenable_candidates"] = candidates[:3]
            a["_possible_jc"] = ""
            out.append(a)
            continue

        if any(k in jc_keyset for k in all_keys):
            continue

        first_key = ""
        for name in candidates:
            kv = list(key_variants(name))
            if kv:
                first_key = kv[0]
                break

        a["_match_key"] = first_key
        a["_tenable_candidates"] = candidates[:3]
        a["_possible_jc"] = find_possible_jc_match(candidates, jc_key_example)
        out.append(a)

    out.sort(key=lambda x: parse_last_seen(x.get("last_seen")))
    return out


def jumpcloud_not_in_tenable(
    jc_systems: List[Dict[str, Any]],
    tenable_keyset: Set[str],
) -> List[Dict[str, Any]]:
    """
    Section 3: JC systems whose keys do NOT match Tenable keyset.
    """
    out: List[Dict[str, Any]] = []
    for s in jc_systems:
        raw = s.get("_raw", {}) or {}
        names = jc_candidate_names(raw)
        all_keys: Set[str] = set()
        for name in names:
            all_keys |= key_variants(name)

        # If no name keys, keep it (still interesting)
        if not all_keys:
            s["_match_key"] = ""
            out.append(s)
            continue

        if any(k in tenable_keyset for k in all_keys):
            continue

        # show a representative key
        first_key = ""
        for name in names:
            kv = list(key_variants(name))
            if kv:
                first_key = kv[0]
                break

        s["_match_key"] = first_key
        out.append(s)

    # Oldest first, if we have last_seen; otherwise show missing first
    out.sort(key=lambda x: parse_last_seen(x.get("last_seen")))
    return out


# ----------------------------
# Tenable delete logic (unlink if needed, then hard delete)
# ----------------------------
def tenable_invoke(method: str, base_url: str, headers: Dict[str, str], path: str, json_body: Any = None) -> requests.Response:
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    if json_body is None:
        return requests.request(method, url, headers=headers, timeout=60)
    return requests.request(method, url, headers=headers, json=json_body, timeout=60)


def tenable_get_all_agents(base_url: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    all_agents: List[Dict[str, Any]] = []
    limit = 1000
    offset = 0
    while True:
        r = tenable_invoke("GET", base_url, headers, f"scanners/null/agents?limit={limit}&offset={offset}")
        r.raise_for_status()
        data = r.json()
        agents = data.get("agents") or []
        if not agents:
            break
        all_agents.extend(agents)
        offset += limit
        if offset > 2_000_000:
            break
    return all_agents


def tenable_unlink_agent_if_present(base_url: str, headers: Dict[str, str], agent_id: int) -> Tuple[bool, str]:
    r = tenable_invoke("DELETE", base_url, headers, f"scanners/null/agents/{agent_id}")
    if r.status_code in (200, 202, 204):
        return True, "unlinked"
    if r.status_code == 404:
        return False, "agent_not_found"
    return False, f"unlink_failed:{r.status_code}:{r.text[:200]}"


def tenable_hard_delete_asset(base_url: str, headers: Dict[str, str], asset_uuid: str) -> Tuple[bool, str]:
    body = {
        "hard_delete": True,
        "query": {"field": "host.id", "operator": "eq", "value": asset_uuid},
    }
    r = tenable_invoke("POST", base_url, headers, "api/v2/assets/bulk-jobs/delete", json_body=body)
    if r.status_code in (200, 201, 202):
        return True, "delete_job_submitted"
    return False, f"delete_failed:{r.status_code}:{r.text[:200]}"


def tenable_unlink_then_delete(asset_uuids: List[str]) -> Dict[str, Any]:
    access, secret, base_url = core.get_tenable_creds()
    if not access or not secret:
        return {"ok": False, "error": "Missing Tenable keys. Add them in /settings."}

    headers = core.tenable_headers(access, secret)

    agents = tenable_get_all_agents(base_url, headers)
    map_asset_to_agent: Dict[str, int] = {}
    for a in agents:
        au = a.get("asset_uuid")
        aid = a.get("id")
        if au and aid is not None:
            map_asset_to_agent[str(au)] = int(aid)

    results = []
    unlinked_count = 0
    already_unlinked_count = 0
    delete_submitted = 0
    errors = 0

    for raw_uuid in asset_uuids:
        asset_uuid = (raw_uuid or "").strip()
        if not asset_uuid:
            continue

        res = {"asset_uuid": asset_uuid, "agent_id": None, "unlink": None, "delete": None}

        agent_id = map_asset_to_agent.get(asset_uuid)
        res["agent_id"] = agent_id

        if agent_id is not None:
            ok_u, msg_u = tenable_unlink_agent_if_present(base_url, headers, agent_id)
            res["unlink"] = msg_u
            if ok_u:
                unlinked_count += 1
            else:
                if msg_u == "agent_not_found":
                    already_unlinked_count += 1
                else:
                    errors += 1
        else:
            res["unlink"] = "already_unlinked_or_no_agent_match"
            already_unlinked_count += 1

        ok_d, msg_d = tenable_hard_delete_asset(base_url, headers, asset_uuid)
        res["delete"] = msg_d
        if ok_d:
            delete_submitted += 1
        else:
            errors += 1

        results.append(res)

    return {
        "ok": True,
        "selected": len(asset_uuids),
        "agents_total": len(agents),
        "unlinked": unlinked_count,
        "already_unlinked": already_unlinked_count,
        "delete_jobs_submitted": delete_submitted,
        "errors": errors,
        "results": results,
    }


# ----------------------------
# Routes
# ----------------------------
@core.app.get("/diff", response_class=HTMLResponse)
def diff_page(
    request: Request,
    page: int = 1,
    per_page: int = 50,
    tag_page: int = 1,
    tag_per_page: int = 50,
    jc_only_page: int = 1,
    jc_only_per_page: int = 50,
):
    # clamp per-page values
    per_page = int(per_page)
    if per_page not in (10, 50, 100, 200, 500):
        per_page = 50
    page = max(1, int(page))

    tag_per_page = int(tag_per_page)
    if tag_per_page not in (10, 50, 100, 200, 500):
        tag_per_page = 50
    tag_page = max(1, int(tag_page))

    jc_only_per_page = int(jc_only_per_page)
    if jc_only_per_page not in (10, 50, 100, 200, 500):
        jc_only_per_page = 50
    jc_only_page = max(1, int(jc_only_page))

    tenable_assets = load_all_tenable_assets()
    jc_systems = load_all_jumpcloud_systems()

    jc_keyset, jc_key_example, jc_acct_counts = build_jumpcloud_keyset_and_examples(jc_systems)
    ten_keyset, _ = build_tenable_keyset_and_examples(tenable_assets)

    # Section 1
    gaps = tenable_not_in_jumpcloud(tenable_assets, jc_keyset, jc_key_example)
    gap_total = len(gaps)
    gap_total_pages = max(1, (gap_total + per_page - 1) // per_page)
    if page > gap_total_pages:
        page = gap_total_pages
    gap_offset = (page - 1) * per_page
    gaps_page = gaps[gap_offset: gap_offset + per_page]

    # Section 2
    TAG_WANTED = "Device:Not in JC"
    tagged = [a for a in tenable_assets if has_tenable_tag(a, TAG_WANTED)]
    tagged.sort(key=lambda x: parse_last_seen(x.get("last_seen")))
    tag_total = len(tagged)
    tag_total_pages = max(1, (tag_total + tag_per_page - 1) // tag_per_page)
    if tag_page > tag_total_pages:
        tag_page = tag_total_pages
    tag_offset = (tag_page - 1) * tag_per_page
    tagged_page = tagged[tag_offset: tag_offset + tag_per_page]

    # Section 3 (NEW)
    jc_only = jumpcloud_not_in_tenable(jc_systems, ten_keyset)
    jc_only_total = len(jc_only)
    jc_only_total_pages = max(1, (jc_only_total + jc_only_per_page - 1) // jc_only_per_page)
    if jc_only_page > jc_only_total_pages:
        jc_only_page = jc_only_total_pages
    jc_only_offset = (jc_only_page - 1) * jc_only_per_page
    jc_only_page_rows = jc_only[jc_only_offset: jc_only_offset + jc_only_per_page]

    return templates.TemplateResponse("diff.html", {
        "request": request,

        "tenable_total": len(tenable_assets),
        "jc_total": len(jc_systems),
        "jc1_label": core.JC1_LABEL,
        "jc2_label": core.JC2_LABEL,
        "jc1_total": jc_acct_counts.get(1, 0),
        "jc2_total": jc_acct_counts.get(2, 0),

        "gap_total": gap_total,
        "gaps": gaps_page,
        "page": page,
        "per_page": per_page,
        "gap_total_pages": gap_total_pages,

        "tag_label": TAG_WANTED,
        "tag_total": tag_total,
        "tagged": tagged_page,
        "tag_page": tag_page,
        "tag_per_page": tag_per_page,
        "tag_total_pages": tag_total_pages,

        # Section 3
        "jc_only_total": jc_only_total,
        "jc_only": jc_only_page_rows,
        "jc_only_page": jc_only_page,
        "jc_only_per_page": jc_only_per_page,
        "jc_only_total_pages": jc_only_total_pages,
    })


@core.app.post("/api/tenable/delete-assets")
def api_tenable_delete_assets(asset_uuids: List[str] = Form([])):
    try:
        result = tenable_unlink_then_delete(asset_uuids)
        status = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=status)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
