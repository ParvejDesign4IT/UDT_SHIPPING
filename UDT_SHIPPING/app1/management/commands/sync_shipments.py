import time

from django.core.management.base import BaseCommand

from app1.models import Shipment
from app1.notifications import notify_recipients
from app1.tracking import TrackingError, sync_shipment


class Command(BaseCommand):
    help = "Fetch live carrier status for all undelivered shipments and send queued emails."

    def handle(self, *args, **options):
        shipments = Shipment.objects.exclude(status=Shipment.Status.DELIVERED)

        for shipment in shipments:
            try:
                sync_shipment(shipment)
                self.stdout.write(self.style.SUCCESS(f"{shipment.shipment_id}: {shipment.carrier_status}"))
            except TrackingError as exc:
                self.stdout.write(self.style.WARNING(f"{shipment.shipment_id}: {exc}"))
                time.sleep(5)
                continue

            sent, failed = notify_recipients(shipment)
            if sent:
                self.stdout.write(f"   emailed: {', '.join(sent)}")
            if failed:
                self.stdout.write(self.style.ERROR(f"   email failed: {', '.join(failed)}"))

            time.sleep(5)  # stay within carrier rate limits