#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Post one experiment run to the tracker.

Reads a JSON payload (file or stdin), fills in git/Slurm/environment provenance that
it can determine itself, and writes it to the tracker repo as a new immutable file
via the GitHub Contents API.

    track.py run.json
    cat run.json | track.py -
    track.py run.json --dry-run          # print what would be sent, touch nothing
    track.py run.json --repo-path /path/to/the/project/being/tracked

Design constraints that shaped this file:

* **stdlib only, Python 3.6+.** It has to run on a LUMI login node, where the system
  interpreter is 3.6.15 and nothing can be pip-installed, and equally on a laptop.
* **One new file per run.** Nothing is ever read-modify-written, so two agents posting
  at the same moment cannot lose each other's data.
* **Nothing secret is ever published.** Remote URLs are stripped of any embedded
  ``user:token@`` before being recorded — an easy and real credential-leak path,
  since everything posted here lands in a public repo.
"""

from __future__ import print_function

import argparse
import base64
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime

try:  # py3
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover - py2 safety net
    from urllib2 import Request, urlopen, HTTPError, URLError

SCHEMA_VERSION = 1
API = "https://api.github.com"

DEFAULT_OWNER = "pe-hy"
DEFAULT_REPO = "experiment_tracker"
DEFAULT_BRANCH = "main"

# ClearML spills at 500 KB; we are stricter because everything here is committed to a
# public repo forever and GitHub Pages has a 1 GB ceiling for the whole published site.
MAX_PATCH_BYTES = 256 * 1024
MAX_UNTRACKED_LISTED = 100

TOKEN_PATHS = (
    os.environ.get("TRACKER_TOKEN_FILE"),
    os.path.expanduser("~/.config/exptracker/token"),
)

REQUIRED = ("project", "variant", "variant_description")


class TrackerError(Exception):
    pass


# --------------------------------------------------------------------- utilities

def eprint(*args):
    print(*args, file=sys.stderr)


def slugify(text, fallback="unnamed"):
    """Filesystem- and URL-safe slug. Used for project and variant directory names."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(text).strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-._")
    return slug[:80] or fallback


def utc_now():
    return datetime.utcnow()


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_run_id(when=None):
    """Sortable and collision-free: a UTC stamp plus randomness.

    Two agents posting in the same second still land on different paths, which is the
    property that makes the whole write path conflict-free.
    """
    when = when or utc_now()
    return "%s-%06x" % (when.strftime("%Y%m%dT%H%M%SZ"), random.getrandbits(24))


