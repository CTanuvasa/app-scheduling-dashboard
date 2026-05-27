#!/usr/bin/env python3
"""APS Scheduling Dashboard - local server.

Run:  python3 scheduling-server.py
      python3 scheduling-server.py --build-static   (regenerate scheduling-dashboard.html and exit)
Open: http://localhost:8765
"""

import argparse
import base64
import http.server
import json
import math
import os
import re
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(HERE, "logo.png")
STATIC_HTML_PATH = os.path.join(HERE, "scheduling-dashboard.html")
PORT = 8765


REQUIRED_KEYS = ("TT_TENANT", "TT_API_CLIENT_ID", "TT_API_CLIENT_SECRET",
                 "TT_API_USERNAME", "TT_API_PASSWORD")


def load_creds():
    # 1. Prefer environment variables (GitHub Actions / CI / any cloud host).
    env_creds = {k: os.environ.get(k) for k in REQUIRED_KEYS}
    if all(env_creds.values()):
        return env_creds
    # 2. Fall back to ~/.tracktik/credentials (local dev / Start Dashboard.command).
    creds = {}
    path = os.path.expanduser("~/.tracktik/credentials")
    if not os.path.exists(path):
        sys.exit(
            "Missing TrackTik credentials.\n"
            "  - Local: run 'Setup Credentials.command' to write ~/.tracktik/credentials.\n"
            "  - CI/cloud: set env vars " + ", ".join(REQUIRED_KEYS) + "."
        )
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    missing = [k for k in REQUIRED_KEYS if not creds.get(k)]
    if missing:
        sys.exit(f"Credentials file is missing keys: {', '.join(missing)}")
    return creds


CREDS = load_creds()
TENANT = CREDS["TT_TENANT"]
BASE = f"https://{TENANT}.staffr.us/rest/v1"

_TOKEN = {"value": None, "expires": 0}


def get_token():
    if _TOKEN["value"] and time.time() < _TOKEN["expires"] - 60:
        return _TOKEN["value"]
    data = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": CREDS["TT_API_CLIENT_ID"],
        "client_secret": CREDS["TT_API_CLIENT_SECRET"],
        "username": CREDS["TT_API_USERNAME"],
        "password": CREDS["TT_API_PASSWORD"],
    }).encode()
    req = urllib.request.Request(
        f"https://{TENANT}.staffr.us/rest/oauth2/access_token",
        data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read())
    _TOKEN["value"] = body["access_token"]
    _TOKEN["expires"] = time.time() + body.get("expires_in", 3600)
    return _TOKEN["value"]


def safe_q(params):
    return "&".join(f"{k}={urllib.parse.quote(str(v), safe='(),')}" for k, v in params.items())


