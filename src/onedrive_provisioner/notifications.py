"""Notification service — Teams webhooks and email reminders via Graph API."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

REMINDER_HOURS = 48  # Send reminder this many hours before scheduled action


class NotificationService:
    """Send notifications via Teams Incoming Webhook and Microsoft Graph email."""

    def __init__(
        self,
        teams_webhook_url: str = "",
        power_automate_url: str = "",
        graph_token_provider=None,  # MsalTokenProvider for Graph sendMail
        sender_upn: str = "",       # UPN to send email from (must have Mail.Send)
    ):
        self._webhook_url = teams_webhook_url
        self._pa_url = power_automate_url
        self._token_provider = graph_token_provider
        self._sender_upn = sender_upn

    # ────────────────── Teams Webhook ──────────────────

    def send_teams_message(self, title: str, text: str, facts: List[Dict[str, str]] = None, color: str = "0076D7"):
        """Post an Adaptive Card-style message to a Teams Incoming Webhook."""
        if not self._webhook_url:
            logger.debug("Teams webhook URL not configured; skipping notification")
            return False

        card = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": color,
            "summary": title,
            "sections": [{
                "activityTitle": title,
                "activitySubtitle": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "text": text,
                "facts": facts or [],
                "markdown": True,
            }],
        }

        try:
            resp = httpx.post(self._webhook_url, json=card, timeout=15)
            resp.raise_for_status()
            logger.info("Teams notification sent: %s", title)
            return True
        except Exception as exc:
            logger.warning("Failed to send Teams webhook: %s", exc)
            return False

    # ────────────────── Power Automate HTTP Trigger ──────────────────

    def send_power_automate_event(self, flow_url: str, event_type: str,
                                   payload: Dict[str, Any] = None):
        """POST a structured JSON event to a Power Automate HTTP trigger."""
        if not flow_url:
            return False

        body = {
            "source": "spektra-hackops",
            "eventType": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload or {},
        }

        try:
            resp = httpx.post(flow_url, json=body, timeout=30)
            resp.raise_for_status()
            logger.info("Power Automate event sent: %s", event_type)
            return True
        except Exception as exc:
            logger.warning("Failed to send Power Automate event: %s", exc)
            return False

    # ────────────────── Graph Email ──────────────────

    def send_email(self, to_emails: List[str], subject: str, body_html: str):
        """Send an email via Microsoft Graph sendMail API."""
        if not self._token_provider or not self._sender_upn:
            logger.debug("Graph email not configured; skipping email notification")
            return False

        token = self._token_provider.get_token()
        if not token:
            logger.warning("Could not acquire Graph token for email")
            return False

        message = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": e}} for e in to_emails],
            },
            "saveToSentItems": "false",
        }

        try:
            resp = httpx.post(
                f"https://graph.microsoft.com/v1.0/users/{self._sender_upn}/sendMail",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=message,
                timeout=30,
            )
            resp.raise_for_status()
            logger.info("Email sent to %s: %s", to_emails, subject)
            return True
        except Exception as exc:
            logger.warning("Failed to send email: %s", exc)
            return False

    # ────────────────── Lifecycle Notifications ──────────────────

    def _also_pa(self, event_type: str, data: Dict[str, Any]):
        """Also send to Power Automate if configured."""
        if self._pa_url:
            self.send_power_automate_event(self._pa_url, event_type, data)

    def notify_batch_complete(self, prefix: str, hack_name: str, user_count: int, failed: int = 0):
        color = "28A745" if failed == 0 else "FFC107"
        status = "completed successfully" if failed == 0 else f"completed with {failed} failure(s)"
        self.send_teams_message(
            f"✅ Provisioning {status}",
            f"Hack **{hack_name}** (`{prefix}`) has been provisioned.",
            facts=[
                {"name": "Users Created", "value": str(user_count)},
                {"name": "Failed", "value": str(failed)},
            ],
            color=color,
        )
        self._also_pa("provision_complete", {
            "prefix": prefix, "hackName": hack_name,
            "usersCreated": user_count, "failed": failed,
        })

    def notify_readonly_applied(self, prefix: str, hack_name: str):
        self.send_teams_message(
            "🔒 Read-Only Mode Applied",
            f"Hack **{hack_name}** (`{prefix}`) is now in read-only mode.",
            color="6C757D",
        )
        self._also_pa("readonly_applied", {"prefix": prefix, "hackName": hack_name})

    def notify_cleanup_complete(self, prefix: str, hack_name: str):
        self.send_teams_message(
            "🧹 Cleanup Complete",
            f"Hack **{hack_name}** (`{prefix}`) has been cleaned up and archived.",
            color="DC3545",
        )
        self._also_pa("cleanup_complete", {"prefix": prefix, "hackName": hack_name})

    def notify_failure(self, prefix: str, hack_name: str, action: str, error: str):
        self.send_teams_message(
            f"❌ {action} Failed",
            f"Hack **{hack_name}** (`{prefix}`) encountered an error during {action.lower()}.\n\n`{error[:500]}`",
            color="DC3545",
        )
        self._also_pa("action_failed", {
            "prefix": prefix, "hackName": hack_name,
            "action": action, "error": error[:500],
        })

    def send_reminder(self, prefix: str, hack_name: str, action: str,
                      scheduled_at: str, owner_emails: List[str]):
        """Send a 48-hour reminder email before a scheduled action."""
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;">
          <h2 style="color:#0076D7;">⏰ Upcoming Scheduled Action</h2>
          <p>This is a reminder that the following action is scheduled for <strong>{hack_name}</strong>:</p>
          <table style="border-collapse:collapse;margin:16px 0;">
            <tr><td style="padding:6px 12px;font-weight:bold;">Hack</td><td style="padding:6px 12px;">{hack_name} ({prefix})</td></tr>
            <tr><td style="padding:6px 12px;font-weight:bold;">Action</td><td style="padding:6px 12px;">{action}</td></tr>
            <tr><td style="padding:6px 12px;font-weight:bold;">Scheduled At</td><td style="padding:6px 12px;">{scheduled_at}</td></tr>
          </table>
          <p>If you need to postpone or cancel this action, please visit the Spektra HackOps dashboard.</p>
          <p style="color:#666;font-size:12px;">This is an automated reminder from Spektra HackOps.</p>
        </div>
        """
        subject = f"[HackOps] Reminder: {action} for {hack_name} in 48 hours"

        if owner_emails:
            self.send_email(owner_emails, subject, body)

        # Also post to Teams
        self.send_teams_message(
            f"⏰ Reminder: {action} in 48 hours",
            f"Hack **{hack_name}** (`{prefix}`) has **{action}** scheduled at `{scheduled_at}`.",
            facts=[
                {"name": "Action", "value": action},
                {"name": "Scheduled", "value": scheduled_at},
                {"name": "Notified", "value": ", ".join(owner_emails) if owner_emails else "—"},
            ],
            color="FFC107",
        )