def run_git(args, cwd, timeout=30):
    """Run a git command, returning stripped stdout or None. Never raises."""
    try:
        proc = subprocess.Popen(
            ["git"] + args, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out, _ = proc.communicate()  # 3.6 has no `timeout=` on Popen.communicate in all builds
        if proc.returncode != 0:
            return None
        return out.decode("utf-8", "replace").rstrip("\n")
    except (OSError, ValueError):
        return None


def strip_credentials(url):
    """Remove any ``user:token@`` embedded in a remote URL.

    Cloning with a PAT baked into the remote is common, and publishing that verbatim
    to a public repo would leak the credential. MLflow does the same thing.
    """
    if not url:
        return url
    return re.sub(r"(://)[^/@]*@", r"\1", url)


def parse_remote(url):
    """Return (forge_host, owner, repo, https_url) for a git remote, or (None,)*4.

    The forge host is recorded explicitly rather than inferred later: a self-hosted
    GitLab and a GitHub Enterprise instance are indistinguishable by hostname alone.
    """
    if not url:
        return None, None, None, None
    clean = strip_credentials(url)
    ssh = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$", clean)
    if ssh:
        host, path = ssh.group(1), ssh.group(2)
        https = "https://%s/%s" % (host, path)
    else:
        https_match = re.match(r"^https?://([^/]+)/(.+?)(?:\.git)?/?$", clean)
        if not https_match:
            return None, None, None, clean
        host, path = https_match.group(1), https_match.group(2)
        https = "https://%s/%s" % (host, path)
    parts = path.split("/")
    owner = parts[0] if parts else None
    repo = parts[-1] if len(parts) > 1 else None
    return host, owner, repo, https


# ------------------------------------------------------------------- provenance

def collect_git(repo_path):
    """Everything we can learn about the code state that produced this run.

    Environment overrides (TRACKER_GIT_*) exist because HPC jobs frequently run from a
    copied or rsync'd tree with no .git at all; the submitting script can pass the real
    provenance through. This mirrors ClearML's CLEARML_VCS_* escape hatch.
    """
    info = {}

    override = {
        "commit": os.environ.get("TRACKER_GIT_COMMIT"),
        "branch": os.environ.get("TRACKER_GIT_BRANCH"),
        "remote_url": os.environ.get("TRACKER_GIT_REMOTE"),
    }

    inside = run_git(["rev-parse", "--is-inside-work-tree"], repo_path)
    if inside != "true":
        info["available"] = False
        info["reason"] = "not a git working tree"
        for key, value in override.items():
            if value:
                info[key] = strip_credentials(value) if key == "remote_url" else value
                info["available"] = True
                info["reason"] = "from TRACKER_GIT_* environment overrides"
        return info

    info["available"] = True
    commit = override["commit"] or run_git(["rev-parse", "HEAD"], repo_path)
    if commit:
        info["commit"] = commit
        info["commit_short"] = commit[:10]

    # A detached HEAD is worth flagging: several trackers silently misreport there.
    symbolic = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo_path)
    info["detached_head"] = symbolic is None
    info["branch"] = override["branch"] or symbolic or None

    # git describe packs tag + distance + sha + dirty into one highly readable string.
    describe = run_git(["describe", "--tags", "--always", "--dirty"], repo_path)
    if describe:
        info["describe"] = describe

    remote_url = override["remote_url"] or run_git(["remote", "get-url", "origin"], repo_path)
    if remote_url:
        host, owner, repo, https = parse_remote(remote_url)
        info["remote_url"] = https
        info["forge"] = host
        if owner:
            info["owner"] = owner
        if repo:
            info["repo"] = repo
    else:
        info["remote_url"] = None
        info["note"] = "no 'origin' remote — this code exists only on the machine that ran it"

    # Tracked-file dirtiness, matching what `git diff HEAD` will actually contain.
    status = run_git(["status", "--porcelain", "--untracked-files=no"], repo_path)
    info["dirty"] = bool(status)

    # Untracked file NAMES only, never contents: contents would publish whatever
    # stray secret or dataset happens to be lying in the working directory.
    untracked = run_git(["ls-files", "--others", "--exclude-standard"], repo_path)
    if untracked:
        names = [n for n in untracked.split("\n") if n]
        info["untracked_count"] = len(names)
        info["untracked_files"] = names[:MAX_UNTRACKED_LISTED]

    # Is this commit actually reachable by anyone else? A SHA that exists only on one
    # login node is not provenance, and nothing else records this.
    upstream = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo_path)
    if upstream:
        info["upstream"] = upstream
        upstream_commit = run_git(["rev-parse", upstream], repo_path)
        if upstream_commit:
            info["upstream_commit"] = upstream_commit
    if commit:
        containing = run_git(["branch", "--remotes", "--contains", commit], repo_path)
        info["commit_pushed"] = bool(containing)

    return info


