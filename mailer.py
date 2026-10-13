"""SMTP mail sending with the generated report attached.

Settings live in ``workspace/settings.json`` (shared with portal settings,
``mail`` section) and are editable in the UI:

    mail: {
        enabled, smtp_host, smtp_port, username, password,
        from_addr, use_tls, recipients   # recipients = list or comma string
    }

``send_report_email`` raises ``MailError`` with a clear message so the caller
can log it / surface it as a toast; it never crashes the run pipeline.
"""

import json
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "workspace" / "settings.json"

DEFAULT_MAIL = {
    "enabled": False,
    "smtp_host": "",
    "smtp_port": 587,
    "username": "",
    "password": "",
    "from_addr": "",
    "use_tls": True,
    "recipients": "",  # comma-separated string
}


class MailError(Exception):
    """Raised when sending fails (config or SMTP error)."""


def load_mail_settings():
    settings = dict(DEFAULT_MAIL)
    if SETTINGS_FILE.is_file():
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(stored.get("mail"), dict):
                settings.update({
                    key: value for key, value in stored["mail"].items()
                    if value is not None
                })
        except Exception:
            pass
    try:
        settings["smtp_port"] = int(settings.get("smtp_port") or 587)
    except (TypeError, ValueError):
        settings["smtp_port"] = 587
    settings["enabled"] = bool(settings.get("enabled"))
    settings["use_tls"] = bool(settings.get("use_tls", True))
    return settings


