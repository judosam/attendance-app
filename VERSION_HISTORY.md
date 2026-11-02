# Version History

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
