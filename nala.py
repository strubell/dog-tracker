#!/usr/bin/env python3
"""Nala's activity tracker.

Keeps a local log of activities done with the dog, seeded from a Strava bulk
export (which includes the "With Pet" tag) and kept current via the Strava API
(which does NOT expose the tag — new activities are matched by keyword or
confirmed with a quick y/n).

Commands:
    seed <activities.csv>   import history from a Strava bulk-export CSV
    auth                    one-time Strava API authorization (opens browser)
    sync [--batch]          pull new activities from the API, then rebuild
    review                  answer y/n for activities the sync couldn't classify
    mark <id> yes|no        classify one activity by ID
    weight <lb> [--date]    log a weigh-in
    note "<text>" [--date]  log a joint-sign / vet note
    build                   regenerate dashboard.html

Requires only the Python 3 standard library.
"""
import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
ACTIVITIES = DATA / "activities.json"
WEIGHT = DATA / "weight.json"
NOTES = DATA / "notes.json"
CIRCLES = DATA / "circles.json"
MEALS = DATA / "meals.json"
GI_LOG = DATA / "gi_incidents.json"
AUTH = DATA / "strava_auth.json"
TEMPLATE = BASE / "dashboard_template.html"
DASHBOARD = BASE / "dashboard.html"
OAUTH_PORT = 8723

METERS_PER_MILE = 1609.344

# some Python builds (e.g. PlatformIO's portable Python) ship an OpenSSL whose
# baked-in CA path doesn't exist on this machine — fall back to certifi's bundle
try:
    if ssl.create_default_context().cert_store_stats()["x509_ca"] == 0:
        import certifi
        _ctx = ssl.create_default_context(cafile=certifi.where())
        urllib.request.install_opener(
            urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ctx)))
except Exception:
    pass


