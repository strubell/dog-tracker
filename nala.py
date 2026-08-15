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
MED_LOG = DATA / "med_log.json"
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
        nc = len(set().union(*(set(unanswered_circle_days(cfg, db, d)) for d in dog_names(cfg))))
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
    """That dog's answer for day k, or None if unanswered.
    Stored as a bool (single circle) or an int count of circles."""
    return (answers.get(k) or {}).get(dog)


def _circle_count(a):
    """Number of circles an answer represents: None/False -> 0, True -> 1, int -> itself."""
    if a is None or a is False:
        return 0
    if a is True:
        return 1
    return int(a)


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
        if k >= cfg["plan_start"]:
            n = _circle_count(a)                       # 0 if unanswered
        elif k not in walked and k not in away:
            n = _circle_count(a) if a is not None else 1   # pre-plan default: 1
        else:
            n = 0
        out.extend([k] * n)                            # one entry per circle that day
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
    # count of circles: "no" -> 0; "yes" -> --count (default 1). Store 1/0 as
    # bool for tidiness (matches existing data), 2+ as an int.
    n = 0 if args.value == "no" else (args.count if args.count is not None else 1)
    val = True if n == 1 else (False if n == 0 else n)
    for d in ([args.dog] if args.dog else dog_names(cfg)):
        entry[d] = val
    save(CIRCLES, answers)
    print(f"Circle on {k} · {args.dog or 'both dogs'}: {n} circle(s)")
    cmd_build(args)


# ---------------------------------------------------------------- meals

