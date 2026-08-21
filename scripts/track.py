#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Post one experiment run to the tracker, or inspect what is already there.

    track.py --list                      # every project and variant, with descriptions
    track.py --list <project>            # variants and recent runs in one project
    track.py run.json --dry-run          # show exactly what would be published
    track.py run.json                    # publish it
    cat run.json | track.py -

Design constraints that shaped this file:

* **stdlib only, Python 3.6+.** It runs on an HPC login node where the system
  interpreter is old and nothing can be pip-installed, and equally on a laptop.
* **One new file per run.** Nothing is ever read-modify-written, so two agents
  posting at the same moment cannot lose each other's data.
* **Idempotent.** The run id is derived from the run's own content, so re-running
  after a lost response re-targets the same path instead of creating a duplicate.
* **Nothing secret is ever published.** The payload and the patch are scanned for
  credentials, and machine-specific paths are redacted, before anything is sent.
  Everything posted lands in a public repo permanently.
"""

from __future__ import print_function

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
except ImportError:  # pragma: no cover
    from urllib2 import Request, urlopen, HTTPError

SCHEMA_VERSION = 1
API = "https://api.github.com"

DEFAULT_OWNER = os.environ.get("TRACKER_OWNER", "pe-hy")
DEFAULT_REPO = os.environ.get("TRACKER_REPO", "experiment_tracker")
DEFAULT_BRANCH = os.environ.get("TRACKER_BRANCH", "main")

MAX_PATCH_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_SNAPSHOT_FILES = 10
GIT_TIMEOUT = 20

REQUIRED = ("project", "variant", "variant_description")

# Idea lineage. Three relations only: an agent months from now has to pick one
# correctly and unprompted, so every extra option is a new way to be wrong.
RELATIONS = ("derived-from", "composes", "replicates")
VARIANT_STATUSES = ("active", "adopted", "refuted", "superseded", "inconclusive",
                    "abandoned", "paused", "done")
VARIANT_ROLES = ("control", "baseline")
MIN_DESCRIPTION = 25

# Credential shapes. Anything matching these must never reach a public repo.
SECRET_PATTERNS = (
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}")),
    ("GitHub fine-grained PAT", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{20,}")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.")),
)

# Config keys whose values are suspicious when long. Catches credentials that do
# not match a known vendor prefix.
SECRET_KEY_RE = re.compile(
    r"(?:^|[_.\-])(?:token|secret|password|passwd|api[_-]?key|auth|credential|private[_-]?key)s?"
    r"(?:$|[_.\-])", re.I)


class TrackerError(Exception):
    pass


def eprint(*args):
    print(*args, file=sys.stderr)


# --------------------------------------------------------------------- utilities

def slugify(text, fallback="unnamed"):
    """Filesystem- and URL-safe slug, used verbatim as a directory name.

    Underscores are folded to hyphens so that `chunks_labeling_v2` and
    `chunks-labeling-v2` cannot become two different projects — see also
    `canonical()`, which catches the remaining near-misses.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(text).strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:64] or fallback


def canonical(slug):
    """Aggressive normalisation used only to detect near-duplicate names."""
    return re.sub(r"[^a-z0-9]", "", str(slug).lower())


def utc_now():
    return datetime.utcnow()


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
    r"(Z|[+-]\d{2}:?\d{2})?$")