def load(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save(path, obj):
    DATA.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


def config():
    return json.loads((BASE / "config.json").read_text())


def dog_names(cfg):
    """Primary dog first, then any others, in config order."""
    return [cfg["dog"]] + list(cfg.get("other_dogs", []))


def derive_pet(dogs):
    """Collapse a per-dog map to the legacy tri-state `pet`: True if any dog
    came, False if all explicitly did not, None while any answer is missing."""
    vals = list(dogs.values())
    if any(v is True for v in vals):
        return True
    if vals and all(v is False for v in vals):
        return False
    return None


def tz():
    return ZoneInfo(config()["timezone"])


# ---------------------------------------------------------------- seed

def cmd_seed(args):
    cfg = config()
    db = load(ACTIVITIES, {})
    with open(args.csv_path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        # the export repeats some column names; take the last occurrence,
        # which holds the raw numeric value (meters / seconds)
        col = {name: i for i, name in enumerate(header)}
        added = updated = 0
        for row in reader:
            def get(name):
                i = col.get(name)
                return row[i] if i is not None and i < len(row) else ""

            raw_pet = get("With Pet")
            when = datetime.strptime(get("Activity Date"), "%b %d, %Y, %I:%M:%S %p")
            when = when.replace(tzinfo=timezone.utc).astimezone(tz())
            # only keep the era where the With Pet tag was actually in use
            if when.date().isoformat() < "2025-12-01":
                continue
            pet = {"1.0": True, "0.0": False}.get(raw_pet)
            if pet is None:
                pet = detect_pet(get("Activity Name"), get("Activity Description"),
                                 get("Activity Private Note"), get("Activity Type"), cfg)
            aid = get("Activity ID")
            rec = {
                "id": aid,
                "date": when.isoformat(),
                "name": get("Activity Name"),
                "type": get("Activity Type"),
                "mi": round(float(get("Distance") or 0) / METERS_PER_MILE, 2),
                "min": round(float(get("Moving Time") or 0) / 60),
                "elev_ft": round(float(get("Elevation Gain") or 0) * 3.28084),
                "pet": pet,
                "source": "export",
            }
            if aid in db:
                # an export re-import can only improve pet info, never erase
                # a classification made via the API or a manual `mark`
                if db[aid]["pet"] is None and pet is not None:
                    db[aid]["pet"] = pet
                    updated += 1
            else:
                db[aid] = rec
                added += 1
    save(ACTIVITIES, db)
    pets = sum(1 for r in db.values() if r["pet"])
    pending = sum(1 for r in db.values() if r["pet"] is None)
    print(f"Seeded {added} activities ({updated} updated). "
          f"{pets} with {cfg['dog']}, {pending} unclassified.")
    if pending:
        print("Run `python3 nala.py review` to classify the rest.")
    cmd_build(args)


# ---------------------------------------------------------------- strava api

class _CodeCatcher(BaseHTTPRequestHandler):
    code = None

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CodeCatcher.code = (q.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Authorized — you can close this tab." if _CodeCatcher.code else "No code received."
        self.wfile.write(f"<h2>{msg}</h2>".encode())

    def log_message(self, *_):
        pass


def _token_request(payload):
    req = urllib.request.Request(
        "https://www.strava.com/oauth/token",
        data=urllib.parse.urlencode(payload).encode(),
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def cmd_auth(args):
    auth = load(AUTH, {})
    print("Create an API app at https://www.strava.com/settings/api")
    print('(set "Authorization Callback Domain" to: localhost)\n')
    auth["client_id"] = input(f"Client ID [{auth.get('client_id', '')}]: ").strip() or auth.get("client_id")
    auth["client_secret"] = input(f"Client Secret [{'saved' if auth.get('client_secret') else ''}]: ").strip() or auth.get("client_secret")
    url = ("https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": auth["client_id"],
        "redirect_uri": f"http://localhost:{OAUTH_PORT}/callback",
        "response_type": "code",
        "scope": "activity:read_all",
    }))
    print(f"\nOpening browser to authorize… (or visit)\n{url}\n")
    server = HTTPServer(("localhost", OAUTH_PORT), _CodeCatcher)
    webbrowser.open(url)
    while _CodeCatcher.code is None:
        server.handle_request()
    tok = _token_request({
        "client_id": auth["client_id"],
        "client_secret": auth["client_secret"],
        "code": _CodeCatcher.code,
        "grant_type": "authorization_code",
    })
    auth.update(access_token=tok["access_token"],
                refresh_token=tok["refresh_token"],
                expires_at=tok["expires_at"])
    save(AUTH, auth)
    os.chmod(AUTH, 0o600)
    print("Authorized. Now run: python3 nala.py sync")


def _access_token():
    auth = load(AUTH, None)
    if not auth or "refresh_token" not in auth:
        sys.exit("Not authorized yet — run: python3 nala.py auth")
    if auth["expires_at"] < time.time() + 60:
        tok = _token_request({
            "client_id": auth["client_id"],
            "client_secret": auth["client_secret"],
            "refresh_token": auth["refresh_token"],
            "grant_type": "refresh_token",
        })
        auth.update(access_token=tok["access_token"],
                    refresh_token=tok["refresh_token"],
                    expires_at=tok["expires_at"])
        save(AUTH, auth)
    return auth["access_token"]


def api_get(path, **params):
    url = f"https://www.strava.com/api/v3{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_access_token()}"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            sys.exit("Strava rate limit hit — try again in ~15 minutes.")
        raise


# ---------------------------------------------------------------- sync

def detect_pet(name, description, private_note, sport_type, cfg):
    """True/False when confident, None when a human should decide."""
    text = " ".join(filter(None, [name, description, private_note])).lower()
    if any(k in text for k in cfg["keywords"]):
        return True
    if sport_type in cfg["auto_no_types"]:
        return False
    return None


def miles_from_home(latlng, cfg):
    if not latlng or "home_latlng" not in cfg:
        return None
    import math
    (la, lo), (ha, ho) = latlng, cfg["home_latlng"]
    dy = (la - ha) * 69.0
    dx = (lo - ho) * 69.0 * math.cos(math.radians(ha))
    return (dx * dx + dy * dy) ** 0.5


def _away_hint(rec, cfg):
    far = miles_from_home(rec.get("latlng"), cfg)
    if far is not None and far > cfg.get("home_radius_mi", 15):
        return f" ⚑ ~{far:.0f} mi from home"
    return ""


def _ask_yn(prompt):
    while True:
        ans = input(prompt).strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        if ans in ("s", "skip", ""):
            return None


def _ask_dogs(rec, cfg, hint=""):
    """Confirm each dog independently for one outing; returns a dogs map.
    Enter/skip leaves a dog's current answer unchanged."""
    print(f"  [{rec['id']}] {rec['date'][:10]}  {rec['type']:<10} {rec['min']:>4} min "
          f"{rec['mi']:>5.1f} mi  {rec['name'][:44]}{hint}")
    dogs = dict(rec.get("dogs") or {})
    for d in dog_names(cfg):
        ans = _ask_yn(f"    with {d}? [y/n/s(kip)] ")
        if ans is not None:
            dogs[d] = ans
        else:
            dogs.setdefault(d, None)
    return dogs


def fetch_new_activities(cfg, db, on_each=None):
    """Pull activities from Strava, add any new ones to db, save as we go.
    Returns (fetched_count, new_count). on_each(rec, hint) is called per new
    activity for logging. Never prompts — unknowns land as pet=None."""
    latest = max((r["date"] for r in db.values()), default="2025-12-01T00:00:00")
    after = datetime.fromisoformat(latest) - timedelta(days=3)
    page, fetched = 1, []
    while True:
        batch = api_get("/athlete/activities", after=int(after.timestamp()),
                        per_page=100, page=page)
        fetched.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    new = [a for a in fetched if str(a["id"]) not in db]
    for a in new:
        detail = api_get(f"/activities/{a['id']}")
        pet = detect_pet(detail.get("name"), detail.get("description"),
                         detail.get("private_note"), detail.get("sport_type"), cfg)
        rec = {
            "id": str(a["id"]),
            "date": detail["start_date_local"].rstrip("Z"),
            "name": detail.get("name", ""),
            "type": detail.get("sport_type", ""),
            "mi": round(detail.get("distance", 0) / METERS_PER_MILE, 2),
            "min": round(detail.get("moving_time", 0) / 60),
            "elev_ft": round(detail.get("total_elevation_gain", 0) * 3.28084),
            "latlng": detail.get("start_latlng") or None,
            "pet": pet,
            # keyword hit or auto-no applies to all dogs; unknown → all None
            "dogs": {d: pet for d in dog_names(cfg)},
            "source": "api",
        }
        db[rec["id"]] = rec
        save(ACTIVITIES, db)
        if on_each:
            on_each(rec, _away_hint(rec, cfg))
    return len(fetched), len(new)


def cmd_sync(args):
    cfg = config()
    db = load(ACTIVITIES, {})
    interactive = sys.stdin.isatty() and not args.batch

    def log_one(rec, hint):
        if rec["pet"] is None and interactive:
            rec["dogs"] = _ask_dogs(rec, cfg, hint)
            rec["pet"] = derive_pet(rec["dogs"])
            save(ACTIVITIES, db)
        with_dogs = [d for d, v in (rec.get("dogs") or {}).items() if v]
        tag = f"with {', '.join(with_dogs)}" if with_dogs else (
            "UNCLASSIFIED" if rec["pet"] is None else "no dogs")
        print(f"  + [{rec['id']}] {rec['date'][:10]} {rec['name'][:40]!r} → {tag}{hint}")

    n_all, n_new = fetch_new_activities(cfg, db, on_each=log_one)
    print(f"{n_all} activities checked, {n_new} new.")
    pending = sum(1 for r in db.values() if r["pet"] is None)
    if pending:
        print(f"{pending} unclassified — run `python3 nala.py review`.")
    if interactive:
        _ask_circles(cfg, db)
    else:
        nc = len(unanswered_circle_days(cfg, db))
        if nc:
            print(f"{nc} circle day(s) unanswered — run `python3 nala.py sync` interactively "
                  f"or `python3 nala.py circle yes --date YYYY-MM-DD`.")
    cmd_build(args)


def cmd_review(args):
    cfg = config()
    db = load(ACTIVITIES, {})
    pending = sorted((r for r in db.values() if r["pet"] is None),
                     key=lambda r: r["date"], reverse=True)
    if not pending:
        print("Nothing to review.")
        return
    print(f"{len(pending)} to classify (Enter to skip):")
    for rec in pending:
        rec["dogs"] = _ask_dogs(rec, cfg, _away_hint(rec, cfg))
        rec["pet"] = derive_pet(rec["dogs"])
        save(ACTIVITIES, db)
    cmd_build(args)


def cmd_mark(args):
    cfg = config()
    db = load(ACTIVITIES, {})
    if args.id not in db:
        sys.exit(f"No activity {args.id} in the log.")
    rec = db[args.id]
    val = args.value == "yes"
    dogs = dict(rec.get("dogs") or {d: None for d in dog_names(cfg)})
    if args.dog:
        dogs[args.dog] = val
    else:
        dogs = {d: val for d in dog_names(cfg)}
    rec["dogs"] = dogs
    rec["pet"] = derive_pet(dogs)
    save(ACTIVITIES, db)
    who = args.dog or "both dogs"
    print(f"{rec['name']!r} · {who} → {args.value}")
    cmd_build(args)


# ---------------------------------------------------------------- logs

def _date_arg(args):
    return args.date or datetime.now(tz()).date().isoformat()


def log_weight(lb, d, dog=None, note=None):
    """One weigh-in per dog per day; entries without a 'dog' field are the
    primary dog's."""
    cfg = config()
    dog = dog or cfg["dog"]
    log = load(WEIGHT, [])
    log = [e for e in log if not (e["date"] == d and e.get("dog", cfg["dog"]) == dog)]
    entry = {"date": d, "lb": lb}
    if dog != cfg["dog"]:
        entry["dog"] = dog
    if note:
        entry["note"] = note
    log.append(entry)
    log.sort(key=lambda e: e["date"])
    save(WEIGHT, log)
    return dog


def cmd_weight(args):
    dog = log_weight(args.lb, _date_arg(args), args.dog, getattr(args, "note", None))
    print(f"Logged {dog} at {args.lb} lb on {_date_arg(args)}.")
    cmd_build(args)


def log_note(text, d, dog=None):
    """Notes without a 'dog' field are the primary dog's."""
    cfg = config()
    log = load(NOTES, [])
    entry = {"date": d, "text": text}
    if dog and dog != cfg["dog"]:
        entry["dog"] = dog
    log.append(entry)
    log.sort(key=lambda e: e["date"])
    save(NOTES, log)
    return dog or cfg["dog"]


def cmd_note(args):
    dog = log_note(args.text, _date_arg(args), args.dog)
    print(f"Noted for {dog} on {_date_arg(args)}: {args.text}")
    cmd_build(args)


# ---------------------------------------------------------------- circle

def _walkish(t):
    return t.replace(" ", "").lower() in ("hike", "walk", "snowshoe")


def _walk_days(db, dog):
    return {r["date"][:10] for r in db.values()
            if (r.get("dogs") or {}).get(dog) and _walkish(r["type"])}


def _away_days(cfg):
    days = set()
    for a, b in cfg["circle"]["away"]:
        d, end = date.fromisoformat(a), date.fromisoformat(b)
        while d <= end:
            days.add(d.isoformat())
            d += timedelta(days=1)
    return days


def _circle_answer(answers, k, dog):
    """That dog's answer for day k, or None if unanswered."""
    return (answers.get(k) or {}).get(dog)


def circle_days(cfg, db, dog):
    """Days the baseline 'circle' walk counts for one dog. Before plan start
    it is assumed on every in-town day without a logged hike (which subsumes
    it); from plan start it counts exactly when explicitly answered yes for
    that dog — independent of any logged hike, since the circle is separate."""
    away, walked = _away_days(cfg), _walk_days(db, dog)
    answers = load(CIRCLES, {})
    out, d = [], date.fromisoformat(cfg["circle"]["since"])
    today = datetime.now(tz()).date()
    while d <= today:
        k = d.isoformat()
        a = _circle_answer(answers, k, dog)
        if (a if k >= cfg["plan_start"]
                else k not in walked and k not in away and (a if a is not None else True)):
            out.append(k)
        d += timedelta(days=1)
    return out


def unanswered_circle_days(cfg, db, dog):
    """Days since plan start (today included) with no circle answer yet for
    that dog. Today can be skipped and will be asked again."""
    answers = load(CIRCLES, {})
    out, d = [], date.fromisoformat(cfg["plan_start"])
    today = datetime.now(tz()).date()
    while d <= today:
        k = d.isoformat()
        if _circle_answer(answers, k, dog) is None:
            out.append(k)
        d += timedelta(days=1)
    return out


def _ask_circles(cfg, db):
    today = datetime.now(tz()).date().isoformat()
    for dog in dog_names(cfg):
        pending = unanswered_circle_days(cfg, db, dog)
        if not pending:
            continue
        answers = load(CIRCLES, {})
        print(f"{len(pending)} day(s) without a circle answer for {dog}:")
        for k in pending:
            nice = date.fromisoformat(k).strftime("%a %b %-d")
            if k == today:
                nice += " (today — skip if not yet)"
            ans = _ask_yn(f"  {nice} ({k}) — {dog} circle? [y/n/s(kip)] ")
            if ans is not None:
                answers.setdefault(k, {})[dog] = ans
                save(CIRCLES, answers)


def cmd_circle(args):
    cfg = config()
    answers = load(CIRCLES, {})
    k = _date_arg(args)
    entry = answers.setdefault(k, {})
    val = args.value == "yes"
    if args.dog:
        entry[args.dog] = val
    else:
        for d in dog_names(cfg):
            entry[d] = val
    save(CIRCLES, answers)
    print(f"Circle on {k} · {args.dog or 'both dogs'}: {args.value}")
    cmd_build(args)


# ---------------------------------------------------------------- meals

def log_meal(dog, d, breakfast=None, dinner=None, other=None):
    """Record a dog's meals for a day. breakfast/dinner: True (ate), False
    (skipped), or None (leave unchanged). other: description of any different/
    new food (empty string clears it, None leaves unchanged)."""
    store = load(MEALS, {})
    day = store.setdefault(dog, {}).setdefault(d, {})
    if breakfast is not None:
        day["breakfast"] = breakfast
    if dinner is not None:
        day["dinner"] = dinner
    if other is not None:
        o = other.strip()
        if o:
            day["other"] = o
        else:
            day.pop("other", None)
    save(MEALS, store)
    return day


def cmd_meal(args):
    dog = args.dog or config()["dog"]
    yn = {"yes": True, "no": False}
    day = log_meal(dog, _date_arg(args), yn.get(args.breakfast),
                   yn.get(args.dinner), args.other)
    ate = sum(1 for m in ("breakfast", "dinner") if day.get(m))
    print(f"{dog} on {_date_arg(args)}: {ate} meal(s) eaten"
          + (f" · other: {day['other']}" if day.get("other") else ""))
    cmd_build(args)


def cmd_gi(args):
    dog = args.dog or config()["dog"]
    log_gi(dog, _date_arg(args), args.severity, args.blood == "yes", args.note)
    print(f"{dog} on {_date_arg(args)}: {args.severity} diarrhea"
          + (", blood" if args.blood == "yes" else ""))
    cmd_build(args)


# ---------------------------------------------------------------- build

def _norm_sev(s):
    s = (s or "").lower()
    for k in ("severe", "moderate", "mild", "chronic"):
        if k in s:
            return k
    return "moderate"


def _blood_present(v):
    return str(v or "").lower() in ("present", "intermittent", "yes", "true") \
        or "blood" in str(v or "").lower()


def _gi_history(path):
    """Parse a dog's GI-history file into (day_map, table_rows).
    day_map: {date: {lvl, sev, blood}}  (lvl: acute|chronic|parasite)
    table_rows: verbatim raw-log entries {date, source, text}."""
    import re
    raw = json.loads(path.read_text())

    def span(onset, resolved, dur):
        o = date.fromisoformat(onset)
        end = None
        if resolved:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", str(resolved))
            if m:
                end = date.fromisoformat(m.group(1))
        if end is None and dur:
            end = o + timedelta(days=int(dur) - 1)
        end = end or o
        out, d = [], o
        while d <= end:
            out.append(d.isoformat())
            d += timedelta(days=1)
        return out

    days = {}
    for want in ("chronic_phase", "episode"):   # chronic first so acute wins
        for e in raw.get("episodes", []):
            t = e.get("type")
            if t == "parasite_finding" or not e.get("date_onset"):
                continue
            lvl = "chronic" if t == "chronic_phase" else "acute"
            if (want == "chronic_phase") != (lvl == "chronic"):
                continue
            # blood flagged only for discrete episodes; the months-long chronic
            # phase had it intermittently, not every day, so don't smear it
            rec = {"lvl": lvl, "sev": _norm_sev(e.get("severity")),
                   "blood": lvl == "acute" and _blood_present(e.get("blood"))}
            for k in span(e["date_onset"], e.get("date_resolved"), e.get("duration_days")):
                days[k] = rec
    for e in raw.get("episodes", []):
        if e.get("type") == "parasite_finding" and e.get("date"):
            days[e["date"]] = {"lvl": "parasite", "note": e.get("finding")}

    table = [{"date": x.get("date"), "source": x.get("source"), "text": x.get("text")}
             for x in raw.get("raw_source_log", {}).get("entries", []) if x.get("text")]
    return days, table


def log_gi(dog, d, severity, blood, note=None):
    """Record a diarrhea incident (one per dog per day; same date replaces)."""
    store = load(GI_LOG, {})
    lst = [x for x in store.get(dog, []) if x["date"] != d]
    entry = {"date": d, "severity": _norm_sev(severity), "blood": bool(blood)}
    if note:
        entry["note"] = note
    lst.append(entry)
    lst.sort(key=lambda x: x["date"])
    store[dog] = lst
    save(GI_LOG, store)
    return entry


# curated fields to surface per vet encounter (label, [synonym keys in priority])
_VET_FIELDS = [
    ("Complaints", ["presenting_complaints"]),
    ("Onset", ["symptom_onset", "symptom_onset_dates"]),
    ("Findings", ["findings"]),
    ("History", ["history_verbatim"]),
    ("Assessment", ["assessment_as_transcribed", "assessment", "assessments"]),
    ("Diagnostics", ["diagnostics"]),
    ("Treatment", ["treatment", "medications"]),
    ("Procedure", ["procedure", "procedures", "extractions"]),
    ("Plan", ["plan_verbatim", "plan", "plan_summary"]),
    ("Outcome", ["outcome"]),
    ("Note", ["significance"]),
]


def _vet_val(v):
    if isinstance(v, list):
        return "; ".join(str(x) for x in v)
    if isinstance(v, dict):
        return "; ".join(f"{k}: {val}" for k, val in v.items())
    return str(v)


def load_vet(cfg):
    """Per-dog vet encounter timeline from data/<dog>_vet_records.json."""
    out = {}
    for d in dog_names(cfg):
        p = DATA / f"{d.lower()}_vet_records.json"
        if not p.exists():
            continue
        try:
            raw = json.loads(p.read_text())
        except Exception:
            continue
        items = []
        for e in raw.get("encounters", []):
            lines = []
            for label, keys in _VET_FIELDS:
                for k in keys:
                    if e.get(k):
                        lines.append({"label": label, "text": _vet_val(e[k])})
                        break
            items.append({
                "date": e.get("date"),
                "provider": e.get("provider"),
                "type": e.get("type"),
                "weight": e.get("weight_lb"),
                "lines": lines,
            })
        items.sort(key=lambda x: (x["date"] or ""), reverse=True)
        if items:
            out[d] = items
    return out


def load_gi(cfg):
    """Merge each dog's compiled history with any incidents logged here."""
    days, table = {}, {}
    store = load(GI_LOG, {})
    for d in dog_names(cfg):
        p = DATA / f"{d.lower()}_gi_history.json"
        dm, tb = {}, []
        if p.exists():
            try:
                dm, tb = _gi_history(p)
            except Exception:
                pass
        for inc in store.get(d, []):
            dm[inc["date"]] = {"lvl": "acute", "sev": _norm_sev(inc.get("severity")),
                               "blood": bool(inc.get("blood"))}
            txt = f"{inc.get('severity', '?')} diarrhea · blood: {'yes' if inc.get('blood') else 'no'}"
            if inc.get("note"):
                txt += f" — {inc['note']}"
            tb.append({"date": inc["date"], "source": "logged", "text": txt})
        if dm:
            days[d] = dm
        if tb:
            tb.sort(key=lambda x: (x["date"] or ""), reverse=True)
            table[d] = tb
    return {"days": days, "table": table}


def build_payload(cfg, db):
    keep = [r for r in db.values() if r["pet"] is not False]
    keep.sort(key=lambda r: r["date"])
    dogs = dog_names(cfg)
    has_circle = "circle" in cfg
    by_dog = {d: (circle_days(cfg, db, d) if has_circle else []) for d in dogs}
    unans = {d: (unanswered_circle_days(cfg, db, d) if has_circle else []) for d in dogs}
    primary = cfg["dog"]
    return {
        "config": cfg,
        "activities": keep,
        "unclassified": sorted((r for r in db.values() if r["pet"] is None),
                               key=lambda r: r["date"], reverse=True),
        # primary dog's lists kept for the activity charts; per-dog maps too
        "circles": by_dog.get(primary, []),
        "circles_unanswered": unans.get(primary, []),
        "circlesByDog": by_dog,
        "circles_unansweredByDog": unans,
        "weight": load(WEIGHT, []),
        "notes": load(NOTES, []),
        "meals": load(MEALS, {}),
        "gi": load_gi(cfg),
        "vet": load_vet(cfg),
        "generated": datetime.now(tz()).isoformat(timespec="minutes"),
    }


def render_html(live=False):
    import base64
    cfg = config()
    db = load(ACTIVITIES, {})
    payload = build_payload(cfg, db)
    payload["live"] = live
    # one header photo per dog, from assets/<dog>.{jpg,jpeg,png} (lowercased)
    photos = {}
    for d in dog_names(cfg):
        for ext, mime in (("jpg", "jpeg"), ("jpeg", "jpeg"), ("png", "png")):
            f = BASE / "assets" / f"{d.lower()}.{ext}"
            if f.exists():
                photos[d] = f"data:image/{mime};base64," + base64.b64encode(f.read_bytes()).decode()
                break
    payload["photos"] = photos
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text().replace("/*__DATA__*/null", blob)


def cmd_build(args):
    DASHBOARD.write_text(render_html(live=False))
    print(f"Dashboard → {DASHBOARD}")


# ---------------------------------------------------------------- serve

def cmd_serve(args):
    from http.server import ThreadingHTTPServer

    cfg = config()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, render_html(live=True), "text/html; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or "{}")
            except json.JSONDecodeError:
                return self._send(400, json.dumps({"error": "bad json"}))
            db = load(ACTIVITIES, {})
            try:
                if self.path == "/api/sync":
                    added = []
                    n_all, n_new = fetch_new_activities(
                        cfg, db, on_each=lambda rec, hint: added.append(rec))
                    return self._send(200, json.dumps(
                        {"checked": n_all, "new": n_new, "activities": added,
                         "circlesByDog": {d: unanswered_circle_days(cfg, db, d)
                                          for d in dog_names(cfg)}}))
                if self.path == "/api/classify":
                    aid = str(body["id"])
                    if aid not in db:
                        return self._send(404, json.dumps({"error": "unknown id"}))
                    rec = db[aid]
                    dogs = dict(rec.get("dogs") or {d: None for d in dog_names(cfg)})
                    if isinstance(body.get("dogs"), dict):
                        for dg, v in body["dogs"].items():
                            dogs[dg] = bool(v)
                    elif body.get("dog"):
                        dogs[body["dog"]] = bool(body["value"])
                    else:
                        dogs = {d: bool(body["value"]) for d in dog_names(cfg)}
                    rec["dogs"] = dogs
                    rec["pet"] = derive_pet(dogs)
                    save(ACTIVITIES, db)
                    return self._send(200, json.dumps({"ok": True}))
                if self.path == "/api/circle":
                    ans = load(CIRCLES, {})
                    entry = ans.setdefault(body["date"], {})
                    if isinstance(body.get("dogs"), dict):
                        for dg, v in body["dogs"].items():
                            entry[dg] = bool(v)
                    elif body.get("dog"):
                        entry[body["dog"]] = bool(body["value"])
                    else:
                        for d in dog_names(cfg):
                            entry[d] = bool(body["value"])
                    save(CIRCLES, ans)
                    return self._send(200, json.dumps({"ok": True}))
                if self.path == "/api/weight":
                    d = body.get("date") or datetime.now(tz()).date().isoformat()
                    log_weight(float(body["lb"]), d, body.get("dog"))
                    return self._send(200, json.dumps({"ok": True}))
                if self.path == "/api/note":
                    d = body.get("date") or datetime.now(tz()).date().isoformat()
                    log_note(body["text"], d, body.get("dog"))
                    return self._send(200, json.dumps({"ok": True}))
                if self.path == "/api/meal":
                    d = body.get("date") or datetime.now(tz()).date().isoformat()
                    log_meal(body["dog"], d, body.get("breakfast"),
                             body.get("dinner"), body.get("other"))
                    return self._send(200, json.dumps({"ok": True}))
                if self.path == "/api/gi":
                    d = body.get("date") or datetime.now(tz()).date().isoformat()
                    log_gi(body["dog"], d, body.get("severity"),
                           body.get("blood"), body.get("note"))
                    return self._send(200, json.dumps({"ok": True}))
            except urllib.error.HTTPError as e:
                return self._send(502, json.dumps({"error": f"Strava API: {e.code}"}))
            except (KeyError, ValueError) as e:
                return self._send(400, json.dumps({"error": str(e)}))
            self._send(404, json.dumps({"error": "no such endpoint"}))

        def log_message(self, *_):
            pass

    port, host = args.port, args.host
    server = ThreadingHTTPServer((host, port), Handler)
    local_url = f"http://localhost:{port}/"
    print(f"Nala dashboard live at {local_url}")
    if host not in ("localhost", "127.0.0.1"):
        # bound to all interfaces — show the addresses other devices can use.
        # (No auth on the write endpoints, so keep this to trusted networks.)
        import socket
        names = [f"{socket.gethostname().split('.')[0]}.local"]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            names.append(s.getsockname()[0])
            s.close()
        except OSError:
            pass
        print("On this network, other devices can reach it at:")
        for n in names:
            print(f"  http://{n}:{port}/")
        print("(anyone on the network can log/sync — trusted networks only)")
    print("Press Ctrl+C to stop.")
    webbrowser.open(local_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="import a Strava bulk-export activities.csv")
    s.add_argument("csv_path")
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("auth", help="authorize with the Strava API")
    s.set_defaults(func=cmd_auth)

    s = sub.add_parser("sync", help="pull new activities from Strava")
    s.add_argument("--batch", action="store_true", help="never prompt; leave unknowns for review")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("review", help="classify pending activities")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("serve", help="run the interactive dashboard (buttons for sync/classify)")
    s.add_argument("--port", type=int, default=8724)
    s.add_argument("--host", default="0.0.0.0",
                   help="bind address; 0.0.0.0 = reachable on your LAN (default), "
                        "localhost = this machine only")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("mark", help="classify one activity by ID")
    s.add_argument("id")
    s.add_argument("value", choices=["yes", "no"])
    s.add_argument("--dog", help="one dog (default: set all dogs)")
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("circle", help="record whether the baseline circle walk happened")
    s.add_argument("value", choices=["yes", "no"])
    s.add_argument("--dog", help="one dog (default: set all dogs)")
    s.add_argument("--date", help="YYYY-MM-DD (default today)")
    s.set_defaults(func=cmd_circle)

    s = sub.add_parser("meal", help="log a dog's meals for a day")
    s.add_argument("--dog", help="which dog (default the primary one)")
    s.add_argument("--breakfast", choices=["yes", "no"], help="ate breakfast?")
    s.add_argument("--dinner", choices=["yes", "no"], help="ate dinner?")
    s.add_argument("--other", help="any different/new food eaten (blank clears)")
    s.add_argument("--date", help="YYYY-MM-DD (default today)")
    s.set_defaults(func=cmd_meal)

    s = sub.add_parser("gi", help="log a diarrhea incident")
    s.add_argument("--dog", help="which dog (default the primary one)")
    s.add_argument("--severity", choices=["mild", "moderate", "severe"], default="moderate")
    s.add_argument("--blood", choices=["yes", "no"], default="no")
    s.add_argument("--note", help="free text")
    s.add_argument("--date", help="YYYY-MM-DD (default today)")
    s.set_defaults(func=cmd_gi)

    s = sub.add_parser("weight", help="log a weigh-in (lb)")
    s.add_argument("lb", type=float)
    s.add_argument("--dog", help="which dog (default the primary one)")
    s.add_argument("--date", help="YYYY-MM-DD (default today)")
    s.add_argument("--note", help="e.g. 'vet visit' or 'approximate'")
    s.set_defaults(func=cmd_weight)

    s = sub.add_parser("note", help="log a joint-sign / vet note")
    s.add_argument("text")
    s.add_argument("--dog", help="which dog (default the primary one)")
    s.add_argument("--date", help="YYYY-MM-DD (default today)")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("build", help="regenerate dashboard.html")
    s.set_defaults(func=cmd_build)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
