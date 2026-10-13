"""Report analytics — read a finished workbook and summarize it for the web UI.

Pure pandas over the workbook the engine produces ("Detailed Report" +
"Overall Summary" sheets). No engine code is touched: this is a read-back
layer that computes the metrics dictionary used by the Dashboard and the
per-report Summary page, exactly mirroring what an Excel user would see by
summing the sheets.

    from report_analytics import analyze_workbook
    summary = analyze_workbook(path_to_xlsx)
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

# Bump when the analytics math changes: cached analytics.json files with an
# older version are recomputed instead of served stale.
ANALYTICS_VERSION = 2

DETAILED = "Detailed Report"
OVERALL = "Overall Summary"
HIGHLIGHTED = "Highlighted Users"

DETAILED_DATE = "Date"
DETAILED_EMP_ID = "Employee ID"
DETAILED_EMP_NAME = "Employee Name"
DETAILED_TEAM = "Team/Department"
DETAILED_ATTENDANCE = "Attendance"
DETAILED_MISMATCH = "Attendance Mismatch"

OVERALL_EMP_ID = "Employee ID"
OVERALL_EMP_NAME = "Employee Name"
OVERALL_TEAM = "Team/Department"

# Engine breach columns on the Overall Summary sheet (per employee / period).
BREACH_COLUMNS = {
    "late": "No of late login",
    "early": "No of Early Logout",
    "under4": "No of Worked below 4 hrs",
    "under8": "No of Worked below 8 hrs",
    "break_hours": "No of More Break Hours",
    "break_counts": "No of Break Counts more than 4 Times (Biometric)",
}

# Canonical attendance statuses (engine writes "Full day" / "Half day").
_STATUS_TITLE = {"full day": "Full Day", "half day": "Half Day"}
_PRESENT = {"Full Day", "Half Day"}
_ABSENT = {"Absent"}
_OFF = {"WeekOff", "Week Off", "Holiday", "Paid Holiday", "Weekoff"}


def _title_status(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return _STATUS_TITLE.get(text.lower(), text)


def _present_absent_off(status: str) -> str:
    if status in _PRESENT:
        return "present"
    if status in _ABSENT:
        return "absent"
    if status in _OFF:
        return "off"
    return "other"


def _is_employee_row(row) -> bool:
    emp = row.get(OVERALL_EMP_ID)
    if emp is None or (isinstance(emp, float) and math.isnan(emp)):
        return False
    name = str(row.get(OVERALL_EMP_NAME, "") or "").strip().lower()
    if name in ("", "nan", "grand total", "total", "totals"):
        return False
    return True


def _series_sum(series) -> float:
    return float(pd.to_numeric(series, errors="coerce").fillna(0).sum())


def _fmt_date(value):
    """Normalize an engine date cell (string 'YYYY-MM-DD' or datetime)."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text[:10]