def collect_patch(repo_path, git_info):
    """Capture uncommitted tracked changes as a raw patch, plus metadata about it.

    The flags matter: --binary and --full-index are what let `git apply --3way`
    reconstruct the tree later; --no-ext-diff and --no-color stop a user's git config
    from corrupting the patch into something unappliable.
    """
    if not git_info.get("available") or not git_info.get("dirty"):
        return None, {}

    patch = run_git([
        "-c", "core.abbrev=no", "diff", "HEAD",
        "--binary", "--full-index", "--no-ext-diff", "--no-color", "-U3",
    ], repo_path)
    if not patch:
        return None, {}

    raw = patch.encode("utf-8", "replace")
    if not raw.endswith(b"\n"):
        raw += b"\n"

    meta = {
        "patch_kind": "patch",
        "patch_bytes": len(raw),
        "patch_sha256": hashlib.sha256(raw).hexdigest(),
        "patch_lines": patch.count("\n") + 1,
        "patch_files_changed": len([l for l in patch.split("\n") if l.startswith("diff --git ")]),
        "patch_lines_added": len([l for l in patch.split("\n")
                                  if l.startswith("+") and not l.startswith("+++")]),
        "patch_lines_removed": len([l for l in patch.split("\n")
                                    if l.startswith("-") and not l.startswith("---")]),
        "patch_truncated": False,
    }

    if len(raw) > MAX_PATCH_BYTES:
        # Truncate loudly. A silently shortened patch that no longer applies is worse
        # than no patch at all, because it still looks authoritative.
        keep = raw[:MAX_PATCH_BYTES]
        note = (
            "\n\n*** TRUNCATED by the tracker at %d bytes of %d. ***\n"
            "*** This patch will NOT apply cleanly. Use commit %s and reconstruct. ***\n"
            % (MAX_PATCH_BYTES, len(raw), git_info.get("commit", "unknown"))
        )
        raw = keep + note.encode("utf-8")
        meta["patch_truncated"] = True

    return raw, meta


def collect_env():
    """Machine, scheduler and container context."""
    env = {}
    env["python"] = "%d.%d.%d" % sys.version_info[:3]

    host = os.environ.get("SLURMD_NODENAME") or os.environ.get("HOSTNAME")
    if not host:
        try:
            import socket
            host = socket.gethostname()
        except Exception:
            host = None
    if host:
        env["hostname"] = host

    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if user:
        env["user"] = user

    slurm = {}
    for var, key in (
        ("SLURM_JOB_ID", "job_id"),
        ("SLURM_JOB_NAME", "job_name"),
        ("SLURM_JOB_PARTITION", "partition"),
        ("SLURM_JOB_NODELIST", "nodelist"),
        ("SLURM_JOB_NUM_NODES", "num_nodes"),
        ("SLURM_NTASKS", "ntasks"),
        ("SLURM_GPUS_ON_NODE", "gpus_on_node"),
        ("SLURM_ARRAY_JOB_ID", "array_job_id"),
        ("SLURM_ARRAY_TASK_ID", "array_task_id"),
        ("SLURM_CLUSTER_NAME", "cluster"),
        ("SLURM_SUBMIT_DIR", "submit_dir"),
    ):
        if os.environ.get(var):
            slurm[key] = os.environ[var]
    if slurm:
        env["slurm"] = slurm

    # Apptainer/Singularity: the .sif path is the only identity available, and its
    # hash is the only thing that is actually immutable.
    container = (os.environ.get("APPTAINER_CONTAINER") or
                 os.environ.get("SINGULARITY_CONTAINER"))
    if container:
        env["container"] = {"path": container}
        name = os.environ.get("APPTAINER_NAME") or os.environ.get("SINGULARITY_NAME")
        if name:
            env["container"]["name"] = name

    return env


# ------------------------------------------------------------------- GitHub I/O

def read_token(explicit=None):
    if explicit:
        return explicit.strip()
    if os.environ.get("TRACKER_TOKEN"):
        return os.environ["TRACKER_TOKEN"].strip()
    for path in TOKEN_PATHS:
        if path and os.path.isfile(path):
            with open(path, "r") as fh:
                token = fh.read().strip()
            if token:
                return token
    raise TrackerError(
        "No token. Set TRACKER_TOKEN, or write one to ~/.config/exptracker/token "
        "(chmod 600). It needs fine-grained 'Contents: Read and write' on "
        "%s/%s." % (DEFAULT_OWNER, DEFAULT_REPO))