def api_get(path, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + safe_q(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {get_token()}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def list_all(path, params):
    rows, offset, limit = [], 0, 1000
    while True:
        p = {**params, "limit": limit, "offset": offset}
        body = api_get(path, p)
        page = body.get("data", [])
        rows.extend(page)
        meta = body.get("meta", {})
        if len(page) < limit or len(rows) >= meta.get("count", 0):
            break
        offset += limit
    return rows


def to_unix(v):
    if v is None: return 0
    if isinstance(v, (int, float)): return int(v)
    if isinstance(v, str):
        try: return int(v)
        except ValueError: pass
        try:
            return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0
    return 0


def current_windows(now=None):
    if now is None: now = time.time()
    d = datetime.fromtimestamp(now, timezone.utc)
    y = d.year
    is_dst = (3 < d.month < 11) or \
             (d.month == 3 and d.day >= 8 + ((6 - datetime(y, 3, 1).weekday()) % 7)) or \
             (d.month == 11 and d.day < 1 + ((6 - datetime(y, 11, 1).weekday()) % 7))
    offset_h = -6 if is_dst else -7
    tz = timezone(timedelta(hours=offset_h))
    now_local = datetime.fromtimestamp(now, tz)
    days_since_sunday = (now_local.weekday() + 1) % 7
    week_start = (now_local - timedelta(days=days_since_sunday)).replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    month_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)
    return {
        "week_start": int(week_start.timestamp()),
        "week_end": int(week_end.timestamp()),
        "month_start": int(month_start.timestamp()),
        "month_end": int(month_end.timestamp()),
        "tz_offset": offset_h,
    }


# --- Exclusions ---------------------------------------------------------------

EXCLUDED_NAME_PATTERNS = [
    "api service account",
    "aps test employee",
    "embrtech it",
    "no court",
    "tracktik business intelligence",
    "tracktik support",
    "urs schedule viewer",
    "lead dispatcher",
]

EXCLUDED_TITLE_PATTERNS = [
    "operations",
    "dispatcher",
    "administrative assistant",
    "process server",
    "office clerk",
    "general manager",
    "ceo",
    "chief executive officer",
    "firewatch",
    "accounting",
    "accountant",  # catches "Staff Accountant"
    "sales - events",
    "business development",
    "support services manager",
    "scheduler",
    "controller",
    "office manager",
    "director of national accounts",
    "client relations manager",
    "recruitment specialist",
]
HR_WORD = re.compile(r"\bhr\b", re.IGNORECASE)


def is_excluded(emp):
    name = (emp.get("name") or "").lower()
    if any(p in name for p in EXCLUDED_NAME_PATTERNS):
        return True
    title = (emp.get("jobTitle") or "")
    tl = title.lower()
    if any(p in tl for p in EXCLUDED_TITLE_PATTERNS):
        return True
    if HR_WORD.search(title):
        return True
    return False


# --- Site filtering (skip admin/training accounts) ----------------------------

def is_non_site_account(name):
    """Accounts that aren't actual coverage sites (admin, training, mobile patrol)."""
    if not name: return True
    n = name.strip().lower()
    if n.startswith("mp "): return True  # mobile patrol routes
    for k in ("aps office", "aps orientation", "field supervisor",
              "training", "orientation"):
        if k in n: return True
    return False


# --- Pull ---------------------------------------------------------------------

# TrackTik skill IDs in this tenant (apsguards)
UNARMED_GUARD_SKILL_ID = 2
ARMED_GUARD_SKILL_ID = 3
DRIVING_PERMISSIONS_SKILL_ID = 32


def pull_data():
    print("[pull] fetching employees...", flush=True)
    employees = list_all("/employees", {"region": 2, "status": "ACTIVE", "include": "address"})
    emp_idx = {e["id"]: e for e in employees}
    print(f"[pull]  {len(employees)} active employees", flush=True)

    print("[pull] fetching employee skills (all)...", flush=True)
    emp_skill_recs = list_all("/employee-skills", {"status": "ACTIVE"})
    emp_skills = defaultdict(set)
    for r in emp_skill_recs:
        e = r.get("employee")
        eid = e.get("id") if isinstance(e, dict) else e
        sid = r.get("skill")
        if eid and sid: emp_skills[eid].add(sid)
    driving_ids = {eid for eid, sks in emp_skills.items()
                   if DRIVING_PERMISSIONS_SKILL_ID in sks}
    print(f"[pull]  {len(emp_skill_recs)} skill assignments across "
          f"{len(emp_skills)} employees; {len(driving_ids)} have driving permissions",
          flush=True)

    print("[pull] fetching position skill requirements...", flush=True)
    pos_skill_recs = list_all("/position-skills", {})
    pos_reqs = defaultdict(lambda: {"hard": set(), "conditional": set()})
    for r in pos_skill_recs:
        pid = r.get("position")
        sid = r.get("skill")
        t = r.get("type")
        if not pid or not sid: continue
        if t == "HARD":
            pos_reqs[pid]["hard"].add(sid)
        elif t == "CONDITIONAL":
            pos_reqs[pid]["conditional"].add(sid)
        # SOFT skills don't affect eligibility
    print(f"[pull]  {len(pos_skill_recs)} position-skill rows across "
          f"{len(pos_reqs)} positions with requirements", flush=True)

    print("[pull] fetching accounts...", flush=True)
    accounts_raw = list_all("/accounts", {"region": 2, "include": "address"})
    print(f"[pull]  {len(accounts_raw)} accounts", flush=True)
    accounts = {}
    for a in accounts_raw:
        addr = a.get("address") or {}
        if not isinstance(addr, dict): addr = {}
        accounts[a["id"]] = {
            "id": a["id"],
            "name": (a.get("name") or "").strip(),
            "lat": addr.get("latitude"),
            "lon": addr.get("longitude"),
            "address": addr.get("formattedAddress", ""),
        }

    print("[pull] fetching positions...", flush=True)
    positions_raw = list_all("/positions", {"limit": 1000})
    positions = {p["id"]: {"id": p["id"], "name": (p.get("name") or "").strip(),
                           "account": p.get("account")} for p in positions_raw}
    print(f"[pull]  {len(positions)} positions", flush=True)

    win = current_windows()
    print(f"[pull] window: week {datetime.fromtimestamp(win['week_start'])} -> "
          f"{datetime.fromtimestamp(win['week_end'])}", flush=True)

    print("[pull] fetching shifts...", flush=True)
    # Window extends +14 days past month-end so we can compute "next shift" for
    # employees whose next assignment is in early next month.
    shifts = list_all("/shifts", {
        "startsOn:gte": win["month_start"] - 86400,
        "startsOn:lt": win["month_end"] + 14 * 86400,
        "include": "employee,position,position.account,payableHours,clockedHours,startsOn,endsOn",
    })
    print(f"[pull]  {len(shifts)} shifts", flush=True)

    def shift_hours(s):
        ph = s.get("payableHours")
        if ph is not None and ph > 0:
            return float(ph)
        start = to_unix(s.get("startsOn"))
        end = to_unix(s.get("endsOn"))
        return max((end - start) / 3600.0, 0)

    week_hours = defaultdict(float)
    month_hours = defaultdict(float)
    site_week_hours = defaultdict(float)
    # account_id -> set of position_ids that had a shift in the current week
    site_positions = defaultdict(set)
    # eid -> list of (start, end) for every shift in the pull window (for
    # client-side timing recomputation against arbitrary target dates)
    emp_shift_list = defaultdict(list)
    # Per-employee shift timing for Call Off Aid columns
    last_end = {}    # eid -> latest endsOn that's < now
    next_start = {}  # eid -> earliest startsOn that's > now
    on_shift_now = set()  # eids currently between startsOn and endsOn
    now_ts = int(time.time())
    for s in shifts:
        emp = s.get("employee")
        eid = emp.get("id") if isinstance(emp, dict) else emp
        reg = emp.get("region") if isinstance(emp, dict) else None
        if reg != 2 or not eid:
            continue
        start = to_unix(s.get("startsOn"))
        end = to_unix(s.get("endsOn"))
        h = shift_hours(s)
        if start < win["month_end"] and end > win["month_start"]:
            month_hours[eid] += h
        if start < win["week_end"] and end > win["week_start"]:
            week_hours[eid] += h
            # Track account-level weekly hours for site dropdown
            pos = s.get("position")
            pos_id = pos.get("id") if isinstance(pos, dict) else pos
            pos_rec = positions.get(pos_id) or {}
            account_obj = pos.get("account") if isinstance(pos, dict) else None
            acct_id = (account_obj.get("id") if isinstance(account_obj, dict)
                       else account_obj) or pos_rec.get("account")
            if acct_id:
                site_week_hours[acct_id] += h
                if pos_id:
                    site_positions[acct_id].add(pos_id)
        # Capture the shift for client-side recomputation against arbitrary dates
        emp_shift_list[eid].append((start, end))
        # Shift timing for Call Off Aid (relative to now)
        if start <= now_ts < end:
            on_shift_now.add(eid)
        elif end <= now_ts:
            if eid not in last_end or end > last_end[eid]:
                last_end[eid] = end
        elif start > now_ts:
            if eid not in next_start or start < next_start[eid]:
                next_start[eid] = start

    def emp_info(eid):
        e = emp_idx.get(eid) or {}
        addr = e.get("address") or {}
        if isinstance(addr, int): addr = {}
        if not isinstance(addr, dict): addr = {}
        return {
            "id": eid,
            "name": e.get("name") or f"Employee #{eid}",
            "customId": e.get("customId", ""),
            "jobTitle": e.get("jobTitle", ""),
            "city": addr.get("city", ""),
            "state": addr.get("state", ""),
            "postalCode": addr.get("postalCode", ""),
            "lat": addr.get("latitude"),
            "lon": addr.get("longitude"),
        }

    def attach_timing(info, eid):
        info["onShiftNow"] = eid in on_shift_now
        info["lastShiftEnd"] = last_end.get(eid)
        info["nextShiftStart"] = next_start.get(eid)
        # Sorted list of (start, end) so JS can recompute timing against any target date
        info["shifts"] = sorted(emp_shift_list.get(eid, []))
        sks = emp_skills.get(eid) or set()
        info["drivingPermissions"] = DRIVING_PERMISSIONS_SKILL_ID in sks
        info["unarmedGuard"] = UNARMED_GUARD_SKILL_ID in sks
        info["armedGuard"] = ARMED_GUARD_SKILL_ID in sks

    out_emps = {}
    terminated_with_hours = 0
    for eid in set(list(week_hours) + list(month_hours)):
        if eid not in emp_idx:
            # Terminated / inactive employee whose old or scheduled shifts
            # still appear in the pull window. Don't surface them.
            terminated_with_hours += 1
            continue
        info = emp_info(eid)
        info["weekHours"] = round(week_hours[eid], 2)
        info["monthHours"] = round(month_hours[eid], 2)
        attach_timing(info, eid)
        out_emps[eid] = info
    for eid, e in emp_idx.items():
        if eid not in out_emps:
            info = emp_info(eid)
            info["weekHours"] = 0
            info["monthHours"] = 0
            attach_timing(info, eid)
            out_emps[eid] = info
    if terminated_with_hours:
        print(f"[pull]  excluded {terminated_with_hours} inactive employees "
              f"who still had shifts in the window", flush=True)

    # Eligibility check: officer is eligible for a position if they have ALL
    # the HARD skills required, AND (if any CONDITIONAL skills are defined)
    # at least one CONDITIONAL skill.
    def is_eligible_for_position(emp_skill_set, pos_id):
        reqs = pos_reqs.get(pos_id)
        if not reqs:
            return True  # position has no requirements -> open to all
        if reqs["hard"] and not reqs["hard"].issubset(emp_skill_set):
            return False
        if reqs["conditional"] and not (reqs["conditional"] & emp_skill_set):
            return False
        return True

    # Build sites list: only accounts with > 2 weekly hours AND lat/lon AND not admin
    sites = []
    for acct_id, hours in site_week_hours.items():
        if hours <= 2: continue
        acct = accounts.get(acct_id)
        if not acct or not acct.get("lat") or not acct.get("lon"):
            continue
        if is_non_site_account(acct["name"]):
            continue
        # Officers eligible for at least one position scheduled at this site this week.
        site_pos_ids = site_positions.get(acct_id, set())
        eligible_ids = []
        for eid, sks in emp_skills.items():
            for pid in site_pos_ids:
                if is_eligible_for_position(sks, pid):
                    eligible_ids.append(eid)
                    break
        # Employees with NO skills on file still need consideration if any position
        # has zero requirements (open positions). Already handled by is_eligible
        # returning True when reqs missing.
        for eid in emp_idx:
            if eid in emp_skills: continue
            for pid in site_pos_ids:
                if not pos_reqs.get(pid):
                    eligible_ids.append(eid)
                    break
        sites.append({
            "id": acct_id,
            "name": acct["name"],
            "lat": acct["lat"],
            "lon": acct["lon"],
            "address": acct["address"],
            "weeklyHours": round(hours, 2),
            "positionCount": len(site_pos_ids),
            "eligibleIds": sorted(set(eligible_ids)),
        })
    sites.sort(key=lambda s: s["name"].lower())

    return {
        "generatedAt": int(time.time()),
        "week": {"start": win["week_start"], "end": win["week_end"]},
        "month": {"start": win["month_start"], "end": win["month_end"]},
        "tzOffset": win["tz_offset"],
        "employees": out_emps,
        "sites": sites,
    }


# --- Render -------------------------------------------------------------------

def fmt_local(unix, fmt, tz_offset=-6):
    return datetime.fromtimestamp(unix, timezone(timedelta(hours=tz_offset))).strftime(fmt)


_LOGO_B64 = None
def logo_b64():
    global _LOGO_B64
    if _LOGO_B64 is None:
        if not os.path.exists(LOGO_PATH):
            return ""
        with open(LOGO_PATH, "rb") as f:
            _LOGO_B64 = base64.b64encode(f.read()).decode()
    return _LOGO_B64


def build_html(data):
    tz_off = data.get("tzOffset", -6)
    emps_all = list(data["employees"].values())
    excluded = sum(1 for e in emps_all if is_excluded(e))
    emps = [e for e in emps_all if not is_excluded(e)]

    week_start_s = fmt_local(data["week"]["start"], "%b %-d", tz_off)
    week_end_s = fmt_local(data["week"]["end"] - 86400, "%b %-d, %Y", tz_off)
    month_label = fmt_local(data["month"]["start"], "%B %Y", tz_off)
    gen_label = fmt_local(data["generatedAt"], "%b %-d, %Y %-I:%M %p MT", tz_off)

    def bucket_week(e):
        h = e["weekHours"]
        if h > 30: return None
        if h <= 8: return "0-8"
        if h <= 16: return "8-16"
        return "16-30"

    sec1 = {"0-8": [], "8-16": [], "16-30": []}
    for e in emps:
        b = bucket_week(e)
        if b: sec1[b].append(e)
    for k in sec1:
        sec1[k].sort(key=lambda x: (x["weekHours"], x["name"]))

    sec2 = sorted([e for e in emps if e["monthHours"] <= 40],
                  key=lambda x: (x["monthHours"], x["name"]))

    b1, b2, b3 = len(sec1["0-8"]), len(sec1["8-16"]), len(sec1["16-30"])

    # Call Off Aid + Extra Help Request pool: every officer with a usable
    # address (lat/lon required for distance sort). We intentionally do NOT
    # cap by weekly hours - these sections are for coverage emergencies
    # where finding *anyone* outweighs avoiding overtime.
    coa_emps = [e for e in emps if e.get("lat") and e.get("lon")]

    coa_data = json.dumps({
        "now": data.get("generatedAt"),
        "sites": data.get("sites", []),
        "employees": [{
            "id": e["id"], "name": e["name"], "customId": e["customId"],
            "jobTitle": e.get("jobTitle", ""), "city": e.get("city", ""),
            "state": e.get("state", ""), "weekHours": e["weekHours"],
            "lat": e["lat"], "lon": e["lon"],
            "onShiftNow": e.get("onShiftNow", False),
            "lastShiftEnd": e.get("lastShiftEnd"),
            "nextShiftStart": e.get("nextShiftStart"),
            "drivingPermissions": e.get("drivingPermissions", False),
            "unarmedGuard": e.get("unarmedGuard", False),
            "armedGuard": e.get("armedGuard", False),
            "shifts": e.get("shifts", []),
        } for e in coa_emps],
    })

    def contact_cell(eid):
        return (
            '<td class="contact">'
            f'<button class="ct-yes" title="Yes - accepted (12h cooldown)" onclick="logContact({eid}, \'yes\')">&#10003;</button>'
            f'<button class="ct-no" title="No - declined (8h cooldown)" onclick="logContact({eid}, \'no\')">&#10007;</button>'
            f'<button class="ct-na" title="No Answer (30 min cooldown)" onclick="logContact({eid}, \'noanswer\')">&#9742;</button>'
            f'<button class="ct-mb" title="Maybe / called back (15 min cooldown)" onclick="logContact({eid}, \'maybe\')">?</button>'
            '</td>'
        )

    def emp_row(e, hours_field="weekHours", show_loc=True, with_contact=False):
        h = e[hours_field]
        job = e.get("jobTitle") or "-"
        cust = e.get("customId", "")
        city = e.get("city", "") or "-"
        state = e.get("state", "")
        loc = f"{city}" + (f", {state}" if state and city != "-" else "")
        loc_cell = f"<td>{loc}</td>" if show_loc else ""
        contact = contact_cell(e["id"]) if with_contact else ""
        eid_attr = f' data-eid="{e["id"]}"' if with_contact else ""
        return (f'<tr{eid_attr}><td class="name"><span class="emp-name">{e["name"]}</span>'
                f'<span class="emp-id">{cust}</span></td>'
                f'<td>{job}</td>{loc_cell}<td class="hr">{h:.1f}</td>{contact}</tr>')

    def render_table(rows, hours_field="weekHours", show_loc=True, with_contact=False):
        if not rows: return '<div class="empty">No employees in this range.</div>'
        loc_th = "<th>Location</th>" if show_loc else ""
        contact_th = '<th class="contact">Log Contact</th>' if with_contact else ""
        return ('<table><thead><tr><th>Name / ID</th><th>Job Title</th>'
                + loc_th + '<th class="hr">Hrs</th>' + contact_th + '</tr></thead><tbody>'
                + "".join(emp_row(e, hours_field, show_loc, with_contact) for e in rows)
                + "</tbody></table>")

    def render_monthly_contact_table(rows):
        """Monthly Tracker variant with Contacts + Last Contact columns
        (filled in by client-side JS on load), plus at-risk flagging
        for officers under the 24-hour monthly minimum, plus inline
        Log Contact buttons so we can capture contacts from here too."""
        if not rows: return '<div class="empty">No employees in this range.</div>'
        body = []
        for e in rows:
            h = e["monthHours"]
            job = e.get("jobTitle") or "-"
            cust = e.get("customId", "")
            at_risk = h < 24
            risk_cls = ' class="at-risk"' if at_risk else ""
            risk_flag = '<span class="risk-flag">At Risk</span>' if at_risk else ""
            body.append(
                f'<tr data-eid="{e["id"]}"{risk_cls}>'
                f'<td class="name"><span class="emp-name">{e["name"]}{risk_flag}</span>'
                f'<span class="emp-id">{cust}</span></td>'
                f'<td>{job}</td>'
                f'<td class="hr">{h:.1f}</td>'
                f'<td class="contact-count" data-month-count="0">0</td>'
                f'<td class="last-contact" data-last-ts="">&mdash;</td>'
                + contact_cell(e["id"])
                + '</tr>'
            )
        return ('<table><thead><tr><th>Name / ID</th><th>Job Title</th>'
                '<th class="hr">Hrs</th><th class="hr">Contacts<br/>(Month)</th>'
                '<th>Last Contact</th><th class="contact">Log Contact</th>'
                '</tr></thead><tbody>'
                + "".join(body)
                + "</tbody></table>")

    site_options = "".join(
        f'<option value="{s["id"]}">{s["name"]} ({s["weeklyHours"]:.1f} hrs)</option>'
        for s in data.get("sites", [])
    )

    site_count = len(data.get("sites", []))
    coa_emp_count = len(coa_emps)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>APS Scheduling Dashboard</title>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: "Barlow Semi Condensed","Helvetica Neue Condensed","Arial Narrow",-apple-system,system-ui,sans-serif;
        background: #F4EFE6; color: #282828; font-size: 14px; line-height: 1.45; }}
.shell {{ max-width: 1200px; margin: 0 auto; padding: 24px 24px 64px; }}
.brand {{ background: #282828; padding: 28px 32px; display: flex; align-items: center; gap: 24px;
          border-bottom: 6px solid #FF3300; border-radius: 6px 6px 0 0; }}
.brand img {{ height: 84px; width: auto; filter: invert(1) brightness(1.08); }}
.brand .titles {{ color: #F4EFE6; }}
.brand .titles h1 {{ margin: 0; font-size: 28px; letter-spacing: 0.04em; font-weight: 800; text-transform: uppercase;
                     font-family: "Cropro","Barlow Semi Condensed","Helvetica Neue Condensed","Arial Narrow",sans-serif; }}
.brand .titles .sub {{ font-size: 13px; color: #C8C1AC; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.18em; }}
.brand .titles .gen {{ font-size: 11px; color: #C8C1AC; margin-top: 8px; opacity: 0.8; }}
.brand .titles .gen strong {{ color: #FFFFFF; opacity: 1; font-weight: 700; }}
.subbar {{ background: #FFFFFF; padding: 14px 24px; display: flex; justify-content: space-between; align-items: center;
            border: 1px solid #D6D6D6; border-top: none; border-radius: 0 0 6px 6px;
            margin-bottom: 32px; font-size: 12px; letter-spacing: 0.1em; text-transform: uppercase; color: #534C37; }}
.subbar .region-pill {{ background: #282828; color: #F4EFE6; padding: 6px 14px; border-radius: 3px;
                         font-weight: 600; letter-spacing: 0.15em; }}
#refresh-btn {{ background: #FF3300; color: #FFFFFF; border: none; padding: 8px 18px; font-size: 11px;
                 letter-spacing: 0.15em; text-transform: uppercase; font-weight: 700; cursor: pointer;
                 border-radius: 3px; font-family: inherit; min-width: 130px; }}
#refresh-btn:hover {{ background: #cc2900; }}
#refresh-btn:disabled {{ background: #C8C1AC; color: #534C37; cursor: wait; }}
#refresh-btn .spinner {{ display: inline-block; width: 10px; height: 10px; border: 2px solid #534C37;
                          border-top-color: transparent; border-radius: 50%;
                          animation: spin 0.7s linear infinite; vertical-align: middle; margin-right: 6px; }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
section {{ background: #FFFFFF; border: 1px solid #D6D6D6; border-radius: 6px;
            margin-bottom: 32px; overflow: hidden; }}
.sec-head {{ background: #282828; color: #F4EFE6; padding: 16px 24px;
              display: flex; justify-content: space-between; align-items: baseline;
              border-bottom: 4px solid #FF3300; }}
.sec-head h2 {{ margin: 0; font-size: 18px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.08em;
                 font-family: "Cropro","Barlow Semi Condensed","Helvetica Neue Condensed","Arial Narrow",sans-serif; }}
.sec-head .meta {{ font-size: 11px; color: #C8C1AC; letter-spacing: 0.15em; text-transform: uppercase; }}
.scroll-body {{ max-height: 420px; overflow-y: auto; }}
.scroll-body::-webkit-scrollbar {{ width: 10px; }}
.scroll-body::-webkit-scrollbar-track {{ background: #F4EFE6; }}
.scroll-body::-webkit-scrollbar-thumb {{ background: #C8C1AC; border-radius: 5px; }}
.scroll-body::-webkit-scrollbar-thumb:hover {{ background: #534C37; }}
.bucket {{ border-bottom: 1px solid #D6D6D6; }}
.bucket:last-child {{ border-bottom: none; }}
.bucket-head {{ padding: 12px 24px; background: #F4EFE6; display: flex;
                 justify-content: space-between; align-items: center; border-left: 5px solid #FF3300;
                 position: sticky; top: 0; z-index: 2; }}
.bucket-head .label {{ font-weight: 700; font-size: 13px; letter-spacing: 0.1em; text-transform: uppercase; color: #282828; }}
.bucket-head .count {{ font-size: 12px; color: #534C37; letter-spacing: 0.1em; text-transform: uppercase; }}
.bucket-head .count strong {{ color: #FF3300; font-size: 16px; }}
table {{ width: 100%; border-collapse: collapse; background: #FFFFFF; }}
th {{ text-align: left; padding: 10px 24px; font-size: 11px; font-weight: 700;
       text-transform: uppercase; letter-spacing: 0.12em; color: #534C37;
       border-bottom: 2px solid #D6D6D6; background: #FFFFFF;
       position: sticky; top: 0; z-index: 1; }}
/* Weekly Breakdown has a sticky bucket-head (top: 0) ABOVE each table thead,
   so the thead must stick below the bucket-head's height (~44px). */
#weekly-breakdown .scroll-body th {{ top: 44px; }}
/* Call Off Aid has a sticky county-header (top: 0) ABOVE the thead too. */
#call-off-aid .scroll-body th {{ top: 0; }}
th.hr, th.dist {{ text-align: right; }}
td {{ padding: 10px 24px; border-bottom: 1px solid #F4EFE6; vertical-align: top; }}
tbody tr:hover {{ background: #F4EFE6; }}
td.hr, td.dist, td.timing {{ text-align: right; font-weight: 700; font-variant-numeric: tabular-nums; color: #282828; }}
th.timing, th.driving {{ text-align: right; }}
th.driving {{ text-align: center; }}
td.driving {{ text-align: center; font-size: 16px; font-weight: 700; }}
td.driving.yes {{ color: #1F8A3C; }}
td.driving.no  {{ color: #FF3300; }}
td.timing.on-shift {{ color: #1F8A3C; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; }}
td.timing.none {{ color: #8a8a8a; font-weight: 400; }}
th.eta, td.eta {{ text-align: right; }}
td.eta {{ font-weight: 700; font-variant-numeric: tabular-nums; color: #282828; }}
th.contact, td.contact {{ text-align: center; min-width: 138px; white-space: nowrap; }}
td.contact button {{ width: 24px; height: 24px; padding: 0; margin: 0 2px; border: 1px solid #C8C1AC;
                       background: #FFFFFF; border-radius: 3px; font-size: 13px; font-weight: 700;
                       cursor: pointer; font-family: inherit; vertical-align: middle; line-height: 1; }}
td.contact button:hover {{ filter: brightness(0.92); }}
td.contact .ct-yes {{ color: #1F8A3C; }}
td.contact .ct-no  {{ color: #FF3300; }}
td.contact .ct-na  {{ color: #534C37; }}
td.contact .ct-mb  {{ color: #C97B00; }}
tr.penalty {{ background: #F4EFE6; color: #8a8a8a; }}
tr.penalty td {{ color: #8a8a8a; }}
tr.penalty td.name .emp-name {{ color: #534C37; }}
.penalty-badge {{ display: inline-block; margin-left: 6px; padding: 2px 7px; border-radius: 10px;
                    font-size: 10px; font-weight: 700; letter-spacing: 0.06em;
                    text-transform: uppercase; background: #C8C1AC; color: #282828; }}
.penalty-badge.yes {{ background: #C2E8CB; color: #1F5C2C; }}
.penalty-badge.no  {{ background: #FFD5C8; color: #8B2A0F; }}
.penalty-badge.na  {{ background: #E5E0D2; color: #534C37; }}
.penalty-badge.mb  {{ background: #FFE7BF; color: #8A5300; }}
tr.at-risk {{ background: #FFF1EC; }}
tr.at-risk td:first-child {{ border-left: 4px solid #FF3300; padding-left: 20px; }}
.risk-flag {{ display: inline-block; margin-left: 8px; padding: 2px 8px; border-radius: 10px;
                font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
                background: #FF3300; color: #FFFFFF; }}
td.contact-count.flag {{ color: #FF3300; }}
.log-controls {{ display: flex; gap: 10px; align-items: center; padding: 10px 24px;
                   background: #F4EFE6; border-bottom: 1px solid #D6D6D6; font-size: 11px;
                   color: #534C37; letter-spacing: 0.08em; text-transform: uppercase; }}
.log-controls button {{ background: transparent; border: 1px solid #534C37; color: #534C37;
                          padding: 4px 10px; font-size: 10px; letter-spacing: 0.1em;
                          text-transform: uppercase; cursor: pointer; border-radius: 3px;
                          font-family: inherit; font-weight: 700; }}
.log-controls button:hover {{ background: #534C37; color: #F4EFE6; }}
.emp-name {{ display: block; font-weight: 600; color: #282828; }}
.emp-id {{ display: block; font-size: 10px; color: #8a8a8a; letter-spacing: 0.05em; margin-top: 2px; }}
.empty {{ padding: 32px 24px; color: #8a8a8a; text-align: center; font-style: italic; font-size: 13px; }}
.coa-controls {{ padding: 16px 24px; background: #F4EFE6; border-bottom: 1px solid #D6D6D6;
                  display: flex; gap: 14px; align-items: center; }}
.coa-controls label {{ font-size: 11px; letter-spacing: 0.15em; text-transform: uppercase;
                        color: #534C37; font-weight: 700; }}
.coa-controls select {{ flex: 1; padding: 8px 12px; font-size: 14px; font-family: inherit;
                         border: 1px solid #C8C1AC; background: #FFFFFF; color: #282828; border-radius: 3px; }}
.coa-site-info {{ padding: 10px 24px; background: #FFFFFF; font-size: 12px; color: #534C37;
                   border-bottom: 1px solid #D6D6D6; }}
.coa-site-info strong {{ color: #282828; }}
.eh-controls {{ padding: 16px 24px; background: #F4EFE6; border-bottom: 1px solid #D6D6D6;
                  display: flex; flex-direction: column; gap: 12px; }}
.eh-row {{ display: flex; gap: 12px; align-items: center; }}
.eh-row label {{ font-size: 11px; letter-spacing: 0.15em; text-transform: uppercase;
                  color: #534C37; font-weight: 700; }}
.eh-row input[type=text] {{ flex: 1; padding: 8px 12px; font-size: 14px; font-family: inherit;
                              border: 1px solid #C8C1AC; background: #FFFFFF; color: #282828; border-radius: 3px; }}
.eh-row .checkbox-group {{ display: flex; gap: 18px; flex-wrap: wrap; }}
.eh-row .checkbox-group label {{ display: flex; align-items: center; gap: 6px; cursor: pointer;
                                   text-transform: none; letter-spacing: 0.04em; font-size: 13px; color: #282828; }}
.eh-row .checkbox-group input[type=checkbox] {{ width: 16px; height: 16px; cursor: pointer; accent-color: #FF3300; }}
#eh-search-btn {{ background: #FF3300; color: #FFFFFF; border: none; padding: 8px 18px; font-size: 11px;
                   letter-spacing: 0.15em; text-transform: uppercase; font-weight: 700; cursor: pointer;
                   border-radius: 3px; font-family: inherit; min-width: 110px; }}
#eh-search-btn:hover {{ background: #cc2900; }}
#eh-search-btn:disabled {{ background: #C8C1AC; color: #534C37; cursor: wait; }}
footer {{ text-align: center; color: #534C37; font-size: 11px; padding: 24px;
           letter-spacing: 0.1em; text-transform: uppercase; }}
footer strong {{ color: #FF3300; }}
</style></head>
<body>
<div class="shell">
<div class="brand">
  <img src="data:image/png;base64,{logo_b64()}" alt="All Pro Security">
  <div class="titles">
    <h1>Scheduling Dashboard</h1>
    <div class="sub">Region - All Pro Security</div>
    <div class="gen">Last refreshed: <strong>{gen_label}</strong> <span id="age-indicator"></span></div>
  </div>
</div>
<div class="subbar">
  <span>Pay Cycle Tracking - TrackTik Live Data - {excluded} non-field roles hidden</span>
  <span class="region-pill">Region 2</span>
</div>

<section id="weekly-breakdown">
  <div class="sec-head"><h2>Weekly Breakdown - {week_start_s} - {week_end_s}</h2>
    <span class="meta">Employees under 30 hrs</span></div>
  <div class="scroll-body">
    <div class="bucket"><div class="bucket-head"><span class="label">0 - 8 Hours</span>
      <span class="count"><strong>{b1}</strong> employees</span></div>
      {render_table(sec1["0-8"], show_loc=False, with_contact=True)}</div>
    <div class="bucket"><div class="bucket-head"><span class="label">8 - 16 Hours</span>
      <span class="count"><strong>{b2}</strong> employees</span></div>
      {render_table(sec1["8-16"], show_loc=False, with_contact=True)}</div>
    <div class="bucket"><div class="bucket-head"><span class="label">16 - 30 Hours</span>
      <span class="count"><strong>{b3}</strong> employees</span></div>
      {render_table(sec1["16-30"], show_loc=False, with_contact=True)}</div>
  </div>
</section>

<section id="monthly-tracker">
  <div class="sec-head"><h2>Monthly Tracker - {month_label}</h2>
    <span class="meta">{len(sec2)} employees - up to 40 hrs - red = under 24 hr policy minimum</span></div>
  <div class="scroll-body">{render_monthly_contact_table(sec2)}</div>
</section>

<section id="call-off-aid">
  <div class="sec-head"><h2>Call Off Aid</h2>
    <span class="meta">{site_count} sites - {coa_emp_count} officers in coverage pool</span></div>
  <div class="coa-controls">
    <label for="coa-site">Select Site:</label>
    <select id="coa-site" onchange="renderCallOffAid()">
      <option value="">-- choose a site --</option>
      {site_options}
    </select>
  </div>
  <div class="coa-site-info" id="coa-site-info">Pick a site above to see officers sorted by distance.</div>
  <div class="log-controls">
    <span>Contact log saved in this browser - <span id="coa-log-summary">no contacts logged yet</span></span>
    <button type="button" onclick="exportContactLog()">Export CSV</button>
    <button type="button" onclick="clearContactLog()">Clear Log</button>
  </div>
  <div class="scroll-body"><div id="coa-list"></div></div>
</section>

<section id="extra-help">
  <div class="sec-head"><h2>Extra Help Request</h2>
    <span class="meta">{coa_emp_count} officers in pool</span></div>
  <div class="eh-controls">
    <div class="eh-row">
      <label for="eh-location">Location:</label>
      <input type="text" id="eh-location" placeholder="Street address or Utah city (e.g. 123 S Main St, Salt Lake City)" onkeydown="if(event.key==='Enter') searchExtraHelp()">
      <button id="eh-search-btn" onclick="searchExtraHelp()">Search</button>
    </div>
    <div class="eh-row">
      <label for="eh-datetime">Date / Time:</label>
      <input type="datetime-local" id="eh-datetime">
      <button type="button" onclick="document.getElementById('eh-datetime').value=''" style="background:#C8C1AC;color:#282828;border:none;padding:6px 12px;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;font-weight:700;cursor:pointer;border-radius:3px;font-family:inherit;">Clear / Now</button>
    </div>
    <div class="eh-row">
      <label>Required Skills:</label>
      <div class="checkbox-group">
        <label><input type="checkbox" id="eh-unarmed"> Unarmed</label>
        <label><input type="checkbox" id="eh-armed"> Armed</label>
        <label><input type="checkbox" id="eh-driving"> Driving Permissions</label>
      </div>
    </div>
  </div>
  <div class="coa-site-info" id="eh-info">Enter a Utah address or city above, optionally pick required skills, then click Search.</div>
  <div class="scroll-body"><div id="eh-list"></div></div>
</section>

<footer>All Pro Security - Region 2 - <strong>Prepared. Positioned. Pointed in the right direction.</strong></footer>
</div>

<script>
const COA = {coa_data};

function haversineMiles(lat1, lon1, lat2, lon2) {{
  const R = 3958.7613;
  const toRad = d => d * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat/2)**2 + Math.cos(toRad(lat1))*Math.cos(toRad(lat2))*Math.sin(dLon/2)**2;
  return 2 * R * Math.asin(Math.sqrt(a));
}}

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
}}

function renderCallOffAid() {{
  const sel = document.getElementById("coa-site");
  const info = document.getElementById("coa-site-info");
  const list = document.getElementById("coa-list");
  const siteId = parseInt(sel.value, 10);
  if (!siteId) {{
    info.innerHTML = "Pick a site above to see officers sorted by distance.";
    list.innerHTML = "";
    return;
  }}
  const site = COA.sites.find(s => s.id === siteId);
  if (!site) {{ list.innerHTML = ""; return; }}
  const now = COA.now || Math.floor(Date.now() / 1000);
  const eligibleSet = new Set(site.eligibleIds || []);
  const pool = COA.employees.filter(e => eligibleSet.has(e.id));
  info.innerHTML = "<strong>" + escapeHtml(site.name) + "</strong> &middot; " +
                   escapeHtml(site.address || "no address on file") +
                   " &middot; " + site.weeklyHours.toFixed(1) + " hrs scheduled this week" +
                   " &middot; " + pool.length + " eligible officer" + (pool.length === 1 ? "" : "s") +
                   " (of " + COA.employees.length + " in pool, " +
                   (site.positionCount || 0) + " position type" + ((site.positionCount === 1) ? "" : "s") + " this week)";
  const ranked = pool.map(e => ({{
    ...e, distance: haversineMiles(site.lat, site.lon, e.lat, e.lon)
  }})).sort((a,b) => a.distance - b.distance);

  function fmtSince(e) {{
    if (e.onShiftNow) return ['<td class="timing on-shift">On Shift</td>', null];
    if (!e.lastShiftEnd) return ['<td class="timing none">&mdash;</td>', null];
    const hrs = Math.max(0, (now - e.lastShiftEnd) / 3600);
    return ['<td class="timing">' + hrs.toFixed(1) + ' h</td>', hrs];
  }}
  function fmtUntil(e) {{
    if (e.onShiftNow) return ['<td class="timing on-shift">On Shift</td>', null];
    if (!e.nextShiftStart) return ['<td class="timing none">&mdash;</td>', null];
    const hrs = Math.max(0, (e.nextShiftStart - now) / 3600);
    return ['<td class="timing">' + hrs.toFixed(1) + ' h</td>', hrs];
  }}
  function fmtDriving(e) {{
    return e.drivingPermissions
      ? '<td class="driving yes" title="Has driving permissions">&#10003;</td>'
      : '<td class="driving no" title="No driving permissions">&#10007;</td>';
  }}

  list.innerHTML = renderRankedTable(ranked);
}}

function timingAt(emp, t) {{
  // Recompute lastEnd / nextStart / onShift relative to target unix t,
  // using the per-employee shift list emitted by the server.
  let lastEnd = null, nextStart = null, onShift = false;
  for (const pair of emp.shifts || []) {{
    const s = pair[0], e = pair[1];
    if (s <= t && t < e) {{ onShift = true; }}
    else if (e <= t) {{ if (lastEnd === null || e > lastEnd) lastEnd = e; }}
    else if (s > t) {{ if (nextStart === null || s < nextStart) nextStart = s; }}
  }}
  return {{ lastEnd, nextStart, onShift }};
}}

// ---------- Contact log (persists in localStorage + optional Cloudflare KV) ----------
const REMOTE_LOG_URL = "https://aps-contact-log.christian-tanuvasa.workers.dev";
const PENALTY_SEC = {{ yes: 12 * 3600, no: 8 * 3600, noanswer: 30 * 60, maybe: 15 * 60 }};
const OUTCOME_LABEL = {{ yes: "Yes", no: "No", noanswer: "No Ans", maybe: "Maybe" }};
const OUTCOME_CLASS = {{ yes: "yes", no: "no", noanswer: "na", maybe: "mb" }};

function getLog() {{
  try {{ return JSON.parse(localStorage.getItem("apsContactLog") || "[]"); }}
  catch (e) {{ return []; }}
}}
function saveLog(log) {{ localStorage.setItem("apsContactLog", JSON.stringify(log)); }}

// Re-render everything visible after a log change (local or remote).
function rerenderAfterLogChange() {{
  updateLogSummary();
  const coaSel = document.getElementById("coa-site");
  if (coaSel && coaSel.value) renderCallOffAid();
  const ehList = document.getElementById("eh-list");
  const ehInput = document.getElementById("eh-location");
  if (ehList && ehList.innerHTML && ehInput && ehInput.value.trim()) searchExtraHelp();
  enhanceMonthlyTracker();
  enhanceWeeklyTracker();
}}

async function pushRemote(entry) {{
  if (!REMOTE_LOG_URL) return null;
  try {{
    const r = await fetch(REMOTE_LOG_URL + "/log", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify(entry),
    }});
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();  // server returns the entry with a server-assigned id
  }} catch (e) {{
    console.warn("Worker push failed; entry stays in local log only.", e);
    return null;
  }}
}}

async function pullRemote() {{
  if (!REMOTE_LOG_URL) return;
  try {{
    const r = await fetch(REMOTE_LOG_URL + "/log");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    const remote = data.entries || [];
    const local = getLog();
    const haveIds = new Set(local.filter(e => e.id).map(e => e.id));
    let added = 0;
    for (const e of remote) {{
      if (e.id && haveIds.has(e.id)) continue;
      // Match against unsynced local entries (no id) by ts + empId + outcome
      const dup = local.find(l => !l.id && l.empId === e.empId
                                && l.outcome === e.outcome
                                && Math.abs((l.ts || 0) - (e.ts || 0)) < 5);
      if (dup) {{ dup.id = e.id; added++; continue; }}
      local.push(e);
      haveIds.add(e.id);
      added++;
    }}
    if (added > 0) {{
      saveLog(local);
      rerenderAfterLogChange();
    }}
    updateSyncStatus("ok");
  }} catch (e) {{
    console.warn("Remote sync failed; using local log only.", e);
    updateSyncStatus("error");
  }}
}}

function updateSyncStatus(state) {{
  const el = document.getElementById("coa-log-summary");
  if (!el) return;
  // Append a small (synced/offline) marker to the summary text
  const log = getLog();
  const today = new Date(); today.setHours(0,0,0,0);
  const tStart = Math.floor(today.getTime() / 1000);
  const todayCount = log.filter(r => r.ts >= tStart).length;
  const baseText = log.length
    ? (log.length + " total contact" + (log.length === 1 ? "" : "s") + ", " + todayCount + " today")
    : "no contacts logged yet";
  let suffix = "";
  if (REMOTE_LOG_URL) {{
    suffix = state === "ok"
      ? " &middot; <span style='color:#1F8A3C;'>synced</span>"
      : state === "error"
      ? " &middot; <span style='color:#FF3300;'>offline (using local only)</span>"
      : " &middot; syncing...";
  }}
  el.innerHTML = baseText + suffix;
}}

async function logContact(empId, outcome) {{
  const ts = Math.floor(Date.now() / 1000);
  const entry = {{ empId, outcome, ts }};
  const local = getLog();
  local.push(entry);
  saveLog(local);
  rerenderAfterLogChange();
  // Fire-and-forget the remote push; if it succeeds, store the server-assigned id locally.
  const remoteEntry = await pushRemote(entry);
  if (remoteEntry && remoteEntry.id) {{
    const cur = getLog();
    const idx = cur.findIndex(l => !l.id && l.ts === entry.ts
                                && l.empId === entry.empId && l.outcome === entry.outcome);
    if (idx >= 0) {{ cur[idx].id = remoteEntry.id; saveLog(cur); }}
    updateSyncStatus("ok");
  }} else if (REMOTE_LOG_URL) {{
    updateSyncStatus("error");
  }}
}}
function clearContactLog() {{
  if (!confirm("Clear the entire contact log? This cannot be undone.")) return;
  localStorage.removeItem("apsContactLog");
  updateLogSummary();
  if (document.getElementById("coa-site").value) renderCallOffAid();
  enhanceMonthlyTracker();
}}
function exportContactLog() {{
  const log = getLog();
  if (!log.length) {{ alert("No contacts logged yet."); return; }}
  const empIdx = new Map(COA.employees.map(e => [e.id, e]));
  const rows = [["Timestamp ISO","Employee ID","Custom ID","Name","Outcome"]];
  for (const r of log) {{
    const e = empIdx.get(r.empId) || {{}};
    rows.push([
      new Date(r.ts * 1000).toISOString(),
      r.empId,
      e.customId || "",
      e.name || "Employee #" + r.empId,
      OUTCOME_LABEL[r.outcome] || r.outcome,
    ]);
  }}
  const csv = rows.map(r => r.map(v => '"' + String(v).replace(/"/g, '""') + '"').join(",")).join("\\n");
  const blob = new Blob([csv], {{ type: "text/csv;charset=utf-8;" }});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "aps-contact-log-" + new Date().toISOString().slice(0,10) + ".csv";
  a.click();
}}
function lastContactFor(empId) {{
  const log = getLog();
  let best = null;
  for (const r of log) {{
    if (r.empId !== empId) continue;
    if (!best || r.ts > best.ts) best = r;
  }}
  return best;
}}
function penaltyAt(empId, t) {{
  const last = lastContactFor(empId);
  if (!last) return null;
  const window = PENALTY_SEC[last.outcome] || 0;
  if (last.ts + window > t) {{
    return {{ until: last.ts + window, lastTs: last.ts, outcome: last.outcome }};
  }}
  return null;
}}
function contactsThisMonth(empId, monthStartUnix) {{
  const log = getLog();
  let count = 0;
  for (const r of log) {{
    if (r.empId === empId && r.ts >= monthStartUnix) count++;
  }}
  return count;
}}
// updateLogSummary is now a thin alias around updateSyncStatus so old callers
// keep working. The full summary (with sync state) lives in updateSyncStatus.
function updateLogSummary() {{ updateSyncStatus("pending"); }}

// ---------- ETA ----------
function etaUnix(targetUnix, miles) {{
  // 15-minute readiness buffer + 1.5 minutes per mile driving estimate
  return targetUnix + (15 + miles * 1.5) * 60;
}}
function fmtEta(unix, refUnix) {{
  const d = new Date(unix * 1000);
  const ref = new Date(refUnix * 1000);
  const sameDay = d.getFullYear() === ref.getFullYear()
               && d.getMonth() === ref.getMonth()
               && d.getDate() === ref.getDate();
  if (sameDay) {{
    return d.toLocaleTimeString(undefined, {{ hour: "numeric", minute: "2-digit" }});
  }}
  return d.toLocaleString(undefined, {{ weekday: "short", hour: "numeric", minute: "2-digit" }});
}}

function monthStartUnix(refUnix) {{
  const d = new Date(refUnix * 1000);
  return Math.floor(new Date(d.getFullYear(), d.getMonth(), 1).getTime() / 1000);
}}

function fmtDurationShort(seconds) {{
  if (seconds <= 0) return "now";
  if (seconds < 60) return Math.round(seconds) + "s";
  const m = Math.round(seconds / 60);
  if (m < 60) return m + "m";
  const h = (seconds / 3600);
  return (h >= 10 ? Math.round(h) : h.toFixed(1)) + "h";
}}

function fmtRelativeContact(ts, refUnix) {{
  const ago = refUnix - ts;
  if (ago < 60) return "just now";
  if (ago < 3600) return Math.round(ago / 60) + " min ago";
  if (ago < 86400) return (ago / 3600).toFixed(1) + " h ago";
  return Math.round(ago / 86400) + " d ago";
}}

function renderRankedTable(ranked, targetTime) {{
  const baseNow = COA.now || Math.floor(Date.now() / 1000);
  const t = (typeof targetTime === "number" && !isNaN(targetTime)) ? targetTime : baseNow;
  const useTarget = (typeof targetTime === "number" && !isNaN(targetTime));
  function fmtSince(e) {{
    const tm = useTarget ? timingAt(e, t) : {{
      lastEnd: e.lastShiftEnd, nextStart: e.nextShiftStart, onShift: e.onShiftNow,
    }};
    if (tm.onShift) return '<td class="timing on-shift">On Shift</td>';
    if (!tm.lastEnd) return '<td class="timing none">&mdash;</td>';
    return '<td class="timing">' + Math.max(0,(t - tm.lastEnd)/3600).toFixed(1) + ' h</td>';
  }}
  function fmtUntil(e) {{
    const tm = useTarget ? timingAt(e, t) : {{
      lastEnd: e.lastShiftEnd, nextStart: e.nextShiftStart, onShift: e.onShiftNow,
    }};
    if (tm.onShift) return '<td class="timing on-shift">On Shift</td>';
    if (!tm.nextStart) return '<td class="timing none">&mdash;</td>';
    return '<td class="timing">' + Math.max(0,(tm.nextStart - t)/3600).toFixed(1) + ' h</td>';
  }}
  function fmtDriving(e) {{
    return e.drivingPermissions
      ? '<td class="driving yes" title="Has driving permissions">&#10003;</td>'
      : '<td class="driving no" title="No driving permissions">&#10007;</td>';
  }}
  if (!ranked.length) {{
    return '<div class="empty">No officers match the selected filters.</div>';
  }}
  // Efficiency-based sort: officers with an active contact penalty go to the bottom.
  // Within each band (fresh vs penalty) we sort by distance ascending.
  const annotated = ranked.map(e => ({{ ...e, _penalty: penaltyAt(e.id, t) }}));
  annotated.sort((a, b) => {{
    if (a._penalty && !b._penalty) return 1;
    if (!a._penalty && b._penalty) return -1;
    return a.distance - b.distance;
  }});

  let html = '<table><thead><tr><th>Name / ID</th><th>Job Title</th><th>City</th>'
           + '<th class="hr">Wk Hrs</th><th class="dist">Miles</th>'
           + '<th class="eta">ETA</th>'
           + '<th class="timing">Since Last</th><th class="timing">Until Next</th>'
           + '<th class="driving">Driving</th>'
           + '<th class="contact">Log Contact</th></tr></thead><tbody>';
  for (const e of annotated) {{
    const city = e.city ? escapeHtml(e.city) + (e.state ? ", " + escapeHtml(e.state) : "") : "-";
    const arrival = etaUnix(t, e.distance);
    const etaText = fmtEta(arrival, t);
    let penaltyBadge = "";
    let rowCls = "";
    if (e._penalty) {{
      rowCls = ' class="penalty"';
      const ago = fmtRelativeContact(e._penalty.lastTs, t);
      const left = fmtDurationShort(e._penalty.until - t);
      const cls = OUTCOME_CLASS[e._penalty.outcome] || "";
      penaltyBadge = ' <span class="penalty-badge ' + cls + '">'
                   + escapeHtml(OUTCOME_LABEL[e._penalty.outcome] || e._penalty.outcome)
                   + ' &middot; ' + ago + ' &middot; ' + left + ' left</span>';
    }}
    const contactButtons =
      '<td class="contact">'
      + '<button class="ct-yes" title="Yes - accepted (12h cooldown)" onclick="logContact(' + e.id + ', &quot;yes&quot;)">&#10003;</button>'
      + '<button class="ct-no"  title="No - declined (8h cooldown)"   onclick="logContact(' + e.id + ', &quot;no&quot;)">&#10007;</button>'
      + '<button class="ct-na"  title="No Answer (30 min cooldown)"    onclick="logContact(' + e.id + ', &quot;noanswer&quot;)">&#9742;</button>'
      + '<button class="ct-mb"  title="Maybe / called back (15 min cooldown)" onclick="logContact(' + e.id + ', &quot;maybe&quot;)">?</button>'
      + '</td>';
    html += '<tr' + rowCls + '>'
         + '<td class="name"><span class="emp-name">' + escapeHtml(e.name) + penaltyBadge + '</span>'
         +   '<span class="emp-id">' + escapeHtml(e.customId || "") + '</span></td>'
         + '<td>' + escapeHtml(e.jobTitle || "-") + '</td>'
         + '<td>' + city + '</td>'
         + '<td class="hr">' + e.weekHours.toFixed(1) + '</td>'
         + '<td class="dist">' + e.distance.toFixed(1) + '</td>'
         + '<td class="eta">' + escapeHtml(etaText) + '</td>'
         + fmtSince(e) + fmtUntil(e) + fmtDriving(e)
         + contactButtons
         + '</tr>';
  }}
  html += '</tbody></table>';
  return html;
}}

// ---------- Monthly Tracker enhancement (contact count + last contact) ----------
function enhanceMonthlyTracker() {{
  const refUnix = COA.now || Math.floor(Date.now() / 1000);
  const mStart = monthStartUnix(refUnix);
  const rows = document.querySelectorAll('#monthly-tracker tr[data-eid]');
  for (const row of rows) {{
    const eid = parseInt(row.getAttribute("data-eid"), 10);
    const count = contactsThisMonth(eid, mStart);
    const last = lastContactFor(eid);
    const cntCell = row.querySelector(".contact-count");
    const lastCell = row.querySelector(".last-contact");
    if (cntCell) {{
      cntCell.textContent = count;
      const atRisk = row.classList.contains("at-risk");
      // Flag in red if at-risk AND contacted fewer than 3 times this month
      if (atRisk && count < 3) cntCell.classList.add("flag");
      else cntCell.classList.remove("flag");
    }}
    if (lastCell) {{
      if (last) {{
        lastCell.innerHTML = escapeHtml(OUTCOME_LABEL[last.outcome] || last.outcome)
                           + ' &middot; ' + fmtRelativeContact(last.ts, refUnix);
      }} else {{
        lastCell.innerHTML = '&mdash;';
      }}
    }}
  }}
}}

// ---------- Weekly Breakdown enhancement (penalty / last contact badge) ----------
function enhanceWeeklyTracker() {{
  const refUnix = COA.now || Math.floor(Date.now() / 1000);
  const rows = document.querySelectorAll('#weekly-breakdown tr[data-eid], #monthly-tracker tr[data-eid]');
  for (const row of rows) {{
    const eid = parseInt(row.getAttribute("data-eid"), 10);
    const nameCell = row.querySelector(".emp-name");
    if (!nameCell) continue;
    // Strip any existing inline badge first
    const old = nameCell.querySelector(".penalty-badge");
    if (old) old.remove();
    const pen = penaltyAt(eid, refUnix);
    if (pen) {{
      const ago = fmtRelativeContact(pen.lastTs, refUnix);
      const left = fmtDurationShort(pen.until - refUnix);
      const cls = OUTCOME_CLASS[pen.outcome] || "";
      const badge = document.createElement("span");
      badge.className = "penalty-badge " + cls;
      badge.innerHTML = (OUTCOME_LABEL[pen.outcome] || pen.outcome) + ' &middot; ' + ago + ' &middot; ' + left + ' left';
      nameCell.appendChild(document.createTextNode(" "));
      nameCell.appendChild(badge);
    }}
  }}
}}

// ---------- Live "Last refreshed: ... (X min ago)" indicator ----------
const BUILD_TS = (COA && COA.now) ? COA.now : Math.floor(Date.now() / 1000);

function formatAge(seconds) {{
  if (seconds < 60) return "(just now)";
  if (seconds < 3600) {{
    const m = Math.round(seconds / 60);
    return "(" + m + " min ago)";
  }}
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  if (h < 24) return "(" + h + "h " + m + "m ago)";
  const d = Math.floor(h / 24);
  return "(" + d + " day" + (d === 1 ? "" : "s") + " ago)";
}}

function tickAge() {{
  const el = document.getElementById("age-indicator");
  if (!el) return;
  const ageSec = Math.max(0, Math.floor(Date.now() / 1000) - BUILD_TS);
  el.textContent = formatAge(ageSec);
  // Color-code: green when fresh, gold mid, red if stale
  if (ageSec < 12 * 60) el.style.color = "#1F8A3C";       // <12 min: fresh
  else if (ageSec < 30 * 60) el.style.color = "#C97B00";  // <30 min: warning
  else el.style.color = "#FF3300";                         // 30+ min: stale
}}

// ---------- Silent page reload every 10 minutes ----------
// The static HTML is rebuilt server-side (GitHub Actions or local server).
// We just reload the page periodically so users see the freshest snapshot.
const PAGE_RELOAD_MS = 10 * 60 * 1000;

function scheduleReload() {{
  if (location.protocol === "file:") return;  // no point if opened as a local file
  setTimeout(function () {{
    // Save UI state we want to survive the reload
    const sel = document.getElementById("coa-site");
    if (sel && sel.value) sessionStorage.setItem("coaSelected", sel.value);
    location.reload();
  }}, PAGE_RELOAD_MS);
}}

// Restore selected site after reload
function restoreCoaSelection() {{
  const saved = sessionStorage.getItem("coaSelected");
  if (!saved) return;
  const sel = document.getElementById("coa-site");
  if (sel && [...sel.options].some(o => o.value === saved)) {{
    sel.value = saved;
    renderCallOffAid();
  }}
}}

async function searchExtraHelp() {{
  const q = document.getElementById("eh-location").value.trim();
  const info = document.getElementById("eh-info");
  const list = document.getElementById("eh-list");
  const btn = document.getElementById("eh-search-btn");
  if (!q) {{
    info.innerHTML = '<span style="color:#FF3300;">Please enter an address or Utah city.</span>';
    list.innerHTML = "";
    return;
  }}
  const wantUnarmed = document.getElementById("eh-unarmed").checked;
  const wantArmed = document.getElementById("eh-armed").checked;
  const wantDriving = document.getElementById("eh-driving").checked;
  const dateStr = document.getElementById("eh-datetime").value;
  let targetTime = null;
  if (dateStr) {{
    const d = new Date(dateStr);
    if (!isNaN(d.getTime())) targetTime = Math.floor(d.getTime() / 1000);
  }}
  btn.disabled = true; btn.textContent = "Searching...";
  info.innerHTML = "Looking up location in Utah...";
  list.innerHTML = "";
  try {{
    // Bias query to Utah USA. countrycodes=us narrows to US, then we look for state=Utah.
    const url = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&countrycodes=us&addressdetails=1&q=" + encodeURIComponent(q + ", Utah, USA");
    const r = await fetch(url, {{ headers: {{ "Accept-Language": "en" }} }});
    if (!r.ok) throw new Error("Geocoder returned " + r.status);
    const arr = await r.json();
    // Prefer the first result whose address.state is Utah
    let loc = arr.find(x => (x.address && (x.address.state === "Utah" || x.address["ISO3166-2-lvl4"] === "US-UT")));
    if (!loc && arr.length) loc = arr[0];
    if (!loc) {{
      info.innerHTML = '<span style="color:#FF3300;">No Utah location found for "' + escapeHtml(q) + '". Try adding more detail or a different city.</span>';
      return;
    }}
    const lat = parseFloat(loc.lat), lon = parseFloat(loc.lon);
    let pool = COA.employees.slice();
    if (wantUnarmed) pool = pool.filter(e => e.unarmedGuard);
    if (wantArmed) pool = pool.filter(e => e.armedGuard);
    if (wantDriving) pool = pool.filter(e => e.drivingPermissions);
    const ranked = pool.map(e => ({{
      ...e, distance: haversineMiles(lat, lon, e.lat, e.lon)
    }})).sort((a, b) => a.distance - b.distance);
    const skillTags = [];
    if (wantUnarmed) skillTags.push("Unarmed");
    if (wantArmed) skillTags.push("Armed");
    if (wantDriving) skillTags.push("Driving Permissions");
    const dateLabel = targetTime
      ? new Date(targetTime * 1000).toLocaleString(undefined, {{
          month: "short", day: "numeric", year: "numeric",
          hour: "numeric", minute: "2-digit"
        }})
      : "now";
    info.innerHTML = "<strong>" + escapeHtml(loc.display_name) + "</strong> &middot; "
                   + ranked.length + " officer" + (ranked.length === 1 ? "" : "s")
                   + (skillTags.length ? " with " + skillTags.join(" + ") : "")
                   + " in pool (under 32 hrs) &middot; timing as of " + escapeHtml(dateLabel);
    list.innerHTML = renderRankedTable(ranked, targetTime);
  }} catch (err) {{
    info.innerHTML = '<span style="color:#FF3300;">Search failed: ' + escapeHtml(err.message) + '</span>';
  }} finally {{
    btn.disabled = false; btn.textContent = "Search";
  }}
}}

// Init on page load
document.addEventListener("DOMContentLoaded", function() {{
  updateLogSummary();
  enhanceMonthlyTracker();
  enhanceWeeklyTracker();
  restoreCoaSelection();
  scheduleReload();
  tickAge();
  setInterval(tickAge, 30 * 1000);
  // Sync the contact log with the Cloudflare KV worker
  if (REMOTE_LOG_URL) {{
    pullRemote();  // initial fetch right away
    setInterval(pullRemote, 30 * 1000);  // then every 30 seconds
  }}
}});

async function doRefresh() {{
  const btn = document.getElementById("refresh-btn");
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Refreshing...';
  try {{
    const r = await fetch("/api/refresh", {{ method: "POST" }});
    if (!r.ok) throw new Error("HTTP " + r.status);
    await r.json();
    location.reload();
  }} catch (e) {{
    btn.innerHTML = original;
    btn.disabled = false;
    alert("Refresh failed: " + e.message);
  }}
}}
</script>
</body></html>
"""


_DATA_LOCK = threading.Lock()
_CACHED = {"data": None}


def get_data(force=False):
    with _DATA_LOCK:
        if force or _CACHED["data"] is None:
            _CACHED["data"] = pull_data()
        return _CACHED["data"]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stdout.write(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = build_html(get_data())
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/healthz":
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers(); self.wfile.write(b"not found")

    def do_POST(self):
        if self.path == "/api/refresh":
            try:
                data = get_data(force=True)
                body = json.dumps({"ok": True, "generatedAt": data["generatedAt"],
                                   "employeeCount": len(data["employees"]),
                                   "siteCount": len(data.get("sites", []))}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                msg = json.dumps({"ok": False, "error": str(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(msg)
        else:
            self.send_response(404); self.end_headers()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def build_static():
    print("[static] pulling data...")
    data = pull_data()
    html = build_html(data)
    with open(STATIC_HTML_PATH, "w") as f:
        f.write(html)
    print(f"[static] wrote {STATIC_HTML_PATH} ({len(html)} bytes)")
    print(f"[static] {len(data['employees'])} employees, {len(data.get('sites', []))} sites")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-static", action="store_true",
                    help="Regenerate scheduling-dashboard.html (static snapshot) and exit")
    args = ap.parse_args()
    if args.build_static:
        build_static()
        return
    print(f"[boot] APS Scheduling Dashboard server")
    print(f"[boot] tenant: {TENANT}")
    print(f"[boot] http://localhost:{PORT}")
    def _prime():
        try:
            get_data()
            print(f"[boot] cache primed ({len(_CACHED['data']['employees'])} employees, "
                  f"{len(_CACHED['data'].get('sites', []))} sites)", flush=True)
        except Exception as e:
            print(f"[boot] WARN: initial pull failed: {e}", flush=True)
    threading.Thread(target=_prime, daemon=True).start()
    httpd = ThreadedHTTPServer(("", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[shutdown]")


if __name__ == "__main__":
    main()
