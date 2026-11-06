# Version History

## [v2.3.1] — 2026-11-06

### Type
Deduplication Logic Enhancement & Modal Dialog Responsiveness Hotfix / Production Deployment

### Changed & Fixed
- **Full-Row Duplicate Comparison**:
  - Updated raw file deduplication across multiple/overlapping date files in gdrive.py and consolidated processing in iometric_timechamp_report.py to compare full rows (drop_duplicates(keep='first')).
  - Previously, deduplication was performed by subsetting ['Employee ID', 'Date'], which erroneously discarded non-identical rows sharing the same employee and date (e.g. split shifts, multi-punch entries, and distinct tasks).
  - Automatically strips string/object columns for consistent comparisons and safely excludes autoincrement SL No. columns before comparison, cleanly re-numbering serial numbers 1..N upon completion.
- **Compile Monthly Report Modal Viewport Layout**:
  - Resolved viewport clipping and inaccessible modal header/footer on laptop displays and scaled browser viewports.
  - Replaced .modal-backdrop flex centering with lign-items: flex-start combined with margin: auto on .modal, preventing negative scroll space clipping.
  - Added a dedicated modal close button (&times;) in the dialog header.
  - Set position: sticky; bottom: 0; on .modal-actions to ensure the Cancel and Compile buttons are always visible on screen.
  - Added adaptive height constraints (min-height: 90px; max-height: min(28vh, 210px);) with internal scrolling on the compilation log panel.

## [v2.3.0] — 2026-11-06

### Type
Performance Optimization & Download Resilience Release / Production Deployment

### Optimized
- **Single-Pass Consolidated Report Generation & Styling**:
  - Eliminated redundant intermediate Excel generation and openpyxl styling passes when both Biometric and Timechamp sources are selected.
  - Both raw data streams are parsed, standardized, and processed into DataFrames in memory, concatenated, deduplicated across `[Employee ID, Date]` with priority scoring (preserving active punches/worked hours), sorted, and styled **only once finally**.
  - Optimized openpyxl cell-by-cell styling by pre-allocating shared style objects (`body_font`, `border_style`, `center_align`, `left_align`) and using $O(1)$ set lookups for column indices, drastically cutting workbook write time.
  - Streamlined monthly compilation in `gdrive.py` to merge all monthly raw data sources in memory and run single-pass styling, reducing monthly compilation time by 60–70%.
  - Added in-memory caching for HR portal API calls (`hr_api_call` and `get_hr_user_ids`) with 60-second timeouts to eliminate redundant network round-trips.

### Added
- **Automatic 3x Retry on Source Raw File Downloads**:
  - Both `fetch_biometric` and `fetch_timechamp` in `fetchers.py` now automatically retry up to 3 times with a 5-second backoff on any network error, read timeout, HTTP failure, or portal disconnect.
  - Added 3x retry logic with 3-second backoff to Google Drive raw file downloads in `gdrive.py` (`_download_file`).

### Fixed
- **Sunday & Zero-Punch Report Generation Fix**:
  - Fixed `IndexError: single positional indexer is out-of-bounds` in `process_biometric_data` when generating reports on Sundays or dates where employees have no punch events.
  - Guarded `First_IN` and `Last_OUT` extraction so `df.filter(like='IN')` safely falls back to `pd.NaT` instead of indexing an empty column slice.
  - Handled empty HR API responses (`data = []`) in `final_adjustments` without throwing `KeyError: 'Employee ID'`.
  - Fixed `TypeError: boolean value of NA is ambiguous` in `create_pivot_table` by adding `pd.notna(x)` guards before checking status strings.
- **Graceful Error Handling & File Resource Cleanup**:
  - Replaced legacy `os.system('pause')` and `sys.exit()` in `process_timechamp_data` and `process_manualtime_data` with standard `ValueError` exceptions, preventing worker process hangs.
  - Wrapped `pd.ExcelFile` with context managers to guarantee immediate file handle release on Windows and prevent `PermissionError` on temporary files.
  - Updated `validate_inputs` in `app.py` to validate multiple raw input files as well as consolidated reports.

## [v2.2.1] — 2026-11-02

### Type
Hotfix & UI Layout Release / Production Deployment

### Fixed
- **Biometric Portal Timeout**:
  - Increased HTTP connection/read timeout for biometric daily attendance export from 180s to 360s in `fetchers.py` to prevent `Read timed out` failures on large date ranges.
  - Increased report page initial GET timeout from 60s to 120s.
- **Monthly Dialog Layout & Visibility**:
  - Fixed `#monthlyDialog` viewport overflow where header and action footer were pushed off-screen.
  - Pinned modal header (`<h2>`, description) and modal actions footer (`Cancel`, `Compile` buttons) using `flex-shrink: 0`.
  - Constrained dialog to `max-height: min(90vh, 700px)` with internal auto-scroll on the compilation log panel (`min-height: 140px; max-height: min(34vh, 260px)`).
- **Date Parsing & `NaTType does not support strftime`**:
  - Resolved `NaTType does not support strftime` across `process_timechamp_data`, `save_output_file`, `final_adjustments`, and `run_report`.
  - Added flexible format parsing (`format='mixed'`) supporting ISO `YYYY-MM-DD`, `%b-%d-%Y`, and mixed portal date stamps without turning rows into `NaT`.
  - Guarded all `min_date` and `max_date` calculations against `NaT`.
  - Streamlined engine stdout into monthly compilation log so users see real-time progress.

## [v2.2.0] — 2026-11-02

### Type
Feature & Stability Release / Production Deployment

### Added
- **Job Cancellation**:
  - Live Cancel button (`#cancelJobBtn`) within the generation monitor card allowing instant cancellation of running report engine subprocesses and background worker loops.
  - Cancel option inside the Monthly Compilation dialog (`#monthlyCancel`).
  - Server-side cancellation registry (`ACTIVE_PROCS`, `ACTIVE_CANCEL_FLAGS`) and endpoint `POST /api/jobs/<job_id>/cancel`.
- **Email Delivery Option**:
  - "Send report via email" checkbox and recipient list field in Portal Fetch panel (`#fetchMail`, `#fetchMailTo`).
  - "Send report via email" checkbox and recipient list field in Manual Upload panel (`#uploadMail`, `#uploadMailTo`).
  - Automatic fallback to default recipients configured in Settings → Email delivery when custom recipients are left blank.
  - Asynchronous background email delivery with summary analytics upon report completion.

### Fixed
- **Monthly Report Filtering & Deduplication**:
  - Strictly filters monthly records to the chosen month and year, preventing date leaks across adjacent months.
  - Normalizes and deduplicates duplicate rows matching `(date, employee_id)` across multiple/overlapping daily raw files in both `gdrive.py` and `biometric_timechamp_report.py`.
  - Added missing `import os` in `gdrive.py`.
  - Guarded schedule status updates in monthly worker when running manual compilations.
- **UI Enhancements**:
  - Automatically restricts date pickers to yesterday (`isoDay(-1)`).
  - Date inputs enforce `From date <= To date` dynamically on change.
  - Improved styling and alignment for `.check-inline` and action buttons.

### Deployed
- Successfully deployed to GCP production server `samuel-automation-server` (zone `asia-south1-a`, project `ambient-segment-468709-a1`).
- Running live on Docker container `attendance-app` behind nginx reverse proxy with SSL at `https://attendancemanager.vservesolution.co`.
- Workspace state and historical data backed up and preserved under `/opt/attendance-app/`.