def put_file(owner, repo, branch, path, content_bytes, message, token,
             attempts=6, verbose=True):
    """PUT one new file, retrying the conflicts that concurrent writers actually cause.

    Unique paths remove the stale-SHA conflict but not the ref race: the Contents API
    commits and pushes server-side against the same branch, so two simultaneous writes
    to *different* paths can still return 409.
    """
    url = "%s/repos/%s/%s/contents/%s" % (API, owner, repo, path)
    body = json.dumps({
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": branch,
    }).encode("utf-8")

    last = None
    for attempt in range(1, attempts + 1):
        req = Request(url, data=body, method="PUT")
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "experiment-tracker/1.0")
        try:
            resp = urlopen(req, timeout=60)
            return json.loads(resp.read().decode("utf-8"))
        except HTTPError as err:
            detail = ""
            try:
                detail = json.loads(err.read().decode("utf-8")).get("message", "")
            except Exception:
                pass
            last = "HTTP %s %s" % (err.code, detail)

            if err.code in (409, 422):
                # Another writer won the race for the branch ref. Back off and retry.
                delay = min(2 ** attempt, 30) + random.uniform(0, 3)
            elif err.code in (403, 429):
                retry_after = err.headers.get("Retry-After") if err.headers else None
                if retry_after and str(retry_after).isdigit():
                    delay = int(retry_after)
                elif "rate limit" in detail.lower() or "abuse" in detail.lower():
                    delay = 60
                else:
                    # A plain 403 here is a permissions problem and will never succeed.
                    raise TrackerError(
                        "403 from GitHub: %s\nThe token most likely lacks "
                        "'Contents: Read and write' on %s/%s." % (detail, owner, repo))
            elif err.code == 401:
                raise TrackerError("401 Unauthorized — the token is invalid or expired.")
            elif err.code == 404:
                raise TrackerError(
                    "404 — repo %s/%s not found, or the token cannot see it." % (owner, repo))
            else:
                raise TrackerError("Unexpected %s" % last)
        except URLError as err:
            last = "network error: %s" % err.reason
            delay = min(2 ** attempt, 30) + random.uniform(0, 3)

        if attempt < attempts:
            if verbose:
                eprint("  retry %d/%d after %s (%.0fs)" % (attempt, attempts - 1, last, delay))
            time.sleep(delay)

    raise TrackerError("Gave up after %d attempts. Last error: %s" % (attempts, last))


# ------------------------------------------------------------------------ main

def load_payload(source):
    if source == "-":
        text = sys.stdin.read()
    else:
        with open(source, "r") as fh:
            text = fh.read()
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise TrackerError("Payload is not valid JSON: %s" % exc)
    if not isinstance(payload, dict):
        raise TrackerError("Payload must be a JSON object.")
    return payload


def validate(payload):
    missing = [k for k in REQUIRED if not str(payload.get(k) or "").strip()]
    if missing:
        raise TrackerError(
            "Missing required field(s): %s.\n"
            "'variant_description' is the whole point of the tracker — it is the "
            "sentence that explains what idea this run is testing." % ", ".join(missing))
    desc = payload["variant_description"].strip()
    if len(desc) < 15:
        raise TrackerError(
            "'variant_description' is only %d characters (%r). Write a real sentence "
            "explaining the idea being tested, not a label." % (len(desc), desc))


