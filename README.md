# Dog tracker

A little local dashboard for tracking Nala's new training plan. A Python script (stdlib
only, Python 3.9+) and generated `dashboard.html`.

## Pulling Strava data

Strava's API does not expose the "with pet" tag, so:

- **History** comes from Strava bulk export. Its `activities.csv` *does* include
  a `With Pet` column. 
- **New activities** pulled from the API and classified by keyword: if the
  title, description, or private note contains `nala` or `🐾`, it counts
  automatically. Anything ambiguous gets a quick y/n prompt during `sync`
  (or later via `review`). 

## One-time setup

1. Go to https://www.strava.com/settings/api and create an API application
   (any name; website `http://localhost`; **Authorization Callback Domain:
   `localhost`**).
2. Run `python3 nala.py auth` — paste the Client ID and Secret, approve in the
   browser tab that opens. Tokens are stored in `data/strava_auth.json`
   (keep private).

## Two ways to use it

**Static file** — `python3 nala.py build` writes `dashboard.html`; open it in any
browser. Read-only; update data from the terminal.

**Live dashboard** — `python3 nala.py serve` opens `http://localhost:8724` with
buttons: **Sync now** (pulls from Strava), **Log weight**, **Add note**, and a
"Needs your confirmation" card where unclassified outings and unanswered circle
days get y/n buttons; no terminal needed. Ctrl+C to stop.

## Day to day

```sh
python3 nala.py serve           # interactive dashboard with buttons (recommended)
# — or the terminal workflow —
python3 nala.py sync            # pull new activities, confirm any unknowns, rebuild
open dashboard.html             # look at it
python3 nala.py weight 53.5 --dog Pepper   # log a weigh-in (--dog optional)
python3 nala.py note "hesitated on stairs" --dog Nala
python3 nala.py meal --dog Pepper --breakfast no --dinner yes --other "broth"
python3 nala.py review          # classify anything sync left pending
python3 nala.py mark <id> yes --dog Pepper   # fix one dog on one activity
python3 nala.py circle yes --dog Nala        # record the circle per dog
```

The dashboard has a tab per dog. Nala's page is activity-focused (plan,
daily floor, weekly miles); Pepper's swaps those for an eating chart (meals
eaten per day, ◆ for anything different/new). Each outing and circle is
confirmed per dog, so their numbers are independent.

`sync --batch` never prompts (for cron); unknowns wait in `review` and show as
a banner on the dashboard.

## Daily circle

The unlogged ~0.44 mi / 12 min property loop counts toward her stats:

- **Before the plan** (Jan 1 – Jul 12, 2026): assumed on every in-town day
  (away windows listed in `config.json` → `circle.away`) unless a hike/walk/
  snowshoe was logged that day. A logged hike typically includes the circle, so it
  isn't double-counted. Runs and swims don't subsume it.
- **From plan start on**: nothing is assumed. For any past day with no hike
  logged, `sync` asks about the circle, or you can record one directly:
  `python3 nala.py circle yes` (today) / `circle no --date 2026-07-20`.
  Unanswered days show a `?` in the daily activity grid.

When you download a fresh bulk export later, `python3 nala.py seed
/path/to/activities.csv` reconciles the real "With Pet" tags into the log
(it will not overwrite answers you gave by hand).

## Moving to another machine

To set up on a new machine:

```sh
git clone <your-repo-url> nala-tracker && cd nala-tracker

# 1. config: copy it over, or start from the template
scp oldmachine:~/path/nala-tracker/config.json .      # your real config, OR:
cp config.example.json config.json                    # then edit home_latlng etc.

# 2. data: copy the whole logs directory (activities, weight, notes, circles,
#    and the Strava OAuth tokens)
scp -r oldmachine:~/path/nala-tracker/data .

# 3. photo (optional)
scp -r oldmachine:~/path/nala-tracker/assets .

# 4. run it
python3 nala.py serve
```

If you'd rather not copy the tokens, skip `data/strava_auth.json` and run
`python3 nala.py auth` fresh on the new machine (re-authorizes with Strava;
the rest of your data still comes from the copied `data/`).

## Files

- `config.json` — plan start date, phase targets, weight targets, feeding,
  home location, detection keywords. **Git-ignored** (personal); start from
  `config.example.json`. Edit freely; `build` re-renders from it.
- `data/activities.json` — the activity log; each has a per-dog `dogs` map
  (plus a derived `pet` flag).
- `data/weight.json`, `data/notes.json`, `data/meals.json` — your manual logs
  (weight/notes/meals carry an optional `dog`; absent means the primary dog).
- `dashboard_template.html` → `dashboard.html` — template and rendered output.