def log_meal(dog, d, items=None, other=None):
    """Record a dog's daily-log items for a day. items: {key: True (ate/given),
    False (skipped), or None (leave unchanged)} — keys are per-dog daily_log
    items (e.g. breakfast/dinner for Pepper, dental for Nala). other: any
    different/new food (empty string clears it, None leaves unchanged)."""
    store = load(MEALS, {})
    day = store.setdefault(dog, {}).setdefault(d, {})
    for k, v in (items or {}).items():
        if v is not None:
            day[k] = bool(v)
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
    items = {}
    if args.breakfast:
        items["breakfast"] = yn[args.breakfast]
    if args.dinner:
        items["dinner"] = yn[args.dinner]
    if args.ate:
        items[args.ate] = True         # any daily_log item (e.g. dental)
    if args.skip:
        items[args.skip] = False
    day = log_meal(dog, _date_arg(args), items, args.other)
    logged = ", ".join(f"{k}={'y' if v else 'n'}" for k, v in items.items())
    print(f"{dog} on {_date_arg(args)}: {logged or 'no change'}"
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
            solid = span(e["date_onset"], e.get("date_resolved"), e.get("duration_days"))
            for k in solid:
                days[k] = rec
            # an uncertain, fading tail: known-abnormal ends at `solid`, but the phase
            # resolved somewhere before fade_until (exact date unknown) — ramp opacity
            # from ~1 down to ~0 across it so the chronic colour fades toward normal
            fu = e.get("fade_until")
            if lvl == "chronic" and fu and solid:
                start = date.fromisoformat(solid[-1]) + timedelta(days=1)
                stop = date.fromisoformat(fu)
                tail = []
                dd = start
                while dd <= stop:
                    tail.append(dd); dd += timedelta(days=1)
                n = len(tail)
                for i, dd in enumerate(tail, 1):
                    days[dd.isoformat()] = {**rec, "fade": round(1 - i / (n + 1), 3)}
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


# curated fields to surface per vet encounter (label, [synonym keys in priority];
# first present key wins). Pure billing/metadata keys are intentionally omitted.
_VET_FIELDS = [
    ("Complaints", ["presenting_complaints", "presenting_complaint"]),
    ("Onset", ["symptom_onset", "symptom_onset_dates"]),
    ("Exam", ["physical_exam"]),
    ("Findings", ["findings"]),
    ("History", ["history_verbatim"]),
    ("Assessment", ["working_diagnosis_verbatim", "assessment_as_transcribed",
                    "assessment", "assessments", "diagnosis_status"]),
    ("Diagnostics", ["diagnostics_performed", "diagnostics"]),
    ("Procedure", ["procedure", "procedures", "extractions", "extraction_detail", "procedure_note"]),
    ("Also noted", ["concurrent_findings"]),
    ("Treatment", ["treatment", "treatment_as_transcribed", "medications",
                   "medications_dispensed", "take_home_medication"]),
    ("Plan", ["plan_verbatim", "plan_verbatim_key_points", "plan", "plan_summary",
              "clinic_guidance_summary"]),
    ("Follow-up", ["follow_up_instruction_verbatim", "monitoring_instructions",
                   "discharge_instructions", "follow_up_due"]),
    ("Outcome", ["outcome"]),
    ("Cost", ["cost_total_usd"]),
    ("Note", ["significance", "note"]),
]


def _vet_val(v):
    if isinstance(v, dict):
        return ", ".join(f"{k}: {_vet_val(val)}" for k, val in v.items())
    if isinstance(v, list):
        return "; ".join(_vet_val(x) for x in v)
    return str(v)


def _corr_label(k):
    """Turn an extracted_facts key like 'gi_status_2024_08_20' into 'Gi status'."""
    import re
    k = re.sub(r"_\d{4}(_\d{2}){0,2}$", "", str(k))     # drop a trailing date suffix
    return k.replace("_", " ").strip().capitalize()


def _corr_entry(m):
    """Shape a correspondence message for the dashboard: date, who, channel, and
    the extracted facts (or a free-text note) as label/text lines."""
    facts = []
    ef = m.get("extracted_facts")
    if isinstance(ef, dict):
        for k, v in ef.items():
            facts.append({"label": _corr_label(k), "text": _vet_val(v)})
    elif ef:
        facts.append({"label": "", "text": _vet_val(ef)})
    if not facts:                       # free-text follow-up note
        for k in ("content_summary", "text", "note", "summary"):
            if m.get(k):
                facts.append({"label": "", "text": _vet_val(m[k])}); break
    chan = m.get("channel")             # phone | email | note
    who = chan or (" → ".join(x for x in (m.get("from"), m.get("to")) if x) or None)
    return {"date": m.get("date"), "who": who, "channel": chan,
            "purpose": m.get("purpose"), "facts": facts}


_VAX_GROUPS = [
    ("Rabies", ["rabies"]),
    ("DHPP", ["dhpp", "distemper", "parvovirus"]),
    ("Lepto", ["lepto"]),
    ("Bordetella", ["bordetella"]),
    ("Lyme", ["lyme"]),
    ("Influenza", ["influenza"]),
]


_VAX_INTERVAL_YEARS = {"Rabies": 3}   # others default to 1


def _add_years(iso, n):
    parts = iso.split("-")
    parts[0] = f"{int(parts[0]) + n:04d}"
    return "-".join(parts)


_VAX_SHORT = {"Distemper": "DHPP", "Leptospirosis": "Lepto", "Parainfluenza": "PI"}


def load_vax(cfg):
    """Per-dog vaccination summary. Prefers the authoritative vaccines_status
    block ('Name (due YYYY-MM-DD)' entries); expired = past due date. Falls back
    to latest-per-group from vaccination_history with computed due dates."""
    import re
    today_str = datetime.now(tz()).date().isoformat()
    out = {}
    for d in dog_names(cfg):
        p = DATA / f"{d.lower()}_vet_records.json"
        if not p.exists():
            continue
        try:
            raw = json.loads(p.read_text())
        except Exception:
            continue
        current = ((raw.get("vaccines_status") or {}).get("current")) or []
        items = []
        if current:
            for entry in current:
                m = re.match(r"(.+?)\s*\(due\s*([0-9-]+)\)", str(entry))
                name = (m.group(1) if m else str(entry)).strip()
                due = (m.group(2) if m else "").strip()
                items.append({"name": _VAX_SHORT.get(name, name), "due": due,
                              "expired": bool(due) and due < today_str})
        else:
            hist = raw.get("vaccination_history", [])
            latest = {}
            for e in hist:
                mm = re.match(r"(\d{4}(?:-\d{2}){0,2})", str(e.get("date", "")))
                date = mm.group(1) if mm else ""
                names = " ".join(str(x) for x in (e.get("vaccines") or [e.get("vaccine")]) if x).lower()
                for label, keys in _VAX_GROUPS:
                    if any(k in names for k in keys) and date > latest.get(label, ""):
                        latest[label] = date
            for label, _ in _VAX_GROUPS:
                if label in latest:
                    due = _add_years(latest[label], _VAX_INTERVAL_YEARS.get(label, 1))
                    items.append({"name": label, "due": due, "expired": due < today_str})
        if items:
            out[d] = items
    return out


def _med_courses(path):
    """Past medication courses from a vet-records file, as {name,dose,reason,start,end}."""
    import re
    raw = json.loads(path.read_text())

    def d(x):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(x or ""))
        return m.group(1) if m else None

    out = []
    for m in raw.get("medications", []):
        rng = str(m.get("date_range") or "")
        start = (d(m.get("course_start")) or d(m.get("date_actually_started"))
                 or d(m.get("date_prescribed")) or d(m.get("date")))
        if not start and " to " in rng:
            start = d(rng.split(" to ")[0])
        if not start:
            continue
        end = d(m.get("course_end")) or d(m.get("estimated_completion"))
        if not end and " to " in rng:
            end = d(rng.split(" to ")[1])
        if not end and m.get("duration_days"):
            try:
                end = (date.fromisoformat(start) + timedelta(days=int(m["duration_days"]) - 1)).isoformat()
            except Exception:
                pass
        out.append({"name": m.get("drug", ""), "dose": m.get("strength") or "",
                    "reason": m.get("indication") or "", "start": start, "end": end or start})
    return out


