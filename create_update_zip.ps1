# Build the code-only update zip for the GCP VM (GCP_Deployment_Runbook.docx, section 8).
#
# NEVER include workspace/ in this zip: the VM holds live history.json,
# schedules.json, settings.json, gdrive_service_account.json and job folders.
#
# Run:  powershell -ExecutionPolicy Bypass -File create_update_zip.ps1
$ErrorActionPreference = "Stop"

$stamp  = Get-Date -Format "yyyyMMdd-HHmmss"
$zip    = "app1-update-$stamp.zip"
$files  = @(
    "app.py",
    "gunicorn.conf.py",
    "fetchers.py",
    "gdrive.py",
    "mailer.py",
    "report_analytics.py",
    "biometric_timechamp_report.py",
    "requirements.txt",
    "static",
    "templates"
)

foreach ($f in $files) {
    if (-not (Test-Path $f)) {
        Write-Error "Missing required file/folder: $f"
    }
}

if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $files -DestinationPath $zip -Force

Write-Host ""
Write-Host "Created $zip ($([math]::Round((Get-Item $zip).Length / 1KB)) KB)"
Write-Host "Next step:"
Write-Host "  powershell -ExecutionPolicy Bypass -File deploy-to-gcp.ps1 -Zip $zip"