def normalise_timestamp(value, field):
    """Coerce a timestamp to UTC `...Z`.

    Everything downstream compares timestamps as strings, and `+02:00` sorts before
    `Z` lexicographically — so a mixed-offset corpus would silently order wrongly.
    Normalising once here is what makes the string comparison safe everywhere else.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise TrackerError("%s must be a string timestamp, got %r" % (field, value))
    match = TS_RE.match(value.strip())
    if not match:
        raise TrackerError(
            "%s is not a recognisable timestamp: %r\n"
            "Use UTC ISO-8601, e.g. 2026-08-19T14:32:00Z "
            "(shell: date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)" % (field, value))
    year, month, day, hour, minute, second, offset = match.groups()
    stamp = datetime(int(year), int(month), int(day), int(hour), int(minute),
                     int(second or 0))
    if offset and offset not in ("Z", "z"):
        sign = 1 if offset[0] == "+" else -1
        body = offset[1:].replace(":", "")
        delta_minutes = sign * (int(body[:2]) * 60 + int(body[2:]))
        stamp = stamp - _timedelta_minutes(delta_minutes)
    return iso(stamp)


def _timedelta_minutes(minutes):
    from datetime import timedelta
    return timedelta(minutes=minutes)


def run_cmd(args, cwd=None, timeout=GIT_TIMEOUT):
    """Run a command, returning stripped stdout or None. Never raises, never hangs.

    The timeout matters: these run in project directories that may sit on a network
    filesystem, where a stalled metadata operation would otherwise block forever
    with no output at all.
    """
    try:
        proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except (OSError, ValueError):
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        eprint("  warning: `%s` timed out after %ds; skipping that field"
               % (" ".join(args[:3]), timeout))
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace").rstrip("\n")


def run_git(args, cwd, timeout=GIT_TIMEOUT):
    return run_cmd(["git"] + args, cwd=cwd, timeout=timeout)


# ------------------------------------------------------------------- redaction

def build_redactions():
    """Machine-specific strings to strip from anything we publish.

    These are not secrets, but they are gratuitous: a home directory reveals a
    username and an allocation directory reveals which compute grant paid for the
    work. Neither helps anyone read the results.
    """
    rules = []
    home = os.path.expanduser("~")
    if home and home != "/":
        rules.append((re.compile(re.escape(home)), "$HOME"))
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if user and len(user) > 2:
        rules.append((re.compile(r"/users?/" + re.escape(user) + r"\b"), "$HOME"))
    # Shared-allocation directory layouts, e.g. /scratch/project_465002631/...
    rules.append((re.compile(r"(/(?:scratch|projappl|project|flash|work)/)"
                             r"(?:project_?|proj_?|p)\d{4,}", re.I), r"\1$ALLOC"))
    return rules


def redact(value, rules):
    """Walk a JSON-ish structure applying the redaction rules to every string."""
    if isinstance(value, str):
        for pattern, replacement in rules:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, list):
        return [redact(v, rules) for v in value]
    if isinstance(value, dict):
        return dict((k, redact(v, rules)) for k, v in value.items())
    return value


# --------------------------------------------------------------- secret scanning

def scan_secrets(text, where):
    findings = []
    for label, pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            snippet = match.group(0)
            masked = snippet[:6] + "…" + snippet[-2:] if len(snippet) > 10 else "…"
            findings.append("%s: possible %s (%s)" % (where, label, masked))
    return findings


def scan_payload_keys(obj, findings, path="") -> None:
    """Flag long values under credential-shaped key names."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = "%s.%s" % (path, key) if path else str(key)
            if (SECRET_KEY_RE.search(str(key)) and isinstance(value, str)
                    and len(value.strip()) >= 16):
                findings.append("payload field %r looks like a credential "
                                "(%d chars)" % (here, len(value)))
            scan_payload_keys(value, findings, here)
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            scan_payload_keys(value, findings, "%s[%d]" % (path, i))


def guard_secrets(run, patch_bytes, allow):
    findings = []
    findings.extend(scan_secrets(json.dumps(run), "payload"))
    scan_payload_keys(run, findings)
    if patch_bytes:
        findings.extend(scan_secrets(patch_bytes.decode("utf-8", "replace"), "patch"))
    if not findings:
        return
    eprint("")
    eprint("REFUSING TO PUBLISH — possible credentials detected:")
    for finding in findings:
        eprint("  * " + finding)
    eprint("")
    if allow:
        eprint("--i-have-checked given; continuing anyway.")
        return
    raise TrackerError(
        "This would be committed to a PUBLIC repository permanently, and deleting "
        "the file afterwards does not remove it from git history.\n"
        "Remove the offending values, or pass --i-have-checked if these are false "
        "positives. If a real credential was already exposed, rotate it — do not "
        "rely on deletion.")


# ------------------------------------------------------------------- provenance

def collect_git(repo_path, include_untracked_names=False):
    """Everything we can learn about the code state that produced this run."""
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

    symbolic = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo_path)
    info["detached_head"] = symbolic is None
    info["branch"] = override["branch"] or symbolic or None

    describe = run_git(["describe", "--tags", "--always", "--dirty"], repo_path)
    if describe:
        info["describe"] = describe

    subject = run_git(["log", "-1", "--format=%s"], repo_path)
    if subject:
        info["commit_subject"] = subject[:200]
    commit_time = run_git(["log", "-1", "--format=%cI"], repo_path)
    if commit_time:
        info["commit_time"] = commit_time

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
        info["no_remote"] = True

    status = run_git(["status", "--porcelain", "--untracked-files=no"], repo_path)
    info["dirty"] = bool(status)

    # Untracked file NAMES are opt-in: publishing them discloses what someone is
    # working on (plan and handover notes, scratch files) without adding anything
    # a reader can act on. The count alone is enough to signal "there was more here".
    untracked = run_git(["ls-files", "--others", "--exclude-standard"], repo_path)
    if untracked:
        names = [n for n in untracked.split("\n") if n]
        info["untracked_count"] = len(names)
        if include_untracked_names:
            info["untracked_files"] = names[:100]

    upstream = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                       repo_path)
    if upstream:
        info["upstream"] = upstream
        merge_base = run_git(["merge-base", "HEAD", upstream], repo_path)
        if merge_base:
            info["upstream_commit"] = merge_base

    # Is this commit reachable by anyone else? A SHA that exists on exactly one
    # machine is not provenance, and no other tracker records this.
    if commit:
        containing = run_git(["branch", "--remotes", "--contains", commit], repo_path)
        info["commit_pushed"] = bool(containing)

    return info


