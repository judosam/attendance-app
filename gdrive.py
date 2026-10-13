"""Google Drive backup — upload raw data and generated reports via a service account.

Folder layout inside the configured Drive folder (month buckets):

    <folder>/2026/september/rawdata/<raw files>
    <folder>/2026/september/reports/<workbooks>

The monthly compilation is uploaded next to the month's daily reports in
``reports/`` and replaces any previous copy of the same month.

Settings live in ``workspace/settings.json`` (``gdrive`` section), shared with
the portal + mail settings, editable in the UI:

    gdrive: {
        enabled, folder_id, upload_raw, upload_reports, delete_with_history
    }

Auth is automatic and GCP-ready: if ``workspace/gdrive_service_account.json``
exists it is used (dev machines), otherwise Application Default Credentials
are used — on GCP the instance's attached service account is picked up with
no configuration. ``delete_with_history=True`` keeps Drive in sync with local
history: when a run is deleted locally (single delete or Clear All), its
Drive copies are deleted too. History pruning never touches Drive.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path


from fetchers import SETTINGS_FILE

APP_DIR = Path(__file__).resolve().parent
DEFAULT_SA_FILE = APP_DIR / "workspace" / "gdrive_service_account.json"

# The user's default shared folder (editable in Settings -> Google Drive).
DEFAULT_FOLDER_ID = "1FPBNgekIJgxrL8_QhPpOq4Z9zbwfRKAX"

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"

MONTHLY_NAME = "Monthly Attendance Compilation {month:02d}-{year}"
MONTHLY_MAX_RETRIES = 3


class DriveError(Exception):
    """Raised with a user-safe message for any Drive failure."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _defaults():
    return {
        "enabled": False,
        "folder_id": DEFAULT_FOLDER_ID,
        "upload_raw": True,
        "upload_reports": True,
        "delete_with_history": True,
    }


def load_drive_settings():
    stored = {}
    if SETTINGS_FILE.is_file():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data.get("gdrive"), dict):
                stored = data["gdrive"]
        except Exception:
            stored = {}
    merged = _defaults()
    merged.update({k: v for k, v in stored.items() if k in merged})
    return merged


