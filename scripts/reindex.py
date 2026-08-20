#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the static index the site reads, from the immutable per-run JSON files.

The run files under ``data/projects/<project>/runs/`` are the only source of truth.
Everything this script emits is derived and disposable — if the index is ever wrong,
delete it and re-run.

Deliberately stdlib-only and Python 3.6-compatible: it runs both in GitHub Actions
and on a LUMI login node, where the system interpreter is 3.6.15 and nothing can be
pip-installed.
"""

from __future__ import print_function

import argparse
import json
import os
import shutil
import sys
from datetime import datetime

SCHEMA_VERSION = 1

# Fields promoted into the index so the browser can render lists, sort and filter
# without fetching every run file. Everything else stays in the run file and is
# loaded on demand when a run is opened.
_SUMMARY_KEYS = (
    "run_id", "run_name", "status", "author", "tags",
    "started_at", "finished_at", "duration_seconds",
    "primary_metric", "metrics", "notes", "seed", "group",
)


# Files that could not be parsed. Surfaced in the index so a typo shows up as a
# banner on the site rather than as a run that quietly ceased to exist.
INVALID = []


def log(msg):
    print("[reindex] " + msg)


def read_json(path):
    """Return parsed JSON, or None if the file is unreadable or malformed.

    A single corrupt run file must never take down the whole site, so this
    reports and skips rather than raising.
    """
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except ValueError as exc:
        log("SKIP malformed JSON: %s (%s)" % (path, exc))
        INVALID.append({"path": path, "error": "malformed JSON: %s" % exc})
    except (IOError, OSError) as exc:
        log("SKIP unreadable: %s (%s)" % (path, exc))
        INVALID.append({"path": path, "error": "unreadable: %s" % exc})
    return None


def parse_ts(value):
    """Best-effort ISO-8601 -> datetime. Returns None on anything unexpected.

    Written out longhand because datetime.fromisoformat is 3.7+ and this must
    run on 3.6.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+0000")
    # Normalise "+02:00" -> "+0200" for %z, which is picky on old Pythons.
    if len(text) >= 6 and text[-3] == ":" and text[-6] in "+-":
        text = text[:-3] + text[-2:]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def sort_key_desc(run):
    """Newest first. Runs with no parseable timestamp sort last, then by id."""
    stamp = parse_ts(run.get("finished_at")) or parse_ts(run.get("started_at"))
    # Strip tzinfo so naive and aware timestamps remain mutually comparable.
    if stamp is not None and stamp.tzinfo is not None:
        stamp = stamp.replace(tzinfo=None)
    return (stamp is None, stamp or datetime.min, run.get("run_id") or "")


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def summarise(run):
    """Project a run down to the fields the list views need."""
    out = {}
    for key in _SUMMARY_KEYS:
        if key in run and run[key] is not None:
            out[key] = run[key]
    metrics = run.get("metrics")
    if isinstance(metrics, dict):
        # Keep only scalars in the index; nested metric objects stay in the run file.
        out["metrics"] = dict((k, v) for k, v in metrics.items() if is_number(v))
    code = run.get("code") or {}
    if isinstance(code, dict):
        slim = {}
        for key in ("commit", "commit_short", "branch", "dirty", "remote",
                    "remote_url", "patch_file", "patch_lines", "patch_truncated",
                    "patch_kind", "visibility", "commit_pushed"):
            if code.get(key) is not None:
                slim[key] = code[key]
        if slim:
            out["code"] = slim
    return out


def collect_variants(runs):
    """Group a project's runs into variants.

    The variant *description* is the point of the whole tracker, so it is carried on
    every run payload rather than living in a separate file that would need a
    read-modify-write to update. The most recent run that supplied a non-empty
    description wins, which makes descriptions editable simply by posting a better
    one with the next run — and keeps the agent's write path to a single new file.
    """
    order = []
    by_slug = {}
    for run in runs:  # already newest-first
        slug = run.get("variant") or "default"
        if slug not in by_slug:
            by_slug[slug] = {
                "variant": slug,
                "variant_name": run.get("variant_name") or slug,
                "description": "",
                "description_from": None,
                "conclusion": "",
                "status": None,
                "runs": [],
            }
            order.append(slug)
        entry = by_slug[slug]
        desc = (run.get("variant_description") or "").strip()
        if desc and not entry["description"]:
            entry["description"] = desc
            entry["description_from"] = run.get("run_id")
        # The verdict on the idea, as opposed to a single run's outcome. This is the
        # field that makes the tracker worth reading a year later.
        conclusion = (run.get("variant_conclusion") or "").strip()
        if conclusion and not entry["conclusion"]:
            entry["conclusion"] = conclusion
        if not entry["status"] and run.get("variant_status"):
            entry["status"] = run["variant_status"]
        entry["runs"].append(summarise(run))

    variants = []
    for slug in order:
        entry = by_slug[slug]
        entry["run_count"] = len(entry["runs"])
        entry["last_activity"] = _last_activity(entry["runs"])
        variants.append(entry)
    # Most recently active variant first.
    variants.sort(key=lambda v: (v["last_activity"] or ""), reverse=True)
    return variants


