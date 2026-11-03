import time
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from .models import Shipment
from .notifications import notify_recipients, send_delivered_email, send_status_update
from .tracking import TrackingError, has_live_tracking, sync_shipment

SEND_TIMEZONE = ZoneInfo("America/New_York")   # handles EST/EDT automatically
SEND_HOUR = 8                                    # 8 AM Eastern
INTERVAL = timedelta(days=2)
TOLERANCE = timedelta(hours=2)                   # so an 8:05 run two days later still counts


def is_update_due(shipment, now):
    anchor = shipment.last_update_email_at or shipment.created_at
    return now - anchor >= INTERVAL - TOLERANCE


def run_scheduled_updates(force=False, pause_seconds=2):
    """
    Every 2 days at 8 AM ET: status update emails until delivered, then one delivered email.
    force=True ignores the 8 AM window and the 2-day interval (testing only).
    Returns a summary dict.
    """
    now = timezone.now()
    now_et = now.astimezone(SEND_TIMEZONE)

    summary = {
        "ran": False,
        "time_et": now_et.strftime("%Y-%m-%d %H:%M %Z"),
        "forced": force,
        "results": [],
    }

    if not force and now_et.hour != SEND_HOUR:
        summary["reason"] = "Outside the 8 AM Eastern window. Nothing was sent."
        return summary

    summary["ran"] = True

    shipments = (
        Shipment.objects
        .filter(recipients__isnull=False, delivered_email_sent_at__isnull=True)
        .exclude(communication=Shipment.Communication.HELD)
        .distinct()
    )

    for shipment in shipments:
        result = {"shipment_id": shipment.shipment_id, "sync_error": None}

        # 1. Refresh live status (link-only carriers such as UPS keep their last known status)
        if has_live_tracking(shipment.carrier):
            try:
                sync_shipment(shipment)
            except TrackingError as exc:
                result["sync_error"] = str(exc)

        result["status"] = shipment.status

        if shipment.communication == Shipment.Communication.HELD:
            result.update({"action": "skipped_held", "sent": [], "failed": []})
            summary["results"].append(result)
            continue

        # 2. First email for any recipient who hasn't had one yet
        notify_recipients(shipment)

        # 3. Delivered → final email; otherwise the 2-day update
        if shipment.status == Shipment.Status.DELIVERED:
            sent, failed = send_delivered_email(shipment)
            action = "delivered_email"
        elif force or is_update_due(shipment, now):
            sent, failed = send_status_update(shipment)
            action = "status_update"
        else:
            sent, failed = [], []
            action = "not_due"

        result.update({"action": action, "sent": sent, "failed": failed})
        summary["results"].append(result)

        if pause_seconds:
            time.sleep(pause_seconds)   # gentle on carrier rate limits

    return summary