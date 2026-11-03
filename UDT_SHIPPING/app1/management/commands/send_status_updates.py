from django.core.management.base import BaseCommand

from app1.scheduler import run_scheduled_updates


class Command(BaseCommand):
    help = "Every 2 days at 8 AM ET: email shipment status updates; send a final email once delivered."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Testing only: ignore the 8 AM ET window and the 2-day interval. Sends real emails.",
        )

    def handle(self, *args, force=False, **options):
        summary = run_scheduled_updates(force=force)

        if not summary["ran"]:
            self.stdout.write(f"{summary['time_et']} – {summary['reason']}")
            return

        self.stdout.write(f"{summary['time_et']} – checked {len(summary['results'])} shipment(s).")

        for item in summary["results"]:
            line = f"{item['shipment_id']}: {item['action']}"
            if item["sent"]:
                line += f" → {', '.join(item['sent'])}"
            self.stdout.write(self.style.SUCCESS(line) if item["sent"] else line)

            if item["failed"]:
                self.stdout.write(self.style.ERROR(f"   failed → {', '.join(item['failed'])}"))
            if item["sync_error"]:
                self.stdout.write(self.style.WARNING(f"   sync: {item['sync_error']}"))