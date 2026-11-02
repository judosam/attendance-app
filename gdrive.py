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
import os
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

def compile_monthly(year: int, month: int, log=None, dest_dir=None, cancel_event=None) -> dict:
    """Compile a month's RAW DATA into one monthly workbook.

    Downloads every raw file in ``<folder>/<YYYY>/<month>/rawdata/`` (or falls
    back to local files), strictly isolates records belonging to the selected
    month, ignores duplicate data across multiple/overlapping date files,
    runs the engine, and consolidates the generated reports into
    ``Monthly Attendance Compilation MM-YYYY.xlsx`` — uploaded to
    ``<folder>/<YYYY>/<month>/reports/`` (replacing any previous copy).
    """
    import warnings

    warnings.filterwarnings("ignore")
    if log is None:
        def log(message):
            pass

    if cancel_event and cancel_event.is_set():
        raise DriveError("Monthly compilation was cancelled by the user.")

    service, folder_id, folder_name = None, None, None
    raw_files = []
    month_label = date(year, month, 1).strftime("%B %Y")
    month_dir_name = _month_dir_name(year, month)
    month_dir = None

    try:
        service, folder_id, folder_name = _drive_and_folder()
        year_dir = _find_child_folder(service, folder_id, str(year))
        if year_dir:
            month_dir = _find_child_folder(service, year_dir, month_dir_name)
            raw_files = _list_raw_files(service, month_dir) if month_dir else []
    except Exception as drive_exc:
        log(f"[Drive] Drive access notice: {drive_exc}")

    # If Drive had no raw files, check for local raw files for this year and month
    local_fallback_files = []
    if not raw_files:
        log(f"[Monthly] Searching local workspace for {month_label} raw files...")
        search_dirs = [
            Path(APP_DIR) / str(year) / month_dir_name / "rawdata",
            Path(APP_DIR) / str(year) / month_dir_name,
            Path(APP_DIR) / "workspace" / "jobs",
        ]
        for sdir in search_dirs:
            if sdir.is_dir():
                for p in sdir.rglob("*"):
                    if p.is_file() and p.suffix.lower() in (".csv", ".xls", ".xlsx"):
                        if not p.name.lower().startswith("monthly") and not p.name.lower().startswith("~"):
                            local_fallback_files.append(p)
        if local_fallback_files:
            log(f"[Monthly] Located {len(local_fallback_files)} potential local raw file(s).")

    if not raw_files and not local_fallback_files:
        raise DriveError(
            f"No raw data files found for {month_label} on Drive "
            f"({year}/{month_dir_name}/rawdata) or locally. Generate and back up some runs first."
        )

    import tempfile
    import shutil
    import pandas as pd
    from biometric_timechamp_report import run_report

    log(f"[Drive] Compiling monthly report for {month_label}...")
    output = None
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        raw_dir = td_path / "raw"
        raw_dir.mkdir()

        if cancel_event and cancel_event.is_set():
            raise DriveError("Monthly compilation was cancelled by the user.")

        # ---- Download or copy raw files ---------------------------------
        local_raws = []
        if raw_files and service:
            for f in raw_files:
                if cancel_event and cancel_event.is_set():
                    raise DriveError("Monthly compilation was cancelled by the user.")
                target = raw_dir / f["name"]
                _download_file(service, f["id"], target)
                local_raws.append(target)
                log(f"[Drive]   fetched raw: {f['name']}")
        else:
            for lf in local_fallback_files:
                target = raw_dir / lf.name
                if not target.exists():
                    shutil.copy2(lf, target)
                    local_raws.append(target)
                    log(f"[Monthly]   using local raw: {lf.name}")

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
            if cancel_event and cancel_event.is_set():
                raise DriveError("Monthly compilation was cancelled by the user.")

            log(f"[Drive] Reading and consolidating {len(paths)} {type_name} raw file(s)...")
            frames = []
            for p in sorted(paths, key=lambda x: x.name):
                try:
                    if p.suffix.lower() == ".csv":
                        frames.append(pd.read_csv(p, dtype=str, keep_default_na=False))
                    else:
                        frames.append(pd.read_excel(p, dtype=str))
                except Exception as read_err:
                    log(f"[Drive] Warning: could not parse {p.name}: {read_err}")
            if not frames:
                continue

            combined = pd.concat(frames, ignore_index=True)
            combined.columns = [str(c).strip() for c in combined.columns]

            # 1. Filter strictly to the selected month and year
            date_col = next((c for c in combined.columns if c.lower() == 'date'), None)
            if date_col:
                parsed_dates = pd.to_datetime(
                    combined[date_col].astype(str).str.strip(), errors='coerce', format='mixed'
                )
                month_mask = (parsed_dates.dt.year == year) & (parsed_dates.dt.month == month)
                before_filter = len(combined)
                combined = combined[month_mask].copy()
                out_of_month = before_filter - len(combined)
                if out_of_month > 0:
                    log(f"[Drive] Filtered to {month_label}: ignored {out_of_month} record(s) outside selected month.")

            if len(combined) == 0:
                log(f"[Drive] Note: No records found for {type_name} in {month_label}.")
                continue

            # 2. Ignore duplicate data across multiple date files
            emp_col = next(
                (c for c in ['Employee Code', 'Employee ID', 'Employee Id', 'User', 'Employee Name']
                 if c in combined.columns),
                None
            )
            if date_col and emp_col:
                # Standardize Date column values to YYYY-MM-DD for accurate deduplication
                parsed_clean = pd.to_datetime(
                    combined[date_col].astype(str).str.strip(), errors='coerce', format='mixed'
                )
                valid_mask = parsed_clean.notna()
                combined.loc[valid_mask, date_col] = parsed_clean[valid_mask].dt.strftime('%Y-%m-%d')
                combined[emp_col] = combined[emp_col].astype(str).str.strip()

                # Prioritize rows with actual punches or working hours
                if 'Punch Records' in combined.columns:
                    combined['__p_score'] = combined['Punch Records'].astype(str).str.strip().str.len()
                    combined = combined.sort_values(by='__p_score', ascending=True).drop(columns=['__p_score'])
                elif 'Working Hours' in combined.columns:
                    combined['__h_score'] = combined['Working Hours'].astype(str).str.strip().str.len()
                    combined = combined.sort_values(by='__h_score', ascending=True).drop(columns=['__h_score'])

                before_dedup = len(combined)
                combined = combined.drop_duplicates(subset=[date_col, emp_col], keep='last')
                dedup_removed = before_dedup - len(combined)
                if dedup_removed > 0:
                    log(f"[Drive] Ignored {dedup_removed} duplicate employee-date record(s) across overlapping files.")
            else:
                before_dedup = len(combined)
                combined = combined.drop_duplicates()
                if len(combined) < before_dedup:
                    log(f"[Drive] Ignored {before_dedup - len(combined)} identical duplicate row(s).")

            if cancel_event and cancel_event.is_set():
                raise DriveError("Monthly compilation was cancelled by the user.")

            # Save clean merged file and run engine
            if type_name == "biometric":
                merged_path = raw_dir / f"biometric_month_{month:02d}_{year}.csv"
                combined.to_csv(merged_path, index=False)
            elif type_name == "timechamp":
                merged_path = raw_dir / f"timechamp_month_{month:02d}_{year}.xlsx"
                combined.to_excel(merged_path, index=False)
            else:
                merged_path = raw_dir / f"manual_month_{month:02d}_{year}.xlsx"
                combined.to_excel(merged_path, index=False)

            log(f"[Drive] Processing {type_name} data ({len(combined)} rows) through the report engine...")
            gen_res = Path(run_report([str(merged_path)], td, fancy_progress=False))
            generated.append(gen_res)

        if not generated:
            raise DriveError(f"No valid raw records found for {month_label} to compile.")

        if cancel_event and cancel_event.is_set():
            raise DriveError("Monthly compilation was cancelled by the user.")

        # ---- Consolidate generated reports into the monthly workbook ----
        if len(generated) == 1:
            output = generated[0]
        else:
            log(f"[Drive] Consolidating {len(generated)} generated reports...")
            output = Path(run_report(
                [str(g) for g in generated], td, fancy_progress=False))

        final_name = MONTHLY_NAME.format(month=month, year=year) + ".xlsx"
        output = output.rename(output.with_name(final_name))
        log(f"[Drive] Monthly workbook ready: {output.name}")

        if dest_dir:
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output, dest / output.name)

        # Upload to the month's reports bucket if Drive is available
        if service and month_dir:
            try:
                reports_dir = ensure_subfolder(service, month_dir, "reports", cache={})
                _replace_drive_file(service, reports_dir, output)
                log(f"[Drive] Uploaded monthly report: {output.name}")
            except Exception as up_err:
                log(f"[Drive] Notice: Drive upload skipped ({up_err})")

    # The temp dir is gone now — point the record at the persistent copy
    if dest_dir:
        output = Path(dest_dir) / output.name

    return {
        "month": date(year, month, 1).strftime("%B"),
        "year": year,
        "files": len(raw_files) or len(local_fallback_files),
        "output_name": output.name,
        "output_path": str(output),
        "folder": folder_name or "Local",
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
