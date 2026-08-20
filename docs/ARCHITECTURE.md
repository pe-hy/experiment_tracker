# Architecture

## The one-sentence version

Agents `PUT` an immutable JSON file per run into this repo over the GitHub Contents API;
a GitHub Action rebuilds a small index and deploys a dependency-free static site to
GitHub Pages.

```
  any project, any machine                 this repo (public)              GitHub Pages
  ────────────────────────                 ──────────────────              ────────────
  /track-experiment                        data/projects/<p>/
     │  collects git provenance              runs/<run-id>.json   ─┐
     │  builds run JSON                      runs/<run-id>.patch  ─┤
     ▼                                                             │
  scripts/track.py  ──PUT /contents──▶  commit on main             │
     (stdlib only,        + retry/backoff        │                 │
      python 3.6+)                               ▼                 │
                                     ┌───────────────────────┐     │
                                     │ Action: reindex+deploy│◀────┘
                                     │  concurrency: collapse│
                                     │  bursts into one run  │
                                     └───────────┬───────────┘
                                                 │ builds data/index.json
                                                 ▼
                                        site/ + data/  ──▶  pe-hy.github.io/experiment_tracker
```

## Why this shape

**Nothing here can expire, sleep, or be discontinued.** That was the deciding criterion.
Surveyed free backends fail on inactivity, which is exactly what a research tracker does
between experiments: Supabase pauses a project after ~7 days idle, Turso archives after 10,
Upstash destroys the endpoint after 30, Appwrite pauses after 7 days without *console*
activity (API traffic does not reset the clock), Render deletes free Postgres after 30+14
days. Recent free tiers that simply vanished: Deta (dead), Deno Deploy Classic (shut down
2026-07-20), PlanetScale free, Fly.io's free allowance, Netlify's classic free tier.
A GitHub repo left alone for a year is still exactly there.

**One immutable file per run** is what makes concurrent writers safe. Two agents never touch
the same path, so there is no read-modify-write anywhere on the write path and no
lost-update class of bug. Run IDs are `<utc-timestamp>-<content-hash>`: they sort
chronologically for free, and because the hash covers the run's own fields, posting the same
result twice lands on the same file rather than duplicating it.

**The index is derived, never authored.** `data/index.json` is a build artifact regenerated
from the run files on every push. If it is ever wrong, delete it and push — it comes back.
The run files are the only source of truth.

## Layout

```
data/                               # IN THE REPO — agent-written, the source of truth
  projects/
    <project-slug>/
      project.meta.json             # OPTIONAL, hand-written. Overrides inferred metadata.
      runs/
        <run-id>.json               # WRITTEN BY AGENT. Immutable, never edited.
        <run-id>.patch              # WRITTEN BY AGENT, optional. Raw git diff text.
_site/                              # BUILT BY CI — never committed, never in the repo
  data/index.json                   # projects, variants, run summaries, recent-runs feed
  data/projects/<slug>/project.json # rolled-up project + variant metadata
  data/projects/<slug>/runs/…       # the run files, copied through verbatim
site/
  index.html                        # the whole app shell
  assets/app.css                    # design system
  assets/app.js                     # router + views. No dependencies.
scripts/
  reindex.py                        # run by the Action; also runnable locally
  track.py                          # the poster; shipped to agents via the skill
skill/track-experiment/             # the /track-experiment skill, installed to ~/.claude/skills/
.github/workflows/                  # reindex + Pages deploy
```

### Why the diff is a sibling `.patch` file, not a JSON string

The run JSON records *that* there is a diff and how big it is; the diff itself lives next to
it as raw patch text. Embedding it as a JSON string costs 5–10% in escaped newlines and,
more importantly, defeats git's delta compression — successive patches from the same project
are highly similar and delta almost to nothing as raw text, but not as escaped JSON blobs.
The patch is written *before* the run JSON, so the JSON never advertises a patch that
failed to upload.

## Known limits, and how far away they are

| Limit | Value | Us |
|---|---|---|
| Files in one directory | 3,000 | shard is `<project>/runs/`, so 3,000 runs *per project* |
| Content-generating API requests | 80/min, 500/hr | ~2 per run, a few runs/day |
| Recommended push rate | 6/min per repo | 1–2 writes per run, a few runs/day |
| Published site | 1 GB | patches capped at 256 KB each |
| Pages bandwidth | 100 GB/month (soft) | two readers |
| Repo size | 1 GB comfortable, 10 GB hard | — |

The write path retries with exponential backoff on `409`/`422` (ref race) and honours
`Retry-After` on `403`/`429`. Concurrent writes to *different* paths can still conflict,
because the Contents API commits and pushes server-side against the same ref — unique paths
remove the stale-SHA conflict, not the ref race. There is no cross-process lock; retries are
the whole mechanism, which is sufficient at a few runs per day and would not be at a few
hundred per minute.

Two failure modes get explicit handling rather than a retry:

* **A lost response after the commit landed.** `urllib` does not wrap a timeout during the
  *response* phase, so it surfaces as a bare `socket.timeout` rather than a `URLError`;
  catching only `URLError` would let it escape as a traceback after the write succeeded. The
  poster catches `OSError` (which covers both) and, before retrying, checks whether the file
  now exists.
* **Duplicate runs.** The run id is a hash of the run's own content, so a retry — or a
  human re-running the same command — targets the same path instead of creating a second
  copy of the same result under a new id.

## Freshness

GitHub Pages serves with `max-age=600` and the Action takes a minute or two, so a run posted
now is visible in roughly 1–10 minutes. The UI states when the index was built rather than
implying it is live. This is a property of the hosting, not a bug to fix.

## Everything is public

The repo is public, so GitHub Pages is free — but that means every metric, hyperparameter,
variant description, git remote URL and code diff posted here is world-readable and
search-indexable. Never post credentials, API keys, licence keys, private datasets, or
anything under embargo. The skill repeats this warning to the agent at write time.

## Deliberately no JavaScript dependencies

No framework, no CDN, no build step — matching the house style already used in
`summer_school_project`. The diff renderer, the metric chart, the sort/filter and the router
are each a few dozen lines of plain ES modules. The obvious library stack (diff2html +
highlight.js + minisearch + uPlot + marked) would be ~113 KB gzipped and five things that
can rot; at a few hundred runs it buys nothing. A consequence worth knowing: `diff2html`'s
default bundle is 335 KB gzipped because it inlines every highlight.js grammar.