def log_med(dog, d, skip=None, add=None):
    """Set a day's medication log: skip = daily-med names not given; add = ad-hoc meds."""
    store = load(MED_LOG, {})
    day = store.setdefault(dog, {}).setdefault(d, {})
    if skip is not None:
        day["skip"] = skip
    if add is not None:
        day["add"] = add
    if not day.get("skip") and not day.get("add"):
        store[dog].pop(d, None)
    save(MED_LOG, store)
    return day


def _add_months(iso, n):
    """Add n calendar months to a YYYY-MM-DD date, clamping the day to month end."""
    import calendar
    y, m, d = (int(x) for x in iso.split("-"))
    idx = (m - 1) + n
    y += idx // 12
    m = idx % 12 + 1
    d = min(d, calendar.monthrange(y, m)[1])
    return f"{y:04d}-{m:02d}-{d:02d}"


def _recurring_status(prof, log):
    """For each recurring med, find the last administration (from the med log,
    else the configured seed) and project the next due date by its cadence."""
    out = []
    for rm in prof.get("recurring_medications", []):
        match = (rm.get("match") or rm.get("name", "")).lower()
        given = [dt for dt, rec in log.items()
                 for a in (rec.get("add") or []) if match in (a.get("name", "") or "").lower()]
        if rm.get("last_given"):
            given.append(rm["last_given"])
        last = max(given) if given else None
        nxt = _add_months(last, rm.get("cadence_months", 1)) if last else None
        out.append({"name": rm.get("name"), "last_given": last,
                    "next_due": nxt, "cadence_months": rm.get("cadence_months", 1)})
    return out


def load_meds(cfg):
    """Per-dog meds: standing daily meds (config), past courses (vet history),
    recurring meds with a projected next-due date, and the ad-hoc/skip log."""
    store = load(MED_LOG, {})
    out = {}
    for dg in dog_names(cfg):
        prof = (cfg.get("profiles") or {}).get(dg, {})
        courses = []
        p = DATA / f"{dg.lower()}_vet_records.json"
        if p.exists():
            try:
                courses = _med_courses(p)
            except Exception:
                pass
        log = store.get(dg, {})
        out[dg] = {"daily": prof.get("daily_medications", []),
                   "courses": courses, "log": log,
                   "recurring": _recurring_status(prof, log)}
    return out


def cmd_med(args):
    dog = args.dog or config()["dog"]
    d = _date_arg(args)
    store = load(MED_LOG, {})
    day = store.setdefault(dog, {}).setdefault(d, {})
    if args.skip:
        day.setdefault("skip", [])
        if args.skip not in day["skip"]:
            day["skip"].append(args.skip)
    if args.add:
        day.setdefault("add", []).append(
            {"name": args.add, "dose": args.dose or "",
             **({"note": args.note} if args.note else {})})
    save(MED_LOG, store)
    print(f"{dog} {d}: " + ", ".join(filter(None, [
        f"skipped {args.skip}" if args.skip else "",
        f"added {args.add}" if args.add else ""])))
    cmd_build(args)