def save_drive_settings(patch):
    """Merge the gdrive block into workspace/settings.json (own section)."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if SETTINGS_FILE.is_file():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    current = load_drive_settings()
    for key in ("enabled", "upload_raw", "upload_reports", "delete_with_history"):
        if key in patch:
            current[key] = bool(patch[key])
    if "folder_id" in patch:
        current["folder_id"] = str(patch["folder_id"] or "").strip() or DEFAULT_FOLDER_ID
    data["gdrive"] = current
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return current


def public_drive_settings():
    return {
        **load_drive_settings(),
        "service_account_email": _sa_email(),
        "auth_source": "key-file" if DEFAULT_SA_FILE.is_file() else "gcp-adc",
    }


def _sa_email():
    """Best-effort service-account e-mail (key file, or GCP attached SA)."""
    if DEFAULT_SA_FILE.is_file():
        try:
            data = json.loads(DEFAULT_SA_FILE.read_text(encoding="utf-8"))
            return data.get("client_email", "")
        except Exception:
            return ""
    try:
        creds, _source = _resolve_credentials()
        return str(getattr(creds, "service_account_email", "") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Auth + API (lazy imports so the app runs without the google libs installed)
# ---------------------------------------------------------------------------

_CREDENTIALS = None
_SERVICE = None
_SERVICE_TS = 0.0
_SERVICE_TTL = 45 * 60  # refresh the token-bearing service hourly


def _local_sa_file():
    """Legacy local key file, if present (dev machines and GCP VMs both work)."""
    return DEFAULT_SA_FILE if DEFAULT_SA_FILE.is_file() else None


def _resolve_credentials():
    """Return (credentials, source_label) for Google Drive.

    Prefers the bundled JSON key file (``workspace/gdrive_service_account.json``
    on dev machines), then falls back to Application Default Credentials — on
    GCP the attached service account is picked up automatically.
    """
    try:
        from google.oauth2 import service_account
    except ImportError as exc:
        raise DriveError(
            "Google libraries are not installed. Run: pip install google-api-python-client"
        ) from exc

    local = _local_sa_file()
    if local is not None:
        creds = service_account.Credentials.from_service_account_file(
            str(local), scopes=SCOPES)
        return creds, "key-file"
    try:
        import google.auth
    except ImportError as exc:
        raise DriveError(
            "Google libraries are not installed. Run: pip install google-api-python-client"
        ) from exc
    try:
        creds, _project = google.auth.default(scopes=SCOPES)
    except Exception as exc:
        raise DriveError(
            "No Drive credentials available. On GCP, attach a service account to the "
            "instance (picked up automatically), or place the JSON key at "
            f"{DEFAULT_SA_FILE}."
        ) from exc
    return creds, "gcp-adc"


def _get_service():
    global _CREDENTIALS, _SERVICE, _SERVICE_TS
    if _SERVICE is not None and time.time() - _SERVICE_TS < _SERVICE_TTL:
        return _SERVICE
    creds, _source = _resolve_credentials()
    try:
        from googleapiclient.discovery import build
        _CREDENTIALS = creds
        _SERVICE = build("drive", "v3", credentials=creds, cache_discovery=False)
        _SERVICE_TS = time.time()
        return _SERVICE
    except Exception as exc:
        raise DriveError(f"Could not build the Drive client: {exc}") from exc


def reset_service():
    """Drop the cached service (called after settings change)."""
    global _SERVICE, _SERVICE_TS
    _SERVICE = None
    _SERVICE_TS = 0.0


def _drive_and_folder():
    service = _get_service()
    folder_id = load_drive_settings()["folder_id"]
    if not folder_id:
        raise DriveError("No Drive folder configured. Set it in Settings → Google Drive.")
    try:
        meta = service.files().get(
            fileId=folder_id, fields="id,name,permissions", supportsAllDrives=True
        ).execute()
    except Exception as exc:
        raise DriveError(
            "Could not open the Drive folder. Check the folder ID and that the "
            f"service account has Editor access: {exc}"
        ) from exc
    return service, folder_id, meta.get("name", "folder")


def ensure_subfolder(service, parent_id, name, cache=None):
    """Find-or-create a subfolder; returns its ID. `cache` speeds up batches."""
    if cache is not None and name in cache:
        return cache[name]
    query = (
        f"name='{name}' and '{parent_id}' in parents and "
        "mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    found = service.files().list(q=query, fields="files(id,name)", pageSize=5).execute()
    if found.get("files"):
        fid = found["files"][0]["id"]
    else:
        created = service.files().create(
            body={
                "name": name,
                "mimeType": FOLDER_MIME,
                "parents": [parent_id],
            },
            fields="id",
        ).execute()
        fid = created["id"]
    if cache is not None:
        cache[name] = fid
    return fid


def _month_dir_name(year: int, month: int) -> str:
    """Month folder name, e.g. ``september``."""
    return date(year, month, 1).strftime("%B").lower()


def _upload_file(service, folder_id, local_path: Path):
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(local_path), resumable=True)
    body = {"name": local_path.name, "parents": [folder_id]}
    request = service.files().create(body=body, media_body=media, fields="id,name,size")
    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=2)
    return response


# ---------------------------------------------------------------------------
# Upload API used by the job worker
# ---------------------------------------------------------------------------

def _replace_drive_file(service, folder_id, local_path: Path):
    """Upload ``local_path``, replacing any same-named file in the folder."""
    escaped = local_path.name.replace("'", "\\'")
    query = f"name='{escaped}' and '{folder_id}' in parents and trashed=false"
    try:
        for f in service.files().list(
            q=query, fields="files(id)", pageSize=10,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute().get("files", []):
            try:
                service.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
            except Exception:
                pass
    except Exception:
        pass
    _upload_file(service, folder_id, local_path)


def upload_run(job_dir_path: Path, raw_files, report_paths, log=None,
               from_day=None, to_day=None):
    """Upload one run's raw inputs and generated reports to Drive.

    Month buckets per the configured layout:

        <folder>/2026/september/rawdata/<raw files>
        <folder>/2026/september/reports/<workbooks>

    The run's start date picks the month (the file's own modified date picks
    it for manual uploads without an explicit range). Returns a short
    summary dict for the log.
    """
    if log is None:
        def log(message):
            pass

    service, folder_id, folder_name = _drive_and_folder()
    cache = {}
    buckets = {}
    uploaded_raw, uploaded_reports = [], []

    def bucket_for(p: Path, is_raw: bool):
        mday = from_day if (from_day and to_day) \
            else date.fromtimestamp(p.stat().st_mtime)
        key = (mday.year, _month_dir_name(mday.year, mday.month), is_raw)
        if key not in buckets:
            year_dir = ensure_subfolder(service, folder_id, str(key[0]), cache)
            month_dir = ensure_subfolder(service, year_dir, key[1], cache)
            buckets[key] = ensure_subfolder(
                service, month_dir, "rawdata" if is_raw else "reports", cache
            )
        return buckets[key]

    for raw in raw_files or []:
        p = Path(raw)
        if not p.is_file():
            continue
        _upload_file(service, bucket_for(p, True), p)
        uploaded_raw.append(p.name)
        log(f"[Drive] Uploaded raw data: {p.name}")

    for report in report_paths or []:
        p = Path(report)
        if not p.is_file():
            continue
        _upload_file(service, bucket_for(p, False), p)
        uploaded_reports.append(p.name)
        log(f"[Drive] Uploaded report: {p.name}")

    return {
        "raw": uploaded_raw,
        "reports": uploaded_reports,
        "folder": folder_name,
    }


# ---------------------------------------------------------------------------
# Delete-sync (local delete ⇒ Drive delete)
# ---------------------------------------------------------------------------

def delete_files_by_name(names):
    """Delete Drive files with these exact names (any folder). Returns count."""
    if not names:
        return 0
    service, _fid, _name = _drive_and_folder()
    deleted = 0
    for name in names:
        safe = name.replace("'", "\\'")
        query = f"name='{safe}' and trashed=false"
        try:
            found = service.files().list(
                q=query, fields="files(id,name)", pageSize=20, supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for f in found.get("files", []):
                try:
                    service.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
                    deleted += 1
                except Exception:
                    pass
        except Exception:
            pass
    return deleted


# ---------------------------------------------------------------------------
# Monthly compilation from already-generated daily reports
# ---------------------------------------------------------------------------

def compile_monthly(year: int, month: int, log=None, dest_dir=None) -> dict:
    """Compile a month's RAW DATA from Drive into one monthly workbook.

    Downloads every raw file in ``<folder>/<YYYY>/<month>/rawdata/``, groups
    them by engine input type (Biometric CSV / Timechamp XLSX / Manual),
    concatenates same-type raws into one file per type, runs the engine once
    per type, and consolidates the generated reports into
    ``Monthly Attendance Compilation MM-YYYY.xlsx`` — uploaded to
    ``<folder>/<YYYY>/<month>/reports/`` (replacing any previous copy).
    """
    import warnings

    warnings.filterwarnings("ignore")
    if log is None:
        def log(message):
            pass

    service, folder_id, folder_name = _drive_and_folder()

    year_dir = _find_child_folder(service, folder_id, str(year))
    if not year_dir:
        raise DriveError(
            f"No '{year}' folder on Drive yet — no raw data has been backed up."
        )
    month_dir_name = _month_dir_name(year, month)
    month_dir = _find_child_folder(service, year_dir, month_dir_name)
    raw_files = _list_raw_files(service, month_dir) if month_dir else []
    if not raw_files:
        month_label = date(year, month, 1).strftime("%B %Y")
        raise DriveError(
            f"No raw data files found for {month_label} on Drive "
            f"({year}/{month_dir_name}/rawdata). Generate and back up some runs first."
        )

    import tempfile
    import shutil
    import pandas as pd
    from biometric_timechamp_report import run_report

    month_label = date(year, month, 1).strftime("%B %Y")
    month_dir_name = _month_dir_name(year, month)
    log(f"[Drive] Compiling monthly report for {month_label} "
        f"from {len(raw_files)} raw data file(s)...")
    output = None
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        raw_dir = td_path / "raw"
        raw_dir.mkdir()

        # ---- Download all raw files for the month ------------------------
        local_raws = []
        for f in raw_files:
            target = raw_dir / f["name"]
            _download_file(service, f["id"], target)
            local_raws.append(target)
            log(f"[Drive]   fetched raw: {f['name']}")

        # ---- Group by engine input type ----------------------------------
        def raw_type(path: Path) -> str:
            lower = path.name.lower()
            if lower.endswith(".csv") or "biometric" in lower:
                return "biometric"
            if "timechamp" in lower:
                return "timechamp"
            return "manual"

        groups = {}
        for p in local_raws:
            groups.setdefault(raw_type(p), []).append(p)
        log("[Drive] Raw types found: " + ", ".join(
            f"{k} ({len(v)})" for k, v in sorted(groups.items())))

        generated = []
        for type_name, paths in sorted(groups.items()):
            if type_name == "manual":
                for p in paths:
                    log(f"[Drive] Processing raw file: {p.name}")
                    generated.append(
                        Path(run_report([str(p)], td, fancy_progress=False)))
                continue
            # Concatenate same-type raws into one file, then ONE engine run.
            log(f"[Drive] Merging {len(paths)} {type_name} raw file(s)...")
            frames = []
            for p in sorted(paths, key=lambda x: x.name):
                if p.suffix.lower() == ".csv":
                    frames.append(pd.read_csv(p, dtype=str, keep_default_na=False))
                else:
                    frames.append(pd.read_excel(p, dtype=str))
            combined = pd.concat(frames, ignore_index=True)
            if type_name == "biometric":
                merged_path = raw_dir / f"biometric_month_{month:02d}_{year}.csv"
                combined.to_csv(merged_path, index=False)
            else:
                merged_path = raw_dir / f"timechamp_month_{month:02d}_{year}.xlsx"
                combined.to_excel(merged_path, index=False)
            log(f"[Drive] Processing {type_name} raw data through the engine...")
            generated.append(
                Path(run_report([str(merged_path)], td, fancy_progress=False)))

        # ---- Consolidate generated reports into the monthly workbook ----
        if len(generated) == 1:
            output = generated[0]
        else:
            log(f"[Drive] Consolidating {len(generated)} generated reports...")
            output = Path(run_report(
                [str(g) for g in generated], td, fancy_progress=False))

        final_name = MONTHLY_NAME.format(month=month, year=year) + ".xlsx"
        output = output.rename(output.with_name(final_name))
        log(f"[Drive] Monthly workbook: {output.name}")

        if dest_dir:
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output, dest / output.name)

        # Upload to the month's reports bucket, replacing any previous copy.
        reports_dir = ensure_subfolder(service, month_dir, "reports", cache={})
        _replace_drive_file(service, reports_dir, output)
        log(f"[Drive] Uploaded monthly report: {output.name}")

    # The temp dir is gone now — point the record at the persistent copy
    # so downloads from history keep working.
    if dest_dir:
        output = Path(dest_dir) / output.name

    return {
        "month": date(year, month, 1).strftime("%B"),
        "year": year,
        "files": len(raw_files),
        "output_name": output.name,
        "output_path": str(output),
        "folder": folder_name,
    }


def _find_child_folder(service, parent_id, name):
    query = (
        f"name='{name}' and '{parent_id}' in parents and "
        "mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    found = service.files().list(q=query, fields="files(id,name)", pageSize=5).execute()
    files = found.get("files", [])
    return files[0]["id"] if files else None


def _list_xlsx(service, folder_id):
    query = f"'{folder_id}' in parents and trashed=false and name contains '.xlsx'"
    found = service.files().list(
        q=query, fields="files(id,name,size)", pageSize=200,
        orderBy="name_natural", supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return [f for f in found.get("files", []) if not f["name"].lower().startswith("monthly")]


def _list_raw_files(service, month_dir):
    """All raw data files in a month's ``rawdata`` bucket.

    Monthly compilation outputs are never returned as raw inputs, and a
    legacy ``RawData``-named bucket (previous layout) is still recognised.
    """
    raw_exts = (".csv", ".xls", ".xlsx")
    raw_bucket = _find_child_folder(service, month_dir, "rawdata") \
        or _find_child_folder(service, month_dir, "RawData")
    if raw_bucket:
        candidates = _list_files(service, raw_bucket)
    else:
        candidates = [
            f for f in _list_files(service, month_dir)
            if f["name"].lower().endswith(raw_exts)
        ]
    return [
        f for f in candidates
        if not f["name"].lower().startswith("monthly")
    ]


def _list_folders(service, folder_id):
    query = (
        f"'{folder_id}' in parents and trashed=false and "
        "mimeType='application/vnd.google-apps.folder'"
    )
    found = service.files().list(
        q=query, fields="files(id,name)", pageSize=200,
        orderBy="name_natural", supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return found.get("files", [])


def _list_files(service, folder_id):
    query = f"'{folder_id}' in parents and trashed=false"
    found = service.files().list(
        q=query, fields="files(id,name)", pageSize=200,
        orderBy="name_natural", supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return found.get("files", [])


def _download_file(service, file_id, target: Path):
    import io
    from googleapiclient.http import MediaIoBaseDownload

    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk(num_retries=2)
    target.write_bytes(fh.getvalue())


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------

def test_connection():
    service, folder_id, folder_name = _drive_and_folder()
    # Write + delete a probe file to confirm write access.
    probe = Path(APP_DIR) / "workspace" / ".drive_probe.txt"
    probe.write_text("probe", encoding="utf-8")
    try:
        result = _upload_file(service, folder_id, probe)
        service.files().delete(fileId=result["id"]).execute()
    finally:
        probe.unlink(missing_ok=True)
    return {"folder_name": folder_name, "email": _sa_email()}
