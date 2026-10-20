import runpy
import uuid
import warnings
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from unittest.mock import ANY, MagicMock, patch

from circuit_maintenance_parser import NotificationData, init_provider
from circuit_maintenance_parser.errors import ProviderError
from circuits.models import Circuit, CircuitType, Provider
from core.choices import JobStatusChoices, ObjectChangeActionChoices
from core.events import OBJECT_CREATED
from core.models import Job, ObjectChange, ObjectType
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from extras.models import CustomField
from netbox.registry import registry
from users.models import User
from utilities.templatetags.builtins.filters import render_markdown

from netbox_circuit_maintenance.constants import (
    MAX_MAINTENANCE_NAME_LENGTH,
    MAX_NOTIFICATION_SENDER_LENGTH,
    MAX_NOTIFICATION_SUBJECT_LENGTH,
    PROVIDER_EMAILS_FIELD,
    PROVIDER_PARSER_FIELD,
)
from netbox_circuit_maintenance.handle_notifications.handler import (
    HandleCircuitMaintenanceNotifications,
    create_circuit_maintenance,
    get_maintenances_from_notification,
    get_since_reference,
    process_raw_notification,
    update_circuit_maintenance,
)
from netbox_circuit_maintenance.handle_notifications.sources import GmailAPI, MaintenanceNotification, Source
from netbox_circuit_maintenance.models import (
    CircuitImpact,
    CircuitMaintenance,
    Note,
    NotificationSource,
    ParsedNotification,
    RawNotification,
)

from .utils import plugin_settings

HANDLER_MODULE = 'netbox_circuit_maintenance.handle_notifications.handler'
SOURCES_MODULE = 'netbox_circuit_maintenance.handle_notifications.sources'

RAW_NOTIFICATION_TEMPLATE = """
BEGIN:VCALENDAR
PRODID:Data::ICal 0.16
VERSION:2.0
{events}END:VCALENDAR
"""

EVENT_TEMPLATE = """BEGIN:VEVENT
DESCRIPTION:{provider} URGENT Maintenance Notification: {status} [{name}] Please refer to the email notification for more details regarding maintenance or outage reason and impact.
DTEND:20210808T071300Z
DTSTAMP:{stamp:%Y%m%dT%H%M%SZ}
DTSTART:20210808T050000Z
SEQUENCE:1
SUMMARY:{provider} URGENT Maintenance Notification: {status} [{name}]
UID:{name}
X-MAINTNOTE-ACCOUNT:{account}
X-MAINTNOTE-IMPACT:OUTAGE
X-MAINTNOTE-MAINTENANCE-ID:{name}
{object_lines}X-MAINTNOTE-PROVIDER:{provider}
X-MAINTNOTE-STATUS:{status}
END:VEVENT
"""


def generate_email_notification(
    notification_data: dict, source: Source, *other_maintenances: dict
) -> MaintenanceNotification:
    """
    Generate email notification text for a provider, announcing the maintenances in other_maintenances too.
    """
    events = ''.join(
        EVENT_TEMPLATE.format(
            **data,
            object_lines=''.join(
                f'X-MAINTNOTE-OBJECT-ID;X-MAINTNOTE-OBJECT-IMPACT={circuitimpact["impact"]}:{circuitimpact["cid"]}\n'
                for circuitimpact in data['circuitimpacts']
            ),
        )
        for data in (notification_data, *other_maintenances)
    )
    email_message = EmailMessage()
    email_message['From'] = 'Sender <sender@example.com>'
    email_message['Date'] = format_datetime(notification_data['stamp'])
    email_message['Subject'] = 'Test subject ' + '1234567890' * 20
    email_message['Content-Type'] = 'text/calendar'
    email_message.set_payload(RAW_NOTIFICATION_TEMPLATE.format(events=events).encode('utf-8'))

    return MaintenanceNotification(
        msg_id=b'12345',
        subject='Test subject ' + '1234567890' * 20,
        sender='sender@example.com',
        source=source,
        raw_payload=email_message.as_bytes(),
        provider_type=notification_data['provider'],
        date=format_datetime(notification_data['stamp']),
    )


def get_base_notification_data(provider_type='ntt') -> dict:
    """
    Provides a dictionary of notification data to build notifications.
    """
    provider = Provider.objects.get(name=provider_type)

    notification_data = {
        'provider': provider.name,
        'account': f'ACC-{provider.name.upper()}',
        'name': f'MNT-{provider.name.upper()}',
        'status': 'CONFIRMED',
        'circuitimpacts': [],
        'stamp': datetime(2021, 2, 1, 9, 33, 34, tzinfo=UTC),
    }

    sample_circuits = Circuit.objects.filter(provider=provider)
    for circuit in sample_circuits:
        # Intentionally convert the CID reference to lower-case,
        # because all of our circuit lookups *should* be case-insensitive
        notification_data['circuitimpacts'].append({'cid': circuit.cid.lower(), 'impact': 'NO-IMPACT'})

    return notification_data


def parse_notification(notification: MaintenanceNotification):
    """
    Return the parser's first maintenance in the notification.
    """
    parser_provider = init_provider(provider_type=notification.provider_type)
    data_to_process = NotificationData.init_from_email_bytes(notification.raw_payload)
    return parser_provider.get_maintenances(data_to_process)[0]


class HandlerTestCase(TestCase):
    """
    Nautobot's fixture as test data: three providers with their emails, two circuits of ntt, and one notification
    source, so that a run fetches once.
    """

    @classmethod
    def setUpTestData(cls):
        for name, email in (('ntt', 'sender'), ('packetfabric', 'sender2'), ('abc', 'sender3')):
            Provider.objects.create(
                name=name, slug=name, custom_field_data={PROVIDER_EMAILS_FIELD: f'{email}@example.com'}
            )
        circuit_type = CircuitType.objects.create(name='Transit', slug='transit')
        for cid in ('CID-000001', 'CID-000002'):
            Circuit.objects.create(cid=cid, provider=Provider.objects.get(name='ntt'), type=circuit_type)
        cls.notification_source = NotificationSource.objects.create(name='whatever 1')
        cls.user = User.objects.create_user(username='user1')

    def setUp(self):
        self.source = Source(name=self.notification_source.name, url='http://example.com')
        self.job = self.get_job()

    def get_job(self, user=None):
        """
        Return a handler JobRunner for an unsaved Job, whose log_entries collect the run's log.
        """
        job = HandleCircuitMaintenanceNotifications(
            Job(name=HandleCircuitMaintenanceNotifications.name, job_id=uuid.uuid4(), user=user)
        )
        # NetBox adds a log handler for each runner to the runner's logger, which is module-level, and never removes it
        self.addCleanup(job.logger.removeHandler, job.logger.handlers[-1])
        return job

    def get_log(self, level=None, job=None):
        """
        Return the messages in the job's log, of one level if given.
        """
        job = job or self.job
        return [entry['message'] for entry in job.job.log_entries if level in (None, entry['level'])]


