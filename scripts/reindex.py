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

# One stamp for the whole build, so every document agrees on when it was made.
BUILT_AT = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

# Fields promoted into the index so the browser can render lists, sort and filter
# without fetching every run file. Everything else stays in the run file and is
# loaded on demand when a run is opened.
_SUMMARY_KEYS = (
    "run_id", "run_name", "status", "author", "tags",
    "started_at", "finished_at", "duration_seconds",
    "primary_metric", "metrics", "notes", "seed", "group",
    "conclusion", "hypothesis", "derived_from",
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


def sort_key_newest_first(run):
    """Key for a `reverse=True` sort: newest first, undated runs last.

    The tuple leads with `stamp is not None` so that reversing still leaves the
    undated runs at the end rather than promoting them to the front.
    """
    stamp = parse_ts(run.get("finished_at")) or parse_ts(run.get("started_at"))
    # Strip tzinfo so naive and aware timestamps remain mutually comparable.
    if stamp is not None and stamp.tzinfo is not None:
        stamp = stamp.replace(tzinfo=None)
    return (stamp is not None, stamp or datetime.min, run.get("run_id") or "")


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


# Three relations, deliberately. `derived-from` is the workhorse; `composes` is the
# only intrinsically multi-parent one and the only one drawn differently (a merge);
# `replicates` says the child contains no new idea, so drawing it as a new branch
# would be a lie. Anything else people reach for — "ablation-of", "supersedes",
# "control-for" — is either a status on a node or a fact already implied by an edge.
RELATIONS = ("derived-from", "composes", "replicates")
VARIANT_STATUSES = ("active", "adopted", "refuted", "superseded", "inconclusive",
                    "abandoned", "paused", "done")
VARIANT_ROLES = ("control", "baseline")


def _clean_edges(raw, self_slug):
    """Normalise agent-supplied lineage edges. Never raises on bad input."""
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        parent = str(item.get("variant") or "").strip()
        if not parent or parent == self_slug or parent in seen:
            continue
        seen.add(parent)
        relation = item.get("relation")
        note = item.get("note")
        out.append({
            "variant": parent,
            "relation": relation if relation in RELATIONS else "derived-from",
            "note": (str(note)[:120] if note else None),
        })
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
                "role": None,
                "derived_from": None,
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
        if not entry["status"] and run.get("variant_status") in VARIANT_STATUSES:
            entry["status"] = run["variant_status"]
        if not entry["role"] and run.get("variant_role") in VARIANT_ROLES:
            entry["role"] = run["variant_role"]
        if entry["derived_from"] is None and isinstance(run.get("variant_derived_from"), list):
            entry["derived_from"] = _clean_edges(run["variant_derived_from"], slug)
        entry["runs"].append(summarise(run))

    variants = []
    for slug in order:
        entry = by_slug[slug]
        entry["run_count"] = len(entry["runs"])
        entry["last_activity"] = _last_activity(entry["runs"])
        entry["first_activity"] = _first_activity(entry["runs"])
        variants.append(entry)
    # Oldest first as the fallback; once lineage edges exist the caller re-sorts
    # both this list and the lineage view into the same descent order, so the two
    # tabs can never disagree about ordering.
    variants.sort(key=lambda v: (v["first_activity"] or "~", v["variant"]))
    return variants


# --------------------------------------------------------------------- lineage

def read_curated(project_dir):
    """Hand-written `lineage.json`, which overrides anything an agent asserted.

    This is the only way a human can say "no, that edge is wrong" without editing
    an immutable run file.
    """
    path = os.path.join(project_dir, "lineage.json")
    if not os.path.isfile(path):
        return {}
    data = read_json(path)
    if not isinstance(data, dict):
        return {}
    variants = data.get("variants")
    return variants if isinstance(variants, dict) else {}


def _reachable(adj, start, target):
    """Can `target` be reached from `start`? Iterative, so a malformed graph cannot
    blow the stack."""
    if start == target:
        return True
    seen, stack = set([start]), [start]
    while stack:
        node = stack.pop()
        for nxt in adj.get(node, ()):
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def build_lineage(variants, curated):
    """Turn variant parent declarations into rows the browser can draw directly.

    The frontend does no graph work: no traversal, no layer assignment, no rail
    allocation. It maps these rows to list items and draws the integers.
    """
    order_seen = {}
    for i, v in enumerate(sorted(variants, key=lambda v: (v.get("first_activity") or "~", v["variant"]))):
        order_seen[v["variant"]] = i
    slugs = set(v["variant"] for v in variants)
    by_slug = dict((v["variant"], v) for v in variants)

    # Authority order: what an agent declared, then what a human curated.
    declared = {}
    sources = {"variant": 0, "curated": 0}
    for v in variants:
        if v.get("derived_from"):
            declared[v["variant"]] = list(v["derived_from"])
            sources["variant"] += len(v["derived_from"])
    for child, entry in curated.items():
        if child in slugs and isinstance(entry, dict) and "derived_from" in entry:
            edges = entry["derived_from"]
            if isinstance(edges, list):
                cleaned = _clean_edges(edges, child)
                # An explicit [] is meaningful: "this really is a root".
                declared[child] = cleaned
                sources["curated"] += len(cleaned)

    dropped = []
    candidate = []
    for child in sorted(declared, key=lambda c: (order_seen.get(c, 0), c)):
        for edge in declared[child]:
            if edge["variant"] not in slugs:
                dropped.append({"child": child, "parent": edge["variant"],
                                "reason": "unknown variant"})
                continue
            candidate.append((child, edge))

    # Cycles are refused at insertion rather than detected during traversal, so
    # everything downstream may assume the graph is acyclic.
    parents, adj = {}, {}
    for child, edge in candidate:
        parent = edge["variant"]
        if _reachable(adj, child, parent):
            dropped.append({"child": child, "parent": parent,
                            "reason": "would create a cycle"})
            continue
        parents.setdefault(child, []).append(edge)
        adj.setdefault(parent, []).append(child)

    children = {}
    for child, edges in parents.items():
        for edge in edges:
            children.setdefault(edge["variant"], []).append(
                {"variant": child, "relation": edge["relation"]})
    for key in children:
        children[key].sort(key=lambda c: (order_seen.get(c["variant"], 0), c["variant"]))

    # Longest path from any root, used for the indented fallback and for sorting.
    depth_memo = {}

    def depth(node):
        if node in depth_memo:
            return depth_memo[node]
        depth_memo[node] = 0          # guards against a cycle that slipped through
        best = 0
        for edge in parents.get(node, ()):
            best = max(best, 1 + depth(edge["variant"]))
        depth_memo[node] = best
        return best

    # An indented tree, not a subway map (owner UX verdict 2026-08-21): each
    # variant nests under its PRIMARY parent -- the first declared edge -- and
    # every further parent stays visible as a chip on the row, so the eye reads
    # descent top-to-bottom the way it reads a file tree. Multi-parent nodes
    # (composes) are therefore placed once, under the parent that mattered most.
    primary = {c: edges[0]["variant"] for c, edges in parents.items()}
    tree_children = {}
    for child, par in primary.items():
        tree_children.setdefault(par, []).append(child)
    for k in tree_children:
        tree_children[k].sort(key=lambda c: (order_seen.get(c, 0), c))
    roots = sorted([v for v in slugs if v not in primary],
                   key=lambda v: (order_seen.get(v, 0), v))

    rows = []

    def emit(node, indent, guides, last):
        variant = by_slug[node]
        kids = tree_children.get(node, [])
        def _named(edges):
            out = []
            for e in edges:
                e = dict(e)
                other = by_slug.get(e.get("variant"))
                if other:
                    e["variant_name"] = other.get("variant_name") or e["variant"]
                out.append(e)
            return out

        rows.append({
            "variant": node,
            "variant_name": variant.get("variant_name") or node,
            "indent": indent,
            "guides": list(guides),      # one flag per ancestor level: does that
                                         # ancestor have later siblings (draw a rail)?
            "last": last,                # last child of its parent: elbow, not tee
            "depth": depth(node),
            "primary_parent": primary.get(node),
            "terminal": not kids and not children.get(node),
            "parents": _named(parents.get(node, ())),
            "children": _named(children.get(node, ())),
            "status": variant.get("status"),
            "role": variant.get("role"),
            "run_count": variant.get("run_count", 0),
            "description": variant.get("description", ""),
            "conclusion": variant.get("conclusion", ""),
            "last_activity": variant.get("last_activity"),
        })
        for j, kid in enumerate(kids):
            emit(kid, indent + 1, guides + [not last] if indent else guides + [False],
                 j == len(kids) - 1)

    for i, root in enumerate(roots):
        emit(root, 0, [], i == len(roots) - 1)
    # A node whose primary parent was dropped (unknown slug / cycle refusal)
    # never enters the tree; append it flat rather than lose it.
    placed = set(r["variant"] for r in rows)
    for node in sorted(slugs - placed, key=lambda v: (order_seen.get(v, 0), v)):
        variant = by_slug[node]
        rows.append({
            "variant": node, "variant_name": variant.get("variant_name") or node,
            "indent": 0, "guides": [], "last": True,
            "depth": depth(node), "primary_parent": None,
            "terminal": True, "parents": [dict(e) for e in parents.get(node, ())],
            "children": [dict(c) for c in children.get(node, ())],
            "status": variant.get("status"), "role": variant.get("role"),
            "run_count": variant.get("run_count", 0),
            "description": variant.get("description", ""),
            "conclusion": variant.get("conclusion", ""),
            "last_activity": variant.get("last_activity"),
        })

    edge_count = sum(len(v) for v in parents.values())
    return {
        "available": edge_count > 0,
        "edge_count": edge_count,
        "node_count": len(rows),
        "sources": sources,
        "dropped": dropped,
        "rows": rows,
    }


def build_provenance(runs, variants):
    """Checkpoint provenance, kept strictly apart from idea lineage.

    These are two different graphs and they diverge at exactly the interesting
    nodes: every variants-lab arm warm-started from the same checkpoint, while
    their *ideas* came from the control arm. Merging them would relabel a true
    artifact edge as a false idea edge, so this is rendered as a chip and never
    as a rail.
    """
    slugs = set(v["variant"] for v in variants)
    run_to_variant = {}
    for run in runs:
        if run.get("run_id"):
            run_to_variant[run["run_id"]] = run.get("variant")

    counted = {}
    for run in runs:
        child = run.get("variant")
        refs = run.get("derived_from")
        if not child or not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            parent = run_to_variant.get(ref.get("run_id"))
            if not parent or parent == child or parent not in slugs:
                continue
            key = (child, parent, ref.get("relation") or "derived_from")
            counted[key] = counted.get(key, 0) + 1

    edges = [{"child": c, "parent": p, "relation": r, "run_count": n}
             for (c, p, r), n in sorted(counted.items())]
    return {"edges": edges, "covered": len(set(e["child"] for e in edges))}


def _first_activity(runs):
    stamps = []
    for run in runs:
        for key in ("started_at", "finished_at"):
            if run.get(key):
                stamps.append(run[key])
                break
    return min(stamps) if stamps else None


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

    runs.sort(key=sort_key_newest_first, reverse=True)

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

    observed = set()
    for run in runs:
        metrics = run.get("metrics")
        if isinstance(metrics, dict):
            observed.update(k for k, v in metrics.items() if is_number(v))
    goals = dict((k, v) for k, v in goals.items() if k in observed)

    project = {
        "slug": slug,
        "built_at": BUILT_AT,
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
        # metric_goals accumulates over every run ever posted and never shrinks, so
        # renamed metrics would leave stale directions behind forever. Keep only the
        # keys some run actually reports.
        # Summed over every run including failures — the compute number nobody can
        # reconstruct after the fact.
        "gpu_hours": _sum_gpu_hours(runs),
    }
    curated = read_curated(project_dir)
    project["lineage"] = build_lineage(variants, curated)
    if project["lineage"]["available"]:
        # The Variants tab follows the lineage's descent order (owner UX verdict
        # 2026-08-21): an idea appears right under the idea it came from.
        pos = {r["variant"]: i for i, r in enumerate(project["lineage"]["rows"])}
        variants.sort(key=lambda v: pos.get(v["variant"], 10 ** 6))
        project["variants"] = variants
    project["provenance"] = build_provenance(runs, variants)
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
            "lineage_edges": project["lineage"]["edge_count"],
            "statuses": project["statuses"],
            "tags": project["tags"],
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
        "built_at": BUILT_AT,
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