def strip_credentials(url):
    """Remove any ``user:token@`` embedded in a remote URL before publishing it."""
    if not url:
        return url
    return re.sub(r"(://)[^/@]*@", r"\1", url)


def parse_remote(url):
    """Return (host, owner, repo, https_url). The host is stored explicitly because
    a self-hosted GitLab and a GitHub Enterprise instance cannot be told apart by
    hostname, and their permalink formats differ."""
    if not url:
        return None, None, None, None
    clean = strip_credentials(url)
    ssh = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$", clean)
    if ssh:
        host, path = ssh.group(1), ssh.group(2)
    else:
        match = re.match(r"^https?://([^/]+)/(.+?)(?:\.git)?/?$", clean)
        if not match:
            return None, None, None, clean
        host, path = match.group(1), match.group(2)
    parts = path.split("/")
    return host, (parts[0] if parts else None), (parts[-1] if len(parts) > 1 else None), \
        "https://%s/%s" % (host, path)


def collect_patch(repo_path, git_info):
    """Uncommitted tracked changes as a raw patch, plus metadata about it.

    --binary and --full-index are what let `git apply --3way` reconstruct the tree
    later; --no-ext-diff and --no-color stop a user's git config from turning the
    patch into something that will not apply.
    """
    if not git_info.get("available") or not git_info.get("dirty"):
        return None, {"patch_kind": "none"}

    patch = run_git([
        "-c", "core.abbrev=no", "diff", "HEAD",
        "--binary", "--full-index", "--no-ext-diff", "--no-color", "-U3",
    ], repo_path, timeout=60)
    if not patch:
        return None, {"patch_kind": "none"}

    raw = patch.encode("utf-8", "replace")
    if not raw.endswith(b"\n"):
        raw += b"\n"

    lines = patch.split("\n")
    meta = {
        "patch_kind": "patch",
        "patch_bytes": len(raw),
        "patch_sha256": hashlib.sha256(raw).hexdigest(),
        "patch_lines": len(lines),
        "patch_files_changed": len([l for l in lines if l.startswith("diff --git ")]),
        "patch_lines_added": len([l for l in lines
                                  if l.startswith("+") and not l.startswith("+++")]),
        "patch_lines_removed": len([l for l in lines
                                    if l.startswith("-") and not l.startswith("---")]),
        "patch_truncated": False,
    }

    if len(raw) > MAX_PATCH_BYTES:
        # Truncate loudly and mark the kind, not just a flag. A shortened patch that
        # no longer applies is worse than none, because it still looks authoritative.
        note = (
            "\n\n*** TRUNCATED by the tracker at %d of %d bytes. ***\n"
            "*** This patch will NOT apply. Reconstruct from commit %s. ***\n"
            % (MAX_PATCH_BYTES, len(raw), git_info.get("commit", "unknown")))
        raw = raw[:MAX_PATCH_BYTES] + note.encode("utf-8")
        meta["patch_truncated"] = True
        meta["patch_kind"] = "too_large"

    return raw, meta


def collect_snapshots(repo_path, paths, rules):
    """Store small text files (the entrypoint, a config, an sbatch script) verbatim.

    This is what makes a run readable when the repo has no reachable remote — a
    commit SHA that resolves nowhere is not a record of anything.
    """
    out = []
    for rel in (paths or [])[:MAX_SNAPSHOT_FILES]:
        candidate = rel if os.path.isabs(rel) else os.path.join(repo_path, rel)
        if not os.path.isfile(candidate):
            eprint("  warning: file to snapshot not found, skipping: %s" % rel)
            continue
        try:
            size = os.path.getsize(candidate)
            if size > MAX_SNAPSHOT_BYTES:
                eprint("  warning: %s is %d bytes, over the %d cap; skipping"
                       % (rel, size, MAX_SNAPSHOT_BYTES))
                continue
            with open(candidate, "rb") as fh:
                blob = fh.read()
            text = blob.decode("utf-8")
        except (IOError, OSError, UnicodeDecodeError) as exc:
            eprint("  warning: cannot snapshot %s (%s)" % (rel, exc))
            continue
        out.append({
            "path": redact(rel, rules),
            "bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
            "content": redact(text, rules),
        })
    return out


def collect_slurm_env():
    slurm = {}
    for var, key in (
        ("SLURM_JOB_ID", "job_id"), ("SLURM_JOB_NAME", "job_name"),
        ("SLURM_JOB_PARTITION", "partition"), ("SLURM_JOB_NODELIST", "nodelist"),
        ("SLURM_JOB_NUM_NODES", "num_nodes"), ("SLURM_NTASKS", "ntasks"),
        ("SLURM_GPUS_ON_NODE", "gpus_on_node"),
        ("SLURM_ARRAY_JOB_ID", "array_job_id"),
        ("SLURM_ARRAY_TASK_ID", "array_task_id"),
        ("SLURM_CLUSTER_NAME", "cluster"), ("SLURM_SUBMIT_DIR", "submit_dir"),
    ):
        if os.environ.get(var):
            slurm[key] = os.environ[var]
    return slurm


