# Deploy a code-only update zip to the attendance-app VM
# (GCP_Deployment_Runbook.docx, section 8: upload, back up workspace/,
# unzip over ~/app, restart the systemd service, verify).
#
# Run:  powershell -ExecutionPolicy Bypass -File deploy-to-gcp.ps1 -Zip app1-update-<stamp>.zip
#
# The gcloud CLI must already be signed in:  gcloud auth login
param(
    [Parameter(Mandatory = $true)]
    [string]$Zip,

    [string]$Vm   = "attendance-app",
    [string]$Zone = "asia-south1-b",
    [string]$User = "automationtool"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Zip)) {
    Write-Error "Zip not found: $Zip"
}

function Invoke-Ssh([string]$Title, [string]$Command) {
    Write-Host ""
    Write-Host "==> $Title"
    & gcloud compute ssh $Vm --zone=$Zone --command=$Command
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Step failed: $Title (gcloud exit code $LASTEXITCODE)"
    }
}

Write-Host "==> Uploading $Zip to $Vm ($Zone) ..."
& gcloud compute scp $Zip "${Vm}:/home/$User/" --zone=$Zone
if ($LASTEXITCODE -ne 0) {
    Write-Error "Upload failed (gcloud exit code $LASTEXITCODE)"
}

$baseName = [IO.Path]::GetFileName($Zip)
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

Invoke-Ssh "Backing up the live workspace/ folder" `
    "cp -r /home/$User/app/workspace /home/$User/workspace-backup-$stamp"

Invoke-Ssh "Unzipping code over ~/app (workspace/ untouched)" `
    "unzip -o -qq /home/$User/$baseName -d /home/$User/app ; s=\$? ; rm -f /home/$User/$baseName ; exit \$s"

Invoke-Ssh "Installing dependencies (no-op if unchanged)" `
    "/home/$User/app/venv/bin/pip install -q -r /home/$User/app/requirements.txt"

# No sudo needed: killing the gunicorn process makes systemd's
# Restart=always bring the service straight back up with the new code.
Invoke-Ssh "Restarting the attendance service" `
    "pkill -f 'gunicorn.*app[:a]pp' ; sleep 4 ; systemctl is-active attendance ; curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8080/"

Write-Host ""
Write-Host "Deployed. Verify in the browser:"
Write-Host "  http://34.47.143.21:8080"
Write-Host "Live logs:  gcloud compute ssh $Vm --zone=$Zone --command=""journalctl -u attendance -n 50 --no-pager"""
Write-Host "Backup at:  /home/$User/workspace-backup-$stamp"
