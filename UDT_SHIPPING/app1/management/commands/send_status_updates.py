import time
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand
from django.utils import timezone

from app1.models import Shipment
from app1.notifications import notify_recipients, send_delivered_email, send_status_update
from app1.tracking import TrackingError, sync_shipment

SEND_TIMEZONE = ZoneInfo("America/New_York")   # handles EST/EDT automatically
SEND_HOUR = 8                                    # 8 AM Eastern
INTERVAL = timedelta(days=2)
TOLERANCE = timedelta(hours=2)                   # so an 8:05 run two days later still counts


def is_update_due(shipment, now):
    anchor = shipment.last_update_email_at or shipment.created_at
    return now - anchor >= INTERVAL - TOLERANCE


class Command(BaseCommand):
    help = "Every 2 days at 8 AM ET: email shipment status updates; send a final email once delivered."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Testing only: ignore the 8 AM ET time window and the 2-day interval. Sends real emails.",
        )

    def handle(self, *args, force=False, **options):
        now = timezone.now()
        now_et = now.astimezone(SEND_TIMEZONE)

        if not force and now_et.hour != SEND_HOUR:
            self.stdout.write(f"{now_et:%Y-%m-%d %H:%M} ET – outside the 8 AM window, nothing to do.")
            return

        shipments = (
            Shipment.objects
            .filter(recipients__isnull=False, delivered_email_sent_at__isnull=True)
            .exclude(communication=Shipment.Communication.HELD)
            .distinct()
        )

        self.stdout.write(f"{now_et:%Y-%m-%d %H:%M} ET – checking {shipments.count()} shipment(s).")

        for shipment in shipments:
            # 1. Refresh live status (continue with the last known status if the carrier fails)
            try:
                sync_shipment(shipment)
            except TrackingError as exc:
                self.stdout.write(self.style.WARNING(f"{shipment.shipment_id}: sync failed – {exc}"))

            # A sync can't put a shipment back on hold, but stay safe
            if shipment.communication == Shipment.Communication.HELD:
                continue

            # 2. First email for any recipient who hasn't had one yet
            notify_recipients(shipment)

            # 3. Delivered → final email; otherwise the 2-day update
            if shipment.status == Shipment.Status.DELIVERED:
                sent, failed = send_delivered_email(shipment)
                label = "delivered email"
            elif force or is_update_due(shipment, now):
                sent, failed = send_status_update(shipment)
                label = "status update"
            else:
                self.stdout.write(f"{shipment.shipment_id}: next update not due yet.")
                time.sleep(2)
                continue

            if sent:
                self.stdout.write(self.style.SUCCESS(f"{shipment.shipment_id}: {label} → {', '.join(sent)}"))
            if failed:
                self.stdout.write(self.style.ERROR(f"{shipment.shipment_id}: {label} failed → {', '.join(failed)}"))

            time.sleep(2)  # gentle on carrier rate limits