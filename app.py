"""Biometric & Timechamp Attendance Report Generator - local web app.

Runs the existing report pipeline (biometric_timechamp_report.run_report)
behind a small web UI with four hash-routed views:

    #/generate          portal fetch by date range OR upload -> run -> download
    #/dashboard         run-health KPIs + latest report band + recent runs
    #/reports           restart-safe run history list
    #/reports/<id>      per-report analytics summary (workbook read-back)
    #/schedules         daily/weekly/monthly auto-fetch schedules

All report logic lives in the engine module and is unchanged; this server
accepts uploads, runs the engine per job, streams its console output to the
browser, keeps a small history store (workspace/history.json) that survives
restarts, and serves per-report analytics computed by reading back the
finished workbook (report_analytics.py).

Usage:
    python app.py            # serves on http://127.0.0.1:<port> and opens the browser
    python app.py --no-browser

Notes:
    - Runs locally only (127.0.0.1) behind a sign-in window. Credentials come
      from AW_USERNAME / AW_PASSWORD (defaults admin/admin123 are printed on
      startup). This is a light local gate, not a hardened multi-user server:
      keep the app bound to 127.0.0.1.
    - The engine calls the HR API over the internet inside final_adjustments,
      so generating a report requires network access.
"""

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from flask import Flask, abort, jsonify, render_template, request, send_file, session

import fetchers
import mailer
import report_analytics as engine_stats
import gdrive as gdrive_mod

APP_DIR = Path(__file__).resolve().parent
ENGINE = APP_DIR / "biometric_timechamp_report.py"
WORKSPACE = APP_DIR / "workspace"
JOBS_DIR = WORKSPACE / "jobs"
HISTORY_FILE = WORKSPACE / "history.json"
SCHEDULES_FILE = WORKSPACE / "schedules.json"
HISTORY_LIMIT = 100  # newest runs kept (folder + analytics), oldest pruned
SCHEDULES_LIMIT = 50
SCHEDULER_TICK_SECONDS = 30

MAX_JOBS = 12  # live in-memory job states (folders are kept per history)
JOB_TIMEOUT_SECONDS = 900  # generous: HR API + styling of very large files
MAX_UPLOAD_MB = 512
ALLOWED_EXTENSIONS = {".xls", ".xlsx", ".csv"}

# Column markers the engine uses to detect the raw-data flavour (single file).
BIOMETRIC_MARK = "Punch Records"
TIMECHAMP_MARK = "Expected Hours"
MANUAL_MARK = "Manual Hours"

