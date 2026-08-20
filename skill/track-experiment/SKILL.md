---
name: track-experiment
description: Record an experiment run (training, finetune, eval, ablation) to the shared experiment tracker at pe-hy.github.io/experiment_tracker. Use whenever a training run, evaluation, or ablation finishes and produced results worth keeping, when the user says "track this", "log this run", "record these results", "add this to the tracker", or when you have just reported metrics the user will want to compare against later.
---

# Track an experiment

Records one run to the shared tracker so that results, the idea behind them, and the exact
code that produced them stay findable months later.

The model is **project → variant → run**:

- **project** — the research effort, usually one repo or directory (`DecisionChains`, `chunks_labeling_v3`).
- **variant** — the *idea* being tried inside that project (`grpo-step-level-rewards`,
  `frozen-encoder`, `baseline`). A new architecture, loss, or data recipe is a new variant.
- **run** — one execution producing metrics. Many runs per variant.

Projects and variants are created implicitly the first time you name them. There is nothing
to register in advance.

## Before you write anything

**Everything you post is public and permanent.** The tracker repo is public, so the payload
is world-readable and search-indexable. Never include API keys, tokens, passwords, dataset
contents, personal data, or anything under embargo. If a config dict holds a credential,
drop that key before posting.

## Steps

### 1. Gather the facts — ask rather than guess

Read the training/eval config, logs, and output files in the project to fill in metrics and
config. Do **not** invent numbers: every metric must come from a file or from the user.

If you cannot determine the metrics, **ask the user** instead of posting a run without them.

### 2. Write `variant_description` properly

This is the field the whole tracker exists for, and the one only you can supply. It must be a
real sentence explaining **what idea this variant tests and why**, written so that someone —
including the user in six months — understands the point without reading the code.

- Good: *"Replace the frozen sentence encoder with a trainable LoRA adapter, to test whether
  the encoder is the bottleneck on long chunks."*
- Useless: *"lora run"*, *"v2"*, *"experiment 3"*.

The posting script rejects descriptions under 15 characters.

If a variant already exists and its description is now inaccurate, just write a better one —
the newest description posted wins, so descriptions improve over time.

Also fill `hypothesis` (what you expected) and, once results are in, `conclusion` (what
actually happened, including "this did not work" — negative results are the main reason this
tracker exists).

### 3. Build the payload

Write JSON to a temp file. Required: `project`, `variant`, `variant_description`.
Everything else is optional but valuable.

```json
{
  "project": "DecisionChains",
  "project_description": "GRPO and naive finetuning on decision-chain reasoning tasks.",

  "variant": "grpo-step-level-rewards",
  "variant_description": "Reward each reasoning step individually instead of only the final answer, to test whether denser credit assignment stabilises GRPO training.",

  "run_name": "grpo-step-lr2e6-seed1",
  "status": "completed",
  "started_at": "2026-08-19T09:00:00Z",
  "finished_at": "2026-08-19T14:32:00Z",

  "metrics": { "exact_match": 0.6412, "step_accuracy": 0.8123, "train_loss": 0.2841 },
  "primary_metric": "exact_match",

  "hypothesis": "Dense per-step rewards reduce variance versus terminal-only reward.",
  "conclusion": "Held up: seed variance dropped and exact match improved 4.1 points.",
  "notes": "Run 3 of 3. Seeds 1/2/3 gave 0.641 / 0.638 / 0.644.",

  "config": { "lr": 2e-6, "batch_size": 32, "model": "Qwen2.5-7B", "steps": 4000 },
  "tags": ["grpo", "ablation"],
  "curves": { "train_loss": [[0, 1.82], [500, 0.91], [1000, 0.54]] },
  "artifacts": [{ "name": "checkpoint", "path": "/scratch/.../checkpoint-4000" }]
}
```

Field notes:

- `status` — `completed`, `failed`, `running`, or `cancelled`. **Record failed runs too.**
  A variant that did not work is a result, and it stops the idea being retried by accident.
- `metrics` — flat, numbers only. Use the same metric names across runs in a project or the
  comparison table cannot line them up.
- `primary_metric` — which key is *the* number. Drives the default table sort.
- `config` — hyperparameters. Nested objects are fine; they are flattened for display.
- `curves` — optional `[[step, value], …]` per metric. Downsample to ≤500 points.
- `code` / `env` — **do not fill these in.** The script collects git and Slurm provenance
  itself, and anything you write by hand will override what it detected.

### 4. Post it

Run from **inside the project being tracked**, so git provenance is picked up:

```bash
python3 ~/.claude/skills/track-experiment/scripts/track.py /tmp/run.json --repo-path .
```

Add `--dry-run` first to show the user exactly what will be published. Do that whenever the
run is the first for a project, or whenever you are unsure about a field.

The script automatically records: commit SHA, branch, detached-HEAD state, `git describe`,
remote URL (with any embedded credentials stripped), whether the working tree was dirty,
whether the commit was ever pushed, names of untracked files, a patch of uncommitted changes,
plus Slurm job details and container info when present.

### 5. Report back

Tell the user the run ID and the page URL the script prints. Mention that the site rebuilds
automatically and the run appears within a few minutes.

## Handling problems

- **`Missing required field(s)`** — supply `variant_description`; it is not optional.
- **`403 … lacks 'Contents: Read and write'`** — the token needs fixing; tell the user rather
  than retrying.
- **`No token`** — the token belongs at `~/.config/exptracker/token`, `chmod 600`, or in
  `$TRACKER_TOKEN`. Never commit it, and never print it.
- **Code ran from a copied tree with no `.git`** — set `TRACKER_GIT_COMMIT`,
  `TRACKER_GIT_BRANCH`, `TRACKER_GIT_REMOTE` and the script will use those instead.

## When *not* to use this

Smoke tests, debugging loops, and runs whose numbers you would not want to see in a
comparison table six months from now. Track results, not every invocation.
