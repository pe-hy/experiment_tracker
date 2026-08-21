---
name: track-experiment
description: Record an experiment run (training, finetune, eval, ablation) to the shared experiment tracker at pe-hy.github.io/experiment_tracker. Use whenever a training run, evaluation, or ablation finishes and produced results worth keeping, when the user says "track this", "log this run", "record these results", "add this to the tracker", or when you have just reported metrics the user will want to compare against later.
---

# Track an experiment

Records one run so that the result, the idea behind it, and the exact code that produced it
are still findable months from now.

The model is **project → variant → run**:

- **project** — the research effort, usually one repo or directory.
- **variant** — the *idea* being tried inside it (`grpo-step-level-rewards`, `frozen-encoder`,
  `baseline`). A new architecture, loss, or data recipe is a new variant.
- **run** — one execution producing metrics. Many runs per variant.

## Step 1 — Look before you name. Do not skip this.

```bash
python3 ~/.claude/skills/track-experiment/scripts/track.py --list
```

**This is the most important step.** You have no memory of previous sessions. If you invent
`grpo-step-rewards` where an earlier run used `grpo-step-level-rewards`, the history of one
idea is split across two names and neither view joins them back together. Six months of that
makes the tracker useless.

Read the output and **copy the quoted slugs exactly**. Use `--list <project>` to see every
variant in one project.

- Same idea as an existing variant → **reuse that variant slug**, even if you would have
  worded it differently.
- Genuinely new idea → new variant slug.
- Not really testing an idea (a baseline, a smoke check) → use `baseline`.

The script refuses names that look like a re-spelling of an existing one. If it refuses,
it is almost always right — use the name it suggests. `--new-name` overrides it, and you
should need that roughly never.

## Step 2 — Gather the facts. Never invent them.

Read the config, logs and output files to fill in metrics and hyperparameters. Every number
must come from a file or from the user. **If you cannot find the metrics, ask the user —
do not post a run with guessed numbers.** A tracker with invented values is worse than none,
because it will be trusted.

## Step 3 — Write `variant_description` properly

This is the field the tracker exists for, and the only one nothing else can supply. A real
sentence explaining **what this variant changes and why you expected it to help**, written
for someone who has forgotten everything about this project.

- Good: *"Replace the frozen sentence encoder with a trainable LoRA adapter, to test whether
  the encoder is the bottleneck on long chunks."*
- Rejected: `"lora run"`, `"v2"`, `"experiment 3"`.

Under 25 characters is refused outright.

If the variant already exists, **reuse its existing description verbatim** — copy it from
`--list`. Only write a new one if you are deliberately improving it, because the newest
description posted becomes the one everyone sees.

Also fill:
- `hypothesis` — what you expected, before seeing results.
- `conclusion` — what this particular run showed.
- `variant_conclusion` — the verdict on the *idea* once you know it. Including "this did not
  work". Negative results are the main reason this tracker is worth keeping.

## Step 3b — Where did this idea come from?

Only when you are creating a **new** variant. Answer the first question that applies:

1. Same recipe as an existing variant, re-run to check the result holds (new seed, new
   machine)? → `"relation": "replicates"`, one parent.
2. A combination of two or more existing variants? → `"relation": "composes"`, and list
   **every** one of them.
3. Otherwise: which single existing variant did you start from and change? →
   `"relation": "derived-from"`.

```json
"variant_derived_from": [
  { "variant": "varlab-deep-emit",          "relation": "composes" },
  { "variant": "varlab-strict-moves-value", "relation": "composes" }
]
```

Copy parent slugs **exactly** from `--list`. If the honest answer is "nothing — this is a
fresh line of work", **omit the field**.

**If you are unsure, omit it.** A missing parent is a blank someone can fill in later. A
wrong parent is a false claim about how the research happened, and it will be believed.

Also set `variant_status` once you know it:

| Value | Means |
|---|---|
| `adopted` | It worked and became the default |
| `refuted` | Tested and it did not work. **Record these — they are the point.** |
| `superseded` | Worked, then something better replaced it |
| `inconclusive` | Measured, no clear signal |
| `active` | Still open |

And `variant_role: "control"` or `"baseline"` for a reference arm everything else is
measured against.

Do **not** invent lineage for variants that already exist — post a run with the corrected
`variant_derived_from`, or edit `data/projects/<project>/lineage.json`, which overrides
anything an agent asserted.

## Step 4 — Build the payload

Required: `project`, `variant`, `variant_description`. Everything else is optional.

