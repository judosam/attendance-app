"""Portal fetchers - download raw attendance data from the two sources.

Ports of the user's working scripts so the app can pull raw data itself:

  * ``fetch_biometric(from, to)``  -> Daily Attendance Report CSV from the
    biometric device portal (ASP.NET WebForms: scrape __VIEWSTATE, AES-ECB
    encrypt the password with the page's txtKey, post the report form).
  * ``fetch_timechamp(from, to)``  -> "Working hours" attendance XLSX from
    the TimeChamp cloud API (login -> Bearer token -> export endpoint).

Both return a ``(path, label)`` tuple; the downloaded file lands in the
caller-provided directory with a stable, engine-friendly name.  The report
engine is untouched: the biometric CSV carries the ``Punch Records`` column
and the TimeChamp XLSX carries ``Expected Hours``, exactly like a manual
upload of the same exports.

Credentials/URLs live in ``workspace/settings.json`` (editable in the UI).
Everything degrades to a clear FetchError the server can show in the log.
"""

import base64
import json
import re
from datetime import date
from pathlib import Path

import requests

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "workspace" / "settings.json"

# ---------------------------------------------------------------------------
# Defaults (pre-filled from the user's scripts; editable in the UI)
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "biometric": {
        "base_url": "http://103.99.148.151:85",
        "username": "",
        "password": "",
    },
    "timechamp": {
        "base_url": "https://vservesolution.v3.timechamp.io",
        "username": "",
        "password": "",
    },
}


class FetchError(Exception):
    """Raised when a portal download fails (login, network, bad response)."""


# ---------------------------------------------------------------------------
# Settings store
# ---------------------------------------------------------------------------

def load_settings():
    """Read portal settings, filling any missing key with the default."""
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    if SETTINGS_FILE.is_file():
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for source in ("biometric", "timechamp"):
                if isinstance(stored.get(source), dict):
                    settings[source].update({
                        key: value for key, value in stored[source].items()
                        if value is not None
                    })
        except Exception:
            pass  # corrupt settings file -> fall back to defaults
    return settings


def save_settings(settings):
    """Persist the editable subset; unknown keys are ignored.

    Sections owned by other modules (``gdrive``, ``mail``, …) are carried
    over from the stored file so saving portal settings never wipes them.
    """
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {}
    for source in ("biometric", "timechamp"):
        payload[source] = {
            "base_url": str(settings.get(source, {}).get("base_url", "")).strip(),
            "username": str(settings.get(source, {}).get("username", "")).strip(),
            "password": str(settings.get(source, {}).get("password", "")),
        }
    if SETTINGS_FILE.is_file():
        try:
            existing = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        for key, value in existing.items():
            if key not in payload:
                payload[key] = value
    SETTINGS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def public_settings():
    """Settings shaped for the UI - passwords are masked, never sent back."""
    settings = load_settings()
    return {
        source: {
            "base_url": data["base_url"],
            "username": data["username"],
            "has_password": bool(data["password"]),
        }
        for source, data in settings.items()
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp_range(from_day, to_day, max_days=370):
    """Validate the date range; cap absurdly long fetches early."""
    if from_day > to_day:
        raise FetchError("The From date must be on or before the To date.")
    if (to_day - from_day).days > max_days:
        raise FetchError(
            f"The date range is too long ({(to_day - from_day).days + 1} days). "
            f"Fetch at most {max_days} days at a time."
        )
    return from_day, to_day


def _future_guard(from_day, to_day):
    """Portals only hold attendance up to today."""
    today = date.today()
    if from_day > today:
        raise FetchError("The From date is in the future - nothing to fetch yet.")
    if to_day > today:
        to_day = today  # silently trim, the engine only knows real days
    return from_day, to_day


# ---------------------------------------------------------------------------
# Biometric portal (ASP.NET WebForms + AES-ECB password)
# ---------------------------------------------------------------------------

def _biometric_login(session, settings, log=None):
    """Log into the biometric portal; the session cookie carries the login."""
    log = log or (lambda message: None)
    base = settings["base_url"].rstrip("/")
    login_url = f"{base}/iclock/Default.aspx"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": login_url,
        "Origin": base,
    }

    log(f"Connecting to biometric portal {base} ...")
    response = session.get(login_url, headers=headers, timeout=30, verify=False)
    response.raise_for_status()
    log("Login page loaded — reading security token (VIEWSTATE + AES key).")

    # scrapy's Selector is heavy for one page; parse the four fields we need.
    viewstate = _input_value(response.text, "__VIEWSTATE")
    generator = _input_value(response.text, "__VIEWSTATEGENERATOR")
    txt_key = _input_value(response.text, "StaffloginDialog_txtKey")
    if not viewstate or not txt_key:
        raise FetchError(
            "Biometric portal: the login page did not look as expected "
            "(missing VIEWSTATE / key). Is the device portal URL correct?"
        )

    key = txt_key.encode("utf-8")
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        cipher = AES.new(key, AES.MODE_ECB)
        encrypted = cipher.encrypt(pad(settings["password"].encode("utf-8"), AES.block_size))
    except ImportError as exc:
        raise FetchError(
            "Server is missing the pycryptodome package - run: pip install pycryptodome"
        ) from exc

    form = {
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": viewstate,
        "__VIEWSTATEGENERATOR": generator or "",
        "StaffloginDialog$txt_LoginName": settings["username"],
        "StaffloginDialog$Txt_Password": base64.b64encode(encrypted).decode(),
        "StaffloginDialog$txtKey": txt_key,
        "StaffloginDialog$Btn_Ok": "Login",
    }
    log(f"Signing in as “{settings['username']}” (password AES-encrypted)...")
    response = session.post(login_url, data=form, headers=headers, timeout=30, verify=False)
    response.raise_for_status()

    if "StaffloginDialog" in response.text and "Logout" not in response.text:
        # The login form is re-rendered on failure.
        log("Biometric login FAILED — credentials rejected.")
        raise FetchError(
            "Biometric portal: login was rejected. Check the username/password in "
            "Settings → Portal sources."
        )
    log("Biometric portal login successful.")