def _last_activity(runs):
    stamps = []
    for run in runs:
        for key in ("finished_at", "started_at"):
            if run.get(key):
                stamps.append(run[key])
                break
    return max(stamps) if stamps else None


def build_project(project_dir, slug):
    runs_dir = os.path.join(project_dir, "runs")
    runs = []
    if os.path.isdir(runs_dir):
        for name in sorted(os.listdir(runs_dir)):
            if not name.endswith(".json"):
                continue
            data = read_json(os.path.join(runs_dir, name))
            if data is None:
                continue
            # The filename is authoritative — it is what the URL will point at.
            if data.get("run_id") and data["run_id"] != name[:-5]:
                log("NOTE: %s declares run_id %r; using the filename instead"
                    % (name, data["run_id"]))
            data["run_id"] = name[:-5]
            runs.append(data)

    runs.sort(key=sort_key_desc)

    # Optional hand-written project metadata, for a title and blurb nicer than
    # anything an agent would invent. Never required — so its absence must not be
    # reported as an unreadable file.
    meta_path = os.path.join(project_dir, "project.meta.json")
    meta = (read_json(meta_path) or {}) if os.path.isfile(meta_path) else {}

    name = meta.get("name")
    description = (meta.get("description") or "").strip()
    if not name or not description:
        # Fall back to whatever the newest run claimed.
        for run in runs:
            if not name and run.get("project_name"):
                name = run["project_name"]
            if not description and (run.get("project_description") or "").strip():
                description = run["project_description"].strip()
            if name and description:
                break

    variants = collect_variants(runs)

    # Explicit metric directions beat the name heuristic, and the newest run that
    # states them wins — same upsert rule as the descriptions.
    goals = dict(meta.get("metric_goals") or {})
    for run in reversed(runs):
        stated = run.get("metric_goals")
        if isinstance(stated, dict):
            for key, value in stated.items():
                if value in ("max", "min"):
                    goals[key] = value

    project = {
        "slug": slug,
        "name": name or slug,
        "description": description,
        "repo": meta.get("repo") or _first(runs, lambda r: (r.get("code") or {}).get("remote_url")),
        "primary_metric": meta.get("primary_metric") or _first(runs, lambda r: r.get("primary_metric")),
        "metric_goals": goals,
        "status": meta.get("status") or _first(runs, lambda r: r.get("project_status")),
        "variants": variants,
        "run_count": len(runs),
        "variant_count": len(variants),
        "last_activity": _last_activity([s for v in variants for s in v["runs"]]),
        "statuses": _tally(runs, "status"),
        "tags": _all_tags(runs),
        "metric_keys": _metric_keys(runs),
        # Summed over every run including failures — the compute number nobody can
        # reconstruct after the fact.
        "gpu_hours": _sum_gpu_hours(runs),
    }
    return project, runs


def _sum_gpu_hours(runs):
    total = 0.0
    seen = False
    for run in runs:
        value = (run.get("env") or {}).get("gpu_hours")
        if is_number(value) and value >= 0:
            total += value
            seen = True
    return round(total, 2) if seen else None


def _first(runs, getter):
    for run in runs:
        value = getter(run)
        if value:
            return value
    return None


def _tally(runs, key):
    counts = {}
    for run in runs:
        value = run.get(key) or "unknown"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _all_tags(runs):
    seen = set()
    for run in runs:
        tags = run.get("tags")
        if isinstance(tags, list):
            seen.update(str(t) for t in tags)
    return sorted(seen)