def analyze_workbook(xlsx_path) -> dict:
    """Compute the analytics dictionary for one generated report workbook."""
    path = Path(xlsx_path)
    source = "Detailed Report"

    with pd.ExcelFile(path) as book:
        names = book.sheet_names
        if DETAILED not in names and HIGHLIGHTED not in names:
            raise ValueError(
                f"“{path.name}” does not look like a generated attendance report "
                "(no Detailed Report / Highlighted Users sheet)."
            )
        detailed_sheet = DETAILED if DETAILED in names else HIGHLIGHTED
        if OVERALL not in names:
            raise ValueError(
                f"“{path.name}” has no Overall Summary sheet, so analytics cannot be computed."
            )
        detailed = pd.read_excel(book, sheet_name=detailed_sheet)
        overall = pd.read_excel(book, sheet_name=OVERALL)

    detailed = detailed.dropna(subset=[DETAILED_EMP_ID])
    detailed = detailed[~detailed[DETAILED_EMP_ID].astype(str).str.lower().isin(
        ["nan", "total", "grand total"]
    )]
    # Consolidated workbooks repeat the same employee-day once per source
    # block (Biometric + Timechamp). Keep one row per employee-day so the
    # attendance mix, mismatch counts and department stats are not inflated.
    if DETAILED_DATE in detailed.columns:
        detailed = detailed.drop_duplicates(
            subset=[DETAILED_EMP_ID, DETAILED_DATE], keep="first"
        )

    statuses = detailed[DETAILED_ATTENDANCE].map(_title_status)
    detail_rows = int(len(detailed))
    employees = int(detailed[DETAILED_EMP_ID].nunique())

    # Attendance mix — counts and shares of each canonical status.
    counts = statuses.value_counts().to_dict()
    order = ["Full Day", "Half Day", "Absent", "WeekOff", "Week Off"]
    order += [k for k in counts if k not in order]
    mix = []
    for label in order:
        count = int(counts.get(label, 0))
        if count:
            mix.append({
                "label": label,
                "count": count,
                "pct": round(count * 100.0 / detail_rows, 1) if detail_rows else 0.0,
            })

    # HR cross-check: mismatches are marked YES in the engine output.
    mismatch_col = detailed[DETAILED_MISMATCH]
    mismatches = int(mismatch_col.fillna("").astype(str).str.upper()
                     .eq("YES").sum())
    agreement_pct = round(
        (detail_rows - mismatches) * 100.0 / detail_rows, 1
    ) if detail_rows else 100.0

    # Overall Summary: per-employee aggregates (some rows may be section
    # headers / totals — only count real employee rows).
    valid = overall[overall.apply(_is_employee_row, axis=1)]
    # Consolidated workbooks contain one block per source, so the same
    # employee can appear twice (name variants like "Sundar Raj .A" vs
    # "Sundar Raj.A" get separate rows). Collapse to one row per employee,
    # taking each metric's best (max) value across the source blocks.
    metric_cols = [c for c in valid.columns
                   if c not in (OVERALL_EMP_ID, OVERALL_EMP_NAME, OVERALL_TEAM)]
    valid = valid.copy()
    for col in metric_cols:
        valid[col] = pd.to_numeric(valid[col], errors="coerce")
    names = (valid.groupby(OVERALL_EMP_ID)[[OVERALL_EMP_NAME, OVERALL_TEAM]]
             .first())
    valid = valid.groupby(OVERALL_EMP_ID, as_index=False).max(numeric_only=True)
    valid[OVERALL_EMP_NAME] = valid[OVERALL_EMP_ID].map(names[OVERALL_EMP_NAME])
    valid[OVERALL_TEAM] = valid[OVERALL_EMP_ID].map(names[OVERALL_TEAM])

    worked_days = _series_sum(valid.get("No of Worked Days"))
    worked_days_attendance = _series_sum(valid.get("No of Worked Days (Attendance)"))

    breach_totals = {}
    for key, column in BREACH_COLUMNS.items():
        if column in valid.columns:
            breach_totals[key] = int(round(_series_sum(valid[column])))

    people_flagged = 0
    per_employee = None
    if valid is not None and len(valid):
        flag_cols = [c for c in BREACH_COLUMNS.values() if c in valid.columns]
        if flag_cols:
            per_employee = valid.assign(
                _flags=pd.to_numeric(
                    valid[flag_cols].sum(axis=1, skipna=True), errors="coerce"
                ).fillna(0)
            )
            per_employee = per_employee.groupby(
                OVERALL_EMP_ID, as_index=False
            ).agg({
                OVERALL_EMP_NAME: "first",
                OVERALL_TEAM: "first",
                **{c: "sum" for c in flag_cols},
                "_flags": "sum",
            })
            people_flagged = int((per_employee["_flags"] > 0).sum())
    total_flags = int(sum(breach_totals.values()))

    # Hours totals from the Overall sheet.
    hours = {}
    for key, column in {
        "hr_hours": "Total HR Attendance Hours",
        "worked_hours": "Total Worked Hours",
        "break_hours_total": "Total Break Hours",
        "diff": "Worked Hours Diff",
    }.items():
        if column in valid.columns:
            hours[key] = round(_series_sum(valid[column]), 2)

    # Top flagged employees (by total flags across the breach columns).
    flagged = []
    if per_employee is not None and len(per_employee):
        for _, row in per_employee.sort_values("_flags", ascending=False).head(25).iterrows():
            person = {
                "id": str(row.get(OVERALL_EMP_ID, "")).strip(),
                "name": str(row.get(OVERALL_EMP_NAME, "") or "").strip(),
                "team": str(row.get(OVERALL_TEAM, "") or "").strip(),
                "flags": int(round(float(row["_flags"]))),
            }
            for key, column in BREACH_COLUMNS.items():
                value = row.get(column, 0)
                person[key] = int(round(float(value))) if value == value else 0
            flagged.append(person)

    # Department breakdown (from the Detailed sheet's team column).
    team = detailed[DETAILED_TEAM].fillna("—").astype(str).replace("nan", "—")
    grouped = detailed.assign(
        _status=statuses,
        _outcome=statuses.map(_present_absent_off),
        _team=team,
        _mismatch=mismatch_col.fillna("").astype(str).str.upper().eq("YES"),
    )
    dept = []
    for name, group in grouped.groupby("_team", sort=False):
        if len(group) < 1:
            continue
        dept.append({
            "name": name,
            "rows": int(len(group)),
            "staff": int(group[DETAILED_EMP_ID].nunique()),
            "present": int((group["_outcome"] == "present").sum()),
            "absent": int((group["_outcome"] == "absent").sum()),
            "off": int((group["_outcome"] == "off").sum()),
            "mismatch": int(group["_mismatch"].sum()),
        })
    dept.sort(key=lambda d: d["rows"], reverse=True)
    dept = dept[:8]

    # Daily trend: per-date present / absent / off / mismatch counts.
    dates = detailed[DETAILED_DATE].map(_fmt_date)
    daily_grouped = grouped.assign(_date=dates).dropna(subset=["_date"])
    daily = []
    for day, group in daily_grouped.groupby("_date", sort=True):
        daily.append({
            "date": day,
            "present": int((group["_outcome"] == "present").sum()),
            "absent": int((group["_outcome"] == "absent").sum()),
            "off": int((group["_outcome"] == "off").sum()),
            "mismatch": int(group["_mismatch"].sum()),
        })

    date_values = sorted(
        d for d in dates.dropna().unique() if isinstance(d, str)
    )
    return {
        "version": ANALYTICS_VERSION,
        "meta": {
            "rows": detail_rows,
            "employees": employees,
            "source": str(detailed["Source"].dropna().iloc[0]).strip()
                       if "Source" in detailed.columns and len(detailed) else "",
            "date_min": date_values[0] if date_values else None,
            "date_max": date_values[-1] if date_values else None,
        },
        "mix": mix,
        "hr": {"mismatches": mismatches, "agreement_pct": agreement_pct},
        "worked": {
            "days": round(worked_days, 1),
            "days_attendance": round(worked_days_attendance, 1),
        },
        "breaches": {
            **breach_totals,
            "people": people_flagged,
            "total_flags": total_flags,
        },
        "hours": hours,
        "flagged": flagged,
        "dept": dept,
        "daily": daily,
    }


def save_analytics(xlsx_path: Path, output_json: Path) -> dict:
    """Compute analytics for a workbook and cache them next to the job."""
    result = analyze_workbook(xlsx_path)
    output_json.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        sys.exit("usage: python report_analytics.py <report.xlsx>")
    payload = analyze_workbook(sys.argv[1])
    print(json.dumps(payload, indent=2, ensure_ascii=False))