def _input_value(html, field_id):
    """Pull one <input id=...> value out of the WebForms page."""
    marker = f'id="{field_id}"'
    start = html.find(marker)
    if start == -1:
        # attribute order can differ
        match = re.search(
            r"<input[^>]*\bid\s*=\s*['\"]" + re.escape(field_id) + r"['\"][^>]*>", html
        )
        if not match:
            return None
        tag = match.group(0)
    else:
        tag_start = html.rfind("<input", 0, start)
        tag = html[tag_start:html.find(">", start) + 1]
    value_match = re.search(r"value\s*=\s*['\"](.*?)['\"]", tag, re.S)
    return value_match.group(1) if value_match else None


def fetch_biometric(target_dir, from_day, to_day, settings=None, log=None):
    """Download the Daily Attendance Report CSV for the date range."""
    log = log or (lambda message: None)
    settings = settings or load_settings().get("biometric", {})
    _clamp_range(from_day, to_day)
    from_day, to_day = _future_guard(from_day, to_day)

    import warnings
    from urllib3.exceptions import InsecureRequestWarning
    warnings.simplefilter("ignore", InsecureRequestWarning)

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    _biometric_login(session, settings, log)

    base = settings["base_url"].rstrip("/")
    report_url = f"{base}/iclock/Reports/DailyAttendanceReport.aspx"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "Referer": report_url,
        "Origin": base,
    }

    log("Opening Daily Attendance Report page...")
    page = session.get(report_url, headers=headers, timeout=60, verify=False)
    page.raise_for_status()
    viewstate = _input_value(page.text, "__VIEWSTATE")
    generator = _input_value(page.text, "__VIEWSTATEGENERATOR")

    form = {
        "h_ReportProtoType_mode": "normal",
        "h_ReportProtoType_top": "10",
        "h_ReportProtoType_left": "40",
        "h_ReportProtoType_width": "680",
        "h_ReportProtoType_height": "570",
        "h_ReportProtoType_opacity": "100",
        "h_ReportProtoType_title": "Daily Attendance Report",
        "h_ReportProtoType_status": "",
        "h_ReportProtoType_url": "__null",
        "h_ReportProtoType_visible": "1",
        "h_ReportProtoType_zIndex": "1010",
        "__EVENTTARGET": "ReportProtoType$btn_GenerateReport",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": viewstate or "",
        "__VIEWSTATEGENERATOR": generator or "",
        "ReportProtoType$Drp_FromDateDD": str(from_day.day),
        "ReportProtoType$Drp_FromDateMM": str(from_day.month),
        "ReportProtoType$Drp_FromDateYYYY": str(from_day.year),
        "ReportProtoType$Drp_ToDateDD": str(to_day.day),
        "ReportProtoType$Drp_ToDateMM": str(to_day.month),
        "ReportProtoType$Drp_ToDateYYYY": str(to_day.year),
        "ReportProtoType$drp_ReportType": "CSV Export",
        "ReportProtoType$chk_RecalculateAttendance": "on",
    }
    log(f"Generating CSV export for {from_day.isoformat()} → {to_day.isoformat()} "
        "(Recalculate Attendance: ON)...")
    response = session.post(report_url, data=form, headers=headers, timeout=180, verify=False)
    response.raise_for_status()

    body = response.content
    log(f"Downloaded {len(body):,} bytes from the biometric portal.")
    if not body or len(body) < 20:
        raise FetchError(
            "Biometric portal returned an empty report for this date range."
        )
    lowered = body[:400].lower()
    if b"<html" in lowered or b"<!doctype" in lowered:
        raise FetchError(
            "Biometric portal returned a web page instead of a CSV - the report "
            "form was rejected (session expired or date format problem)."
        )

    path = target_dir / f"biometric_daily_{from_day.isoformat()}_{to_day.isoformat()}.csv"
    path.write_bytes(body)
    log(f"Saved raw data: {path.name}")
    return path, f"Biometric portal CSV ({from_day.isoformat()} → {to_day.isoformat()})"