def collect_slurm_sacct(job_id):
    """Look a job up after the fact.

    Slurm environment variables exist only inside the job. When a run is recorded
    afterwards from a login node — the common case — this is the only way to learn
    what happened, and it additionally reports TIMEOUT / OUT_OF_MEMORY / CANCELLED,
    which is exactly the information you want about the runs that failed.
    """
    fields = ["JobID", "JobName", "State", "Elapsed", "ExitCode", "NNodes",
              "NodeList", "Partition", "Start", "End"]
    out = run_cmd(["sacct", "-j", str(job_id), "-X", "-P", "-n",
                   "-o", ",".join(fields)], timeout=30)
    if not out:
        return {}
    row = out.split("\n")[0].split("|")
    if len(row) < len(fields):
        return {}
    data = dict(zip([f.lower() for f in fields], row))
    result = {"job_id": data["jobid"], "job_name": data["jobname"],
              "state": data["state"], "elapsed": data["elapsed"],
              "exit_code": data["exitcode"], "num_nodes": data["nnodes"],
              "nodelist": data["nodelist"], "partition": data["partition"]}
    return dict((k, v) for k, v in result.items() if v and v != "Unknown")


SLURM_STATE_TO_STATUS = {
    "COMPLETED": "completed", "FAILED": "failed", "TIMEOUT": "failed",
    "OUT_OF_MEMORY": "failed", "NODE_FAIL": "failed", "CANCELLED": "cancelled",
    "RUNNING": "running", "PENDING": "running",
}


def collect_env(slurm_job_id=None):
    env = {"python": "%d.%d.%d" % sys.version_info[:3]}

    host = os.environ.get("SLURMD_NODENAME") or os.environ.get("HOSTNAME")
    if not host:
        try:
            import socket
            host = socket.gethostname()
        except Exception:
            host = None
    if host:
        env["hostname"] = host

    slurm = collect_slurm_env()
    if slurm_job_id:
        looked_up = collect_slurm_sacct(slurm_job_id)
        if looked_up:
            slurm.update(looked_up)
        else:
            eprint("  warning: sacct returned nothing for job %s" % slurm_job_id)
            slurm.setdefault("job_id", str(slurm_job_id))
    if slurm:
        env["slurm"] = slurm

    container = (os.environ.get("APPTAINER_CONTAINER")
                 or os.environ.get("SINGULARITY_CONTAINER"))
    if container:
        env["container"] = {"path": container}
        name = os.environ.get("APPTAINER_NAME") or os.environ.get("SINGULARITY_NAME")
        if name:
            env["container"]["name"] = name

    return env


# ------------------------------------------------------------------- GitHub I/O

def read_token():
    """Token from the environment or a file. Deliberately not a CLI flag — an
    argument is visible in `ps` and in any transcript that records the command."""
    if os.environ.get("TRACKER_TOKEN"):
        return os.environ["TRACKER_TOKEN"].strip()
    for path in (os.environ.get("TRACKER_TOKEN_FILE"),
                 os.path.expanduser("~/.config/exptracker/token")):
        if path and os.path.isfile(path):
            with open(path, "r") as fh:
                token = fh.read().strip()
            if token:
                return token
    raise TrackerError(
        "No token found. Put one in ~/.config/exptracker/token (chmod 600), or set "
        "$TRACKER_TOKEN. It needs fine-grained 'Contents: Read and write' on %s/%s."
        % (DEFAULT_OWNER, DEFAULT_REPO))


def api_request(url, token, method="GET", body=None, timeout=60):
    req = Request(url, data=body, method=method) if body is not None \
        else Request(url, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "experiment-tracker/1.0")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    return json.loads(urlopen(req, timeout=timeout).read().decode("utf-8"))


def get_file(owner, repo, branch, path, token):
    url = "%s/repos/%s/%s/contents/%s?ref=%s" % (API, owner, repo, path, branch)
    try:
        return api_request(url, token)
    except Exception:
        # Any failure here means "cannot confirm it exists", which is all the
        # caller needs; it must never mask the error it was called to diagnose.
        return None