class HandleNotificationsJobTestCase(HandlerTestCase):
    """
    Test case for all the related methods in Handle Notifications.
    """

    def test_run_simple(self):
        """
        Test the simple execution to create a Circuit Maintenance.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)

        with (
            patch(f'{HANDLER_MODULE}.get_notifications') as mock_get_notifications,
            patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message,
        ):
            mock_get_notifications.return_value = [test_notification]
            self.job.run()

        mock_get_notifications.assert_called_once_with(self.job, [self.notification_source], ANY, dry_run=False)
        self.assertEqual(1, RawNotification.objects.count())
        self.assertEqual(1, ParsedNotification.objects.count())
        self.assertEqual(1, CircuitMaintenance.objects.count())
        self.assertEqual(2, CircuitImpact.objects.count())
        self.assertEqual(0, Note.objects.count())
        mock_tag_message.assert_called_with(self.job, test_notification.msg_id, 'parsed')
        self.assertEqual(self.get_log('info')[-1], '1 notifications processed.')
        # The job's data replaces Nautobot's return value
        self.assertEqual(self.job.job.data, {'dry_run': False, 'raw_notifications': [RawNotification.objects.get().pk]})

    def test_run_nonexistent_circuit(self):
        """
        Test when a Notification contains a nonexistent circuit.
        """
        notification_data = get_base_notification_data()
        fake_cid = 'nonexistent circuit'
        notification_data['circuitimpacts'].append({'cid': fake_cid, 'impact': 'NO-IMPACT'})
        test_notification = generate_email_notification(notification_data, self.source)

        with (
            patch(f'{HANDLER_MODULE}.get_notifications') as mock_get_notifications,
            patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message,
        ):
            mock_get_notifications.return_value = [test_notification]
            self.job.run()

        mock_get_notifications.assert_called_once()
        self.assertEqual(1, len(self.job.job.data['raw_notifications']))
        self.assertEqual(1, RawNotification.objects.count())
        self.assertEqual(1, ParsedNotification.objects.count())
        self.assertEqual(1, CircuitMaintenance.objects.count())
        self.assertEqual(2, CircuitImpact.objects.count())
        self.assertEqual(1, Note.objects.count())
        self.assertIn(fake_cid, Note.objects.first().title)
        mock_tag_message.assert_any_call(self.job, test_notification.msg_id, 'parsed')
        mock_tag_message.assert_any_call(self.job, test_notification.msg_id, 'unknown-cids')
        self.assertEqual(self.get_log('info')[-1], '1 notifications processed.')

        # String representations fit event rule notifications' object_repr (varchar 200). Nautobot also saved an
        # ObjectChange for each, which NetBox truncates itself; test_changelog records them through the job
        for model in (RawNotification, ParsedNotification, CircuitMaintenance, CircuitImpact, Note):
            with self.subTest(model=model.__name__):
                self.assertLessEqual(len(str(model.objects.first())), 200)

    def test_run_no_notifications(self):
        """
        Test when a there are no notifications.
        """
        with patch(f'{HANDLER_MODULE}.get_notifications') as mock_get_notifications:
            mock_get_notifications.return_value = []
            self.job.run()

        mock_get_notifications.assert_called_once()
        self.assertEqual(self.job.job.data, {'dry_run': False, 'raw_notifications': []})
        self.assertEqual(0, RawNotification.objects.count())
        self.assertEqual(0, ParsedNotification.objects.count())
        self.assertEqual(0, CircuitMaintenance.objects.count())
        self.assertEqual(0, CircuitImpact.objects.count())
        self.assertEqual(0, Note.objects.count())
        self.assertEqual(self.get_log('info')[-1], 'No notifications received.')

    def test_run_no_notification_source(self):
        """
        Test when a there are no Notification Sources.
        """
        NotificationSource.objects.all().delete()
        self.job.run()
        self.assertEqual(self.job.job.data, {'dry_run': False, 'raw_notifications': []})
        self.assertEqual(
            self.get_log('warning'), ['No notification sources configured to retrieve notifications from.']
        )

    def test_run_invalid_notification(self):
        """
        Test when a there is an invalid notification.
        """
        notification_data = get_base_notification_data()
        notification_data['status'] = 'Non valid status'
        test_notification = generate_email_notification(notification_data, self.source)

        with (
            patch(f'{HANDLER_MODULE}.get_notifications') as mock_get_notifications,
            patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message,
        ):
            mock_get_notifications.return_value = [test_notification]
            self.job.run()

        mock_get_notifications.assert_called_once()
        self.assertEqual(0, RawNotification.objects.count())
        self.assertEqual(0, ParsedNotification.objects.count())
        self.assertEqual(0, CircuitMaintenance.objects.count())
        self.assertEqual(0, CircuitImpact.objects.count())
        self.assertEqual(0, Note.objects.count())
        mock_tag_message.assert_called_with(self.job, test_notification.msg_id, 'parsing-failed')
        # The job log keeps only messages, so they carry the reason
        self.assertIn(
            f'Unexpected exception when parsing notification `{test_notification.subject}`: '
            'Failed creating Maintenance notification for NTT.',
            self.get_log('error')[-1],
        )
        self.assertEqual(self.get_log('info')[-1], '0 notifications processed.')

    def test_process_raw_notification_no_provider_in_parser(self):
        """
        Test process_raw_notification with non existant Provider in the parser library.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        test_notification.provider_type = 'abc'
        with patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message:
            res = process_raw_notification(self.job, test_notification)

        self.assertNotEqual(res, None)
        mock_tag_message.assert_called_with(self.job, test_notification.msg_id, 'parsing-failed')
        self.assertEqual(self.get_log('warning')[-1], 'Notification Parser abc not found for provider abc')

    def test_process_raw_notification_no_provider_in_plugin(self):
        """
        Test process_raw_notification with non existant provider in the App.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        test_notification.provider_type = 'telstra'
        with patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message:
            res = process_raw_notification(self.job, test_notification)

        self.assertEqual(res, None)
        mock_tag_message.assert_called_with(self.job, test_notification.msg_id, 'unknown-provider')
        self.assertEqual(
            self.get_log('warning')[-1],
            f'Raw notification `{test_notification.subject}` not created because it refers to provider telstra, which '
            "doesn't exist.",
        )

    def test_process_raw_notification(self):
        """
        Test process_raw_notification.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        res = process_raw_notification(self.job, test_notification)

        raw_notification = RawNotification.objects.get(pk=res)
        self.assertEqual(raw_notification.pk, res)
        self.assertEqual(raw_notification.parsed, True)
        self.assertEqual(raw_notification.source, self.notification_source)
        self.assertEqual(1, ParsedNotification.objects.count())
        self.assertIn(f'Raw notification `{raw_notification.subject}` created.', self.get_log('info'))

    def test_process_raw_notification_duplicated_issue(self):
        """
        Test process_raw_notification duplicated.
        """
        self.test_process_raw_notification()
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)

        # A duplicated RawNotification skip creation
        res = process_raw_notification(self.job, test_notification)
        self.assertEqual(res, None)
        self.assertIn('Raw notification `Test subject', self.get_log('debug')[-1])
        self.assertIn('already existed with ID', self.get_log('debug')[-1])

        # After a duplicated RawNotification, a new RawNotification should be inserted
        test_notification.subject = 'another_subject'
        res = process_raw_notification(self.job, test_notification)
        raw_notification = RawNotification.objects.get(pk=res)
        self.assertEqual(raw_notification.pk, res)

    def test_process_raw_notification_parser_issue(self):
        """
        Test process_raw_notification with parsing issues
        """
        notification_data = get_base_notification_data()
        notification_data['status'] = 'Non valid status'
        test_notification = generate_email_notification(notification_data, self.source)
        with patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message, self.assertRaises(ProviderError):
            process_raw_notification(self.job, test_notification)

        self.assertEqual(0, ParsedNotification.objects.count())
        mock_tag_message.assert_called_with(self.job, test_notification.msg_id, 'parsing-failed')
        self.assertIn('Raw notification `Test subject', self.get_log('info')[-1])
        self.assertIn(f'Parsing failed for notification `{test_notification.subject}`: ', self.get_log('error')[-1])

    def test_get_maintenances_from_notification(self):
        """
        Test get_maintenances_from_notification.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        provider = Provider.objects.get(name=test_notification.provider_type)
        with patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message:
            parser_maintenances = get_maintenances_from_notification(self.job, test_notification, provider)

        self.assertEqual(1, len(parser_maintenances))
        mock_tag_message.assert_called_with(self.job, test_notification.msg_id, 'parsed')

    def test_get_maintenances_from_notification_wrong_data(self):
        """
        Test get_maintenances_from_notification.
        """
        notification_data = get_base_notification_data()
        notification_data['status'] = 'Non valid status'
        test_notification = generate_email_notification(notification_data, self.source)
        provider = Provider.objects.get(name=test_notification.provider_type)
        with patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message, self.assertRaises(ProviderError):
            get_maintenances_from_notification(self.job, test_notification, provider)

        mock_tag_message.assert_called_with(self.job, test_notification.msg_id, 'parsing-failed')
        self.assertTrue(self.get_log('error'))

    def test_get_maintenances_from_notification_non_existent_provider_in_parser(self):
        """
        Test get_maintenances_from_notification.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        provider = Provider.objects.get(name=test_notification.provider_type)
        # custom_field_data, which save() stores, rather than Nautobot's provider.cf
        provider.custom_field_data[PROVIDER_PARSER_FIELD] = 'unkown_provider_in_parser'
        with patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message:
            parser_maintenances = get_maintenances_from_notification(self.job, test_notification, provider)

        self.assertIsNone(parser_maintenances)
        mock_tag_message.assert_called_with(self.job, test_notification.msg_id, 'parsing-failed')
        # The message names the parser looked for (Nautobot named the provider only)
        self.assertEqual(
            self.get_log('warning')[-1],
            f'Notification Parser unkown_provider_in_parser not found for provider {provider}',
        )

    def test_get_maintenances_from_notification_parser_field(self):
        """
        The parser named in the Provider custom field is used lower-cased, and any other value means the provider's
        name: a deleted field, None, a blank or a value that isn't a string.
        """
        test_notification = generate_email_notification(get_base_notification_data(), self.source)
        provider = Provider.objects.get(name='ntt')
        provider.name = 'not a parser'
        with self.subTest(value='NTT'):
            provider.custom_field_data[PROVIDER_PARSER_FIELD] = 'NTT'
            self.assertEqual(1, len(get_maintenances_from_notification(self.job, test_notification, provider)))

        provider.name = 'ntt'
        for value in (None, '', 123, ['ntt']):
            with self.subTest(value=value):
                provider.custom_field_data[PROVIDER_PARSER_FIELD] = value
                self.assertEqual(1, len(get_maintenances_from_notification(self.job, test_notification, provider)))
        with self.subTest(value='deleted field'):
            del provider.custom_field_data[PROVIDER_PARSER_FIELD]
            self.assertEqual(1, len(get_maintenances_from_notification(self.job, test_notification, provider)))

    def test_get_maintenances_from_notification_not_accepted(self):
        """
        Data the parser doesn't accept is reported by the notification's subject, not its payload (Nautobot's message
        lacked its f prefix).
        """
        test_notification = generate_email_notification(get_base_notification_data(), self.source)
        test_notification.raw_payload = b'\xff not UTF-8'
        provider = Provider.objects.get(name='ntt')
        # The parser logs the payload itself
        with (
            patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message,
            self.assertLogs('circuit_maintenance_parser.data', 'ERROR'),
            self.assertRaises(ValueError),
        ):
            get_maintenances_from_notification(self.job, test_notification, provider)

        mock_tag_message.assert_called_with(self.job, test_notification.msg_id, 'parsing-failed')
        self.assertEqual(
            self.get_log('error'),
            [f'Notification data of `{test_notification.subject}` was not accepted by the parser'],
        )

    def test_create_circuit_maintenance(self):
        """
        Test create_circuit_maintenance.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        provider = Provider.objects.get(name=test_notification.provider_type)
        parsed_maintenance = parse_notification(test_notification)
        create_circuit_maintenance(
            self.job,
            test_notification,
            f'{provider.name}-{parsed_maintenance.maintenance_id}',
            parsed_maintenance,
            provider,
        )
        self.assertEqual(1, CircuitMaintenance.objects.count())
        self.assertEqual(2, CircuitImpact.objects.count())
        self.assertEqual(0, Note.objects.count())
        self.assertEqual(self.get_log('info')[0], 'Created Circuit Maintenance ntt-MNT-NTT.')

    def test_create_circuit_maintenance_no_circuits(self):
        """
        Test create_circuit_maintenance without existent circuits.
        """
        notification_data = get_base_notification_data()
        notification_data['circuitimpacts'] = [{'cid': 'nonexistent', 'impact': 'NO-IMPACT'}]
        test_notification = generate_email_notification(notification_data, self.source)
        provider = Provider.objects.get(name=test_notification.provider_type)
        parsed_maintenance = parse_notification(test_notification)
        with patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message:
            create_circuit_maintenance(
                self.job,
                test_notification,
                f'{provider.name}-{parsed_maintenance.maintenance_id}',
                parsed_maintenance,
                provider,
            )

        self.assertEqual(1, CircuitMaintenance.objects.count())
        self.assertEqual(0, CircuitImpact.objects.count())
        self.assertEqual(1, Note.objects.count())
        mock_tag_message.assert_called_with(self.job, test_notification.msg_id, 'unknown-cids')
        self.assertEqual(
            self.get_log('warning')[-1], 'Circuit Maintenance ntt-MNT-NTT has no circuits in the database.'
        )

    def test_create_circuit_maintenance_unknown_status(self):
        """
        Test create_circuit_maintenance with an unknown status.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        provider = Provider.objects.get(name=test_notification.provider_type)
        parsed_maintenance = parse_notification(test_notification)
        parsed_maintenance.status = 'No idea!'
        create_circuit_maintenance(
            self.job,
            test_notification,
            f'{provider.name}-{parsed_maintenance.maintenance_id}',
            parsed_maintenance,
            provider,
        )

        self.assertEqual(1, CircuitMaintenance.objects.count())
        self.assertEqual(2, CircuitImpact.objects.count())
        self.assertEqual(0, Note.objects.count())

        self.assertEqual('UNKNOWN', CircuitMaintenance.objects.first().status)

    def test_update_circuit_maintenance(self):
        """
        Test update_circuit_maintenance.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        provider = Provider.objects.get(name=test_notification.provider_type)
        with patch(f'{HANDLER_MODULE}.get_notifications') as mock_get_notifications:
            mock_get_notifications.return_value = [test_notification]
            self.job.run()

        # Adding changes
        notification_data['status'] = 'COMPLETED'
        circuit_to_update = notification_data['circuitimpacts'].pop()
        notification_data['circuitimpacts'].pop()
        notification_data['circuitimpacts'].append({'cid': 'nonexistent', 'impact': 'NO-IMPACT'})
        circuit_to_update['impact'] = 'OUTAGE'
        notification_data['circuitimpacts'].append(circuit_to_update)
        test_notification = generate_email_notification(notification_data, self.source)
        parsed_maintenance = parse_notification(test_notification)
        maintenance_id = f'{provider.name}-{parsed_maintenance.maintenance_id}'
        circuit_maintenance_entry = CircuitMaintenance.objects.get(name=maintenance_id)
        update_circuit_maintenance(self.job, test_notification, circuit_maintenance_entry, parsed_maintenance, provider)
        self.assertEqual(1, CircuitMaintenance.objects.count())
        self.assertEqual(1, CircuitImpact.objects.count())
        self.assertEqual(1, Note.objects.count())
        circuit_maintenance_entry = CircuitMaintenance.objects.get(name=maintenance_id)
        self.assertEqual(notification_data['status'], circuit_maintenance_entry.status)
        circuit_impact_entry = CircuitImpact.objects.get(circuit__cid__iexact=circuit_to_update['cid'])
        self.assertEqual(circuit_to_update['impact'], circuit_impact_entry.impact)
        self.assertIn('Circuit ID CID-000001 unlinked from Maintenance ntt-MNT-NTT', self.get_log('info'))
        self.assertEqual(self.get_log('info')[-1], 'Updated Circuit Maintenance ntt-MNT-NTT')

    def test_update_circuit_maintenance_unordered_notifications(self):
        """
        Test update_circuit_maintenance with unordered notifications.
        """
        notification_data = get_base_notification_data()
        test_notification_older = generate_email_notification(notification_data, self.source)

        notification_data['status'] = 'COMPLETED'
        notification_data['stamp'] = datetime(2021, 2, 2, 9, 33, 34, tzinfo=UTC)
        test_notification_newer = generate_email_notification(notification_data, self.source)

        provider = Provider.objects.get(name=test_notification_older.provider_type)
        with (
            patch(f'{HANDLER_MODULE}.get_notifications') as mock_get_notifications,
            patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message,
        ):
            # We simulate that the newer notifications are retrieved first, so processed first
            mock_get_notifications.return_value = [test_notification_newer, test_notification_older]
            self.job.run()

        # Verify that both notifications where related to same CircuitMaintenance
        self.assertEqual(1, CircuitMaintenance.objects.count())

        mock_tag_message.assert_called_with(self.job, test_notification_older.msg_id, 'out-of-sequence')

        maintenance_id = f'{provider.name}-{notification_data["name"]}'
        circuit_maintenance_entry = CircuitMaintenance.objects.get(name=maintenance_id)
        # Verify that the final status of the CircuitMaintenance depends on the newest notification
        self.assertEqual(circuit_maintenance_entry.status, 'COMPLETED')
        # Verify that both parsed notifications are linked to the CircuitMaintenance for future reference
        self.assertEqual(circuit_maintenance_entry.parsed_notifications.count(), 2)

    def test_update_circuit_maintenance_out_of_sequence_newest(self):
        """
        A notification older than the newest one applied is skipped, even if it's newer than an earlier one. Nautobot
        compared it with the oldest, so the notice of 2 February replaced the status of 3 February.
        """
        notification_data = get_base_notification_data()
        notifications = []
        for day, status in ((1, 'CONFIRMED'), (3, 'COMPLETED'), (2, 'CANCELLED')):
            notification_data['status'] = status
            notification_data['stamp'] = datetime(2021, 2, day, 9, 33, 34, tzinfo=UTC)
            notifications.append(generate_email_notification(notification_data, self.source))
            notifications[-1].subject = f'Notice {day}'

        with (
            patch(f'{HANDLER_MODULE}.get_notifications', return_value=notifications),
            patch(f'{SOURCES_MODULE}.Source.tag_message') as mock_tag_message,
        ):
            self.job.run()

        self.assertEqual(CircuitMaintenance.objects.get().status, 'COMPLETED')
        mock_tag_message.assert_called_with(self.job, notifications[2].msg_id, 'out-of-sequence')
        self.assertEqual(3, ParsedNotification.objects.count())

    def test_update_circuit_maintenance_status_no_change(self):
        """
        Test update_circuit_maintenance with a "NO-CHANGE" status value.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        provider = Provider.objects.get(name=test_notification.provider_type)
        with patch(f'{HANDLER_MODULE}.get_notifications') as mock_get_notifications:
            mock_get_notifications.return_value = [test_notification]
            self.job.run()

        # Adding changes
        parsed_maintenance = parse_notification(test_notification)
        parsed_maintenance.status = 'NO-CHANGE'
        maintenance_id = f'{provider.name}-{parsed_maintenance.maintenance_id}'
        circuit_maintenance_entry = CircuitMaintenance.objects.get(name=maintenance_id)

        update_circuit_maintenance(self.job, test_notification, circuit_maintenance_entry, parsed_maintenance, provider)

        circuit_maintenance_entry = CircuitMaintenance.objects.get(name=maintenance_id)
        # Status should not be changed:
        self.assertEqual('CONFIRMED', circuit_maintenance_entry.status)

    def test_create_or_update_circuit_maintenance_truncated_fields(self):
        """
        Test create_or_update_circuit_maintenance with long fields that must be truncated to fit the DB.
        """
        notification_data = get_base_notification_data()
        notification_data['name'] = f'MNT-{"1234567890" * 20}'
        test_notification = generate_email_notification(notification_data, self.source)
        test_notification.subject = 'abcdefghijiklmnopqrstuvwxyz ' * 10
        test_notification.sender = f'{"abcdefghij" * 20}@example.com'

        notification_data['status'] = 'COMPLETED'
        notification_data['stamp'] = datetime(2021, 2, 2, 9, 33, 34, tzinfo=UTC)
        test_notification_newer = generate_email_notification(notification_data, self.source)
        test_notification_newer.subject = 'abcdefghijiklmnopqrstuvwxyz ' * 10
        test_notification_newer.sender = f'{"abcdefghij" * 20}@example.com'
        provider = Provider.objects.get(name=test_notification.provider_type)
        with patch(f'{HANDLER_MODULE}.get_notifications') as mock_get_notif:
            mock_get_notif.return_value = [test_notification, test_notification_newer]
            self.job.run()

        # Make sure raw notification sender and subject were correctly truncated
        self.assertEqual(2, RawNotification.objects.count())
        for raw_notification in RawNotification.objects.filter(provider=provider):
            self.assertEqual(MAX_NOTIFICATION_SENDER_LENGTH, len(raw_notification.sender))
            self.assertEqual(test_notification.sender[:MAX_NOTIFICATION_SENDER_LENGTH], raw_notification.sender)
            self.assertEqual(MAX_NOTIFICATION_SUBJECT_LENGTH, len(raw_notification.subject))
            self.assertEqual(test_notification.subject[:MAX_NOTIFICATION_SENDER_LENGTH], raw_notification.subject)

        # Make sure maintenance name was correctly truncated on both create and update
        maintenance_id = f'{provider.name}-{notification_data["name"]}'[:MAX_MAINTENANCE_NAME_LENGTH]
        self.assertEqual(MAX_MAINTENANCE_NAME_LENGTH, len(maintenance_id))
        self.assertEqual(1, CircuitMaintenance.objects.count())
        circuit_maintenance_entry = CircuitMaintenance.objects.get(name=maintenance_id)
        self.assertEqual('COMPLETED', circuit_maintenance_entry.status)
        # Verify that both parsed notifications are linked to the CircuitMaintenance for future reference
        self.assertEqual(circuit_maintenance_entry.parsed_notifications.count(), 2)

    def test_raw_notification_size(self):
        """
        A payload over the field's limit is stored cut to the raw_notification_size setting: full_clean() checks the
        limit before save() applies the setting.
        """
        test_notification = generate_email_notification(get_base_notification_data(), self.source)
        # A long header, which the parser ignores
        test_notification.raw_payload = b'X-Padding: ' + b'x' * 20000 + b'\n' + test_notification.raw_payload
        with plugin_settings(raw_notification_size=100):
            raw_id = process_raw_notification(self.job, test_notification)
        self.assertEqual(bytes(RawNotification.objects.get(pk=raw_id).raw), test_notification.raw_payload[:100])

    def test_get_since_with_previous_raw_notification(self):
        """
        Test get_since_reference with a previous raw_notification.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        raw_id = process_raw_notification(self.job, test_notification)
        since_reference = get_since_reference(self.job, self.notification_source)
        self.assertEqual(since_reference, RawNotification.objects.get(id=raw_id).created.timestamp())

    def test_get_since_reference_created(self):
        """
        The window starts when the source's newest raw notification was created, whatever its stamp or last_updated
        (Nautobot read the last_updated of the one with the newest stamp).
        """
        provider = Provider.objects.get(name='ntt')
        created = (datetime(2026, 1, 3, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
        for subject, stamp, created_at in (('older', 2, created[0]), ('newer', 3, created[1])):
            raw_notification = RawNotification.objects.create(
                subject=subject,
                provider=provider,
                raw=b'raw',
                source=self.notification_source,
                stamp=datetime(2021, 2, stamp, tzinfo=UTC),
            )
            # update() leaves last_updated alone, so it stays later than created
            RawNotification.objects.filter(pk=raw_notification.pk).update(created=created_at)

        self.assertEqual(get_since_reference(self.job, self.notification_source), created[0].timestamp())
        self.assertEqual(
            self.get_log('info')[-1],
            f'Processing notifications from whatever 1 since {timezone.localtime(created[0]).isoformat()}',
        )

    def test_get_since_reference_initial(self):
        """
        A source without raw notifications starts raw_notification_initial_days_since days ago.
        """
        for days in (7, 3):
            with self.subTest(days=days), plugin_settings(raw_notification_initial_days_since=days):
                expected = (timezone.now() - timedelta(days=days)).timestamp()
                self.assertAlmostEqual(get_since_reference(self.job, self.notification_source), expected, delta=60)

    def test_run_per_source_window(self):
        """
        Each source is fetched from its own newest raw notification, so a source that runs skipped keeps its window
        until it's fetched again.
        """
        provider = Provider.objects.get(name='ntt')
        other_source = NotificationSource.objects.create(name='whatever 2')
        raw_notification = RawNotification.objects.create(
            subject='subject',
            provider=provider,
            raw=b'raw',
            source=self.notification_source,
            stamp=datetime(2021, 2, 1, tzinfo=UTC),
        )
        created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        RawNotification.objects.filter(pk=raw_notification.pk).update(created=created)

        with patch(f'{HANDLER_MODULE}.get_notifications', return_value=[]) as mock_get_notifications:
            self.job.run()

        since = {call.args[1][0]: call.args[2] for call in mock_get_notifications.call_args_list}
        self.assertEqual(list(since), [self.notification_source, other_source])
        self.assertEqual(since[self.notification_source], created.timestamp())
        self.assertAlmostEqual(since[other_source], (timezone.now() - timedelta(days=7)).timestamp(), delta=60)

    def test_run_fetch_error(self):
        """
        A failed fetch stops the run, which the job framework records as errored, with the reason in the log.
        """
        with (
            patch(f'{HANDLER_MODULE}.get_notifications', side_effect=RuntimeError('Boom')),
            self.assertRaisesMessage(RuntimeError, 'Boom'),
        ):
            self.job.run()

        self.assertEqual(
            self.get_log('error'), ['Unexpected exception when retrieving notifications from whatever 1: Boom']
        )

    def test_run_continues_after_error(self):
        """
        A notification that fails is rolled back, and the run goes on with the next one.
        """
        notification_data = get_base_notification_data()
        notification_data['status'] = 'Non valid status'
        failing_notification = generate_email_notification(notification_data, self.source)
        failing_notification.subject = 'Failing'
        notification_data['status'] = 'CONFIRMED'
        test_notification = generate_email_notification(notification_data, self.source)

        with patch(f'{HANDLER_MODULE}.get_notifications', return_value=[failing_notification, test_notification]):
            self.job.run()

        raw_notification = RawNotification.objects.get()
        self.assertEqual(raw_notification.subject, test_notification.subject[:MAX_NOTIFICATION_SUBJECT_LENGTH])
        self.assertEqual(self.job.job.data['raw_notifications'], [raw_notification.pk])
        self.assertTrue(
            self.get_log('error')[-1].startswith('Unexpected exception when parsing notification `Failing`: ')
        )
        self.assertEqual(self.get_log('info')[-1], '1 notifications processed.')

    def test_update_circuit_maintenance_with_duplicated_notes(self):
        """
        Test update_circuit_maintenance with duplicated notes.
        """
        notification_data = get_base_notification_data()
        # Updating the circuit impact to reference to an unexistent cid
        for circuitimpact in notification_data['circuitimpacts']:
            circuitimpact['cid'] = ''

        with patch(f'{HANDLER_MODULE}.get_notifications') as mock_get_notifications:
            test_notification = generate_email_notification(notification_data, self.source)
            mock_get_notifications.return_value = [test_notification]
            self.job.run()
            # Running it again with another notification but for same maintenance wit the same unexistent circuit
            test_notification = generate_email_notification(notification_data, self.source)
            test_notification.subject = 'another subject'
            mock_get_notifications.return_value = [test_notification]
            self.job.run()

        self.assertEqual(1, CircuitMaintenance.objects.count())
        self.assertEqual(0, CircuitImpact.objects.count())
        self.assertEqual(1, Note.objects.count())

    def test_update_circuit_maintenance_edited_note(self):
        """
        The note about a nonexistent circuit is found by its title, so the next notification keeps a user's edits to
        its comment and level. Nautobot looked the comment and level up too, then failed on the unique title.
        """
        notification_data = get_base_notification_data()
        notification_data['circuitimpacts'].append({'cid': 'nonexistent', 'impact': 'NO-IMPACT'})
        test_notification = generate_email_notification(notification_data, self.source)
        with patch(f'{HANDLER_MODULE}.get_notifications', return_value=[test_notification]):
            self.job.run()
        Note.objects.update(comment='Edited', level='INFO')

        notification_data['stamp'] = datetime(2021, 2, 2, 9, 33, 34, tzinfo=UTC)
        test_notification = generate_email_notification(notification_data, self.source)
        test_notification.subject = 'another subject'
        with patch(f'{HANDLER_MODULE}.get_notifications', return_value=[test_notification]):
            self.job.run()

        self.assertEqual(2, RawNotification.objects.count())
        self.assertEqual(list(Note.objects.values_list('comment', 'level')), [('Edited', 'INFO')])

    def test_note_comment_markdown(self):
        """
        The note's comment, which renders as Markdown, shows the provider's circuit ID verbatim.
        """
        notification_data = get_base_notification_data()
        notification_data['circuitimpacts'] = [
            {'cid': '<ABC-1>', 'impact': 'NO-IMPACT'},
            {'cid': 'A*B*C', 'impact': 'NO-IMPACT'},
            {'cid': 'X``Y`', 'impact': 'NO-IMPACT'},
        ]
        test_notification = generate_email_notification(notification_data, self.source)
        process_raw_notification(self.job, test_notification)

        notes = {note.title: note for note in Note.objects.all()}
        for cid, html in (('<ABC-1>', '&lt;ABC-1&gt;'), ('A*B*C', 'A*B*C'), ('X``Y`', 'X``Y`')):
            with self.subTest(cid=cid):
                note = notes[f'Nonexistent circuit ID {cid}']
                self.assertEqual(note.level, 'WARNING')
                self.assertIn(f'Circuit ID <code>{html}</code> referenced was not found', render_markdown(note.comment))

    def test_parsed_notification_json(self):
        """
        The parser's output is stored as a JSON object (Nautobot stored the JSON string), with plain values.
        """
        test_notification = generate_email_notification(get_base_notification_data(), self.source)
        process_raw_notification(self.job, test_notification)

        parsed_json = ParsedNotification.objects.get().json
        self.assertEqual(parsed_json['maintenance_id'], 'MNT-NTT')
        self.assertEqual(parsed_json['status'], 'CONFIRMED')
        self.assertEqual(
            parsed_json['circuits'],
            [{'circuit_id': 'cid-000001', 'impact': 'NO-IMPACT'}, {'circuit_id': 'cid-000002', 'impact': 'NO-IMPACT'}],
        )

    def test_date_without_time_zone(self):
        """
        A Date header without a time zone is read in TIME_ZONE, without Django's naive datetime warning.
        """
        test_notification = generate_email_notification(get_base_notification_data(), self.source)
        test_notification.date = 'Mon, 1 Feb 2021 09:33:34'
        with warnings.catch_warnings():
            warnings.simplefilter('error', RuntimeWarning)
            raw_id = process_raw_notification(self.job, test_notification)
            # The duplicate check finds it the same way
            self.assertIsNone(process_raw_notification(self.job, test_notification))

        self.assertEqual(
            RawNotification.objects.get(pk=raw_id).stamp,
            datetime(2021, 2, 1, 9, 33, 34, tzinfo=timezone.get_default_timezone()),
        )

    def test_several_maintenances(self):
        """
        A notification announcing several maintenances gets one parsed notification for each, and is marked parsed once.
        """
        notification_data = get_base_notification_data()
        other_data = {**notification_data, 'name': 'MNT-OTHER'}
        job = self.get_job(user=self.user)
        test_notification = generate_email_notification(notification_data, self.source, other_data)
        with patch(f'{HANDLER_MODULE}.get_notifications', return_value=[test_notification]):
            job.run()

        self.assertEqual(
            sorted(ParsedNotification.objects.values_list('maintenance__name', flat=True)),
            ['ntt-MNT-NTT', 'ntt-MNT-OTHER'],
        )
        raw_notification = RawNotification.objects.get()
        self.assertTrue(raw_notification.parsed)
        self.assertEqual(
            ObjectChange.objects.filter(
                changed_object_id=raw_notification.pk, action=ObjectChangeActionChoices.ACTION_UPDATE
            ).count(),
            1,
        )

    def test_update_circuit_maintenance_other_provider(self):
        """
        An update notice changes only the impacts on the notifying provider's circuits. An impact a user added on
        another provider's circuit stays, and this provider's circuit is linked even if the other one has the same ID.
        Nautobot kept the impact too, but failed on a circuit ID that another provider's linked circuit shared.
        """
        notification_data = get_base_notification_data()
        notification_data['circuitimpacts'] = [{'cid': 'cid-000001', 'impact': 'NO-IMPACT'}]
        with patch(f'{HANDLER_MODULE}.get_notifications') as mock_get_notifications:
            mock_get_notifications.return_value = [generate_email_notification(notification_data, self.source)]
            self.job.run()
            maintenance = CircuitMaintenance.objects.get()
            packetfabric = Provider.objects.get(name='packetfabric')
            circuit_type = CircuitType.objects.get()
            for cid in ('PF-1', 'CID-000002'):
                CircuitImpact.objects.create(
                    maintenance=maintenance,
                    circuit=Circuit.objects.create(cid=cid, provider=packetfabric, type=circuit_type),
                    impact='DEGRADED',
                )

            notification_data['stamp'] = datetime(2021, 2, 2, 9, 33, 34, tzinfo=UTC)
            notification_data['circuitimpacts'] = [{'cid': 'cid-000002', 'impact': 'OUTAGE'}]
            test_notification = generate_email_notification(notification_data, self.source)
            test_notification.subject = 'another subject'
            mock_get_notifications.return_value = [test_notification]
            self.job.run()

        self.assertEqual(
            sorted(maintenance.impacts.values_list('circuit__provider__name', 'circuit__cid', 'impact')),
            [
                ('ntt', 'CID-000002', 'OUTAGE'),
                ('packetfabric', 'CID-000002', 'DEGRADED'),
                ('packetfabric', 'PF-1', 'DEGRADED'),
            ],
        )

    def test_dry_run_tagging(self):
        """
        The sources that a dry run fetches with leave messages untagged: the handler tags them, and the source does
        nothing in a dry run.
        """
        for dry_run, tagged in ((True, False), (False, True)):
            with self.subTest(dry_run=dry_run):
                source = GmailAPI(
                    name=self.notification_source.name,
                    url='https://accounts.google.com/o/oauth2/auth',
                    account='me@example.com',
                    credentials_file='path_to_file',
                    labels={'parsed': 'Label_1'},
                    dry_run=dry_run,
                )
                service = source.service = MagicMock()
                test_notification = generate_email_notification(get_base_notification_data(), source)
                with patch(f'{HANDLER_MODULE}.get_notifications', return_value=[test_notification]):
                    self.get_job().run(dry_run=dry_run)
                self.assertEqual(service.users.return_value.messages.return_value.modify.called, tagged)

    def test_create_circuit_maintenance_duplicated_circuit_id(self):
        """
        Test create_circuit_maintenance.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        provider = Provider.objects.get(name=test_notification.provider_type)
        parsed_maintenance = parse_notification(test_notification)
        # Duplicating the circuit ID
        parsed_maintenance.circuits[1].circuit_id = parsed_maintenance.circuits[0].circuit_id
        create_circuit_maintenance(
            self.job,
            test_notification,
            f'{provider.name}-{parsed_maintenance.maintenance_id}',
            parsed_maintenance,
            provider,
        )
        self.assertEqual(1, CircuitMaintenance.objects.count())
        self.assertEqual(1, CircuitImpact.objects.count())
        self.assertEqual(0, Note.objects.count())


