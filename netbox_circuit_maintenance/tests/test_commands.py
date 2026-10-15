from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from netbox_circuit_maintenance.models import NotificationSource

from .utils import plugin_settings


class SyncNotificationSourcesCommandTestCase(TestCase):
    # The sync itself is tested with sync_notification_sources() in test_utils

    def call_command(self, *sources, verbosity=1):
        stdout = StringIO()
        with plugin_settings(notification_sources=list(sources)):
            call_command('sync_notification_sources', verbosity=verbosity, stdout=stdout)
        return stdout.getvalue()

    def test_sync(self):
        NotificationSource.objects.create(name='Source 1')
        self.assertEqual(
            self.call_command({'name': 'Source 2'}),
            'Created notification source Source 2\n'
            'Deleted notification source Source 1, which is no longer in PLUGINS_CONFIG\n',
        )
        self.assertEqual(list(NotificationSource.objects.values_list('name', flat=True)), ['Source 2'])

    def test_no_changes(self):
        self.assertEqual(self.call_command(), 'The notification sources already match PLUGINS_CONFIG.\n')

    def test_verbosity_0(self):
        self.assertEqual(self.call_command({'name': 'Source 1'}, verbosity=0), '')
        self.assertTrue(NotificationSource.objects.exists())

    def test_invalid(self):
        with self.assertRaisesMessage(CommandError, 'notification_sources[0] in PLUGINS_CONFIG needs a name'):
            self.call_command({})

    def test_protected(self):
        NotificationSource.objects.create(name='Source 1')
        protection_rules = {'netbox_circuit_maintenance.notificationsource': [{'name': {'eq': 'Unprotected'}}]}
        with (
            self.settings(PROTECTION_RULES=protection_rules),
            self.assertRaisesMessage(CommandError, "Notification source Source 1 can't be deleted."),
        ):
            self.call_command()