def put_file(owner, repo, branch, path, content_bytes, message, token,
             attempts=6, verbose=True):
    """PUT one new file, handling every conflict a concurrent writer can cause.

    Unique paths remove the stale-SHA conflict but not the ref race: the Contents
    API commits and pushes server-side against the same branch, so simultaneous
    writes to *different* paths can still return 409.
    """
    url = "%s/repos/%s/%s/contents/%s" % (API, owner, repo, path)
    body = json.dumps({
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": branch,
    }).encode("utf-8")

    last = None
    for attempt in range(1, attempts + 1):
        try:
            return api_request(url, token, method="PUT", body=body)
        except HTTPError as err:
            detail = ""
            try:
                detail = json.loads(err.read().decode("utf-8")).get("message", "")
            except Exception:
                pass
            last = "HTTP %s %s" % (err.code, detail)

            if err.code in (409, 422):
                # Either our own earlier attempt landed and the response was lost,
                # or another writer won the ref race. Only the first is terminal.
                existing = get_file(owner, repo, branch, path, token)
                if existing and existing.get("sha"):
                    if verbose:
                        eprint("  already present — an earlier attempt succeeded")
                    return {"content": existing,
                            "commit": {"html_url": existing.get("html_url")}}
                delay = min(2 ** attempt, 30)
            elif err.code in (403, 429):
                retry_after = err.headers.get("Retry-After") if err.headers else None
                lowered = detail.lower()
                if retry_after and str(retry_after).isdigit():
                    delay = int(retry_after)
                elif "rate limit" in lowered or "abuse" in lowered or "secondary" in lowered:
                    delay = 60
                elif "push protection" in lowered or "secret scanning" in lowered:
                    raise TrackerError(
                        "GitHub push protection rejected this content — it believes "
                        "the payload contains a credential:\n  %s\n"
                        "Do not bypass it. Remove the value and rotate it." % detail)
                else:
                    raise TrackerError(
                        "403 from GitHub: %s\nThe token most likely lacks "
                        "'Contents: Read and write' on %s/%s." % (detail, owner, repo))
            elif err.code == 401:
                raise TrackerError("401 Unauthorized — the token is invalid or expired.")
            elif err.code == 404:
                raise TrackerError(
                    "404 — %s/%s not found, or the token cannot see it." % (owner, repo))
            else:
                raise TrackerError("Unexpected %s" % last)
        except OSError as err:
            # OSError covers socket.timeout and ssl.SSLError as well as URLError.
            # urllib does NOT wrap a timeout during the *response* phase, so without
            # this a lost response escapes as a traceback after the commit landed —
            # and the obvious retry would create a duplicate run.
            last = "network error: %s" % err
            existing = get_file(owner, repo, branch, path, token)
            if existing and existing.get("sha"):
                if verbose:
                    eprint("  network error, but the file did land — treating as success")
                return {"content": existing,
                        "commit": {"html_url": existing.get("html_url")}}
            delay = min(2 ** attempt, 30)

        if attempt < attempts:
            if verbose:
                eprint("  retry %d/%d after %s (waiting %ds)"
                       % (attempt, attempts - 1, last, delay))
            time.sleep(delay)

    raise TrackerError("Gave up after %d attempts. Last error: %s" % (attempts, last))


def fetch_index(owner, repo):
    """Read the published index. Public and unauthenticated, so it costs no quota."""
    url = "https://%s.github.io/%s/data/index.json" % (owner, repo)
    try:
        req = Request(url)
        req.add_header("User-Agent", "experiment-tracker/1.0")
        return json.loads(urlopen(req, timeout=30).read().decode("utf-8"))
    except Exception:
        return None


# ------------------------------------------------------------------ validation

def validate(payload):
    missing = [k for k in REQUIRED if not str(payload.get(k) or "").strip()]
    if missing:
        raise TrackerError(
            "Missing required field(s): %s.\n"
            "'variant_description' is the point of the tracker: it is the sentence "
            "that explains what idea this run tests." % ", ".join(missing))

    desc = payload["variant_description"].strip()
    if len(desc) < MIN_DESCRIPTION:
        raise TrackerError(
            "'variant_description' is only %d characters (%r).\n"
            "Write a real sentence explaining the idea being tested and why you "
            "expected it to help — not a label." % (len(desc), desc))

    metrics = payload.get("metrics")
    if metrics is not None:
        if not isinstance(metrics, dict):
            raise TrackerError("'metrics' must be an object of name -> number.")
        bad = [(k, v) for k, v in metrics.items()
               if v is not None and not isinstance(v, (int, float))
               or isinstance(v, bool)]
        if bad:
            raise TrackerError(
                "These metrics are not numbers: %s\n"
                "Post 0.6412, not \"64.12%%\" or \"0.6412\" — non-numeric values are "
                "dropped from every table and the run would show no results at all."
                % ", ".join("%s=%r" % (k, v) for k, v in bad))

    primary = payload.get("primary_metric")
    if primary and isinstance(metrics, dict) and primary not in metrics:
        eprint("  warning: primary_metric %r is not one of the metrics (%s)"
               % (primary, ", ".join(sorted(metrics)) or "none"))

    goals = payload.get("metric_goals")
    if goals is not None:
        if not isinstance(goals, dict) or any(v not in ("max", "min")
                                              for v in goals.values()):
            raise TrackerError("'metric_goals' must map metric name -> \"max\" or \"min\".")

    status = payload.get("variant_status")
    if status is not None and status not in VARIANT_STATUSES:
        raise TrackerError(
            "'variant_status' must be one of: %s (got %r).\n"
            "Use 'refuted' for an idea that was tested and did not work — those are "
            "the ones worth keeping." % (", ".join(VARIANT_STATUSES), status))

    role = payload.get("variant_role")
    if role is not None and role not in VARIANT_ROLES:
        raise TrackerError("'variant_role' must be one of: %s (got %r)."
                           % (", ".join(VARIANT_ROLES), role))

    lineage = payload.get("variant_derived_from")
    if lineage is not None:
        if not isinstance(lineage, list):
            raise TrackerError("'variant_derived_from' must be a list of objects.")
        for item in lineage:
            if not isinstance(item, dict) or not str(item.get("variant") or "").strip():
                raise TrackerError(
                    "Each entry in 'variant_derived_from' needs a non-empty "
                    "\"variant\" naming the parent variant's slug. Got: %r" % (item,))
            relation = item.get("relation")
            # A typo is caught here rather than silently coerced, because the poster
            # is the last place a human sees the payload.
            if relation is not None and relation not in RELATIONS:
                raise TrackerError(
                    "Unknown relation %r. Use one of: %s.\n"
                    "  derived-from = started from that idea and changed something\n"
                    "  composes     = combines two or more existing variants\n"
                    "  replicates   = same recipe re-run to check the result holds"
                    % (relation, ", ".join(RELATIONS)))
            if str(item.get("variant")).strip() == str(payload.get("variant")).strip():
                raise TrackerError("A variant cannot derive from itself.")