# ---------------------------------------------------------------------------
# TimeChamp cloud API
# ---------------------------------------------------------------------------

def _timechamp_login(session, settings, log=None):
    """Authorise against TimeChamp and return the Bearer token."""
    log = log or (lambda message: None)
    base = settings["base_url"].rstrip("/")
    log(f"Connecting to TimeChamp {base} ...")
    response = session.post(
        f"{base}/backend/api/LoginApi/Authorise",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Referer": f"{base}/sessions/signin",
            "Origin": base,
        },
        json={"UserName": settings["username"], "Password": settings["password"]},
        timeout=30,
    )
    if response.status_code != 200:
        raise FetchError(
            f"TimeChamp login failed (HTTP {response.status_code}). Check the "
            "username/password in Settings → Portal sources."
        )
    try:
        token = (response.json().get("data") or {}).get("authToken") or ""
    except Exception:
        token = ""
    if not token:
        log("TimeChamp login FAILED — no token returned.")
        raise FetchError(
            "TimeChamp login was rejected (no token returned). Check the "
            "username/password in Settings → Portal sources."
        )
    log(f"TimeChamp login successful (token received for {settings['username']}).")
    return token


def fetch_timechamp(target_dir, from_day, to_day, settings=None, log=None):
    """Download the TimeChamp 'Working hours' attendance XLSX for the range."""
    log = log or (lambda message: None)
    settings = settings or load_settings().get("timechamp", {})
    _clamp_range(from_day, to_day)
    from_day, to_day = _future_guard(from_day, to_day)

    target_dir = Path(target_dir)
    base = settings["base_url"].rstrip("/")
    session = requests.Session()
    token = _timechamp_login(session, settings, log)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "CurrentCulture": "en",
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": f"{base}/activitytracker/activitydashboard/attendance",
        "Origin": base,
    }
    payload = {
        "screenName": "Working hours",
        "userId": None,
        "lineManagerId": None,
        "teamId": None,
        "dateFrom": from_day.isoformat(),
        "dateTo": to_day.isoformat(),
        "exportAs": "exel",
        "exportTitle": "Attendance report",
        "isDetailedReport": True,
        "timezoneOffset": "+05:30",
        "isTaskBasedTracking": False,
        "requestedTimeType": "workinghours",
        "conditionEnum": 0,
        "isAllUsersSelected": True,
        "conditionalHours": None,
        "splitBy": "day",
        "selectedColumns": (
            "employeeid,shift,email,role,contactnumber,timezone,desktop,department,"
            "branch,reporthead,desktopinstallationdate,teamname,reportheademailid,"
            "totaltime,workinghours,productivehours,unproductivetime,neutraltime,"
            "avgworkinghours,avgtotaltime,avgproductivehours,idletime,"
            "totaltimewithpercentage,workinghourspercentage,"
            "productivehourspercentage,unproductivetimewithpercentage,"
            "neutraltimewithpercentage,idletimewithpercentage,productiveIdleTime,"
            "unproductiveIdleTime,neutralIdleTime,awaytime,breaktime"
        ),
    }
    log(f"Requesting Working-hours XLSX export for {from_day.isoformat()} → "
        f"{to_day.isoformat()} (all users, split by day)...")
    response = session.post(
        f"{base}/backend/Timesheet/TimeSheetApi/DownloadAttendanceReportDetails",
        headers=headers, json=payload, timeout=300,
    )
    log(f"TimeChamp responded with HTTP {response.status_code}.")
    if response.status_code != 200:
        raise FetchError(
            f"TimeChamp export failed (HTTP {response.status_code}) for "
            f"{from_day.isoformat()} → {to_day.isoformat()}."
        )
    try:
        stream = (response.json().get("data") or {}).get("byteStream") or ""
    except Exception:
        stream = ""
    log(f"Received base64 payload ({len(stream):,} chars) — decoding to XLSX...")
    if not stream:
        raise FetchError(
            "TimeChamp returned no report data for this date range "
            "(maybe attendance was not recorded for those days yet)."
        )

    path = target_dir / f"timechamp_attendance_{from_day.isoformat()}_{to_day.isoformat()}.xlsx"
    try:
        path.write_bytes(base64.b64decode(stream))
    except Exception as exc:
        raise FetchError(f"TimeChamp report could not be decoded: {exc}") from exc
    if path.stat().st_size < 200:
        raise FetchError("TimeChamp report file looks empty for this date range.")
    log(f"Saved raw data: {path.name} ({path.stat().st_size:,} bytes)")
    return path, f"TimeChamp XLSX ({from_day.isoformat()} → {to_day.isoformat()})"


FETCHERS = {
    "biometric": fetch_biometric,
    "timechamp": fetch_timechamp,
}