def _resolve_record_pdf(filename, actual):
    """Match a (possibly mangled) source filename to a real file in vet_records/.
    Returns the actual basename, or None. PDFs and record images both count."""
    import os, re
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    key = norm(filename)
    for f in actual:                       # exact (normalized) match
        if norm(f) == key:
            return f
    m = re.search(r"cn\d+", key)           # by clinical-note number token
    if m:
        for f in actual:
            if m.group(0) in norm(f):
                return f
    for f in actual:                       # filename embedded in a longer ref
        nf = norm(f)                        # e.g. "Invoices.pdf invoice 597888"
        if len(nf) >= 6 and key.startswith(nf):
            return f
    best, blen = None, 0                   # longest shared prefix (>=12)
    for f in actual:
        c = len(os.path.commonprefix([norm(f), key]))
        if c > blen and c >= 12:
            best, blen = f, c
    return best


def add_vet_document(dog, filename, raw, date=None, provider=None,
                     doc_type=None, note=None):
    """Save an uploaded vet document into vet_records/ and append a matching
    encounter (linked to it) in data/<dog>_vet_records.json. Returns the stored
    filename. Raises ValueError for an unsupported file type."""
    import re
    recdir = BASE / "vet_records"
    recdir.mkdir(exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(filename or "document"))
    ext = ext.lower()
    if ext not in (".pdf", ".jpg", ".jpeg", ".png"):
        raise ValueError("unsupported file type (use PDF, JPG, or PNG)")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "document"
    name, dest, i = stem + ext, recdir / (stem + ext), 1
    while dest.exists():                       # never overwrite an existing record
        name = f"{stem}-{i}{ext}"; dest = recdir / name; i += 1
    dest.write_bytes(raw)
    p = DATA / f"{dog.lower()}_vet_records.json"
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except Exception:
            data = {}
    d = date or datetime.now(tz()).date().isoformat()
    enc = {"encounter_id": f"{dog.lower()}_upload_{d}_{stem}"[:90],
           "date": d, "provider": provider or None,
           "type": doc_type or "uploaded document",
           "source": [name], "added_via": "dashboard upload"}
    if note:
        enc["findings"] = note
    data.setdefault("encounters", []).append(enc)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return name


def load_vet(cfg):
    """Per-dog vet encounter timeline from data/<dog>_vet_records.json, with each
    encounter linked to its source document in vet_records/ where resolvable."""
    recdir = BASE / "vet_records"
    actual = [f.name for f in recdir.iterdir()] if recdir.is_dir() else []
    out = {}
    for d in dog_names(cfg):
        p = DATA / f"{d.lower()}_vet_records.json"
        if not p.exists():
            continue
        try:
            raw = json.loads(p.read_text())
        except Exception:
            continue
        # doc_id -> served path, resolved against the real files on disk
        docmap = {}
        for sd in raw.get("source_documents", []):
            fn = sd.get("filename", "")
            if fn.endswith(".json"):        # internal (owner tracker), not a document
                continue
            match = _resolve_record_pdf(fn, actual)
            if match:
                docmap[sd.get("doc_id")] = "vet_records/" + match
        # correspondence (emails / follow-up messages): index by id, and by the
        # encounter each one relates to, so it can be shown under that visit
        corr_by_id, corr_by_enc = {}, {}
        for m in raw.get("correspondence", {}).get("messages", []):
            corr_by_id[m.get("msg_id")] = m
            for eid in (m.get("relates_to_encounter") or []):
                corr_by_enc.setdefault(eid, []).append(m)
        items = []
        for e in raw.get("encounters", []):
            lines = []
            for label, keys in _VET_FIELDS:
                for k in keys:
                    if e.get(k):
                        text = _vet_val(e[k])
                        if label == "Cost":
                            text = "$" + text
                        lines.append({"label": label, "text": text})
                        break
            src = e.get("source")
            ids = src if isinstance(src, list) else [src]
            paths = []
            for i in ids:
                if not i:
                    continue
                pth = docmap.get(i)         # doc_id, else treat as a raw filename
                if not pth:
                    match = _resolve_record_pdf(i, actual)
                    if match:
                        pth = "vet_records/" + match
                if pth and pth not in paths:
                    paths.append(pth)
            # collect this encounter's correspondence (from either direction of link)
            cmsgs, seen = [], set()
            for ref in (e.get("follow_up_correspondence") or []):
                m = corr_by_id.get(ref.get("msg_id"))
                if m and id(m) not in seen:
                    cmsgs.append(m); seen.add(id(m))
            for m in corr_by_enc.get(e.get("encounter_id"), []):
                if id(m) not in seen:
                    cmsgs.append(m); seen.add(id(m))
            items.append({
                "id": e.get("encounter_id"),
                "date": e.get("date"),
                "provider": e.get("provider"),
                "type": e.get("type"),
                "weight": e.get("weight_lb"),
                "lines": lines,
                "sources": paths,
                "correspondence": [_corr_entry(m) for m in
                                   sorted(cmsgs, key=lambda x: str(x.get("date") or ""))],
            })
        items.sort(key=lambda x: (x["date"] or ""), reverse=True)
        if items:
            out[d] = items
    return out


