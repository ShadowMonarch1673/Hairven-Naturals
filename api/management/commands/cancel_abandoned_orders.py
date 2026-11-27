from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import Order
from api.services import cancel_unpaid_order


class Command(BaseCommand):
    help = "Cancel unpaid orders older than a time limit and return their stock."

    def add_arguments(self, parser):
        parser.add_argument('--minutes', type=int, default=60,
                            help="Cancel unpaid orders older than this many minutes (default 60).")
        parser.add_argument('--dry-run', action='store_true',
                            help="Only list the orders that would be cancelled.")

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(minutes=options['minutes'])
        stale = Order.objects.filter(is_paid=False, status='PENDING', created_at__lt=cutoff)
        order_ids = list(stale.values_list('pk', flat=True))

        if options['dry_run']:
            self.stdout.write(f"Would cancel {len(order_ids)} order(s): {order_ids}")
            return

        cancelled = sum(1 for order_id in order_ids if cancel_unpaid_order(order_id))
        self.stdout.write(self.style.SUCCESS(
            f"Cancelled {cancelled} abandoned order(s) older than {options['minutes']} minutes."))