# Experiment Tracker

A shared record of what we tried, what happened, and which code produced it — across every
project, not one at a time.

**Site: https://pe-hy.github.io/experiment_tracker/**

An AI agent working in any repository runs one command when a training run or evaluation
finishes. The result lands here as an immutable JSON file, the site rebuilds itself, and the
run stays findable years later.

```
project  →  variant  →  run
   │          │           └─ one execution: metrics, config, code state, curves
   │          └─ one IDEA, with a written explanation of what it changes and why
   └─ the research effort, usually one repo
```

The **variant description** is the point of the whole thing. Anyone can record a number; the
thing that is always lost is *why you tried it*.

---

## ⚠️ Everything here is public and permanent

This repository is public, so GitHub Pages is free — and that means every metric, config
value, variant description, commit message and code diff posted here is **world-readable**.

Deleting a run removes it from the site. It does **not** remove it from git history, and it
does not remove it from anyone who already read it. **If a credential is ever posted, rotate
it — do not rely on deletion.**

The poster refuses to publish payloads that look like they contain credentials, and rewrites
home and allocation paths before sending. Those are safety nets, not permission to be careless.

---

## Setup

### 1. Get a token

Create a **fine-grained** personal access token at
<https://github.com/settings/personal-access-tokens/new>:

- **Repository access:** Only select repositories → `pe-hy/experiment_tracker`
- **Permissions:** Repository permissions → **Contents: Read and write**
- **Expiration:** whatever you are willing to renew. On a personal account you may choose
  *No expiration*; a token that silently expires breaks every agent on the same day.

Scope it to this one repository. Then it can only ever write to a tracker that is rebuildable
from its own history.

```bash
mkdir -p ~/.config/exptracker && chmod 700 ~/.config/exptracker
printf '%s' '<your token>' > ~/.config/exptracker/token
chmod 600 ~/.config/exptracker/token
```

### 2. Install the skill

```bash
git clone https://github.com/pe-hy/experiment_tracker
cd experiment_tracker && ./install.sh
```

This symlinks the skill into `~/.claude/skills/`, so `/track-experiment` works from any
project directory and a `git pull` here updates it.

### 3. Check it

```bash
python3 scripts/track.py --list
```

---

## Using it

In any project, when a run finishes, ask Claude to *"track this run"* — or invoke
`/track-experiment`. The agent reads your config and logs, fills in the payload, shows you a
dry run, and posts once you agree.

By hand:

```bash
python3 scripts/track.py run.json --repo-path . --dry-run   # inspect
python3 scripts/track.py run.json --repo-path .             # publish
```

Useful flags:

| Flag | Why |
|---|---|
| `--list [project]` | See existing projects and variants. **Always do this before naming one.** |
| `--snapshot PATH` | Store a script or config verbatim. Essential when the repo has no remote. |
| `--slurm-job-id ID` | Look the job up with `sacct` — records `TIMEOUT` / `OUT_OF_MEMORY` properly. |
| `--new-name` | Allow a name that resembles an existing one. You should rarely need this. |

The minimum payload:

```json
{
  "project": "my-project",
  "variant": "bigger-encoder",
  "variant_description": "Double the encoder width to test whether capacity is the bottleneck.",
  "metrics": { "accuracy": 0.83 }
}
```

Everything else — commit SHA, branch, dirty state, whether the commit was ever pushed, the
uncommitted diff, Slurm details, container, accelerator-hours — is collected automatically.

---

## How it works

Agents `PUT` one immutable file per run through the GitHub Contents API. A GitHub Action
rebuilds the index and deploys the site. No server, no database, nothing that can expire.

```
agent ──PUT──▶ data/projects/<p>/runs/<id>.json ──push──▶ Action ──▶ GitHub Pages
```

Two properties make this safe rather than fragile:

- **One file per run, never edited.** Two agents posting at the same moment write different
  paths, so no write can lose another. Run ids are derived from run content, so retrying
  after a lost network response re-targets the same file instead of creating a duplicate.
- **The index is a build artifact, never committed.** The run files are the only source of
  truth. If the index is ever wrong, re-run the workflow and it comes back.

`docs/ARCHITECTURE.md` has the details and the limits.

## Reading it

- **Landing page** — every project, plus a cross-project feed of the most recent runs.
- **Project page** — one collapsible panel per variant, each headed by the description of
  the idea and its conclusion, with a sortable run table underneath. The best value per
  metric is marked. Tick two or more runs and hit **Compare** for a side-by-side of metrics,
  config and code state, filtered to *differences only* by default.
- **Run page** — metrics, hypothesis and conclusion, training curves, full config, the exact
  code state, the uncommitted diff, and any snapshotted files. Loud warnings when a commit
  was never pushed or a diff was truncated, because a record that looks authoritative and
  is not is worse than a missing one.
- **Copy CSV / Copy LaTeX** on any run table, for pasting into a paper.

## Fixing things

Runs are immutable by design, but mistakes happen. Every run page has **Edit on GitHub** and
**Delete run** buttons; editing in the GitHub web UI retriggers the rebuild automatically.

To rename a project or improve its title, add `data/projects/<slug>/project.meta.json`:

```json
{ "name": "Decision Chains", "description": "…", "primary_metric": "exact_match",
  "metric_goals": { "exact_match": "max" } }
```

Hand-written metadata always wins over anything an agent inferred.

## Development

```bash
NODE=/path/to/node bash tests/run.sh
```

Runs the poster/indexer self-checks and renders every view under a stub DOM. No dependencies,
no package manager — the site has no build step and the tests do not need one either. CI runs
the same thing on every push.

## Limits worth knowing

| | |
|---|---|
| A newly posted run appears | in ~1–10 minutes (Pages caches for 10 min) |
| Runs per project | 3,000 (GitHub's per-directory limit) |
| Uncommitted diff stored | up to 256 KB, truncated loudly beyond that |
| Snapshotted files | up to 10 files, 64 KB each |
| Write rate | GitHub allows ~500 content-creating requests/hour |
