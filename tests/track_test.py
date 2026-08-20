#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-checks for the poster and the indexer.

Stdlib only and Python 3.6-compatible, like everything else here, so this runs on a
login node as readily as in CI.
"""

from __future__ import print_function

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import track      # noqa: E402
import reindex    # noqa: E402

FAILURES = []
CHECKS = [0]


def check(name, condition, detail=""):
    CHECKS[0] += 1
    if condition:
        print("  ok   " + name)
    else:
        FAILURES.append(name)
        print("  FAIL " + name + ("  — " + detail if detail else ""))


def expect_error(name, fn, fragment):
    try:
        fn()
    except track.TrackerError as exc:
        check(name, fragment.lower() in str(exc).lower(),
              "message was: %s" % str(exc).replace("\n", " ")[:120])
    except Exception as exc:  # pragma: no cover
        check(name, False, "raised %s instead" % type(exc).__name__)
    else:
        check(name, False, "no error raised")


print("\nslugs — the anti-proliferation rules")
check("underscores and hyphens fold together",
      track.slugify("chunks_labeling_v2") == track.slugify("chunks-labeling-v2"))
check("spaces and case fold too",
      track.slugify("Chunks Labeling v2") == "chunks-labeling-v2")
check("punctuation is stripped", track.slugify("Demo (delete me)!") == "demo-delete-me")
check("leading junk does not survive", track.slugify("__demo__") == "demo")
check("empty input falls back", track.slugify("!!!", "fallback") == "fallback")
check("canonical() catches separator-only differences",
      track.canonical("decision-chains") == track.canonical("decisionchains"))
check("canonical() still distinguishes real differences",
      track.canonical("grpo-step") != track.canonical("grpo-token"))

print("\ntimestamps")
check("Z form passes through",
      track.normalise_timestamp("2026-08-19T14:32:00Z", "t") == "2026-08-19T14:32:00Z")
check("positive offset converts to UTC",
      track.normalise_timestamp("2026-08-19T14:32:00+02:00", "t") == "2026-08-19T12:32:00Z")
check("negative offset converts to UTC",
      track.normalise_timestamp("2026-08-19T14:32:00-05:00", "t") == "2026-08-19T19:32:00Z")
check("space separator accepted",
      track.normalise_timestamp("2026-08-19 14:32:00", "t") == "2026-08-19T14:32:00Z")
check("fractional seconds accepted",
      track.normalise_timestamp("2026-08-19T14:32:00.123Z", "t") == "2026-08-19T14:32:00Z")
expect_error("garbage is rejected loudly",
             lambda: track.normalise_timestamp("last tuesday", "started_at"),
             "not a recognisable timestamp")

print("\nvalidation")
good = {"project": "p", "variant": "v",
        "variant_description": "A real sentence explaining the idea under test here.",
        "metrics": {"acc": 0.5}}
track.validate(good)
check("a well-formed payload validates", True)
expect_error("missing variant_description is rejected",
             lambda: track.validate({"project": "p", "variant": "v"}),
             "variant_description")
expect_error("a one-word description is rejected",
             lambda: track.validate(dict(good, variant_description="lora run")),
             "real sentence")
expect_error("string metrics are rejected",
             lambda: track.validate(dict(good, metrics={"acc": "64.12%"})),
             "not numbers")
expect_error("boolean metrics are rejected",
             lambda: track.validate(dict(good, metrics={"acc": True})),
             "not numbers")
expect_error("a bad metric_goals value is rejected",
             lambda: track.validate(dict(good, metric_goals={"acc": "higher"})),
             "max")
check("null metrics are allowed (attempted but unavailable)",
      track.validate(dict(good, metrics={"acc": None})) is None)

print("\ncredential scanning")


def scan(payload, patch=None):
    return track.guard_secrets(payload, patch, allow=False)


for label, blob in (
    ("GitHub PAT", "github_pat_11ABCDEFG0aaaaaaaaaaaa_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    ("classic GitHub token", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
    ("OpenAI key", "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
    ("HF token", "hf_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
    ("AWS key id", "AKIAIOSFODNN7EXAMPLE"),
    ("private key", "-----BEGIN RSA PRIVATE KEY-----"),
):
    expect_error("refuses a %s in config" % label,
                 lambda b=blob: scan({"config": {"x": b}}), "credential")

expect_error("refuses a credential hiding in the patch",
             lambda: scan({"config": {}}, b"+api_key = ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n"),
             "credential")
expect_error("flags a long value under a credential-shaped key name",
             lambda: scan({"config": {"wandb_api_key": "0123456789abcdef0123456789"}}),
             "credential")
check("ordinary payloads pass untouched",
      scan({"config": {"lr": 0.0003, "model": "Qwen2.5-7B"},
            "notes": "loss spiked at step 12k"}) is None)
check("a short value under a key-ish name is not a false positive",
      scan({"config": {"token_budget": "4096"}}) is None)

print("\nredaction")
rules = track.build_redactions()
check("allocation directories are masked",
      "$ALLOC" in track.redact("/scratch/project_465002631/Petr/x", rules))
check("redaction reaches nested structures",
      "$ALLOC" in json.dumps(track.redact(
          {"a": [{"p": "/scratch/project_465002631/y"}]}, rules)))
check("unrelated paths are left alone",
      track.redact("/usr/lib/python3", rules) == "/usr/lib/python3")

print("\nremote URL handling")
check("credentials are stripped from https remotes",
      track.strip_credentials("https://user:ghp_secret@github.com/o/r.git")
      == "https://github.com/o/r.git")
host, owner, repo, https = track.parse_remote("git@github.com:pe-hy/DecisionChains.git")
check("scp-style ssh remotes parse", (host, owner, repo) == ("github.com", "pe-hy", "DecisionChains"))
check("ssh remotes become browsable https", https == "https://github.com/pe-hy/DecisionChains")
host2, _, _, _ = track.parse_remote("https://gitlab.example.org/group/sub/proj.git")
check("self-hosted forges keep their host", host2 == "gitlab.example.org")
check("a remote with an embedded token never leaks the token",
      "ghp_" not in (track.parse_remote(
          "https://x:ghp_AAAAAAAAAAAAAAAAAAAA@github.com/o/r.git")[3] or ""))

print("\nnear-duplicate detection")
index = {"projects": [{"slug": "decisionchains", "name": "DecisionChains",
                       "variant_preview": [{"variant": "grpo-step-level-rewards"}]}]}
expect_error("a re-spelled project is refused",
             lambda: track.check_naming({"project": "decision chains", "variant": "x"},
                                        index, force_new=False),
             "another spelling")
expect_error("copying the display name is refused",
             lambda: track.check_naming({"project": "DecisionChains ", "variant": "x"},
                                        index, force_new=False),
             "another spelling") if track.slugify("DecisionChains ") != "decisionchains" else \
    check("display name already slugifies onto the existing project", True)
check("--new-name overrides the refusal",
      track.check_naming({"project": "decision chains", "variant": "x"},
                         index, force_new=True) is None)
check("an unrelated new project is allowed",
      track.check_naming({"project": "totally-different", "variant": "x"},
                         index, force_new=False) is None)

print("\nrun ids")
check("ids are content-derived and stable", True)
tmp = tempfile.mkdtemp()
try:
    class Args(object):
        no_code = True
        snapshot = []
        slurm_job_id = None
        include_untracked_names = False

    payload = {"project": "P", "variant": "V",
               "variant_description": "A description long enough to satisfy the rule.",
               "run_name": "r1", "started_at": "2026-01-01T00:00:00Z",
               "finished_at": "2026-01-01T01:00:00Z", "metrics": {"a": 1.0}}
    run_a, _ = track.build_run(dict(payload), tmp, Args(), [])
    run_b, _ = track.build_run(dict(payload), tmp, Args(), [])
    check("identical payloads produce the identical run id — retries cannot duplicate",
          run_a["run_id"] == run_b["run_id"], "%s vs %s" % (run_a["run_id"], run_b["run_id"]))
    run_c, _ = track.build_run(dict(payload, metrics={"a": 2.0}), tmp, Args(), [])
    check("a different result produces a different run id",
          run_a["run_id"] != run_c["run_id"])
    check("duration is derived from the timestamps", run_a["duration_seconds"] == 3600.0)
    check("the run id matches the documented shape",
          __import__("re").match(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$", run_a["run_id"]) is not None,
          run_a["run_id"])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\nindexer")
check("numbers are recognised, booleans are not",
      reindex.is_number(1.5) and not reindex.is_number(True))
variants = reindex.collect_variants([
    {"variant": "v", "variant_description": "newest description", "run_id": "b",
     "finished_at": "2026-02-01T00:00:00Z"},
    {"variant": "v", "variant_description": "older description", "run_id": "a",
     "finished_at": "2026-01-01T00:00:00Z"},
])
check("the newest run supplies the variant description",
      variants[0]["description"] == "newest description")
check("the description records which run it came from",
      variants[0]["description_from"] == "b")
check("runs are grouped under one variant", variants[0]["run_count"] == 2)

conclusions = reindex.collect_variants([
    {"variant": "v", "variant_description": "d", "run_id": "b",
     "variant_conclusion": "it worked", "finished_at": "2026-02-01T00:00:00Z"},
    {"variant": "v", "variant_description": "d", "run_id": "a",
     "finished_at": "2026-01-01T00:00:00Z"},
])
check("the newest conclusion wins", conclusions[0]["conclusion"] == "it worked")

check("gpu-hours sum across runs including failures",
      reindex._sum_gpu_hours([
          {"env": {"gpu_hours": 4.0}, "status": "completed"},
          {"env": {"gpu_hours": 2.5}, "status": "failed"},
      ]) == 6.5)
check("gpu-hours are absent rather than zero when nothing reported them",
      reindex._sum_gpu_hours([{"status": "completed"}]) is None)

print("\n" + ("%d of %d checks FAILED: %s" % (len(FAILURES), CHECKS[0], ", ".join(FAILURES))
             if FAILURES else "all %d checks passed" % CHECKS[0]))
sys.exit(1 if FAILURES else 0)