def build_run(payload, repo_path, capture_code=True):
    now = utc_now()
    run = dict(payload)

    run["schema_version"] = SCHEMA_VERSION
    run.setdefault("run_id", make_run_id(now))
    run["project"] = slugify(payload["project"])
    run.setdefault("project_name", str(payload["project"]).strip())
    run["variant"] = slugify(payload["variant"])
    run.setdefault("variant_name", str(payload["variant"]).strip())
    run.setdefault("status", "completed")
    run.setdefault("recorded_at", iso(now))
    run.setdefault("author", os.environ.get("USER") or "unknown")

    if not run.get("started_at") and not run.get("finished_at"):
        run["finished_at"] = iso(now)

    # Derive duration when both ends are known and the caller did not state one.
    if run.get("duration_seconds") is None:
        try:
            start = run.get("started_at")
            end = run.get("finished_at")
            if start and end:
                fmt = "%Y-%m-%dT%H:%M:%SZ"
                delta = datetime.strptime(end, fmt) - datetime.strptime(start, fmt)
                run["duration_seconds"] = delta.total_seconds()
        except (ValueError, TypeError):
            pass

    code = dict(run.get("code") or {})
    patch_bytes = None
    if capture_code:
        discovered = collect_git(repo_path)
        # Anything the caller stated explicitly wins over what we sniffed.
        for key, value in discovered.items():
            code.setdefault(key, value)
        patch_bytes, patch_meta = collect_patch(repo_path, discovered)
        if patch_bytes:
            code.update(patch_meta)
            code["patch_file"] = "%s.patch" % run["run_id"]
    if code:
        run["code"] = code

    env = dict(run.get("env") or {})
    for key, value in collect_env().items():
        env.setdefault(key, value)
    if env:
        run["env"] = env

    return run, patch_bytes


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Post one experiment run to the tracker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("payload", help="JSON file describing the run, or '-' for stdin")
    parser.add_argument("--repo-path", default=".",
                        help="working tree of the project being tracked (default: cwd)")
    parser.add_argument("--owner", default=os.environ.get("TRACKER_OWNER", DEFAULT_OWNER))
    parser.add_argument("--repo", default=os.environ.get("TRACKER_REPO", DEFAULT_REPO))
    parser.add_argument("--branch", default=os.environ.get("TRACKER_BRANCH", DEFAULT_BRANCH))
    parser.add_argument("--token", default=None, help="token (prefer the token file)")
    parser.add_argument("--no-code", action="store_true",
                        help="skip all git provenance capture")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the payload that would be sent and exit")
    args = parser.parse_args(argv)

    payload = load_payload(args.payload)
    validate(payload)
    run, patch_bytes = build_run(payload, args.repo_path, capture_code=not args.no_code)

    base = "data/projects/%s/runs/%s" % (run["project"], run["run_id"])
    run_json = json.dumps(run, indent=1, sort_keys=True).encode("utf-8")

    if args.dry_run:
        print(json.dumps(run, indent=2, sort_keys=True))
        eprint("")
        eprint("[dry-run] would write %s.json (%d bytes)" % (base, len(run_json)))
        if patch_bytes:
            eprint("[dry-run] would write %s.patch (%d bytes)" % (base, len(patch_bytes)))
        return 0

    token = read_token(args.token)
    label = "%s/%s" % (run["project"], run["variant"])

    # The patch goes first, so the run JSON never advertises a patch that failed to
    # upload. A missing patch degrades gracefully; a dangling reference does not.
    if patch_bytes:
        eprint("Uploading patch (%d bytes)…" % len(patch_bytes))
        put_file(args.owner, args.repo, args.branch, base + ".patch", patch_bytes,
                 "run %s: uncommitted changes" % label, token)

    eprint("Uploading run %s…" % run["run_id"])
    result = put_file(args.owner, args.repo, args.branch, base + ".json", run_json,
                      "run %s (%s)" % (label, run["run_id"]), token)

    site = "https://%s.github.io/%s/#/p/%s/r/%s" % (
        args.owner, args.repo, run["project"], run["run_id"])
    print("Recorded %s" % run["run_id"])
    print("  commit: %s" % (result.get("commit", {}).get("html_url") or "?"))
    print("  page:   %s" % site)
    print("  (the site rebuilds automatically; allow a few minutes)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TrackerError as exc:
        eprint("error: %s" % exc)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