class HandleNotificationsChangesTestCase(HandlerTestCase):
    """
    The run's writes: change records and events under a user, dry runs and validation.
    """

    def get_changes(self, job=None):
        """
        Return the change records of the job's run: model, action, user, and the impact or status before and after.
        """
        job = job or self.job
        fields = {'circuitimpact': 'impact', 'circuitmaintenance': 'status', 'rawnotification': 'parsed'}
        changes = []
        for change in ObjectChange.objects.filter(request_id=job.job.job_id).order_by('pk'):
            model = change.changed_object_type.model
            field = fields.get(model)
            changes.append(
                (
                    model,
                    change.action,
                    change.user,
                    (change.prechange_data or {}).get(field) if field else None,
                    (change.postchange_data or {}).get(field) if field else None,
                )
            )
        return changes

    def run_notifications(self, job, *notifications, dry_run=False):
        with patch(f'{HANDLER_MODULE}.get_notifications', return_value=list(notifications)) as mock_get_notifications:
            job.run(dry_run=dry_run)
        return mock_get_notifications

    def test_changelog(self):
        """
        A run records its changes as the job's user, with the job's ID as the request ID. Values are the parser's
        plain values, not its Enum members, and the raw notification has one update, for parsed.
        """
        job = self.get_job(user=self.user)
        self.run_notifications(job, generate_email_notification(get_base_notification_data(), self.source))

        create, update = ObjectChangeActionChoices.ACTION_CREATE, ObjectChangeActionChoices.ACTION_UPDATE
        self.assertEqual(
            self.get_changes(job),
            [
                ('rawnotification', create, self.user, None, False),
                ('circuitmaintenance', create, self.user, None, 'CONFIRMED'),
                ('circuitimpact', create, self.user, None, 'NO-IMPACT'),
                ('circuitimpact', create, self.user, None, 'NO-IMPACT'),
                ('rawnotification', update, self.user, False, True),
                ('parsednotification', create, self.user, None, None),
            ],
        )
        self.assertFalse(self.get_log('warning', job=job))

    def test_changelog_update(self):
        """
        An update records each object as it was: the maintenance's status, an impact's impact, an unlinked impact.
        """
        notification_data = get_base_notification_data()
        self.run_notifications(
            self.get_job(user=self.user), generate_email_notification(notification_data, self.source)
        )

        notification_data['status'] = 'COMPLETED'
        notification_data['stamp'] = datetime(2021, 2, 2, 9, 33, 34, tzinfo=UTC)
        notification_data['circuitimpacts'] = [{'cid': 'cid-000002', 'impact': 'OUTAGE'}]
        test_notification = generate_email_notification(notification_data, self.source)
        test_notification.subject = 'another subject'
        job = self.get_job(user=self.user)
        self.run_notifications(job, test_notification)

        create, update, delete = (
            ObjectChangeActionChoices.ACTION_CREATE,
            ObjectChangeActionChoices.ACTION_UPDATE,
            ObjectChangeActionChoices.ACTION_DELETE,
        )
        self.assertEqual(
            self.get_changes(job),
            [
                ('rawnotification', create, self.user, None, False),
                ('circuitmaintenance', update, self.user, 'CONFIRMED', 'COMPLETED'),
                ('circuitimpact', delete, self.user, 'NO-IMPACT', None),
                ('circuitimpact', update, self.user, 'NO-IMPACT', 'OUTAGE'),
                ('rawnotification', update, self.user, False, True),
                ('parsednotification', create, self.user, None, None),
            ],
        )

    def test_changelog_job_user(self):
        """
        A run without a user (a scheduled run) records its changes as the job_user setting's user.
        """
        with plugin_settings(job_user=self.user.username):
            self.run_notifications(self.job, generate_email_notification(get_base_notification_data(), self.source))

        changes = self.get_changes()
        self.assertEqual(len(changes), 6)
        self.assertEqual({change[2] for change in changes}, {self.user})
        self.assertFalse(self.get_log('warning'))

    def test_changelog_job_user_and_user(self):
        """
        The job's user, who ran it, takes precedence over the job_user setting.
        """
        other_user = User.objects.create_user(username='user2')
        job = self.get_job(user=self.user)
        with plugin_settings(job_user=other_user.username):
            self.run_notifications(job, generate_email_notification(get_base_notification_data(), self.source))

        self.assertEqual({change[2] for change in self.get_changes(job)}, {self.user})

    def test_changelog_without_user(self):
        """
        Without a user, or with a job_user that doesn't exist, the run records no changes and says so.
        """
        for job_user, warning in (
            (
                None,
                "Changes aren't recorded in the change log and send no events: the job has no user, and the job_user "
                'setting is unset.',
            ),
            (
                'nobody',
                "Changes aren't recorded in the change log and send no events: user nobody in the job_user setting "
                "doesn't exist.",
            ),
        ):
            with self.subTest(job_user=job_user):
                job = self.get_job()
                test_notification = generate_email_notification(get_base_notification_data(), self.source)
                test_notification.subject = str(job_user)
                with plugin_settings(job_user=job_user):
                    self.run_notifications(job, test_notification)

                self.assertEqual(len(job.job.data['raw_notifications']), 1)
                self.assertFalse(ObjectChange.objects.exists())
                self.assertEqual(self.get_log('warning', job=job), [warning])

    def test_events(self):
        """
        Each notification's events are sent once its transaction has committed, one per object; a notification that
        fails sends none. The default EVENTS_PIPELINE entry is patched rather than queueing jobs in Redis.
        """
        notification_data = get_base_notification_data()
        test_notification = generate_email_notification(notification_data, self.source)
        notification_data['status'] = 'Non valid status'
        failing_notification = generate_email_notification(notification_data, self.source)
        failing_notification.subject = 'Failing'

        # Sent after the notification's savepoint is released. The depth is checked afterwards: the handler would catch
        # an assertion failing inside the pipeline
        savepoints = len(connection.savepoint_ids)
        depths = []
        with patch(
            'extras.events.process_event_queue',
            side_effect=lambda events: depths.append(len(connection.savepoint_ids)),
        ) as process_event_queue:
            self.run_notifications(self.get_job(user=self.user), test_notification, failing_notification)

        self.assertEqual(depths, [savepoints])
        process_event_queue.assert_called_once()
        events = sorted(
            (event['object_type'].model, event['event_type']) for event in process_event_queue.call_args.args[0]
        )
        self.assertEqual(
            events,
            [
                ('circuitimpact', OBJECT_CREATED),
                ('circuitimpact', OBJECT_CREATED),
                ('circuitmaintenance', OBJECT_CREATED),
                ('parsednotification', OBJECT_CREATED),
                ('rawnotification', OBJECT_CREATED),
            ],
        )

    def test_request_processors(self):
        """
        Each notification runs under every registered request processor, as NetBox's background jobs do (a plugin can
        register one), with the run's request: the job's user, and the job's ID as the request ID.
        """
        requests = []

        @contextmanager
        def request_processor(request):
            requests.append((request.user, request.id))
            yield

        registry['request_processors'].append(request_processor)
        self.addCleanup(registry['request_processors'].remove, request_processor)
        job = self.get_job(user=self.user)
        self.run_notifications(job, generate_email_notification(get_base_notification_data(), self.source))

        self.assertEqual(requests, [(self.user, job.job.job_id)])

    def test_events_error(self):
        """
        A notification whose events fail to be sent after it committed stays processed, and the log says what failed.
        """
        test_notification = generate_email_notification(get_base_notification_data(), self.source)
        job = self.get_job(user=self.user)
        with patch('extras.events.process_event_queue', side_effect=RuntimeError('Boom')):
            self.run_notifications(job, test_notification)

        self.assertEqual(job.job.data['raw_notifications'], [RawNotification.objects.get().pk])
        self.assertEqual(
            self.get_log('error', job=job),
            [f'Unexpected exception when sending the events of notification `{test_notification.subject}`: Boom'],
        )
        self.assertEqual(self.get_log('info', job=job)[-1], '1 notifications processed.')

    def handle_scheduled_job(self, job_interval):
        """
        Run a scheduled job of the handler with a 15-minute interval through handle(), as the RQ worker does, and return
        it. Scheduling queues nothing in Redis: TestCase doesn't run on_commit callbacks.
        """
        job = HandleCircuitMaintenanceNotifications.enqueue(schedule_at=timezone.now(), interval=15)
        logger = self.job.logger
        with (
            patch(f'{HANDLER_MODULE}.get_notifications', return_value=[]) as self.mock_get_notifications,
            plugin_settings(job_interval=job_interval),
        ):
            HandleCircuitMaintenanceNotifications.handle(job)
        self.addCleanup(
            lambda: [logger.removeHandler(h) for h in list(logger.handlers) if getattr(h, 'job', None) is job]
        )
        return job

    def test_unset_job_interval(self):
        """
        A scheduled run that starts after job_interval was unset does nothing and ends the schedule: NetBox reschedules
        every job with an interval, as a system job or not.
        """
        job = self.handle_scheduled_job(job_interval=None)

        self.mock_get_notifications.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertIsNone(job.interval)
        self.assertEqual(
            [entry['message'] for entry in job.log_entries if entry['level'] == 'warning'],
            ['The job_interval setting is unset, so this scheduled run does nothing and the schedule ends.'],
        )
        # No next run was scheduled
        self.assertEqual(list(Job.objects.filter(name=job.name)), [job])

    def test_scheduled_run(self):
        """
        A scheduled run with job_interval set fetches as usual, and NetBox schedules the next one.
        """
        job = self.handle_scheduled_job(job_interval=15)

        self.mock_get_notifications.assert_called_once()
        self.assertEqual(Job.objects.filter(name=job.name, status=JobStatusChoices.STATUS_SCHEDULED).count(), 1)

    def test_required_custom_fields(self):
        """
        A required custom field's default is filled in before validation, on new and updated objects, as save() fills
        it after: otherwise full_clean() would reject every write.
        """
        notification_data = get_base_notification_data()
        notification_data['circuitimpacts'].append({'cid': 'nonexistent', 'impact': 'NO-IMPACT'})
        self.run_notifications(self.get_job(), generate_email_notification(notification_data, self.source))

        models = (CircuitMaintenance, CircuitImpact, Note, RawNotification, ParsedNotification)
        custom_field = CustomField.objects.create(name='required_field', type='text', required=True, default='x')
        custom_field.object_types.set(ObjectType.objects.get_for_model(model) for model in models)
        # NetBox gave the existing objects the default. Without it, they stand for objects saved before the field had one
        for model in models:
            model.objects.update(custom_field_data={})
        notification_data['status'] = 'COMPLETED'
        notification_data['stamp'] = datetime(2021, 2, 2, 9, 33, 34, tzinfo=UTC)
        notification_data['circuitimpacts'].append({'cid': 'nonexistent 2', 'impact': 'NO-IMPACT'})
        test_notification = generate_email_notification(notification_data, self.source)
        test_notification.subject = 'another subject'
        job = self.get_job()
        self.run_notifications(job, test_notification)

        self.assertFalse(self.get_log('error', job=job))
        self.assertEqual(CircuitMaintenance.objects.get().status, 'COMPLETED')
        for model, count in (
            (CircuitMaintenance, 1),
            (CircuitImpact, 2),
            (Note, 1),
            (RawNotification, 1),
            (ParsedNotification, 1),
        ):
            with self.subTest(model=model.__name__):
                # The updated objects, and the ones created after the custom field
                self.assertEqual(model.objects.filter(custom_field_data__required_field='x').count(), count)

    def test_enqueue(self):
        """
        Through NetBox's job framework, as Run now or the schedule start it: run() takes the arguments and job_id that
        handle() passes on, and the job is saved with its data and log.
        """
        test_notification = generate_email_notification(get_base_notification_data(), self.source)
        logger = self.job.logger
        with patch(f'{HANDLER_MODULE}.get_notifications', return_value=[test_notification]):
            job = HandleCircuitMaintenanceNotifications.enqueue(immediate=True, user=self.user, dry_run=False)
        self.addCleanup(
            lambda: [logger.removeHandler(h) for h in list(logger.handlers) if getattr(h, 'job', None) is job]
        )

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(job.name, 'Update Circuit Maintenances')
        self.assertEqual(job.data, {'dry_run': False, 'raw_notifications': [RawNotification.objects.get().pk]})
        self.assertEqual(job.log_entries[-1]['message'], '1 notifications processed.')
        self.assertEqual({change.user for change in ObjectChange.objects.filter(request_id=job.job_id)}, {self.user})

    def test_dry_run(self):
        """
        A dry run fetches with dry_run, which leaves the mailbox alone, and commits nothing and records no changes.
        """
        job = self.get_job(user=self.user)
        test_notification = generate_email_notification(get_base_notification_data(), self.source)
        mock_get_notifications = self.run_notifications(job, test_notification, dry_run=True)

        mock_get_notifications.assert_called_once_with(job, [self.notification_source], ANY, dry_run=True)
        for model in (RawNotification, ParsedNotification, CircuitMaintenance, CircuitImpact, Note, ObjectChange):
            with self.subTest(model=model.__name__):
                self.assertFalse(model.objects.exists())
        self.assertIn('DRYRUN mode, nothing has been committed.', self.get_log('info', job=job))
        self.assertEqual(self.get_log('info', job=job)[-1], '1 notifications processed.')
        # The raw notifications were rolled back
        self.assertEqual(job.job.data, {'dry_run': True, 'raw_notifications': []})
        self.assertFalse(self.get_log('warning', job=job))

    def test_custom_validators(self):
        """
        Every object the handler writes is validated, so CUSTOM_VALIDATORS rules apply. A rule that fails rolls the
        notification back.
        """
        notification_data = get_base_notification_data()
        notification_data['circuitimpacts'].append({'cid': 'nonexistent', 'impact': 'NO-IMPACT'})
        test_notification = generate_email_notification(notification_data, self.source)
        for model, rule in (
            # Fails on the create only, passes on the parsed re-save
            ('rawnotification', {'parsed': {'eq': True}}),
            ('circuitmaintenance', {'name': {'regex': '^X'}}),
            ('circuitimpact', {'impact': {'eq': 'OUTAGE'}}),
            ('note', {'title': {'regex': '^X'}}),
            ('rawnotification', {'parsed': {'neq': True}}),
            ('parsednotification', {'maintenance': {'prohibited': True}}),
        ):
            with (
                self.subTest(model=model, rule=rule),
                override_settings(CUSTOM_VALIDATORS={f'netbox_circuit_maintenance.{model}': [rule]}),
            ):
                job = self.get_job(user=self.user)
                self.run_notifications(job, test_notification)

                self.assertFalse(RawNotification.objects.exists())
                self.assertFalse(CircuitMaintenance.objects.exists())
                self.assertFalse(ObjectChange.objects.exists())
                self.assertTrue(
                    self.get_log('error', job=job)[-1].startswith(
                        f'Unexpected exception when parsing notification `{test_notification.subject}`: '
                    )
                )

    def test_custom_validators_update(self):
        """
        Updates are validated too.
        """
        notification_data = get_base_notification_data()
        self.run_notifications(self.get_job(), generate_email_notification(notification_data, self.source))

        notification_data['status'] = 'COMPLETED'
        notification_data['stamp'] = datetime(2021, 2, 2, 9, 33, 34, tzinfo=UTC)
        notification_data['circuitimpacts'] = [
            {'cid': circuitimpact['cid'], 'impact': 'OUTAGE'} for circuitimpact in notification_data['circuitimpacts']
        ]
        test_notification = generate_email_notification(notification_data, self.source)
        test_notification.subject = 'another subject'
        for model, rule in (
            ('circuitmaintenance', {'status': {'neq': 'COMPLETED'}}),
            ('circuitimpact', {'impact': {'neq': 'OUTAGE'}}),
        ):
            with (
                self.subTest(model=model, rule=rule),
                override_settings(CUSTOM_VALIDATORS={f'netbox_circuit_maintenance.{model}': [rule]}),
            ):
                self.run_notifications(self.get_job(), test_notification)

                self.assertEqual(1, RawNotification.objects.count())
                self.assertEqual(CircuitMaintenance.objects.get().status, 'CONFIRMED')
                self.assertEqual(set(CircuitImpact.objects.values_list('impact', flat=True)), {'NO-IMPACT'})