def load_vet_open(cfg):
    """Per-dog 'open items': pending/missing results and open questions that live
    in the records file but aren't tied to a single encounter — normally invisible."""
    out = {}
    for d in dog_names(cfg):
        p = DATA / f"{d.lower()}_vet_records.json"
        if not p.exists():
            continue
        try:
            raw = json.loads(p.read_text())
        except Exception:
            continue
        pending = []
        for x in raw.get("pending_or_missing_results", []):
            if isinstance(x, dict):
                who = ", ".join(str(x[k]) for k in ("provider", "date") if x.get(k))
                pending.append({"item": x.get("item") or _vet_val(x),
                                "why": x.get("why"), "who": who or None,
                                "priority": x.get("priority", 99)})
            elif x:
                pending.append({"item": str(x), "priority": 99})
        pending.sort(key=lambda z: z.get("priority", 99))
        questions = [q if isinstance(q, str) else _vet_val(q)
                     for q in raw.get("open_questions", [])]
        if pending or questions:
            out[d] = {"pending": pending, "questions": questions}
    return out


def add_vet_followup(dog, encounter_id, text, date=None, channel="note"):
    """Append a free-text follow-up note (phone/email/note), linked to a visit, to
    the dog's correspondence so it renders under that visit. Returns the msg_id."""
    if not (text or "").strip():
        raise ValueError("empty follow-up note")
    p = DATA / f"{dog.lower()}_vet_records.json"
    data = json.loads(p.read_text()) if p.exists() else {}
    d = date or datetime.now(tz()).date().isoformat()
    msgs = data.setdefault("correspondence", {}).setdefault("messages", [])
    n = sum(1 for m in msgs if str(m.get("msg_id", "")).startswith(f"corr_{d}_note"))
    mid = f"corr_{d}_note{n + 1}"
    msgs.append({"msg_id": mid, "date": d,
                 "channel": channel if channel in ("phone", "email", "note") else "note",
                 "relates_to_encounter": [encounter_id] if encounter_id else [],
                 "text": text.strip(), "added_via": "dashboard follow-up"})
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return mid