def check_naming(payload, index, force_new):
    """Refuse silent near-duplicates of an existing project or variant.

    Proliferation is the failure mode that makes a tracker useless: an agent with no
    memory of last time invents `grpo-step-rewards` where the previous one wrote
    `grpo-step-level-rewards`, and six months later the history of one idea is
    scattered across four names that no view joins back together.
    """
    if not index:
        return
    project_slug = slugify(payload["project"])
    variant_slug = slugify(payload["variant"])

    projects = index.get("projects") or []
    known_projects = dict((p.get("slug"), p) for p in projects)

    if project_slug not in known_projects:
        # Compare against the display name too. An agent reading a listing sees
        # "demo (Demo (delete me))" and may well pass the human-readable half, which
        # would slugify into a brand new project holding one orphaned run.
        match = _near_match(project_slug,
                            [(p.get("slug"), p.get("slug")) for p in projects] +
                            [(p.get("name"), p.get("slug")) for p in projects])
        if match and not force_new:
            raise TrackerError(
                "Project %r looks like another spelling of the existing project %r.\n"
                "Pass \"project\": \"%s\" to add to it, or --new-name if it really is "
                "a separate project.\nRun `track.py --list` to see what exists."
                % (project_slug, match, match))

    project = known_projects.get(project_slug)
    if not project:
        return

    # variant_preview carries only the most recent few, so use the full project
    # document, which lists every variant.
    full = _fetch_project(index, project_slug)
    variants = (full or {}).get("variants") or project.get("variant_preview") or []
    candidates = ([(v.get("variant"), v.get("variant")) for v in variants] +
                  [(v.get("variant_name") or v.get("name"), v.get("variant"))
                   for v in variants])

    if variant_slug in [v.get("variant") for v in variants]:
        return
    match = _near_match(variant_slug, candidates)
    if match and not force_new:
        raise TrackerError(
            "Variant %r looks like another spelling of the existing variant %r in "
            "project %r.\nPass \"variant\": \"%s\" to add runs to that idea, or "
            "--new-name if this really is a different idea.\n"
            "Run `track.py --list %s` to see what exists."
            % (variant_slug, match, project_slug, match, project_slug))


def _near_match(slug, candidates):
    """Return the existing slug that `slug` collides with, if any."""
    target = canonical(slug)
    if not target:
        return None
    for label, existing in candidates:
        if not label or not existing:
            continue
        if canonical(label) == target and existing != slug:
            return existing
    return None


_PROJECT_CACHE = {}


def _fetch_project(index, slug):
    if slug in _PROJECT_CACHE:
        return _PROJECT_CACHE[slug]
    owner, repo = DEFAULT_OWNER, DEFAULT_REPO
    url = "https://%s.github.io/%s/data/projects/%s/project.json" % (owner, repo, slug)
    try:
        req = Request(url)
        req.add_header("User-Agent", "experiment-tracker/1.0")
        data = json.loads(urlopen(req, timeout=30).read().decode("utf-8"))
    except Exception:
        data = None
    _PROJECT_CACHE[slug] = data
    return data


# ---------------------------------------------------------------- build the run