# The engine aborts (sys.exit) when these are missing, so validate up front.
# "User" is accepted in place of "Employee Name": the engine renames User ->
# Employee Name itself, and the portal export ships the column as "User".
TIMECHAMP_REQUIRED = [
    "Date", "Employee Id", "Team Name", "Shift",
    "In Time", "Out Time", "Working Hours", "Away Time", "Idle Time", "Break Time",
]
TIMECHAMP_NAME_ALTS = ("Employee Name", "User")  # at least one must be present
MANUAL_REQUIRED = [
    "Date", "Employee ID", "Employee Name", "Team/Department", "Shift Timing",
    "Login Time", "Logout Time", "Manual Hours", "Break Hours",
]
# Minimum columns for consolidating already-generated report files (>= 2 files).
CONSOLIDATE_REQUIRED = ["SL No.", "Date", "Attendance", "Employee ID", "Employee Name"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
# Local tool: never let the browser keep a stale app.js / style.css for hours.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
# Sign-in stays valid for one day, so reopening the app keeps you logged in.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=1)

# --------------------------------------------------------------------------
# Simple local sign-in. Credentials come from env vars (defaults below are
# printed on startup so nobody gets locked out of a fresh install).
# --------------------------------------------------------------------------
AUTH_USER = os.environ.get("AW_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("AW_PASSWORD", "admin123")
AUTH_USES_DEFAULTS = "AW_USERNAME" not in os.environ or "AW_PASSWORD" not in os.environ


def _load_secret_key():
    key_file = APP_DIR / ".secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()


app.secret_key = _load_secret_key()

# job_id -> dict with status/logs/input paths/output (guarded by JOBS_LOCK)
JOBS_STATE = {}
JOBS_LOCK = threading.Lock()

# Subprocess & cancellation registries (guarded by JOBS_LOCK)
ACTIVE_PROCS = {}  # job_id -> subprocess.Popen
ACTIVE_CANCEL_FLAGS = {}  # job_id -> threading.Event

# Restart-safe run history: list[dict] cached in memory, persisted to
# workspace/history.json (guarded by HISTORY_LOCK).
HISTORY_STORE = None
HISTORY_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Live job helpers (in-memory)
# --------------------------------------------------------------------------

def is_job_cancelled(job_id):
    flag = ACTIVE_CANCEL_FLAGS.get(job_id)
    return flag is not None and flag.is_set()


def cancel_job(job_id):
    """Signal job worker and kill any running engine subprocess."""
    flag = ACTIVE_CANCEL_FLAGS.get(job_id)
    if flag is not None:
        flag.set()
    with JOBS_LOCK:
        proc = ACTIVE_PROCS.get(job_id)
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        job = JOBS_STATE.get(job_id)
        if job is not None and job.get("status") in ("queued", "running"):
            job["status"] = "error"
            job["error"] = "Generation was cancelled by the user."
            job["ended"] = time.time()
            log_line(job_id, "[Cancel] Generation cancelled by user.")
    upsert_history({
        "id": job_id,
        "status": "error",
        "error": "Cancelled by user",
        "ended": time.time(),
    })


def log_line(job_id, line):
    """Append one console line to a job's log (safe from any thread)."""
    with JOBS_LOCK:
        job = JOBS_STATE.get(job_id)
        if job is not None:
            job["logs"].append(line)


def job_error(job_id, message):
    with JOBS_LOCK:
        job = JOBS_STATE.get(job_id)
        if job is not None:
            job["status"] = "error"
            job["error"] = message
            job["ended"] = time.time()
    upsert_history(
        {"id": job_id, "status": "error", "error": message,
         "ended": job["ended"] if job else None}
    )


def job_dir(job_id):
    return JOBS_DIR / job_id


def prune_old_jobs():
    """Drop the oldest live in-memory states (folders stay - history owns them)."""
    with JOBS_LOCK:
        ordered = sorted(
            JOBS_STATE.items(), key=lambda kv: kv[1].get("created", 0), reverse=True
        )
        for job_id, _job in ordered[MAX_JOBS:]:
            JOBS_STATE.pop(job_id, None)


# --------------------------------------------------------------------------
# Persistent run history (workspace/history.json)
# --------------------------------------------------------------------------

def _empty_run(job_id):
    return {
        "id": job_id,
        "created": None, "started": None, "ended": None,
        "status": "queued", "input_type": "—", "display_names": [],
        "error": None,
        "output": None, "output_name": None, "output_folder": None,
        "duration": None,
        "trigger": "Manual upload",
        "range_label": None,
    }


def detect_type(paths):
    """Best-effort flavour label for a run's input files (for history rows)."""
    if len(paths) > 1:
        return "Consolidated"
    try:
        columns = header_columns(paths[0])
    except Exception:
        return "—"
    if BIOMETRIC_MARK in columns:
        return "Biometric"
    if TIMECHAMP_MARK in columns:
        return "Timechamp"
    if MANUAL_MARK in columns:
        return "Manual"
    return "—"


def _legacy_row_from_folder(folder):
    """Build a history row for a finished job folder on disk, or None."""
    if not folder.is_dir():
        return None
    manifest = folder / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        files = json.loads(manifest.read_text(encoding="utf-8")).get("files") or []
    except Exception:
        return None
    output = _find_output_path(folder)
    if output is None:
        return None
    row = _empty_run(folder.name)
    row.update({
        "created": folder.stat().st_mtime,
        "ended": folder.stat().st_mtime,
        "status": "done",
        "display_names": [Path(p).name for p in files],
        "input_type": detect_type(files) if files else "—",
        "output": str(output),
        "output_name": output.name,
        "output_folder": str(output.parent),
    })
    return row


def _adopt_orphan_runs(rows):
    """Merge job folders that finished on disk but are unrecorded in history.

    Covers every way a run can be missing from the list: a server crash before
    the write, a run finished by an older version, or a second app instance.
    Returns how many runs were adopted.
    """
    if not JOBS_DIR.is_dir():
        return 0
    known = {row["id"] for row in rows}
    added = 0
    for folder in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime):
        if folder.name in known:
            continue
        row = _legacy_row_from_folder(folder)
        if row is not None:
            rows.append(row)
            added += 1
    return added


def load_history():
    """History from workspace/history.json, adopting any finished runs on disk."""
    global HISTORY_STORE
    with HISTORY_LOCK:
        if HISTORY_STORE is not None:
            return HISTORY_STORE
        rows = []
        if HISTORY_FILE.is_file():
            try:
                rows = json.loads(
                    HISTORY_FILE.read_text(encoding="utf-8")
                ).get("runs", [])
            except Exception:
                rows = []
        added = _adopt_orphan_runs(rows)
        HISTORY_STORE = rows
        if added:
            _write_history()
        return HISTORY_STORE


def _write_history():
    """Persist the store, pruning runs past HISTORY_LIMIT (folder + row).

    Pruning is local-only: Drive copies are never removed by pruning (only by
    an explicit local delete with delete-with-Drive enabled).
    """
    active = set(JOBS_STATE.keys())
    store = sorted(HISTORY_STORE or [], key=lambda r: r.get("created") or 0)
    kept, removed = store[-HISTORY_LIMIT:], store[:-HISTORY_LIMIT]
    for row in removed:
        # Never delete the folder of a job that is still live (could be running).
        if row["id"] not in active:
            shutil.rmtree(job_dir(row["id"]), ignore_errors=True)
    HISTORY_STORE[:] = kept
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(
        json.dumps({"version": 1, "runs": kept}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def upsert_history(patch):
    """Merge `patch` into the run with the same id (creating it if needed)."""
    rows = load_history()
    with HISTORY_LOCK:
        row = next((r for r in rows if r["id"] == patch["id"]), None)
        if row is None:
            row = _empty_run(patch["id"])
            rows.append(row)
        row.update(patch)
        if row.get("started") and row.get("ended") and row["status"] in ("done", "error"):
            row["duration"] = round(row["ended"] - row["started"], 1)
        _write_history()
    return row


def public_run(row):
    """Shape a history row for the client (dates as epoch seconds + duration)."""
    return {
        "id": row.get("id"),
        "status": row.get("status"),
        "created": row.get("created"),
        "input_type": row.get("input_type") or "—",
        "display_names": row.get("display_names") or [],
        "error": row.get("error"),
        "output_name": row.get("output_name"),
        "output_folder": row.get("output_folder"),
        "duration": row.get("duration"),
        "trigger": row.get("trigger") or "Manual upload",
        "range_label": row.get("range_label"),
    }


def resolve_output(job_id):
    """Return (path, name) for a job from live state or persisted history."""
    with JOBS_LOCK:
        job = JOBS_STATE.get(job_id)
        if job and job.get("output"):
            return job["output"], job.get("output_name")
    for row in load_history():
        if row["id"] == job_id and row.get("output"):
            return row["output"], row.get("output_name")
    return None, None


def resolve_run(job_id):
    """Return a public run record for a job (live state preferred)."""
    with JOBS_LOCK:
        job = JOBS_STATE.get(job_id)
        if job is not None:
            row = _empty_run(job_id)
            row.update({
                "created": job.get("created"),
                "started": job.get("started"),
                "ended": job.get("ended"),
                "status": job.get("status"),
                "display_names": job.get("display_names") or [],
                "input_type": job.get("input_type") or "—",
                "error": job.get("error"),
                "output": job.get("output"),
                "output_name": job.get("output_name"),
                "output_folder": job.get("output_folder"),
            })
            return public_run(row)
    for row in load_history():
        if row["id"] == job_id:
            return public_run(row)
    return None


# --------------------------------------------------------------------------
# Validation / engine helpers
# --------------------------------------------------------------------------

def validation_error(message):
    return jsonify({"ok": False, "error": message}), 400


def header_columns(path):
    """Return the cleaned header row of an uploaded file (no full read)."""
    ext = Path(path).suffix.lower()
    try:
        if ext == ".csv":
            try:
                frame = pd.read_csv(path, nrows=0, encoding="utf-8")
            except Exception:
                frame = pd.read_csv(path, nrows=0, encoding="latin-1")
        else:
            frame = pd.read_excel(path, nrows=0)
    except Exception as exc:  # unreadable/corrupt file
        raise ValueError(f"Could not read “{Path(path).name}”: {exc}") from exc
    columns = []
    for name in frame.columns:
        value = str(name).strip()
        if value and value.lower() != "nan":
            columns.append(value)
    return columns


def validate_inputs(paths):
    """Mirror the engine's input expectations so users get clear messages.

    Returns (ok, message). Single file -> one raw-data flavour must be
    detected (same markers as the engine). Multiple files -> each must be an
    already-generated report (the engine's consolidate branch).
    """
    if len(paths) == 1:
        path = paths[0]
        try:
            columns = header_columns(path)
        except ValueError as exc:
            return False, str(exc)
        name = Path(path).name

        if BIOMETRIC_MARK in columns:
            return True, ""
        if TIMECHAMP_MARK in columns:
            missing = [col for col in TIMECHAMP_REQUIRED if col not in columns]
            if missing:
                return False, (
                    f"“{name}” looks like Timechamp data but is missing columns: "
                    f"{', '.join(missing)}. Export the full Timechamp report and try again."
                )
            if not any(alt in columns for alt in TIMECHAMP_NAME_ALTS):
                return False, (
                    f"“{name}” looks like Timechamp data but has no “Employee Name” "
                    "or “User” column. Export the full Timechamp report and try again."
                )
            return True, ""
        if MANUAL_MARK in columns:
            missing = [col for col in MANUAL_REQUIRED if col not in columns]
            if missing:
                return False, (
                    f"“{name}” looks like manual-tracking data but is missing columns: "
                    f"{', '.join(missing)}. Export the full manual-tracking report and try again."
                )
            return True, ""
        return False, (
            f"“{name}” does not look like supported raw data. Expected one file with a "
            f"“{BIOMETRIC_MARK}”, “{TIMECHAMP_MARK}”, or “{MANUAL_MARK}” column, or several "
            "already-generated report files to consolidate."
        )

    # Multiple files -> consolidate previously generated reports.
    for path in paths:
        try:
            columns = header_columns(path)
        except ValueError as exc:
            return False, str(exc)
        name = Path(path).name
        missing = [col for col in CONSOLIDATE_REQUIRED if col not in columns]
        if missing:
            return False, (
                f"“{name}” is missing report columns needed for consolidation: "
                f"{', '.join(missing)}. Add already-generated Detailed Reports "
                "(from earlier runs), not raw data files."
            )
    return True, ""


def _find_output_path(folder):
    """Locate the newest workbook the engine wrote inside a job folder."""
    best, best_mtime = None, -1.0
    for path in folder.rglob("*.xlsx"):
        if "input" in path.parts:
            continue
        mtime = path.stat().st_mtime
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def find_output(job_id):
    path = _find_output_path(job_dir(job_id))
    return path


def _send_report_email_if_requested(job_id, final_output, email_to, log, schedule_name=None):
    """Send generated report via email if requested; never fails the job."""
    if not email_to:
        return None
    try:
        import mailer
        mail_settings = mailer.load_mail_settings()
        recipients = mail_settings.get("recipients") if email_to == "__DEFAULT__" else email_to
        if not recipients:
            msg = "No recipients specified. Configure default recipients in Settings → Email delivery."
            log(f"[Mail] EMAIL SKIPPED: {msg}")
            with JOBS_LOCK:
                JOBS_STATE[job_id]["email_error"] = msg
            return msg
        mail_settings["recipients"] = recipients
        log(f"[Mail] Emailing report to {recipients}...")
        stats = None
        duration = None
        with JOBS_LOCK:
            started_at = JOBS_STATE[job_id].get("started")
            ended_at = JOBS_STATE[job_id].get("ended")
        if started_at and ended_at:
            duration = int(max(ended_at - started_at, 0))
        try:
            stats = engine_stats.save_analytics(
                Path(final_output), job_dir(job_id) / "analytics.json"
            )
        except Exception as exc:
            log(f"[Mail] (Email stats unavailable: {exc})")
        meta_note = (f"Schedule: {schedule_name}" if schedule_name else "")
        if duration is not None:
            meta_note = (meta_note + " · " if meta_note else "") \
                + f"Run duration {duration // 60}m {duration % 60}s"
        mailer.send_report_email(
            Path(final_output),
            subject=(f"Attendance Report {Path(final_output).stem}"
                     + (f" (schedule: {schedule_name})" if schedule_name else "")),
            settings=mail_settings,
            log=log,
            stats=stats,
            meta_note=meta_note,
        )
        log(f"[Mail] Email delivered to {recipients}.")
        with JOBS_LOCK:
            JOBS_STATE[job_id]["email_sent_to"] = recipients
        return None
    except mailer.MailError as exc:
        mail_error = str(exc)
        log(f"[Mail] EMAIL FAILED: {exc}")
        with JOBS_LOCK:
            JOBS_STATE[job_id]["email_error"] = mail_error
        return mail_error
    except Exception as exc:
        mail_error = str(exc)
        log(f"[Mail] EMAIL FAILED: {exc}")
        with JOBS_LOCK:
            JOBS_STATE[job_id]["email_error"] = mail_error
        return mail_error


# --------------------------------------------------------------------------
# Engine runner
# --------------------------------------------------------------------------

def run_job_worker(job_id):
    """Execute the engine in an isolated subprocess and stream its output."""
    if is_job_cancelled(job_id):
        return
    with JOBS_LOCK:
        paths = list(JOBS_STATE[job_id]["input_paths"])
        JOBS_STATE[job_id]["status"] = "running"
        JOBS_STATE[job_id]["started"] = time.time()
        trigger = JOBS_STATE[job_id].get("trigger") or "Manual upload"
        email_to = JOBS_STATE[job_id].get("email_to")
    upsert_history({"id": job_id, "status": "running", "started": time.time(), "trigger": trigger})

    manifest = {"files": paths, "base_dir": str(job_dir(job_id))}
    manifest_path = job_dir(job_id) / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # Runs the engine's run_report() with console-style output disabled so the
    # log is plain text we can stream to the browser.
    snippet = (
        "import json, sys\n"
        "from biometric_timechamp_report import run_report\n"
        "cfg = json.load(open(sys.argv[1], encoding='utf-8'))\n"
        "print(run_report(cfg['files'], cfg['base_dir'], fancy_progress=False), flush=True)\n"
    )
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        if is_job_cancelled(job_id):
            return
        proc = subprocess.Popen(
            [sys.executable, "-c", snippet, str(manifest_path)],
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            creationflags=creationflags,
        )
        with JOBS_LOCK:
            ACTIVE_PROCS[job_id] = proc

        def pump_output():
            try:
                for raw in proc.stdout:
                    if is_job_cancelled(job_id):
                        break
                    log_line(job_id, raw.rstrip("\r\n"))
            except Exception:
                pass

        pump = threading.Thread(target=pump_output, daemon=True)
        pump.start()
        proc.wait(timeout=JOB_TIMEOUT_SECONDS)
        pump.join(timeout=10)

        if is_job_cancelled(job_id):
            return

        output = find_output(job_id)
        if proc.returncode == 0 and output is not None:
            # Drive backup completes BEFORE the run is marked done, so a
            # local delete afterwards can always find the Drive copies.
            _upload_run_to_drive(job_id, lambda m: log_line(job_id, m))
            with JOBS_LOCK:
                job = JOBS_STATE[job_id]
                job["status"] = "done"
                job["output"] = str(output)
                job["output_name"] = output.name
                job["output_folder"] = str(output.parent)
                job["ended"] = time.time()
            upsert_history({
                "id": job_id,
                "status": "done",
                "ended": job["ended"],
                "output": str(output),
                "output_name": output.name,
                "output_folder": str(output.parent),
            })
            # Optional email delivery if requested
            if email_to:
                _send_report_email_if_requested(
                    job_id, output, email_to, lambda m: log_line(job_id, m)
                )
        else:
            if is_job_cancelled(job_id):
                return
            tail = []
            with JOBS_LOCK:
                tail = JOBS_STATE[job_id]["logs"][-12:]
            message = "The report engine stopped without producing a file."
            meaningful = [ln for ln in tail if ln.strip() and "Styling" not in ln]
            if meaningful:
                message = "\n".join(meaningful[-6:])
            job_error(job_id, message)
    except subprocess.TimeoutExpired:
        proc.kill()
        job_error(
            job_id,
            f"Generation timed out after {JOB_TIMEOUT_SECONDS // 60} minutes. "
            "Try a smaller file or run the original script directly.",
        )
    except Exception as exc:  # unexpected server-side failure
        if not is_job_cancelled(job_id):
            job_error(job_id, f"Server error while running the engine: {exc}")
    finally:
        with JOBS_LOCK:
            ACTIVE_PROCS.pop(job_id, None)
            ACTIVE_CANCEL_FLAGS.pop(job_id, None)


# --------------------------------------------------------------------------
# Pages & API
# --------------------------------------------------------------------------

AUTH_FREE = {"/api/login", "/api/session", "/api/logout"}


@app.before_request
def require_login_for_api():
    """Keep every data endpoint behind the sign-in. The HTML shell and static
    assets stay public; they contain no data and the UI shows its own lock."""
    if request.path.startswith("/api/") and request.path not in AUTH_FREE:
        if not session.get("user"):
            return jsonify({"error": "Authentication required."}), 401


@app.get("/api/session")
def api_session():
    user = session.get("user")
    payload = {"authenticated": bool(user)}
    if user:
        payload["user"] = user
    if not user and AUTH_USES_DEFAULTS:
        payload["hint"] = "Default sign-in: admin / admin123"
    return jsonify(payload), (200 if user else 401)


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if (
        username
        and secrets.compare_digest(username, AUTH_USER)
        and secrets.compare_digest(password, AUTH_PASSWORD)
    ):
        session.clear()
        session.permanent = True  # 1-day session cookie (set above)
        session["user"] = username
        return jsonify({"ok": True, "user": username})
    return jsonify({"ok": False, "error": "Wrong username or password."}), 401


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/")
def index():
    # Cache-bust static assets on every server start so UI fixes reach the
    # browser without a manual hard refresh.
    app.config["ASSET_V"] = str(int(time.time()))
    return render_template("index.html", v=app.config["ASSET_V"])


@app.post("/api/run")
def api_run():
    files = request.files.getlist("files")
    files = [f for f in files if f and f.filename]
    if not files:
        return validation_error("Choose at least one file to generate a report.")

    job_id = uuid.uuid4().hex[:12]
    target_dir = job_dir(job_id)
    input_dir = target_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for uploaded in files:
        name = Path(uploaded.filename).name or "upload"
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            shutil.rmtree(target_dir, ignore_errors=True)
            return validation_error(
                f"“{name}” is not a supported file type. Upload .xls, .xlsx, or .csv files."
            )
        destination = input_dir / name
        uploaded.save(destination)
        saved.append(str(destination))

    ok, message = validate_inputs(saved)
    if not ok:
        shutil.rmtree(target_dir, ignore_errors=True)
        return validation_error(message)

    trigger = (request.form.get("trigger") or "Manual upload").strip()[:40]
    range_label = (request.form.get("range_label") or "").strip()[:120] or None
    drive_upload = str(request.form.get("drive_upload") or "").lower() in ("1", "true", "yes", "on")
    email_to = (request.form.get("email_to") or "").strip() or None

    with JOBS_LOCK:
        ACTIVE_CANCEL_FLAGS[job_id] = threading.Event()
        JOBS_STATE[job_id] = {
            "id": job_id,
            "status": "queued",
            "created": time.time(),
            "started": None,
            "ended": None,
            "logs": [],
            "error": None,
            "input_paths": saved,
            "display_names": [Path(p).name for p in saved],
            "input_type": detect_type(saved),
            "output": None,
            "output_name": None,
            "output_folder": None,
            "trigger": trigger,
            "range_label": range_label,
            "drive_upload": drive_upload,
            "email_to": email_to,
        }
    upsert_history({
        "id": job_id,
        "status": "queued",
        "created": time.time(),
        "input_type": JOBS_STATE[job_id]["input_type"],
        "display_names": JOBS_STATE[job_id]["display_names"],
        "trigger": trigger,
        "range_label": range_label,
    })
    prune_old_jobs()

    threading.Thread(target=run_job_worker, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


def _upload_run_to_drive(job_id, log):
    """Best-effort Drive upload of a finished run's raws + reports.

    Enabled per-run via JOBS_STATE['drive_upload'] (set from the request or
    the schedule), gated by the global Drive settings. Never fails the run.
    Files land in <folder>/YYYY/Month/<day-range>/RawData and /Report.
    """
    with JOBS_LOCK:
        want = bool(JOBS_STATE.get(job_id, {}).get("drive_upload"))
    cfg = gdrive_mod.load_drive_settings()
    if not (want and cfg.get("enabled")):
        return
    jd = job_dir(job_id)
    raws = sorted(
        str(p) for p in (jd / "input").glob("*")
        if p.is_file() and p.suffix.lower() in (".csv", ".xls", ".xlsx")
    ) if (jd / "input").is_dir() else []
    if not raws:
        raws = sorted(
            str(p) for p in jd.rglob("*")
            if p.is_file() and p.suffix.lower() in (".csv", ".xls", ".xlsx")
            and "input" not in p.parts and p.suffix.lower() == ".csv"
        )
    reports = sorted(
        str(p) for p in jd.rglob("*")
        if p.is_file() and p.suffix.lower() == ".xlsx"
        and "input" not in p.parts and not p.name.lower().startswith("monthly")
    )
    if not raws and not reports:
        return
    try:
        log("[Drive] Backing up raw data and reports to Google Drive...")
        from_day, to_day = _job_range_days(job_id)
        summary = gdrive_mod.upload_run(
            jd, raws if cfg.get("upload_raw") else [],
            reports if cfg.get("upload_reports") else [], log=log,
            from_day=from_day, to_day=to_day,
        )
        with JOBS_LOCK:
            JOBS_STATE[job_id]["drive_result"] = summary
        log(
            f"[Drive] Backup complete: {len(summary['raw'])} raw file(s), "
            f"{len(summary['reports'])} report(s) → folder “{summary['folder']}”."
        )
    except gdrive_mod.DriveError as exc:
        log(f"[Drive] UPLOAD FAILED: {exc}")
        with JOBS_LOCK:
            JOBS_STATE[job_id]["drive_error"] = str(exc)
    except Exception as exc:
        log(f"[Drive] UPLOAD FAILED: {exc}")
        with JOBS_LOCK:
            JOBS_STATE[job_id]["drive_error"] = str(exc)


def _job_range_days(job_id):
    """(from_day, to_day) for a job from its range_label, else (None, None)."""
    with JOBS_LOCK:
        label = (JOBS_STATE.get(job_id, {}) or {}).get("range_label") or ""
    if "–" in label:
        left, right = label.split("–", 1)
        try:
            return (
                datetime.strptime(left.strip(), "%d-%b-%Y").date(),
                datetime.strptime(right.strip(), "%d-%b-%Y").date(),
            )
        except ValueError:
            return None, None
    try:
        day = datetime.strptime(label.strip(), "%d-%b-%Y").date()
        return day, day
    except ValueError:
        return None, None


def _schedule_range(schedule):
    """(from_day, to_day) a schedule should fetch.

    ``range_days == 'full'`` means the entire previous calendar month with
    its real length (28/29/30/31 days auto-detected). Otherwise the last N
    days up to yesterday (N == 1 -> yesterday only).
    """
    today = date.today()
    yesterday = today - timedelta(days=1)
    raw_range = str(schedule.get("range_days")).strip().lower()
    if raw_range == "full":
        first_of_this_month = yesterday.replace(day=1)
        from_day = first_of_this_month - timedelta(days=1)  # last day of prev month
        from_day = from_day.replace(day=1)                  # first day of prev month
        return from_day, first_of_this_month - timedelta(days=1)
    # Legacy rows saved with the removed "workday" range now fetch yesterday,
    # same as range_days == 1 (day selection still controls WHEN they run).
    try:
        days = max(1, int(schedule.get("range_days") or 1))
    except (TypeError, ValueError):
        days = 1  # includes legacy "workday" rows
    return (today - timedelta(days=days), yesterday)


# --------------------------------------------------------------------------
# Portal fetch (date-range download -> straight into the same job pipeline)
# --------------------------------------------------------------------------

def _parse_range(body):
    """Extract + validate {from, to} dates from a JSON body."""
    try:
        from_day = date.fromisoformat(str(body.get("from") or ""))
        to_day = date.fromisoformat(str(body.get("to") or ""))
    except ValueError:
        raise ValueError("Pick a valid From and To date for the fetch.")
    return from_day, to_day


def _engine_snippet(manifest_path):
    """The subprocess program that runs the engine without console progress."""
    return (
        "import json, sys\n"
        "from biometric_timechamp_report import run_report\n"
        "cfg = json.load(open(sys.argv[1], encoding='utf-8'))\n"
        "run_report(cfg['files'], cfg['base_dir'], fancy_progress=False)\n"
    )


def _run_engine_sync(job_id, files, log):
    """Run the engine synchronously on `files`; returns the output path.

    Streams engine stdout into the job's log as it goes. Raises ValueError
    with the meaningful log tail on failure (the job folder is kept).
    """
    if is_job_cancelled(job_id):
        raise ValueError("Generation was cancelled by the user.")

    manifest = {"files": [str(f) for f in files], "base_dir": str(job_dir(job_id))}
    manifest_path = job_dir(job_id) / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", _engine_snippet(manifest_path), str(manifest_path)],
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            creationflags=creationflags,
        )
        with JOBS_LOCK:
            ACTIVE_PROCS[job_id] = proc

        for raw in proc.stdout:
            if is_job_cancelled(job_id):
                try:
                    proc.kill()
                except Exception:
                    pass
                raise ValueError("Generation was cancelled by the user.")
            log(raw.rstrip("\r\n"))
        code = proc.wait(timeout=JOB_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise ValueError(
            f"The report engine timed out after {JOB_TIMEOUT_SECONDS // 60} minutes."
        )
    except Exception as exc:
        if is_job_cancelled(job_id):
            raise ValueError("Generation was cancelled by the user.")
        raise ValueError(f"Could not start the report engine: {exc}")
    finally:
        with JOBS_LOCK:
            ACTIVE_PROCS.pop(job_id, None)

    if is_job_cancelled(job_id):
        raise ValueError("Generation was cancelled by the user.")

    output = find_output(job_id)
    if code != 0 or output is None:
        tail = [ln for ln in (job_logs_tail(job_id, 10)) if ln.strip()]
        raise ValueError(
            "\n".join(tail[-5:]) or "The report engine stopped without producing a file."
        )
    return output


def job_logs_tail(job_id, count):
    with JOBS_LOCK:
        job = JOBS_STATE.get(job_id)
        return list(job["logs"][-count:]) if job else []


def _fetched_job_worker(job_id, sources, from_day, to_day, schedule_name=None,
                        email_to=None, drive_upload=False):
    """Full portal-fetch pipeline for ONE job, run in a thread:

    login + download each source -> validate -> run the engine per raw file
    -> consolidate the generated reports -> final consolidated report ->
    optional email with the report attached.
    """
    def log(message):
        log_line(job_id, message)

    with JOBS_LOCK:
        JOBS_STATE[job_id]["status"] = "running"
        JOBS_STATE[job_id]["started"] = time.time()
        trigger = JOBS_STATE[job_id].get("trigger") or "Portal fetch"
    upsert_history({"id": job_id, "status": "running", "started": time.time(), "trigger": trigger})

    input_dir = job_dir(job_id) / "input"
    try:
        # ---- Phase 1: portal downloads -----------------------------------
        saved = []
        for source in sources:
            if is_job_cancelled(job_id):
                raise ValueError("Generation was cancelled by the user.")
            label = "Biometric" if source == "biometric" else "TimeChamp"
            log(f"[{label}] Login & raw data download starting...")
            path, _label = fetchers.FETCHERS[source](
                input_dir, from_day, to_day,
                fetchers.load_settings().get(source), log=log,
            )
            if is_job_cancelled(job_id):
                raise ValueError("Generation was cancelled by the user.")
            # Each raw file is validated alone: two raw flavours together
            # would look like the engine's consolidate branch (which needs
            # already-generated reports, not raw data).
            ok, message = validate_inputs([str(path)])
            if not ok:
                raise ValueError(message)
            saved.append(str(path))
        log(f"Raw data ready: {len(saved)} file(s) validated against the engine.")

        # ---- Phase 2: engine per raw file ---------------------------------
        generated = []
        if len(saved) == 1:
            if is_job_cancelled(job_id):
                raise ValueError("Generation was cancelled by the user.")
            log("Processing raw data through the report engine...")
            output = _run_engine_sync(job_id, saved, log)
            generated.append(output)
        else:
            for raw in saved:
                if is_job_cancelled(job_id):
                    raise ValueError("Generation was cancelled by the user.")
                label = ("Biometric" if "biometric" in Path(raw).name
                         else "Timechamp")
                log(f"Processing {label} raw data through the report engine...")
                output = _run_engine_sync(job_id, [raw], log)
                generated.append(output)
                log(f"{label} report generated: {Path(output).name}")

        # ---- Phase 3: consolidate into the final report -------------------
        if is_job_cancelled(job_id):
            raise ValueError("Generation was cancelled by the user.")
        final_output = generated[0]
        if len(generated) > 1:
            log("Consolidating generated reports into the final combined report...")
            final_output = _run_engine_sync(job_id, generated, log)
            log(f"Final consolidated report: {Path(final_output).name}")

        if is_job_cancelled(job_id):
            raise ValueError("Generation was cancelled by the user.")

        # ---- Phase 4: Google Drive backup (raw + reports) -----------------
        _upload_run_to_drive(job_id, log)

        with JOBS_LOCK:
            job = JOBS_STATE[job_id]
            job["status"] = "done"
            job["output"] = str(final_output)
            job["output_name"] = Path(final_output).name
            job["output_folder"] = str(Path(final_output).parent)
            job["ended"] = time.time()
        upsert_history({
            "id": job_id,
            "status": "done",
            "ended": job["ended"],
            "output": str(final_output),
            "output_name": Path(final_output).name,
            "output_folder": str(Path(final_output).parent),
        })

        # ---- Phase 5: optional email with the report attached -------------
        if email_to:
            _send_report_email_if_requested(
                job_id, final_output, email_to, log, schedule_name=schedule_name
            )
        return
    except ValueError as exc:
        if is_job_cancelled(job_id):
            return
        job_error(job_id, str(exc))
    except Exception as exc:  # unexpected
        if is_job_cancelled(job_id):
            return
        job_error(job_id, f"Unexpected server error: {exc}")
    finally:
        with JOBS_LOCK:
            ACTIVE_PROCS.pop(job_id, None)
            ACTIVE_CANCEL_FLAGS.pop(job_id, None)


def _start_fetch_job(sources, from_day, to_day, trigger, schedule_name=None,
                     email_to=None, drive_upload=False):
    """Create ONE job that fetches + processes + consolidates in a thread."""
    job_id = uuid.uuid4().hex[:12]
    target_dir = job_dir(job_id)
    input_dir = target_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    range_label = f"{from_day.strftime('%d-%b-%Y')} – {to_day.strftime('%d-%b-%Y')}"
    display_names = [
        f"{ 'Biometric' if s == 'biometric' else 'Timechamp' } portal ({range_label})"
        for s in sources
    ]
    input_type = ("Biometric" if sources == ["biometric"]
                  else "Timechamp" if sources == ["timechamp"]
                  else "Consolidated")
    with JOBS_LOCK:
        ACTIVE_CANCEL_FLAGS[job_id] = threading.Event()
        JOBS_STATE[job_id] = {
            "id": job_id,
            "status": "queued",
            "created": time.time(),
            "started": None,
            "ended": None,
            "logs": [],
            "error": None,
            "input_paths": [],
            "display_names": display_names,
            "input_type": input_type,
            "output": None,
            "output_name": None,
            "output_folder": None,
            "trigger": trigger,
            "range_label": range_label,
            "schedule": schedule_name,
            "drive_upload": bool(drive_upload),
            "email_to": email_to,
        }
    upsert_history({
        "id": job_id,
        "status": "queued",
        "created": time.time(),
        "input_type": input_type,
        "display_names": display_names,
        "trigger": trigger,
        "range_label": range_label,
    })
    prune_old_jobs()

    threading.Thread(
        target=_fetched_job_worker,
        args=(job_id, sources, from_day, to_day, schedule_name, email_to),
        kwargs={"drive_upload": bool(drive_upload)},
        daemon=True,
    ).start()
    return job_id


@app.post("/api/fetch")
def api_fetch():
    """Fetch raw data from the selected portals for a date range and run it.

    One job does the whole pipeline: portal logins + downloads -> engine per
    source -> consolidation -> optional email. With both sources selected the
    final workbook is “Biometric and Timechamp Consolidated Report …”.
    """
    body = request.get_json(silent=True) or {}
    sources = body.get("sources") or []
    sources = [s for s in sources if s in fetchers.FETCHERS]
    if not sources:
        return validation_error("Select at least one source: Biometric or TimeChamp.")
    try:
        from_day, to_day = _parse_range(body)
    except ValueError as exc:
        return validation_error(str(exc))

    email_to = str(body.get("email_to") or "").strip() or None
    drive_upload = bool(body.get("drive_upload"))
    try:
        job_id = _start_fetch_job(sources, from_day, to_day, trigger="Portal fetch",
                                  email_to=email_to, drive_upload=drive_upload)
    except ValueError as exc:
        return validation_error(str(exc))
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/api/mail-settings")
def api_mail_settings_get():
    return jsonify({"ok": True, "settings": mailer.public_mail_settings()})


@app.put("/api/mail-settings")
def api_mail_settings_put():
    body = request.get_json(silent=True) or {}
    # Accept both the nested form ({mail: {...}}) sent by the UI and a flat dict.
    mail_block = body.get("mail") if isinstance(body.get("mail"), dict) else body
    saved = mailer.save_mail_settings(mail_block)
    return jsonify({"ok": True, "settings": mailer.public_mail_settings()})


@app.post("/api/mail-test")
def api_mail_test():
    """Send a small test email to verify the SMTP configuration."""
    body = request.get_json(silent=True) or {}
    settings = mailer.load_mail_settings()
    if body.get("recipients"):
        settings["recipients"] = body["recipients"]
    # Preview the real design: reuse the latest successful run's stats.
    stats = None
    try:
        runs = [r for r in load_history() if r.get("status") == "done" and r.get("output")]
        if runs:
            latest = max(runs, key=lambda r: r.get("ended") or 0)
            cache = job_dir(latest["id"]) / "analytics.json"
            if cache.is_file():
                stats = json.loads(cache.read_text(encoding="utf-8"))
            elif Path(latest["output"]).is_file():
                stats = engine_stats.analyze_workbook(Path(latest["output"]))
    except Exception:
        stats = None
    try:
        recipients = mailer.send_report_email(
            None,
            subject="Attendance Workbench — test email",
            body="Mail is configured correctly. Reports can be emailed automatically.",
            settings=settings,
            stats=stats,
            meta_note="This is how scheduled report emails will look.",
        )
    except mailer.MailError as exc:
        return validation_error(str(exc))
    return jsonify({"ok": True, "sent_to": recipients})


# --------------------------------------------------------------------------
# Google Drive backup (service account)
# --------------------------------------------------------------------------

@app.get("/api/drive-settings")
def api_drive_settings_get():
    return jsonify({"ok": True, "settings": gdrive_mod.public_drive_settings()})


@app.put("/api/drive-settings")
def api_drive_settings_put():
    body = request.get_json(silent=True) or {}
    gdrive_mod.save_drive_settings(body)
    gdrive_mod.reset_service()
    return jsonify({"ok": True, "settings": gdrive_mod.public_drive_settings()})


@app.post("/api/drive-test")
def api_drive_test():
    """Open the configured folder and write+delete a probe file."""
    try:
        result = gdrive_mod.test_connection()
    except gdrive_mod.DriveError as exc:
        return validation_error(str(exc))
    return jsonify({"ok": True, **result})


@app.post("/api/drive-monthly")
def api_drive_monthly():
    """Start a tracked monthly-compilation job; logs stream to the dialog."""
    body = request.get_json(silent=True) or {}
    try:
        year = int(body.get("year"))
        month = int(body.get("month"))
        if not 1 <= month <= 12 or not 2000 <= year <= 2100:
            raise ValueError
    except (TypeError, ValueError):
        return validation_error("Pick a valid month and year to compile.")
    job_id = _start_compile_job(None, year, month)
    return jsonify({"ok": True, "job_id": job_id, "mode": "monthly_compile"})


@app.get("/api/portal-settings")
def api_portal_settings_get():
    return jsonify({"ok": True, "settings": fetchers.public_settings()})


@app.put("/api/portal-settings")
def api_portal_settings_put():
    body = request.get_json(silent=True) or {}
    current = fetchers.load_settings()
    for source in ("biometric", "timechamp"):
        incoming = body.get(source) or {}
        for field in ("base_url", "username", "password"):
            if field in incoming and str(incoming[field]).strip() != "":
                # Empty password means "keep the stored one" (masked in UI).
                if field == "password" and incoming[field] is None:
                    continue
                current[source][field] = str(incoming[field])
    fetchers.save_settings(current)
    if isinstance(body.get("mail"), dict):
        mailer.save_mail_settings(body["mail"])
    return jsonify({
        "ok": True,
        "settings": fetchers.public_settings(),
        "mail": mailer.public_mail_settings(),
    })


# --------------------------------------------------------------------------
# Schedules: daily / weekly / monthly auto-fetch + auto-generate
# --------------------------------------------------------------------------

SCHEDULES_STORE = None
SCHEDULES_LOCK = threading.RLock()  # reentrant: loaders + routes both take it
FREQ_LABEL = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _prev_month():
    """(year, month) of the previous calendar month."""
    prev = date.today().replace(day=1) - timedelta(days=1)
    return prev.year, prev.month


def _load_schedules():
    global SCHEDULES_STORE
    with SCHEDULES_LOCK:
        if SCHEDULES_STORE is not None:
            return SCHEDULES_STORE
        rows = _read_schedule_rows(SCHEDULES_FILE)
        if rows is None:  # main file missing or corrupt -> last good copy
            rows = _read_schedule_rows(SCHEDULES_FILE.with_suffix(".json.bak")) or []
        SCHEDULES_STORE = rows
        _heal_schedule_rows(rows)
        return SCHEDULES_STORE


def _read_schedule_rows(path):
    """Parsed schedules from a store file; None only when it exists but is
    unreadable (torn write / corruption)."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("schedules", [])
    except Exception:
        return None


def _sane_next_run(value, schedule):
    """Return a trustworthy epoch-seconds next_run for a stored row.

    Builds before 2026-09-19 stored this value in milliseconds/microseconds;
    those rows displayed garbage dates ("Nov 6, 9:30 AM, 58691" - a year of
    58691 means µs read as seconds) and never fired, because _schedule_due
    compares against seconds. Anything implausible is recomputed fresh.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = None
    if value is not None and value == value and abs(value) != float("inf"):
        while value > 1e11:  # ms/µs epochs collapse into seconds here
            value /= 1000.0
        now = time.time()
        if now - 366 * 86400 <= value <= now + 366 * 86400:
            return round(value)
    try:
        return compute_next_run(schedule)
    except Exception:
        return None


def _heal_schedule_rows(rows):
    """Normalize legacy/corrupt next_run values once per load; rewrites the
    store only when a value actually changed."""
    changed = False
    for row in rows:
        fixed = _sane_next_run(row.get("next_run"), row)
        if fixed != row.get("next_run"):
            row["next_run"] = fixed
            changed = True
    if changed:
        _write_schedules()


def _write_schedules():
    # Atomic write + keep the last good copy: a crash or power cut mid-write
    # must never leave a truncated/empty schedules.json behind (that silently
    # deletes every schedule).
    SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "schedules": SCHEDULES_STORE}, indent=2)
    with SCHEDULES_LOCK:
        tmp = SCHEDULES_FILE.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        if SCHEDULES_FILE.is_file() and SCHEDULES_FILE.stat().st_size > 2:
            shutil.copy2(SCHEDULES_FILE, SCHEDULES_FILE.with_suffix(".json.bak"))
        os.replace(tmp, SCHEDULES_FILE)


def _validate_schedule(body, existing_id=None):
    """Normalize + validate one schedule payload. Raises ValueError."""
    name = str(body.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("Give the schedule a name.")
    frequency = str(body.get("frequency") or "").strip().lower()
    if frequency not in FREQ_LABEL:
        raise ValueError("Frequency must be daily, weekly, or monthly.")
    time_text = str(body.get("time") or "").strip()
    try:
        time_part = datetime.strptime(time_text, "%H:%M").time()
    except ValueError:
        raise ValueError("Pick a run time (HH:MM, 24-hour).")

    weekday = body.get("weekday")
    month_day = body.get("month_day")
    if frequency == "weekly":
        try:
            weekday = int(weekday)
        except (TypeError, ValueError):
            raise ValueError("Pick a day of the week for weekly schedules.")
        if not 0 <= weekday <= 6:
            raise ValueError("Weekday must be between Monday (0) and Sunday (6).")
        month_day = None
    elif frequency == "monthly":
        try:
            month_day = int(month_day)
        except (TypeError, ValueError):
            raise ValueError("Pick a day of the month for monthly schedules.")
        if not 1 <= month_day <= 31:
            raise ValueError("Day of month must be between 1 and 31.")
        weekday = None
    else:
        weekday = month_day = None

    # Multi-day sending for the previous-working-day range: a list of weekdays
    # (Mon=0..Sun=6). For weekly schedules an empty/None list keeps the old
    # single-weekday behaviour; for daily schedules it limits which days the
    # schedule fires on (an empty list = every day).
    raw_send_days = body.get("send_days")
    if isinstance(raw_send_days, (list, tuple)):
        send_days = sorted({int(d) for d in raw_send_days if isinstance(d, (int, float)) and 0 <= int(d) <= 6})
    else:
        send_days = []
    if send_days and frequency not in ("daily", "weekly"):
        send_days = []  # only meaningful for daily/weekly schedules
    if frequency == "weekly" and not send_days:
        send_days = [weekday]  # weekly always fires on its weekday

    sources = [s for s in (body.get("sources") or []) if s in fetchers.FETCHERS]
    if not sources:
        raise ValueError("Select at least one source: Biometric or TimeChamp.")

    # Optional email delivery of the finished report (uses global SMTP config).
    # Cap is generous: supports a long comma-separated list of recipients.
    email_to = str(body.get("email_to") or "").strip()[:500] or None

    # Google Drive backup of this schedule's runs + optional monthly compile.
    drive_upload = bool(body.get("drive_upload"))
    monthly_compile = bool(body.get("monthly_compile"))



    # Date range the schedule should cover: last N days up to yesterday or
    # the whole previous calendar month ("full"). The removed "workday"
    # range (previous working day) normalizes to yesterday for legacy rows.
    range_days = str(body.get("range_days") or 1).strip().lower()
    if range_days == "full":
        pass  # keep "full"
    else:
        try:
            range_days = max(1, min(int(range_days), 365))
        except (TypeError, ValueError):
            range_days = 1

    schedule = {
        "id": existing_id or uuid.uuid4().hex[:10],
        "name": name,
        "frequency": frequency,
        "time": time_part.strftime("%H:%M"),
        "weekday": weekday,
        "send_days": send_days,
        "month_day": month_day,
        "sources": sources,
        "range_days": range_days,
        "enabled": bool(body.get("enabled", True)),
        "email_to": email_to,
        "drive_upload": drive_upload,
        "monthly_compile": monthly_compile,
        "last_run": body.get("last_run"),
        "last_status": body.get("last_status"),
        "last_job_id": body.get("last_job_id"),
        "next_run": None,
    }
    schedule["next_run"] = compute_next_run(schedule)
    return schedule


def compute_next_run(schedule, after=None):
    """Next UTC-fire datetime for a schedule, strictly after `after`."""
    after = after or datetime.now()
    hour, minute = (int(p) for p in schedule["time"].split(":"))
    frequency = schedule["frequency"]

    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if frequency == "daily":
        # Daily schedules may carry "send_days" (Mon=0..Sun=6) to fire only
        # on those days; an empty list/None keeps the every-day behaviour.
        targets = schedule.get("send_days") or []
        if not targets:
            if candidate <= after:
                candidate += timedelta(days=1)
            return candidate.timestamp()
        for offset in range(8):  # today .. next week covers any day filter
            probe = (after + timedelta(days=offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0)
            if probe > after and probe.weekday() in targets:
                return probe.timestamp()
        return candidate.timestamp()  # unreachable; keeps type-checkers calm
    if frequency == "weekly":
        # Multi-send-day support: "send_days" is a list of weekdays (Mon=0..Sun=6)
        # used by the previous-working-day range (e.g. mon+sat -> two fires a week,
        # each fetching the prior working day). Falls back to the single weekday.
        targets = schedule.get("send_days") or []
        if not targets and schedule.get("weekday") is not None:
            targets = [schedule.get("weekday")]
        if not targets:
            return None
        for offset in range(8):  # today .. same weekday next week
            probe = (after + timedelta(days=offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0)
            if probe > after and probe.weekday() in targets:
                return probe.timestamp()
        return None
    # monthly: pick the chosen day in this/next month (clamped to month length)
    import calendar
    year, month = candidate.year, candidate.month
    for _ in range(14):  # bounded scan even for day 29-31
        last_day = calendar.monthrange(year, month)[1]
        day = min(schedule.get("month_day") or 1, last_day)
        candidate = datetime(year, month, day, hour, minute)
        if candidate > after:
            return candidate.timestamp()
        month += 1
        if month == 13:
            month, year = 1, year + 1
    return None


def _schedule_due(schedule, now=None):
    """True when a schedule's next_run has passed (and it is enabled)."""
    if not schedule.get("enabled"):
        return False
    next_run = schedule.get("next_run")
    if not next_run:
        return False
    now = now or time.time()
    # Legacy/corrupt rows stored ms/µs values; treat anything implausible as
    # not due — _heal_schedule_rows has already rewritten it by now.
    if not (now - 366 * 86400 <= next_run <= now + 366 * 86400):
        return False
    return next_run <= now


def _mark_schedule_fired(schedule_id, status, job_id=None):
    rows = _load_schedules()
    with SCHEDULES_LOCK:
        for row in rows:
            if row["id"] == schedule_id:
                row["last_run"] = time.time()
                row["last_status"] = status
                if job_id:
                    row["last_job_id"] = job_id
                row["next_run"] = compute_next_run(row)
                break
        _write_schedules()


def _start_compile_job(schedule, year, month):
    """Track a monthly Drive compilation as a job the UI can watch.

    Creates a normal job entry + history row, then compiles in a thread.
    ``schedule`` may be None for manual compiles from the dialog (no email,
    no schedule bookkeeping). Returns the job id.
    """
    job_id = uuid.uuid4().hex[:12]
    target_dir = job_dir(job_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    title = f"Monthly compilation {month:02d}-{year}" + (
        f" · {schedule.get('name')}" if schedule else "")
    trigger = "Scheduled" if schedule else "Manual"
    cancel_event = threading.Event()
    with JOBS_LOCK:
        ACTIVE_CANCEL_FLAGS[job_id] = cancel_event
        JOBS_STATE[job_id] = {
            "id": job_id,
            "status": "queued",
            "created": time.time(),
            "started": None,
            "ended": None,
            "logs": [],
            "error": None,
            "input_paths": [],
            "display_names": [title],
            "input_type": "Monthly compile",
            "output": None,
            "output_name": None,
            "output_folder": None,
            "trigger": trigger,
            "summary": None,
            "range_label": f"01-{date(year, month, 1).strftime('%b-%Y')} – "
                           f"{(date(year, month, 1) + timedelta(days=31)).replace(day=1) - timedelta(days=1):%d-%b-%Y}",
            "schedule": schedule.get("name") if schedule else None,
        }
    upsert_history({
        "id": job_id,
        "status": "queued",
        "created": time.time(),
        "input_type": "Monthly compile",
        "display_names": [title],
        "trigger": trigger,
    })
    prune_old_jobs()

    def log(message):
        log_line(job_id, message)

    def worker():
        with JOBS_LOCK:
            JOBS_STATE[job_id]["status"] = "running"
            JOBS_STATE[job_id]["started"] = time.time()
        upsert_history({"id": job_id, "status": "running", "started": time.time()})
        log(f"[Monthly] Compiling {date(year, month, 1).strftime('%B %Y')} from raw data on Google Drive...")
        try:
            result = gdrive_mod.compile_monthly(
                year, month, log=log,
                dest_dir=target_dir,
                cancel_event=cancel_event,
            )
            output = Path(result["output_path"])
            with JOBS_LOCK:
                job = JOBS_STATE[job_id]
                job["status"] = "done"
                job["output"] = str(output)
                job["output_name"] = output.name
                job["output_folder"] = str(output.parent)
                job["ended"] = time.time()
                job["summary"] = {
                    "month": result.get("month"),
                    "year": result.get("year"),
                    "files": result.get("files"),
                    "output_name": output.name,
                    "folder": result.get("folder"),
                    "emailed": False,
                }
            upsert_history({
                "id": job_id,
                "status": "done",
                "ended": job["ended"],
                "output": str(output),
                "output_name": output.name,
                "output_folder": str(output.parent),
            })
            # Optional email with the monthly workbook attached.
            if schedule and schedule.get("email_to"):
                log(f"Emailing the monthly report to {schedule['email_to']}...")
                try:
                    import mailer
                    mail_settings = mailer.load_mail_settings()
                    mail_settings["recipients"] = schedule["email_to"]
                    stats = None
                    try:
                        stats = engine_stats.save_analytics(
                            output, job_dir(job_id) / "analytics.json"
                        )
                    except Exception as exc:
                        log(f"(Email stats unavailable: {exc})")
                    mailer.send_report_email(
                        output,
                        subject=(f"Monthly Attendance Compilation "
                                 f"{result['month']} {result['year']}"),
                        settings=mail_settings,
                        log=log,
                        stats=stats,
                        meta_note=(f"{result['files']} daily raw file(s) compiled "
                                   f"from Google Drive"),
                    )
                    with JOBS_LOCK:
                        JOBS_STATE[job_id]["summary"]["emailed"] = True
                    log("Email sent.")
                except Exception as exc:
                    with JOBS_LOCK:
                        JOBS_STATE[job_id]["email_error"] = str(exc)
                    log(f"EMAIL FAILED: {exc}")
            if schedule:
                _mark_schedule_fired(schedule["id"], f"done (monthly compiled)", job_id)
        except gdrive_mod.DriveError as exc:
            if is_job_cancelled(job_id):
                return
            log(f"MONTHLY COMPILE FAILED: {exc}")
            job_error(job_id, str(exc))
            if schedule:
                _mark_schedule_fired(schedule["id"], f"error: {str(exc)[:100]}")
        except Exception as exc:
            if is_job_cancelled(job_id):
                return
            log(f"MONTHLY COMPILE FAILED: {exc}")
            job_error(job_id, f"Monthly compilation failed: {exc}")
            if schedule:
                _mark_schedule_fired(schedule["id"], f"error: {str(exc)[:100]}")
        finally:
            with JOBS_LOCK:
                ACTIVE_CANCEL_FLAGS.pop(job_id, None)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def _compile_and_email_monthly(schedule, year, month):
    """Compile a month's Drive dailies and email the workbook if set.

    Returns a short status string appended to the schedule's last_status.
    Raises nothing — failures become the status text.
    """
    try:
        result = gdrive_mod.compile_monthly(year, month)
    except gdrive_mod.DriveError as exc:
        return f"monthly compile failed: {str(exc)[:90]}"
    except Exception as exc:
        return f"monthly compile failed: {str(exc)[:90]}"
    if schedule.get("email_to"):
        try:
            import mailer
            mail_settings = mailer.load_mail_settings()
            mail_settings["recipients"] = schedule["email_to"]
            stats = None
            try:
                stats = engine_stats.analyze_workbook(result["output_path"])
            except Exception:
                stats = None
            mailer.send_report_email(
                Path(result["output_path"]),
                subject=f"Monthly Attendance Compilation {result['month']} {result['year']}",
                settings=mail_settings,
                stats=stats,
                meta_note=f"{result['files']} daily raw file(s) compiled from Google Drive",
            )
            return f"monthly compiled + emailed ({result['files']} dailies)"
        except Exception as exc:
            return f"monthly compiled; email failed: {str(exc)[:70]}"
    return f"monthly compiled ({result['files']} dailies)"


def _scheduler_worker():
    """Daemon loop: fire due schedules through the fetch+run pipeline."""
    # Catch-up: any schedule that was missed while the app was closed fires
    # once on startup (within this first tick), then re-slots forward.
    while True:
        try:
            now = time.time()
            for schedule in list(_load_schedules()):
                if not _schedule_due(schedule, now):
                    continue
                schedule_id = schedule["id"]
                try:
                    if schedule.get("monthly_compile"):
                        # Monthly-compile mode: NO portal fetch — generate
                        # the PREVIOUS calendar month's report from the raw
                        # data already backed up on Drive, as a tracked job
                        # the UI can watch (a run on 1 Oct sends September's
                        # report; next month's run sends October's, etc.).
                        py, pm = _prev_month()
                        job_id = _start_compile_job(schedule, py, pm)
                        _mark_schedule_fired(schedule_id, "running", job_id)
                    else:
                        from_day, to_day = _schedule_range(schedule)
                        job_ids = _start_fetch_job(
                            schedule["sources"], from_day, to_day,
                            trigger="Scheduled",
                            schedule_name=schedule["name"],
                            email_to=(schedule.get("email_to") or None),
                            drive_upload=bool(schedule.get("drive_upload")),
                        )
                        _mark_schedule_fired(schedule_id, "done", job_ids)
                except ValueError as exc:
                    _mark_schedule_fired(schedule_id, f"error: {str(exc)[:120]}")
        except Exception:
            pass  # never let the scheduler thread die
        time.sleep(SCHEDULER_TICK_SECONDS)


# Scheduler thread bootstrap: exactly one thread per process.
# systemd runs the app as ``gunicorn app:app`` where __main__ never executes,
# so the thread is also re-seeded from a before_request hook — that is what
# keeps schedules firing on the GCP VM without any UI being opened.
_SCHEDULER_STARTED = False
_SCHEDULER_START_LOCK = threading.Lock()


def start_scheduler_once():
    global _SCHEDULER_STARTED
    if _SCHEDULER_STARTED:
        return
    with _SCHEDULER_START_LOCK:
        if _SCHEDULER_STARTED:
            return
        _SCHEDULER_STARTED = True
    threading.Thread(target=_scheduler_worker, daemon=True,
                     name="auto-fetch-scheduler").start()


@app.before_request
def _ensure_scheduler_running():
    # Cheap flag check on the hot path; takes the lock only on the first call.
    if not _SCHEDULER_STARTED:
        start_scheduler_once()


def _public_schedule(row):
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "frequency": row.get("frequency"),
        "time": row.get("time"),
        "weekday": row.get("weekday"),
        "send_days": row.get("send_days") or [],
        "month_day": row.get("month_day"),
        "sources": row.get("sources") or [],
        "range_days": row.get("range_days") if row.get("range_days") == "full" else (row.get("range_days") or 1),
        "enabled": bool(row.get("enabled")),
        "email_to": row.get("email_to"),
        "drive_upload": bool(row.get("drive_upload")),
        "monthly_compile": bool(row.get("monthly_compile")),
        "last_run": row.get("last_run"),
        "last_status": row.get("last_status"),
        "last_job_id": row.get("last_job_id"),
        "next_run": row.get("next_run"),
    }


@app.get("/api/schedules")
def api_schedules_list():
    rows = sorted(_load_schedules(), key=lambda r: r.get("next_run") or 0)
    return jsonify({"ok": True, "schedules": [_public_schedule(r) for r in rows]})


@app.post("/api/schedules")
def api_schedules_create():
    body = request.get_json(silent=True) or {}
    try:
        schedule = _validate_schedule(body)
    except ValueError as exc:
        return validation_error(str(exc))
    rows = _load_schedules()
    with SCHEDULES_LOCK:
        if len(rows) >= SCHEDULES_LIMIT:
            return validation_error(
                f"The schedule list is full ({SCHEDULES_LIMIT}). Delete one first."
            )
        rows.append(schedule)
        _write_schedules()
    return jsonify({"ok": True, "schedule": _public_schedule(schedule)})


@app.put("/api/schedules/<schedule_id>")
def api_schedules_update(schedule_id):
    body = request.get_json(silent=True) or {}
    rows = _load_schedules()
    target = next((r for r in rows if r["id"] == schedule_id), None)
    if target is None:
        abort(404, description="Unknown schedule. Refresh the list and try again.")
    try:
        updated = _validate_schedule(body, existing_id=schedule_id)
    except ValueError as exc:
        return validation_error(str(exc))
    # Keep the run bookkeeping unless the timing actually changed.
    timing_same = (
        updated["frequency"] == target["frequency"]
        and updated["time"] == target["time"]
        and updated.get("weekday") == target.get("weekday")
        and updated.get("month_day") == target.get("month_day")
    )
    if timing_same:
        updated["last_run"] = target.get("last_run")
        updated["last_status"] = target.get("last_status")
        updated["last_job_id"] = target.get("last_job_id")
    with SCHEDULES_LOCK:
        rows[:] = [updated if r["id"] == schedule_id else r for r in rows]
        _write_schedules()
    return jsonify({"ok": True, "schedule": _public_schedule(updated)})


@app.delete("/api/schedules/<schedule_id>")
def api_schedules_delete(schedule_id):
    rows = _load_schedules()
    target = next((r for r in rows if r["id"] == schedule_id), None)
    if target is None:
        abort(404, description="Unknown schedule. Refresh the list and try again.")
    with SCHEDULES_LOCK:
        rows[:] = [r for r in rows if r["id"] != schedule_id]
        _write_schedules()
    return jsonify({"ok": True})


@app.post("/api/schedules/<schedule_id>/run")
def api_schedules_run_now(schedule_id):
    rows = _load_schedules()
    target = next((r for r in rows if r["id"] == schedule_id), None)
    if target is None:
        abort(404, description="Unknown schedule. Refresh the list and try again.")
    if target.get("monthly_compile"):
        # Compile-from-Drive mode: run in a tracked job so the UI can watch.
        # Always compiles the previous calendar month (a run on 1 Oct sends
        # September's report; the next month's run sends October's, etc.).
        py, pm = _prev_month()
        job_id = _start_compile_job(target, py, pm)
        _mark_schedule_fired(schedule_id, "running", job_id)
        return jsonify({"ok": True, "job_id": job_id, "mode": "monthly_compile"})
    from_day, to_day = _schedule_range(target)
    try:
        job_ids = [_start_fetch_job(
            target["sources"], from_day, to_day,
            trigger="Scheduled",
            schedule_name=target["name"],
            email_to=(target.get("email_to") or None),
            drive_upload=bool(target.get("drive_upload")),
        )]
    except ValueError as exc:
        return validation_error(str(exc))
    _mark_schedule_fired(schedule_id, "done", job_ids[0] if job_ids else None)
    return jsonify({"ok": True, "job_id": job_ids[0]})


@app.get("/api/history")
def api_history():
    status = (request.args.get("status") or "").strip().lower()
    rows = load_history()
    runs = [public_run(r) for r in rows]
    runs.sort(key=lambda r: r.get("created") or 0, reverse=True)
    if status in ("done", "error", "running", "queued"):
        runs = [r for r in runs if r["status"] == status]
    return jsonify({"ok": True, "runs": runs})


@app.delete("/api/history")
def api_history_clear():
    """Clear all run history (folders included) - the UI confirms first.

    With Drive delete-sync enabled, every run's Drive copies are removed too;
    Drive files of runs already pruned locally are NOT touched.
    """
    drive_names = []
    drive_cfg = gdrive_mod.load_drive_settings()
    sync_drive = drive_cfg.get("enabled") and drive_cfg.get("delete_with_history")
    with HISTORY_LOCK:
        rows = load_history()
        for row in rows:
            jd = job_dir(row["id"])
            if sync_drive:
                drive_names.extend(
                    p.name for p in jd.rglob("*")
                    if p.is_file() and p.suffix.lower() in (".csv", ".xls", ".xlsx")
                )
            shutil.rmtree(jd, ignore_errors=True)
        HISTORY_STORE[:] = []
        _write_history()
    drive_deleted = 0
    if drive_names:
        try:
            drive_deleted = gdrive_mod.delete_files_by_name(drive_names)
        except gdrive_mod.DriveError:
            drive_deleted = 0
    return jsonify({"ok": True, "drive_deleted": drive_deleted})


@app.delete("/api/jobs/<job_id>")
def api_job_delete(job_id):
    """Delete one run from history and remove its folder - the UI confirms.

    With Google Drive enabled and 'delete with history' on, the run's Drive
    copies (same file names) are removed too.
    """
    rows = load_history()
    target = next((r for r in rows if r["id"] == job_id), None)
    if target is None:
        abort(404, description="Unknown run. Refresh the list and try again.")
    drive_names = []
    drive_cfg = gdrive_mod.load_drive_settings()
    if drive_cfg.get("enabled") and drive_cfg.get("delete_with_history"):
        jd = job_dir(job_id)
        drive_names = [
            p.name for p in jd.rglob("*")
            if p.is_file() and p.suffix.lower() in (".csv", ".xls", ".xlsx")
            and not p.name.lower().startswith(("analytics", "manifest"))
        ]
    with HISTORY_LOCK:
        rows[:] = [r for r in rows if r["id"] != job_id]
        _write_history()
    with JOBS_LOCK:
        JOBS_STATE.pop(job_id, None)
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
    drive_deleted = 0
    if drive_names:
        try:
            drive_deleted = gdrive_mod.delete_files_by_name(drive_names)
        except gdrive_mod.DriveError:
            drive_deleted = 0
    return jsonify({"ok": True, "drive_deleted": drive_deleted})


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    """Poll endpoint: returns new log lines since `cursor` plus job status."""
    with JOBS_LOCK:
        job = JOBS_STATE.get(job_id)
        if job is None:
            abort(404, description="Unknown job. Generate a new report first.")
        try:
            cursor = max(0, int(request.args.get("cursor", 0)))
        except ValueError:
            cursor = 0
        log_slice = job["logs"][cursor:]
        payload = {
            "status": job["status"],
            "cursor": cursor + len(log_slice),
            "logs": log_slice,
            "error": job.get("error"),
            "email_error": job.get("email_error"),
            "drive_error": job.get("drive_error"),
            "drive_result": job.get("drive_result"),
        }
        if job.get("output_name"):
            payload["output"] = {
                "name": job["output_name"],
                "folder": job.get("output_folder"),
            }
        if job.get("summary"):
            payload["summary"] = job["summary"]
        if job.get("started"):
            payload["elapsed"] = round(time.time() - job["started"], 1)
    return jsonify(payload)


@app.post("/api/jobs/<job_id>/cancel")
def api_job_cancel(job_id):
    """Cancel a running report generation or monthly compilation."""
    cancel_job(job_id)
    return jsonify({"ok": True, "status": "cancelled"})


@app.get("/api/jobs/<job_id>/download")
def api_download(job_id):
    output, name = resolve_output(job_id)
    if not output or not Path(output).is_file():
        abort(404, description="Report not found. Generate a new report first.")
    return send_file(output, as_attachment=True, download_name=name or Path(output).name)


@app.get("/api/jobs/<job_id>/analytics")
def api_analytics(job_id):
    """Per-report analytics: read the workbook back once, then cache JSON."""
    cache = job_dir(job_id) / "analytics.json"
    if cache.is_file():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            if payload.get("version") == engine_stats.ANALYTICS_VERSION:
                return jsonify({"ok": True, "cached": True, **payload})
            # Older math version — recompute below.
        except Exception:
            pass

    output, _name = resolve_output(job_id)
    if not output or not Path(output).is_file():
        abort(
            404,
            description="No report found for this run — its workbook was pruned "
            "or the run never finished.",
        )
    try:
        payload = engine_stats.save_analytics(Path(output), cache)
    except Exception as exc:
        abort(422, description=f"Could not summarize “{Path(output).name}”: {exc}")
    return jsonify({"ok": True, "cached": False, **payload})


def find_free_port(preferred_start=8765, tries=20):
    """First free port, probing on the same interface the server binds to.

    SO_REUSEADDR lets a just-killed server's TIME_WAIT socket be reused so
    restarts land on the same port instead of drifting.
    """
    bind_host = os.environ.get("HOST", "0.0.0.0")
    for port in range(preferred_start, preferred_start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((bind_host, port))
                return port
            except OSError:
                continue
    return 0  # let Flask pick any free port


def main():
    open_browser = "--no-browser" not in sys.argv
    # GCP-ready: HOST/PORT env vars override the defaults (Cloud Run / App
    # Engine / VM firewalls control exposure). Binds 0.0.0.0 by default so a
    # GCP VM can serve it; the browser still opens the local URL.
    host = os.environ.get("HOST", "0.0.0.0")
    # PORT is used by GCP (e.g. 8080); ignore empty/zero values so a stray
    # PORT=0 in the environment doesn't make Flask pick a random port.
    env_port = os.environ.get("PORT", "").strip()
    if env_port.isdigit() and int(env_port) > 0:
        port = int(env_port)
    else:
        port = find_free_port()

    # Auto-fetch scheduler: fires due daily/weekly/monthly schedules.
    start_scheduler_once()

    url = f"http://127.0.0.1:{port}"
    print("=" * 64)
    print("  Biometric & Timechamp Attendance Report Generator")
    print(f"  Open this page in your browser:  {url}")
    print("  Close this window to stop the app.")
    if AUTH_USES_DEFAULTS:
        print(f"  Sign-in:  admin / admin123")
        print("  (Set AW_USERNAME and AW_PASSWORD to change the credentials.)")
    else:
        print(f"  Sign-in as: {AUTH_USER} (password from AW_PASSWORD)")
    print("=" * 64, flush=True)

    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    # The engine fetches the HR API itself, so no proxy is needed.
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