def load_origins(cfg):
    """Per-dog adoption / origin story from data/origins.json (optional)."""
    p = DATA / "origins.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except Exception:
        return {}
    return {d: raw[d] for d in dog_names(cfg) if d in raw}


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
        "vet_open": load_vet_open(cfg),
        "origins": load_origins(cfg),
        "vax": load_vax(cfg),
        "meds": load_meds(cfg),
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
    # embed origin photos (gallery + labelled siblings) as base64 data URIs,
    # resolving each entry's filename against origins/<dog>/
    def _embed_origin(dog, fname):
        f = BASE / "origins" / dog.lower() / (fname or "")
        mime = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png"}.get(f.suffix.lower())
        if fname and mime and f.is_file():
            return f"data:image/{mime};base64," + base64.b64encode(f.read_bytes()).decode()
        return None
    for d, o in (payload.get("origins") or {}).items():
        entries = list(o.get("photos", [])) + list(o.get("siblings", []))
        for post in o.get("posts", []):        # posts may carry their own photos
            entries += list(post.get("photos", []))
        for entry in entries:
            src = _embed_origin(d, entry.get("file", ""))
            if src:
                entry["src"] = src
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
                return self._send(200, render_html(live=True), "text/html; charset=utf-8")
            # serve source records (read-only): PDFs anywhere under the project dir,
            # image records only from vet_records/ so private photos aren't exposed.
            path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
            _CT = {".pdf": "application/pdf", ".jpg": "image/jpeg",
                   ".jpeg": "image/jpeg", ".png": "image/png"}
            ext = os.path.splitext(path)[1].lower()
            if ext in _CT:
                target = (BASE / path.lstrip("/")).resolve()
                recdir = (BASE / "vet_records").resolve()
                ok_dir = ext == ".pdf" or str(target).startswith(str(recdir))
                if (str(target).startswith(str(BASE.resolve()))
                        and ok_dir and target.is_file()):
                    return self._send(200, target.read_bytes(), _CT[ext])
                return self._send(404, "not found", "text/plain")
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
                    # return the FULL unconfirmed backlog (not just this sync's new
                    # ones) so re-syncing before confirming doesn't drop earlier items
                    unclassified = sorted((r for r in db.values() if r["pet"] is None),
                                          key=lambda r: r["date"], reverse=True)
                    return self._send(200, json.dumps(
                        {"checked": n_all, "new": n_new, "activities": added,
                         "unclassified": unclassified,
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
                    items = {k: v for k, v in body.items()
                             if k not in ("dog", "date", "other")}
                    log_meal(body["dog"], d, items, body.get("other"))
                    return self._send(200, json.dumps({"ok": True}))
                if self.path == "/api/gi":
                    d = body.get("date") or datetime.now(tz()).date().isoformat()
                    log_gi(body["dog"], d, body.get("severity"),
                           body.get("blood"), body.get("note"))
                    return self._send(200, json.dumps({"ok": True}))
                if self.path == "/api/med":
                    d = body.get("date") or datetime.now(tz()).date().isoformat()
                    log_med(body["dog"], d, body.get("skip"), body.get("add"))
                    return self._send(200, json.dumps({"ok": True}))
                if self.path == "/api/vet_upload":
                    import base64
                    raw = base64.b64decode(body.get("data", ""))   # binascii.Error → ValueError → 400
                    if len(raw) > 30 * 1024 * 1024:
                        return self._send(400, json.dumps({"error": "file too large (max 30 MB)"}))
                    name = add_vet_document(
                        body["dog"], body.get("filename", ""), raw,
                        body.get("date"), body.get("provider"),
                        body.get("type"), body.get("note"))
                    return self._send(200, json.dumps({"ok": True, "file": name}))
                if self.path == "/api/vet_followup":
                    mid = add_vet_followup(
                        body["dog"], body.get("encounter_id"), body.get("text", ""),
                        body.get("date"), body.get("channel", "note"))
                    return self._send(200, json.dumps({"ok": True, "id": mid}))
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
    s.add_argument("--count", type=int, help="number of circles that day (default 1 for yes)")
    s.add_argument("--date", help="YYYY-MM-DD (default today)")
    s.set_defaults(func=cmd_circle)

    s = sub.add_parser("meal", help="log a dog's meals for a day")
    s.add_argument("--dog", help="which dog (default the primary one)")
    s.add_argument("--breakfast", choices=["yes", "no"], help="ate breakfast? (Pepper)")
    s.add_argument("--dinner", choices=["yes", "no"], help="ate dinner? (Pepper)")
    s.add_argument("--ate", metavar="ITEM", help="mark a daily_log item given/eaten (e.g. dental)")
    s.add_argument("--skip", metavar="ITEM", help="mark a daily_log item skipped/not given (e.g. dental)")
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

    s = sub.add_parser("med", help="log a medication given/skipped for a day")
    s.add_argument("--dog", help="which dog (default the primary one)")
    s.add_argument("--add", help="ad-hoc medication name given")
    s.add_argument("--dose", help="dose for --add")
    s.add_argument("--note", help="note for --add")
    s.add_argument("--skip", help="daily-med name that was NOT given")
    s.add_argument("--date", help="YYYY-MM-DD (default today)")
    s.set_defaults(func=cmd_med)

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
