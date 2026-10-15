from django.core.management.base import BaseCommand, CommandError

from netbox_circuit_maintenance.utils import NotificationSourceSyncError, sync_notification_sources


class Command(BaseCommand):
    help = (
        'Sync the notification sources with PLUGINS_CONFIG, deleting those no longer configured. migrate runs this too.'
    )

    def handle(self, *args, **options):
        try:
            messages = sync_notification_sources()
        except NotificationSourceSyncError as e:
            raise CommandError(e) from e

        if options['verbosity'] >= 1:
            for message in messages:
                self.stdout.write(message)
            if not messages:
                self.stdout.write('The notification sources already match PLUGINS_CONFIG.')