def _metric_keys(runs):
    """Metric names ordered by how often they appear, so the run table can pick
    sensible default columns without being told."""
    counts = {}
    for run in runs:
        metrics = run.get("metrics")
        if isinstance(metrics, dict):
            for key, value in metrics.items():
                if is_number(value):
                    counts[key] = counts.get(key, 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data", help="source data directory")
    parser.add_argument("--out", default="_site/data", help="directory to write")
    args = parser.parse_args()

    projects_root = os.path.join(args.data, "projects")
    out_projects = os.path.join(args.out, "projects")
    if not os.path.isdir(out_projects):
        os.makedirs(out_projects)

    summaries = []
    all_projects = []
    total_runs = 0

    slugs = []
    if os.path.isdir(projects_root):
        slugs = sorted(d for d in os.listdir(projects_root)
                       if os.path.isdir(os.path.join(projects_root, d)))

    for slug in slugs:
        project_dir = os.path.join(projects_root, slug)
        project, runs = build_project(project_dir, slug)
        all_projects.append((slug, project, runs))
        total_runs += len(runs)

        dest = os.path.join(out_projects, slug)
        if not os.path.isdir(dest):
            os.makedirs(dest)
        with open(os.path.join(dest, "project.json"), "w") as fh:
            json.dump(project, fh, indent=1, sort_keys=True)

        # Copy the run files and their patches through verbatim, so the detail view
        # is served same-origin from Pages rather than cross-origin from raw.
        src_runs = os.path.join(project_dir, "runs")
        if os.path.isdir(src_runs):
            dest_runs = os.path.join(dest, "runs")
            if not os.path.isdir(dest_runs):
                os.makedirs(dest_runs)
            for name in os.listdir(src_runs):
                if name.endswith(".json") or name.endswith(".patch"):
                    shutil.copyfile(os.path.join(src_runs, name),
                                    os.path.join(dest_runs, name))

        summaries.append({
            "slug": project["slug"],
            "name": project["name"],
            "description": project["description"],
            "repo": project["repo"],
            "run_count": project["run_count"],
            "variant_count": project["variant_count"],
            "last_activity": project["last_activity"],
            "primary_metric": project["primary_metric"],
            "metric_goals": project["metric_goals"],
            "status": project["status"],
            "gpu_hours": project["gpu_hours"],
            "statuses": project["statuses"],
            "tags": project["tags"],
            "metric_keys": project["metric_keys"],
            # A few recent variant blurbs make the project cards genuinely informative
            # instead of being a wall of identical tiles.
            "variant_preview": [
                {"variant": v["variant"], "name": v["variant_name"],
                 "description": v["description"], "conclusion": v["conclusion"],
                 "run_count": v["run_count"]}
                for v in project["variants"][:3]
            ],
        })

    summaries.sort(key=lambda p: (p["last_activity"] or ""), reverse=True)

    # A cross-project feed. The stated problem is losing track *across* projects, and
    # without this the landing page could only be built by fetching every project
    # document, so it belongs in the one file the page already loads.
    recent = []
    for project_slug, project, runs in all_projects:
        for run in runs:
            recent.append({
                "project": project_slug,
                "project_name": project["name"],
                "variant": run.get("variant"),
                "run_id": run.get("run_id"),
                "run_name": run.get("run_name"),
                "status": run.get("status"),
                "author": run.get("author"),
                "when": run.get("finished_at") or run.get("started_at"),
                "primary_metric": run.get("primary_metric") or project.get("primary_metric"),
                "metrics": dict((k, v) for k, v in (run.get("metrics") or {}).items()
                                if is_number(v)),
            })
    recent.sort(key=lambda r: (r["when"] or ""), reverse=True)
    recent = recent[:60]

    total_gpu_hours = sum(p["gpu_hours"] for p in summaries if p.get("gpu_hours"))
    index = {
        "schema_version": SCHEMA_VERSION,
        "built_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project_count": len(summaries),
        "run_count": total_runs,
        "gpu_hours": round(total_gpu_hours, 2) if total_gpu_hours else None,
        "invalid": INVALID,
        "recent_runs": recent,
        "projects": summaries,
    }
    with open(os.path.join(args.out, "index.json"), "w") as fh:
        json.dump(index, fh, indent=1, sort_keys=True)

    log("%d project(s), %d run(s) -> %s" % (len(summaries), total_runs, args.out))
    if INVALID:
        log("WARNING: %d unreadable file(s); they are listed in index.json" % len(INVALID))
    return 0


if __name__ == "__main__":
    sys.exit(main())