class SystemJobTestCase(TestCase):
    """
    jobs.py, which ready() imports, schedules the handler when job_interval is set. Each test runs the module again
    with runpy, which leaves sys.modules alone, so test_plugin can check that ready() imported it.
    """

    def setUp(self):
        # Restore the registration that startup made, if any
        system_jobs = registry['system_jobs']
        startup = system_jobs.pop(HandleCircuitMaintenanceNotifications, None)

        def restore():
            system_jobs.pop(HandleCircuitMaintenanceNotifications, None)
            if startup is not None:
                system_jobs[HandleCircuitMaintenanceNotifications] = startup

        self.addCleanup(restore)

    def run_jobs_module(self):
        # runpy warns that the module is in sys.modules already: ready() imported it
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', "'netbox_circuit_maintenance.jobs' found in sys.modules", RuntimeWarning)
            runpy.run_module('netbox_circuit_maintenance.jobs')

    def test_job_interval(self):
        with plugin_settings(job_interval=15):
            self.run_jobs_module()
        self.assertEqual(registry['system_jobs'][HandleCircuitMaintenanceNotifications], {'interval': 15})

    def test_job_interval_unset(self):
        self.run_jobs_module()
        self.assertNotIn(HandleCircuitMaintenanceNotifications, registry['system_jobs'])

    def test_job_interval_invalid(self):
        # system_job() would accept none of the first three, and NetBox's Job model rejects the last two
        for value in ('15', 15.0, True, 0, -5):
            with (
                self.subTest(value=value),
                plugin_settings(job_interval=value),
                self.assertRaisesMessage(
                    ImproperlyConfigured,
                    "PLUGINS_CONFIG['netbox_circuit_maintenance']['job_interval'] must be a whole number of minutes, "
                    f'at least 1, or None, not {value!r}.',
                ),
            ):
                self.run_jobs_module()
            self.assertNotIn(HandleCircuitMaintenanceNotifications, registry['system_jobs'])
