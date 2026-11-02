import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

from .models import Shipment, ShipmentEvent

logger = logging.getLogger(__name__)


def _send(shipment, recipients, subject, template, event_title):
    """Send one email per recipient. Returns (sent_emails, failed_emails)."""
    if not recipients:
        return [], []

    body = render_to_string(template, {"shipment": shipment})
    sent, failed = [], []

    for recipient in recipients:
        try:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [recipient.email])
        except Exception as exc:
            logger.warning("Email to %s failed for %s: %s", recipient.email, shipment.shipment_id, exc)
            failed.append(recipient.email)
            continue
        sent.append(recipient.email)

    if sent:
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.EMAIL_SENT,
            title=event_title,
            description=", ".join(sent),
        )

    return sent, failed


def _subject(shipment, label):
    return f"{label}: {shipment.shipment_id} ({shipment.get_carrier_display()} {shipment.awb})"


def notify_recipients(shipment):
    """
    First tracking email, once per recipient.
    Exports held for customs are skipped until release.
    """
    if shipment.communication == Shipment.Communication.HELD:
        return [], []

    pending = list(shipment.recipients.filter(notified_at__isnull=True))
    sent, failed = _send(
        shipment, pending,
        _subject(shipment, "Shipment update"),
        "emails/shipment_notification.txt",
        "Tracking email sent",
    )

    now = timezone.now()
    shipment.recipients.filter(email__in=sent).update(notified_at=now)
    return sent, failed


def send_status_update(shipment):
    """Scheduled 2-day update, to recipients who already received the first email."""
    if shipment.communication == Shipment.Communication.HELD:
        return [], []

    # Skip anyone who got their first email in the last hour (no back-to-back emails)
    cutoff = timezone.now() - timedelta(hours=1)
    recipients = list(shipment.recipients.filter(notified_at__lt=cutoff))

    sent, failed = _send(
        shipment, recipients,
        _subject(shipment, "Status update"),
        "emails/status_update.txt",
        "Status update email sent",
    )

    if sent:
        shipment.last_update_email_at = timezone.now()
        shipment.save(update_fields=["last_update_email_at"])
    return sent, failed


def send_delivered_email(shipment):
    """Final email once the shipment is delivered. Sent only once."""
    if shipment.delivered_email_sent_at:
        return [], []

    recipients = list(shipment.recipients.all())
    sent, failed = _send(
        shipment, recipients,
        _subject(shipment, "Delivered"),
        "emails/delivered.txt",
        "Delivered email sent",
    )

    if sent:
        now = timezone.now()
        shipment.delivered_email_sent_at = now
        shipment.last_update_email_at = now
        shipment.save(update_fields=["delivered_email_sent_at", "last_update_email_at"])
        shipment.recipients.filter(notified_at__isnull=True, email__in=sent).update(notified_at=now)
    return sent, failed