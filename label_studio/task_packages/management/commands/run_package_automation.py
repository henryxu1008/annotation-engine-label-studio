"""Optional long-running worker; restart scans are safe and bounded."""

import json
import signal
import threading

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from task_packages.automation import scan_allocations, scan_notices
from task_packages.pools import local_automation_identity, postgres_required


class Command(BaseCommand):
    help = 'Run bounded package allocation and overdue reconciliation until stopped.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--interval', type=int, default=30)
        parser.add_argument('--batch-size', type=int, default=100)

    def handle(self, *args, **options):
        if not (
            settings.ANNOTATION_PACKAGE_AUTOMATION_ENABLED
            and settings.ANNOTATION_FIXED_PACKAGES_ENABLED
            and settings.ANNOTATION_PACKAGE_CLAIM_ENABLED
            and local_automation_identity()
        ):
            raise CommandError('Package automation, fixed packages, claims, and local identity are required.')
        if not 1 <= options['batch_size'] <= 500 or not 1 <= options['interval'] <= 3600:
            raise CommandError('Expected batch size 1–500 and interval 1–3600 seconds.')
        postgres_required()
        stop = threading.Event()
        old_handlers = {}
        if not options['once']:
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        allocation_cursor = None
        notice_cursor = 0
        try:
            while not stop.is_set():
                try:
                    allocation = scan_allocations(allocation_cursor, options['batch_size'])
                    next_notice = scan_notices(notice_cursor, options['batch_size'])
                    allocation_cursor = allocation['next']
                    notice_cursor = next_notice or 0
                    self.stdout.write(json.dumps({'allocation': allocation, 'notice_next_after': next_notice}))
                except Exception as exc:
                    close_old_connections()
                    if options['once']:
                        raise CommandError('Package automation scan failed; safe to retry.') from exc
                    self.stderr.write('Package automation scan failed; retrying the same page.')
                if options['once']:
                    break
                stop.wait(options['interval'])
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
