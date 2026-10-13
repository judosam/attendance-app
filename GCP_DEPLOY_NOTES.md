# GCP Deployment — Quick Notes

Companion to `GCP_Deployment_Runbook.docx`. VM: `attendance-app` (asia-south1-b),
static IP `34.47.143.21`, systemd service `attendance`, gunicorn on port 8080,
OS user `automationtool`, app dir `/home/automationtool/app`.

## Scheduled runs without opening the app (why this works now)

The auto-fetch scheduler used to be started only from `main()` (`python app.py`),
which never executes under gunicorn. Two changes make schedules fire on the VM
with **no browser open**:

1. **`app.py`** — `start_scheduler_once()` starts the scheduler thread exactly
   once per process. It is triggered from a Flask `before_request` hook (covers
   any request) and from `main()` (local runs).
2. **`gunicorn.conf.py`** — `post_worker_init` hook calls `start_scheduler_once()`
   the moment the gunicorn worker boots, so the scheduler runs 24/7 on the VM
   even when nobody opens the UI.

Keep the systemd service at **one worker** (`--threads` only): each extra worker
process would run its own scheduler and fire duplicate fetches.

## Deploying an update

```powershell
# 1. Build the code-only zip (workspace/, secrets and local data are excluded)
powershell -ExecutionPolicy Bypass -File create_update_zip.ps1

# 2. Upload + backup VM workspace + unzip + pip install + restart + verify
powershell -ExecutionPolicy Bypass -File deploy-to-gcp.ps1 -Zip app1-update-<stamp>.zip
```

Sign in to gcloud first (`gcloud auth login`) — tokens expire every so often and
gcloud errors with "Reauthentication failed" until you re-run it.

The zip contains code only: the VM's live `workspace/` (history, schedules,
portal/SMTP credentials, Drive service-account key, job folders) is never
overwritten. The deploy script also snapshots it to
`/home/automationtool/workspace-backup-<stamp>` first.

### Notes / gotchas learned on the way

- The restart step does **not** use sudo: `kill <master pid>` makes systemd's
  `Restart=always` restart the service immediately with the new code. (`pkill -f`
  patterns tend to match the SSH command itself and self-kill the session.)
- `unzip` may report an error for Windows-built zips (backslash path separators)
  even though extraction succeeds — verify with `ls` before assuming failure.
- The VM runs in IST (`Asia/Kolkata`): schedule times are local wall-clock times.

## Quick reference

```bash
# service status / logs
gcloud compute ssh attendance-app --zone=asia-south1-b --command="systemctl status attendance --no-pager"
gcloud compute ssh attendance-app --zone=asia-south1-b --command="journalctl -u attendance -n 50 --no-pager"

# restart (no sudo needed)
gcloud compute ssh attendance-app --zone=asia-south1-b --command="kill \$(pgrep -o -f gunicorn)"

# app health
curl -s -o /dev/null -w "%{http_code}\n" http://34.47.143.21:8080/
```

## Recommended follow-ups (from the runbook, still open)

- Change `AW_USERNAME` / `AW_PASSWORD` away from `admin` / `admin123` — the app
  is reachable from the whole internet on port 8080.
- Restrict firewall rule `allow-8080` to office/VPN IPs if possible.
- Move credentials (portal passwords, SMTP, Drive key) into Secret Manager.
- Add a domain + HTTPS in front of the raw IP.