def save_mail_settings(mail):
    """Merge the mail block into workspace/settings.json."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if SETTINGS_FILE.is_file():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    current = load_mail_settings()
    for key in DEFAULT_MAIL:
        incoming = mail.get(key)
        # Empty password means "keep the stored one" (masked in the UI).
        if key == "password" and (incoming is None or str(incoming).strip() == ""):
            continue
        if incoming is not None:
            current[key] = incoming
    data["mail"] = current
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return current


def public_mail_settings():
    settings = load_mail_settings()
    return {
        "enabled": settings["enabled"],
        "smtp_host": settings["smtp_host"],
        "smtp_port": settings["smtp_port"],
        "username": settings["username"],
        "has_password": bool(settings.get("password")),
        "from_addr": settings["from_addr"],
        "use_tls": settings["use_tls"],
        "recipients": settings["recipients"],
    }


def _recipients(settings):
    raw = settings.get("recipients") or ""
    if isinstance(raw, list):
        items = raw
    else:
        items = [part.strip() for part in str(raw).replace(";", ",").split(",")]
    return [item for item in items if "@" in item]


def validate_mail_settings(settings):
    """Return an error string for incomplete config, or None when usable."""
    if not settings.get("smtp_host"):
        return "SMTP host is missing."
    if not _recipients(settings):
        return "Add at least one recipient email address."
    return None


def _mix_row(label, count, pct, color):
    """One row of the attendance-mix table (bar cell + legend)."""
    bar = (
        f'<td style="padding:4px 0;">'
        f'<div style="height:10px;border-radius:6px;background:{color};'
        f'width:{max(pct, 1.5)}%;min-width:6px;"></div></td>'
    )
    return (
        f'<tr>{bar}'
        f'<td style="padding:4px 0 4px 12px;font-size:13px;color:#0f172a;'
        f'white-space:nowrap;font-family:Arial,sans-serif;"><strong>{label}</strong>'
        f'&nbsp;&nbsp;<span style="color:#64748b;">{count:,} · {pct}%</span></td>'
        f'</tr>'
    )


def _stat_card(value, label, color="#0f172a"):
    """Small stat card cell used in the header stats row."""
    return (
        '<td style="background:#f1f5f9;border-radius:10px;padding:12px 10px;'
        'text-align:center;width:25%;">'
        f'<div style="font-size:20px;font-weight:700;color:{color};'
        'font-family:Arial,sans-serif;letter-spacing:-0.5px;">' + value + '</div>'
        '<div style="font-size:10px;font-weight:600;color:#64748b;'
        'text-transform:uppercase;letter-spacing:0.6px;padding-top:3px;'
        'font-family:Arial,sans-serif;">' + label + '</div>'
        '</td>'
    )


MIX_COLORS = {
    "Full Day": "#16a34a",
    "Half Day": "#d97706",
    "Absent": "#dc2626",
}


def render_stats_html(stats, report_name="", meta_note=""):
    """Modern branded HTML body with attendance statistics.

    ``stats`` is the analytics dictionary produced by report_analytics
    (keys: meta, mix, hr, breaches). Missing keys degrade gracefully.
    """
    meta = stats.get("meta") or {}
    mix = stats.get("mix") or []
    hr = stats.get("hr") or {}
    breaches = stats.get("breaches") or {}
    flagged = stats.get("flagged") or []

    rows = meta.get("rows") or 0
    employees = meta.get("employees") or 0
    full = next((m for m in mix if m["label"] == "Full Day"), None)
    agreement = hr.get("agreement_pct")
    total_flags = breaches.get("total_flags") or 0

    cards = _stat_card(f"{rows:,}", "Records") + _stat_card(f"{employees:,}", "Employees")
    if full:
        cards += _stat_card(f"{full['pct']}%", "Full Day", "#16a34a")
    cards += _stat_card(
        f"{agreement}%" if agreement is not None else "—",
        "HR Agreement", "#2563eb" if (agreement or 0) >= 90 else "#d97706",
    )

    # Attendance mix table (color + count + pct — never color alone).
    mix_rows = "".join(
        _mix_row(m["label"], m["count"], m["pct"], MIX_COLORS.get(m["label"], "#94a3b8"))
        for m in mix[:6]
    ) or (
        '<tr><td style="padding:6px 0;font-size:13px;color:#64748b;'
        'font-family:Arial,sans-serif;">No attendance rows summarized.</td></tr>'
    )

    # Compliance snapshot: three compact figures + top flagged employee.
    late = breaches.get("late")
    early = breaches.get("early")
    top = flagged[0] if flagged else None
    compliance_items = []
    if late is not None:
        compliance_items.append(("Late logins", f"{late:,}"))
    if early is not None:
        compliance_items.append(("Early logouts", f"{early:,}"))
    if total_flags:
        compliance_items.append(("Total flags", f"{total_flags:,}"))
    compliance_html = "".join(
        '<td style="padding:6px 14px 6px 0;font-size:13px;font-family:Arial,sans-serif;'
        f'color:#334155;"><strong style="color:#0f172a;">{val}</strong> '
        f'<span style="color:#94a3b8;font-size:12px;">{lbl}</span></td>'
        for lbl, val in compliance_items
    ) or (
        '<td style="padding:6px 0;font-size:13px;color:#64748b;'
        'font-family:Arial,sans-serif;">No compliance flags recorded.</td>'
    )
    top_html = (
        '<div style="margin-top:10px;padding:10px 12px;background:#fef2f2;'
        'border:1px solid #fecaca;border-radius:10px;font-size:13px;'
        'font-family:Arial,sans-serif;color:#7f1d1d;">'
        f'<strong>Top flagged:</strong> {top["name"]}'
        + (f' · {top["team"]}' if top.get("team") else '')
        + f' — {top["flags"]} flags'
        '</div>'
    ) if top else ""

    date_note = ""
    if meta.get("date_min") and meta.get("date_max"):
        date_note = (
            f'<span style="color:#64748b;font-size:13px;font-family:Arial,sans-serif;">'
            f'Date range: <strong style="color:#0f172a;">{meta["date_min"]} '
            f'&rarr; {meta["date_max"]}</strong></span>'
        )
    note = (
        f'<div style="margin-top:6px;">{date_note}</div>'
        f'<div style="margin-top:2px;font-size:12px;color:#94a3b8;'
        f'font-family:Arial,sans-serif;">{meta_note}</div>' if (date_note or meta_note) else ""
    )

    return (
        '<div style="max-width:600px;margin:0 auto;background:#ffffff;'
        'border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;'
        'font-family:Arial,sans-serif;">'
        '<div style="background:linear-gradient(135deg,#2563eb,#1d4ed8);'
        'padding:18px 24px;">'
        '<div style="font-size:16px;font-weight:700;color:#ffffff;'
        'letter-spacing:-0.2px;">Attendance Workbench</div>'
        '<div style="font-size:12px;color:#bfdbfe;padding-top:2px;">'
        'Biometric &amp; Verification Engine</div>'
        '</div>'
        '<div style="padding:22px 24px;">'
        f'<div style="font-size:17px;font-weight:700;color:#0f172a;'
        f'letter-spacing:-0.3px;">{report_name or "Attendance Report"}</div>'
        f'{note}'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin-top:16px;border-collapse:separate;border-spacing:6px 0;">'
        f'<tr>{cards}</tr></table>'
        '<div style="font-size:11px;font-weight:600;color:#64748b;'
        'text-transform:uppercase;letter-spacing:0.6px;margin:20px 0 6px;">'
        'Attendance mix</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f'{mix_rows}</table>'
        '<div style="font-size:11px;font-weight:600;color:#64748b;'
        'text-transform:uppercase;letter-spacing:0.6px;margin:18px 0 6px;">'
        'Compliance snapshot</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'{compliance_html}</tr></table>{top_html}'
        '</div>'
        '<div style="padding:14px 24px;background:#f8fafc;border-top:1px solid #e2e8f0;'
        'font-size:12px;color:#94a3b8;font-family:Arial,sans-serif;">'
        'The report workbook is attached &middot; Generated automatically by '
        'Attendance Workbench</div>'
        '</div>'
    )


def render_plain_text(stats, report_name="", meta_note=""):
    """Plain-text twin of the stats email (fallback for simple clients)."""
    meta = stats.get("meta") or {}
    lines = [report_name or "Attendance Report", ""]
    if meta.get("rows"):
        lines.append(f"Records: {meta['rows']:,}  Employees: {meta.get('employees', 0):,}")
    for m in (stats.get("mix") or [])[:6]:
        lines.append(f"  {m['label']}: {m['count']:,} ({m['pct']}%)")
    hr = stats.get("hr") or {}
    if hr.get("agreement_pct") is not None:
        lines.append(f"HR agreement: {hr['agreement_pct']}%  "
                     f"Mismatches: {hr.get('mismatches', 0):,}")
    breaches = stats.get("breaches") or {}
    if breaches.get("total_flags"):
        lines.append(f"Compliance flags: {breaches['total_flags']:,}")
    if meta_note:
        lines.append(""); lines.append(meta_note)
    lines.append(""); lines.append("The report workbook is attached.")
    lines.append("Generated automatically by Attendance Workbench.")
    return "\n".join(lines)


def send_report_email(attachment_path, subject=None, body=None, settings=None,
                      log=None, stats=None, meta_note=""):
    """Email a report file (or a plain test email when path is None).

    When ``stats`` (analytics dict) is provided the email renders a modern
    HTML summary with stat cards; otherwise a clean branded note is sent.
    Always multipart: plain text + HTML so every client shows something.
    """
    log = log or (lambda message: None)
    settings = settings or load_mail_settings()

    problem = validate_mail_settings(settings)
    if problem:
        raise MailError(problem)
    if attachment_path is not None:
        attachment_path = Path(attachment_path)
        if not attachment_path.is_file():
            raise MailError(f"Report file not found: {attachment_path.name}")

    report_name = Path(attachment_path).stem if attachment_path is not None else ""
    if stats:
        text_body = body or render_plain_text(stats, report_name, meta_note)
        html_body = render_stats_html(stats, report_name, meta_note)
    else:
        text_body = body or (
            "The attendance report is attached.\n\n"
            "Generated automatically by Attendance Workbench."
        )
        html_body = (
            '<div style="max-width:600px;margin:0 auto;font-family:Arial,sans-serif;">'
            '<div style="background:linear-gradient(135deg,#2563eb,#1d4ed8);'
            'border-radius:14px 14px 0 0;padding:18px 24px;">'
            '<div style="font-size:16px;font-weight:700;color:#ffffff;">'
            'Attendance Workbench</div>'
            '<div style="font-size:12px;color:#bfdbfe;padding-top:2px;">'
            'Biometric &amp; Verification Engine</div></div>'
            '<div style="border:1px solid #e2e8f0;border-top:none;'
            'border-radius:0 0 14px 14px;padding:20px 24px;font-size:14px;'
            'color:#334155;">'
            + (f'<div style="font-size:16px;font-weight:700;color:#0f172a;'
               f'padding-bottom:8px;">{report_name}</div>' if report_name else '')
            + text_body.replace("\n", "<br>") +
            '</div></div>'
        )

    recipients = _recipients(settings)
    from_addr = settings.get("from_addr") or settings.get("username")
    message = EmailMessage()
    message["Subject"] = subject or (
        f"Attendance Report — {attachment_path.stem}" if attachment_path
        else "Attendance Workbench"
    )
    message["From"] = from_addr or "Attendance Workbench"
    message["To"] = ", ".join(recipients)
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    if attachment_path is not None:
        message.add_attachment(
            attachment_path.read_bytes(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=attachment_path.name,
        )

    host = settings["smtp_host"]
    port = int(settings.get("smtp_port") or 587)
    use_tls = bool(settings.get("use_tls", True))
    username = settings.get("username")
    password = settings.get("password")

    log(f"Connecting to SMTP {host}:{port} "
        f"({'STARTTLS' if use_tls else 'no encryption'})...")
    try:
        if use_tls:
            with smtplib.SMTP(host, port, timeout=60) as server:
                server.ehlo()
                try:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                    log("TLS session started.")
                except smtplib.SMTPNotSupportedError:
                    log("Server does not support STARTTLS — continuing without it.")
                if username and password:
                    server.login(username, password)
                    log(f"SMTP login OK ({username}).")
                server.send_message(message)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=60,
                                  context=ssl.create_default_context()) as server:
                if username and password:
                    server.login(username, password)
                    log(f"SMTP login OK ({username}).")
                server.send_message(message)
    except (smtplib.SMTPAuthenticationError,) as exc:
        raise MailError(
            f"SMTP login rejected by {host} — check the username/password. ({exc})"
        ) from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailError(f"Could not send the email via {host}:{port} — {exc}") from exc

    if attachment_path is not None:
        log(f"Email sent to {', '.join(recipients)} with “{attachment_path.name}” attached.")
    else:
        log(f"Test email sent to {', '.join(recipients)}.")
    return recipients