```json
{
  "project": "decisionchains",
  "project_description": "GRPO and naive finetuning on decision-chain reasoning tasks.",

  "variant": "grpo-step-level-rewards",
  "variant_description": "Reward each reasoning step individually instead of only the final answer, to test whether denser credit assignment stabilises GRPO training.",
  "variant_conclusion": "Held up across three seeds; adopted as the default.",

  "run_name": "grpo-step-lr2e6-seed1",
  "status": "completed",
  "started_at": "2026-08-19T09:00:00Z",
  "finished_at": "2026-08-19T14:32:00Z",

  "metrics": { "exact_match": 0.6412, "step_accuracy": 0.8123 },
  "primary_metric": "exact_match",
  "metric_goals": { "exact_match": "max", "step_accuracy": "max" },

  "hypothesis": "Dense per-step rewards reduce variance versus terminal-only reward.",
  "conclusion": "Held up: seed variance dropped and exact match improved 4.1 points.",
  "notes": "Run 3 of 3.",
  "seed": 1,
  "group": "seed-sweep-lr2e6",
  "variant_status": "adopted",
  "variant_derived_from": [{ "variant": "grpo-terminal-reward", "relation": "derived-from" }],
  "derived_from": [
    { "project": "decisionchains", "run_id": "20260810T101500Z-3f21ab", "relation": "evaluates" }
  ],

  "config": { "lr": 2e-6, "batch_size": 32, "model": "Qwen2.5-7B" },
  "tags": ["grpo", "ablation"],
  "curves": { "train_loss": [[0, 1.82], [500, 0.91], [1000, 0.54]] },
  "artifacts": [{ "name": "checkpoint", "path": "/scratch/.../checkpoint-4000" }]
}
```

Field notes:

- `status` — `completed`, `failed`, or `cancelled`. **Record failed runs too**; a dead end is
  a result, and it stops the idea being retried by accident. Do not use `running`: runs are
  immutable, so it would stay "running" forever.
- `metrics` — flat, **numbers only**. `0.6412`, never `"64.12%"`. A non-numeric value is
  rejected. Use the same metric names across runs or the comparison table cannot line them up.
- `primary_metric` / `metric_goals` — which number matters and whether higher or lower is
  better. Without `metric_goals` the site guesses from the name and gets things like
  `regret` wrong.
- `seed` / `group` — set both when running the same config across seeds.
- `derived_from` — when this run measures or continues an earlier one (an eval scoring a
  checkpoint, GRPO started from an SFT run), point at it. `relation` is `evaluates`,
  `continues` or `initialised_from`. Without it, an eval and the thing it evaluates end up
  as unrelated rows.
- `curves` — optional `[[step, value], …]`. Downsample to ≤500 points.
- `code` / `env` — **do not fill these in.** The script collects them, and anything you write
  by hand overrides what it detected.

## Step 5 — Dry run, and show the user

```bash
python3 ~/.claude/skills/track-experiment/scripts/track.py /tmp/run.json --repo-path . --dry-run
```

Run from **inside the project being tracked** so git provenance is picked up.

**Show the user the dry-run output and get their go-ahead before posting.** A post is a
commit to a public repository and cannot be truly undone.

If the project has no git remote, or the commit was never pushed, the script says so. In that
case add the files that define the run:

```bash
--snapshot train.py --snapshot configs/se3.yaml --snapshot sbatch/train.sh
```

These are stored verbatim, so the run stays reproducible even when the SHA resolves nowhere.

For a Slurm job, pass the job id and the real state (including `TIMEOUT` and `OUT_OF_MEMORY`)
is looked up for you:

```bash
--slurm-job-id 21406869
```

## Step 6 — Post

Drop `--dry-run`. Report the run ID and page URL the script prints, and mention the site
rebuilds in a few minutes.

## Never publish secrets

Everything posted goes to a **public** repository, permanently. Deleting a file afterwards
does **not** remove it from git history.

The script scans the payload and the diff for credentials and refuses to post if it finds
any. If it refuses: remove the value, and tell the user to rotate it if it was real. Do not
reach for `--i-have-checked` unless you have confirmed it is a false positive.

Before building the payload, drop any config key holding a token, password or API key.

## When it goes wrong

- **`Missing required field(s)`** — supply `variant_description`; it is not optional.
- **`looks like another spelling`** — you invented a new name for an existing thing. Use the
  suggested one.
- **`not numbers`** — a metric is a string. Convert it.
- **`403 … lacks 'Contents: Read and write'`** — the token needs fixing. Tell the user; do
  not retry.
- **`No token found`** — it belongs at `~/.config/exptracker/token`, `chmod 600`. Never print it.
- **Code ran from a copied tree with no `.git`** — set `TRACKER_GIT_COMMIT`,
  `TRACKER_GIT_BRANCH`, `TRACKER_GIT_REMOTE`, or use `--snapshot`.

Re-running after a failure is safe: the run id is derived from the run's content, so a retry
lands on the same file rather than creating a duplicate.

## When *not* to use this

Smoke tests, debugging loops, and runs whose numbers you would not want to see in a
comparison table six months from now. Track results, not every invocation.