def build_run(payload, repo_path, args, rules):
    now = utc_now()
    run = dict(payload)

    run["schema_version"] = SCHEMA_VERSION
    run["project"] = slugify(payload["project"])
    run.setdefault("project_name", str(payload["project"]).strip())
    run["variant"] = slugify(payload["variant"])
    run.setdefault("variant_name", str(payload["variant"]).strip())
    run.setdefault("status", "completed")
    run.setdefault("author", os.environ.get("USER") or "unknown")
    run["recorded_at"] = iso(now)

    for field in ("started_at", "finished_at"):
        if run.get(field):
            run[field] = normalise_timestamp(run[field], field)
    if not run.get("started_at") and not run.get("finished_at"):
        run["finished_at"] = iso(now)

    if run.get("duration_seconds") is None and run.get("started_at") and run.get("finished_at"):
        try:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            delta = (datetime.strptime(run["finished_at"], fmt)
                     - datetime.strptime(run["started_at"], fmt))
            run["duration_seconds"] = delta.total_seconds()
        except ValueError:
            pass

    code = dict(run.get("code") or {})
    patch_bytes = None
    if not args.no_code:
        discovered = collect_git(repo_path, args.include_untracked_names)
        for key, value in discovered.items():
            code.setdefault(key, value)
        patch_bytes, patch_meta = collect_patch(repo_path, discovered)
        code.update(patch_meta)
        snapshots = collect_snapshots(repo_path, args.snapshot, rules)
        if snapshots:
            code["snapshots"] = snapshots
    if code:
        run["code"] = code

    env = dict(run.get("env") or {})
    for key, value in collect_env(args.slurm_job_id).items():
        env.setdefault(key, value)

    # A scheduler-reported terminal state is more trustworthy than whatever the
    # caller guessed, but only when the caller did not state one explicitly.
    state = (env.get("slurm") or {}).get("state")
    if state and "status" not in payload:
        mapped = SLURM_STATE_TO_STATUS.get(str(state).split()[0].upper())
        if mapped:
            run["status"] = mapped

    if env.get("n_devices") is None:
        slurm = env.get("slurm") or {}
        try:
            per_node = int(slurm.get("gpus_on_node", 0))
            nodes = int(slurm.get("num_nodes", 1))
            if per_node > 0:
                env["n_devices"] = per_node * nodes
        except (TypeError, ValueError):
            pass

    if env.get("gpu_hours") is None:
        duration, devices = run.get("duration_seconds"), env.get("n_devices")
        if isinstance(duration, (int, float)) and isinstance(devices, int) and devices > 0:
            env["gpu_hours"] = round(duration / 3600.0 * devices, 3)
    if env:
        run["env"] = env

    run = redact(run, rules)

    # The id is derived from the run's own content, so a retry after a lost response
    # targets the same path instead of creating a second copy of the same run.
    stamp = (run.get("finished_at") or run.get("started_at") or iso(now))
    fingerprint = json.dumps({
        "project": run["project"], "variant": run["variant"],
        "run_name": run.get("run_name"), "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"), "metrics": run.get("metrics"),
        "commit": (run.get("code") or {}).get("commit"),
        "slurm": (run.get("env") or {}).get("slurm", {}).get("job_id"),
    }, sort_keys=True)
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:6]
    given = payload.get("run_id")
    run["run_id"] = slugify(given, "run") if given else \
        "%s-%s" % (stamp.replace("-", "").replace(":", ""), digest)

    if patch_bytes:
        run["code"]["patch_file"] = "%s.patch" % run["run_id"]

    return run, patch_bytes


# ------------------------------------------------------------------- listing

def cmd_list(args):
    index = fetch_index(args.owner, args.repo)
    if index is None:
        eprint("Could not read the published index at "
               "https://%s.github.io/%s/data/index.json" % (args.owner, args.repo))
        return 1
    projects = index.get("projects") or []
    if not projects:
        print("No projects tracked yet.")
        return 0

    target = slugify(args.list) if args.list and args.list is not True else None
    print("Tracker: %d project(s), %d run(s), index built %s\n"
          % (index.get("project_count", 0), index.get("run_count", 0),
             index.get("built_at", "?")))

    print('The quoted strings are what to put in "project" and "variant". Copy them '
          'exactly.\nDo not retype the human-readable name — that creates a second '
          'project.\n')

    for project in projects:
        slug = project.get("slug")
        if target and slug != target:
            continue
        print('  "project": "%s"      %s' % (slug, project.get("name") or ""))
        if project.get("description"):
            print("      %s" % _wrap(project["description"], 6))
        print("      %d variants, %d runs, last activity %s"
              % (project.get("variant_count", 0), project.get("run_count", 0),
                 project.get("last_activity") or "never"))
        full = _fetch_project(index, slug) if target else None
        variants = (full or {}).get("variants") or project.get("variant_preview") or []
        for variant in variants:
            name = variant.get("variant") or variant.get("name")
            print('        "variant": "%s"   (%d runs)'
                  % (name, variant.get("run_count", 0)))
            if variant.get("description"):
                print("            %s" % _wrap(variant["description"], 12))
        if not target and project.get("variant_count", 0) > len(variants):
            print("        … run `track.py --list %s` for all %d variants"
                  % (slug, project.get("variant_count", 0)))
        print("")
    print("If this run tests an idea that already has a variant, reuse that variant "
          "slug.\nOnly create a new variant for a genuinely new idea.")
    return 0


def _wrap(text, indent, width=88):
    words = str(text).split()
    lines, current = [], ""
    for word in words:
        if len(current) + len(word) + 1 > width - indent:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return ("\n" + " " * indent).join(lines)


# ------------------------------------------------------------------------ main