def check_upcoming_reminders(
    jobs: list,
    notifier: NotificationService,
    state_getter=None,
    sent_reminders: set = None,
) -> set:
    """Check for jobs due within REMINDER_HOURS and send reminders.

    Returns the set of job IDs that have been notified (to avoid duplicates).
    """
    if sent_reminders is None:
        sent_reminders = set()

    now = datetime.now(timezone.utc)
    reminder_window = now + timedelta(hours=REMINDER_HOURS)

    for job in jobs:
        if job.status != "pending":
            continue
        if job.id in sent_reminders:
            continue

        try:
            due_at = datetime.fromisoformat(job.scheduled_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        if now < due_at <= reminder_window:
            # This job is due within the reminder window
            hack_name = job.hack_prefix
            owner_emails = []
            if state_getter:
                state = state_getter(job.hack_prefix)
                if state:
                    hack_name = state.get("hackName") or job.hack_prefix
                    cfg = state.get("config") or {}
                    if cfg.get("createdBy"):
                        owner_emails = [cfg["createdBy"]]

            action_map = {
                "cleanup": "Cleanup & Deletion",
                "readonly": "Read-Only Transition",
                "provision": "Scheduled Provisioning",
            }
            action = action_map.get(job.job_type, job.job_type)
            notifier.send_reminder(job.hack_prefix, hack_name, action,
                                   job.scheduled_at, owner_emails)
            sent_reminders.add(job.id)
            logger.info("Sent %dh reminder for job %s (%s/%s)",
                        REMINDER_HOURS, job.id, job.job_type, job.hack_prefix)

    return sent_reminders
