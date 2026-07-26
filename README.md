# Nala tracker

A little local dashboard for Nala's Comeback Plan. One Python script (stdlib
only, Python 3.9+), one generated `dashboard.html` you open in a browser.

## The "with pet" problem

Strava's API does not expose the "with pet" tag (long-standing limitation), so:

- **History** comes from your bulk export — its `activities.csv` *does* include
  a `With Pet` column. Already seeded. (Caveat: Strava's tag means "with *a*
  pet," so outings with other dogs import as Nala — fix those with `mark`.
  Sync flags activities starting far from home with a ⚑ as a tell.)
- **New activities** are pulled from the API and classified by keyword: if the
  title, description, or private note contains `nala` or `🐾`, it counts
  automatically. Anything ambiguous gets a quick y/n prompt during `sync`
  (or later via `review`). Easiest habit: keep tagging "with pet" as usual
  **and** drop a 🐾 in the title — then it's fully automatic, and the tag stays
  correct on Strava for future export reconciles.

## One-time setup

1. Go to https://www.strava.com/settings/api and create an API application
   (any name; website `http://localhost`; **Authorization Callback Domain:
   `localhost`**).
2. Run `python3 nala.py auth` — paste the Client ID and Secret, approve in the
   browser tab that opens. Tokens are stored in `data/strava_auth.json`
   (keep that file private).

## Two ways to use it

**Static file** — `python3 nala.py build` writes `dashboard.html`; open it in any
browser. Read-only; you update data from the terminal.

**Live dashboard** — `python3 nala.py serve` opens `http://localhost:8724` with
buttons: **Sync now** (pulls from Strava), **Log weight**, **Add note**, and a
"Needs your confirmation" card where unclassified outings and unanswered circle
days get Yes/No buttons — no terminal needed. Ctrl+C to stop. Everything stays
local; nothing is exposed beyond your machine.

## Day to day

```sh
python3 nala.py serve           # interactive dashboard with buttons (recommended)
# — or the terminal workflow —
python3 nala.py sync            # pull new activities, confirm any unknowns, rebuild
open dashboard.html             # look at it
python3 nala.py weight 53.5     # log a weigh-in (every 2–4 weeks)
python3 nala.py note "hesitated on stairs after the long hike"
python3 nala.py review          # classify anything sync left pending
python3 nala.py mark <id> yes   # fix a single activity by ID
```

`sync --batch` never prompts (for cron); unknowns wait in `review` and show as
a banner on the dashboard.

## The daily "circle"

The unlogged ~0.44 mi / 12 min property loop counts toward her stats:

- **Before the plan** (Jan 1 – Jul 12, 2026): assumed on every in-town day
  (away windows listed in `config.json` → `circle.away`) unless a hike/walk/
  snowshoe was logged that day — a logged hike *includes* the circle, so it
  isn't double-counted. Runs/swims don't subsume it.
- **From plan start on**: nothing is assumed. For any past day with no hike
  logged, `sync` asks "did she do the circle?" — or record one directly:
  `python3 nala.py circle yes` (today) / `circle no --date 2026-07-20`.
  Unanswered days show a `?` in the daily-floor grid.

When you download a fresh bulk export later, `python3 nala.py seed
/path/to/activities.csv` reconciles the real "With Pet" tags into the log
(it never overwrites answers you gave by hand).

## Moving to another machine

The code lives in git; **your data stays local and never gets committed**
(see `.gitignore`). To set up on a new machine:

```sh
git clone <your-repo-url> nala-tracker && cd nala-tracker

# 1. config: copy your real one over, or start from the template
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

`home_latlng`, the `circle.away` travel dates, and the OAuth tokens are the
only genuinely private bits — that's why `config.json` and `data/` are
git-ignored and `config.example.json` ships with the coordinates blanked.

## Files

- `config.json` — plan start date, phase targets, weight targets, feeding,
  home location, detection keywords. **Git-ignored** (personal); start from
  `config.example.json`. Edit freely; `build` re-renders from it.
- `data/activities.json` — the activity log (`pet: true/false/null`).
- `data/weight.json`, `data/notes.json` — your manual logs.
- `dashboard_template.html` → `dashboard.html` — template and rendered output.