def load_payload(source):
    text = sys.stdin.read() if source == "-" else open(source).read()
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise TrackerError("Payload is not valid JSON: %s" % exc)
    if not isinstance(payload, dict):
        raise TrackerError("Payload must be a JSON object.")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Post one experiment run to the tracker.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("payload", nargs="?",
                        help="JSON file describing the run, or '-' for stdin")
    parser.add_argument("--list", nargs="?", const=True, default=None, metavar="PROJECT",
                        help="list existing projects and variants, then exit")
    parser.add_argument("--repo-path", default=".",
                        help="working tree of the project being tracked (default: cwd)")
    parser.add_argument("--snapshot", action="append", default=[], metavar="PATH",
                        help="store this small text file verbatim with the run "
                             "(the training script, a config, an sbatch file); repeatable")
    parser.add_argument("--slurm-job-id", default=None,
                        help="look this job up with sacct and record its real state")
    parser.add_argument("--new-name", action="store_true",
                        help="allow a project/variant name that resembles an existing one")
    parser.add_argument("--include-untracked-names", action="store_true",
                        help="publish the names of untracked files (off by default)")
    parser.add_argument("--i-have-checked", action="store_true",
                        help="publish despite a possible-credential warning")
    parser.add_argument("--no-redact", action="store_true",
                        help="do not rewrite home and allocation paths")
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--no-code", action="store_true",
                        help="skip all git provenance capture")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be published and exit")
    args = parser.parse_args(argv)

    if args.list is not None:
        return cmd_list(args)
    if not args.payload:
        parser.error("a payload file is required (or --list)")

    payload = load_payload(args.payload)
    validate(payload)

    rules = [] if args.no_redact else build_redactions()
    run, patch_bytes = build_run(payload, args.repo_path, args, rules)
    guard_secrets(run, patch_bytes, args.i_have_checked)

    base = "data/projects/%s/runs/%s" % (run["project"], run["run_id"])
    run_json = json.dumps(run, indent=1, sort_keys=True).encode("utf-8")

    if args.dry_run:
        print(json.dumps(run, indent=2, sort_keys=True))
        eprint("")
        eprint("[dry-run] would write %s.json (%d bytes)" % (base, len(run_json)))
        if patch_bytes:
            eprint("[dry-run] would write %s.patch (%d bytes)" % (base, len(patch_bytes)))
        _warn_unreachable(run)
        return 0

    check_naming(payload, fetch_index(args.owner, args.repo), args.new_name)
    token = read_token()

    code = run.get("code") or {}
    if code.get("visibility") is None and code.get("forge") == "github.com":
        visibility = resolve_visibility(code, token)
        if visibility:
            code["visibility"] = visibility
            run["code"] = code
            run_json = json.dumps(run, indent=1, sort_keys=True).encode("utf-8")

    label = "%s/%s" % (run["project"], run["variant"])

    # Patch first: the run JSON must never advertise a patch that failed to upload.
    if patch_bytes:
        eprint("Uploading patch (%d bytes)…" % len(patch_bytes))
        put_file(args.owner, args.repo, args.branch, base + ".patch", patch_bytes,
                 "run %s: uncommitted changes" % label, token)

    eprint("Uploading run %s…" % run["run_id"])
    result = put_file(args.owner, args.repo, args.branch, base + ".json", run_json,
                      "run %s (%s)" % (label, run["run_id"]), token)

    print("Recorded %s" % run["run_id"])
    print("  commit: %s" % (result.get("commit", {}).get("html_url") or "?"))
    print("  page:   https://%s.github.io/%s/#/p/%s/r/%s"
          % (args.owner, args.repo, run["project"], run["run_id"]))
    print("  (the site rebuilds automatically; allow a few minutes)")
    _warn_unreachable(run)
    return 0


def _warn_unreachable(run):
    code = run.get("code") or {}
    if code.get("available") and code.get("commit") and code.get("commit_pushed") is False:
        eprint("")
        eprint("NOTE: commit %s has not been pushed anywhere. The tracker records the "
               "SHA, but nobody else can resolve it. Push the branch, or re-record "
               "with --snapshot pointing at the script and config."
               % (code.get("commit_short") or "")[:10])
    if code.get("no_remote"):
        eprint("")
        eprint("NOTE: this repository has no 'origin' remote, so the commit SHA "
               "resolves nowhere. Consider --snapshot for the files that matter.")


def resolve_visibility(git_info, token):
    """Resolve at write time — the site cannot: GitHub returns 404 rather than 403
    for private repos precisely so they cannot be probed, and the page has no
    credentials anyway."""
    owner, repo = git_info.get("owner"), git_info.get("repo")
    if not owner or not repo or not token:
        return None
    try:
        data = api_request("%s/repos/%s/%s" % (API, owner, repo), token, timeout=20)
        return "private" if data.get("private") else "public"
    except HTTPError as err:
        return "private_or_missing" if err.code == 404 else None
    except Exception:
        return None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TrackerError as exc:
        eprint("error: %s" % exc)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
