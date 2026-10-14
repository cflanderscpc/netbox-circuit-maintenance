import email
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from unittest.mock import ANY, MagicMock, patch

from circuits.models import Provider
from django.test import TestCase
from django.utils import timezone
from pydantic import ValidationError

from netbox_circuit_maintenance.constants import PROVIDER_EMAILS_FIELD
from netbox_circuit_maintenance.enum import MessageProcessingStatus
from netbox_circuit_maintenance.handle_notifications.sources import (
    IMAP,
    EmailSource,
    MaintenanceNotification,
    RedirectAuthorize,
    Source,
    get_notifications,
)
from netbox_circuit_maintenance.models import NotificationSource

from .utils import MockedJob, assert_called_with_substring, plugin_settings

SOURCES_MODULE = 'netbox_circuit_maintenance.handle_notifications.sources'

SOURCE_IMAP = {
    'name': 'source imap',
    'account': 'me@example.com',
    'secret': 'supersecret',
    'url': 'imap://example.com',
}

# Noon UTC, so the date is 1 February 2021 from UTC-11 to UTC+11
SINCE = int(datetime(2021, 2, 1, 12, tzinfo=UTC).timestamp())
SINCE_TXT = '01-Feb-2021'

DATE_HEADER = b'Date: Mon, 1 Feb 2021 09:33:34 +0000'


def imap_source(**kwargs):
    return IMAP(
        name='whatever', url='imap://localhost', account='account', password='pass', imap_server='localhost', **kwargs
    )


def message_from_headers(*headers):
    """
    A message with the given raw header lines, parsed as IMAP.fetch_email() parses one.
    """
    return email.message_from_bytes(b'\r\n'.join([*headers, b'', b'Some text goes here']))


def raw_email(sender='sender@example.com', subject='Maintenance'):
    return b'\r\n'.join([f'From: {sender}'.encode(), f'Subject: {subject}'.encode(), DATE_HEADER, b'', b'body'])


def build_notification(source):
    return MaintenanceNotification(
        msg_id=b'12345',
        source=source,
        sender='sender@example.com',
        subject='Test subject',
        provider_type='ntt',
        raw_payload=raw_email(),
        date='Mon, 1 Feb 2021 09:33:34 +0000',
    )


def without(config, key):
    return {name: value for name, value in config.items() if name != key}


class SourceTestCase(TestCase):
    def test_source_factory_nonexistent_name(self):
        # Without any configured source (the default setting)
        non_existent_name = 'abc'
        with self.assertRaisesMessage(ValueError, f'Name {non_existent_name} not found in PLUGINS_CONFIG.'):
            Source.init(name=non_existent_name)

    def test_source_factory_nonexistent_url(self):
        with (
            plugin_settings(notification_sources=[without(SOURCE_IMAP, 'url')]),
            self.assertRaisesMessage(ValueError, f'URL for {SOURCE_IMAP["name"]} not found in PLUGINS_CONFIG'),
        ):
            Source.init(name=SOURCE_IMAP['name'])

    def test_source_factory_url_scheme_not_supported(self):
        # EWS is replaced by Microsoft Graph, and isn't coming back
        for url, scheme in (('ftp://example.com', 'ftp'), ('ews://example.com', 'ews')):
            with (
                self.subTest(url=url),
                plugin_settings(notification_sources=[{**SOURCE_IMAP, 'url': url}]),
                self.assertRaisesMessage(
                    ValueError, f'Scheme {scheme} not supported as Notification Source (only IMAP).'
                ),
            ):
                Source.init(name=SOURCE_IMAP['name'])

    def test_source_factory_url_malformed(self):
        with (
            plugin_settings(notification_sources=[{**SOURCE_IMAP, 'url': 'wrong url'}]),
            self.assertRaisesMessage(ValueError, 'Scheme  not supported as Notification Source (only IMAP).'),
        ):
            Source.init(name=SOURCE_IMAP['name'])

    def test_test_authentication(self):
        source = imap_source()
        for exc, message in (
            # imaplib raises the server's response as bytes, which needn't be UTF-8
            (Exception(b'[AUTHENTICATIONFAILED] Invalid credentials'), '[AUTHENTICATIONFAILED] Invalid credentials'),
            (Exception(b'Mot de passe incorrect \xe9'), 'Mot de passe incorrect �'),
            (ConnectionRefusedError(111, 'Connection refused'), '[Errno 111] Connection refused'),
            # Nautobot raised IndexError for an exception without arguments
            (TimeoutError(), ''),
        ):
            with self.subTest(exc=exc), patch(f'{SOURCES_MODULE}.IMAP.open_session', side_effect=exc):
                self.assertEqual(source.test_authentication(), (False, message))

        redirect = RedirectAuthorize(url_name='google_authorize', source_name='whatever')
        with patch(f'{SOURCES_MODULE}.IMAP.open_session', side_effect=redirect), self.assertRaises(RedirectAuthorize):
            source.test_authentication()

    def test_validation_error_hides_input(self):
        # A wrongly typed secret doesn't appear in the error, which the job log and the source's page show
        with (
            plugin_settings(notification_sources=[{**SOURCE_IMAP, 'secret': 12345678}]),
            self.assertRaises(ValidationError) as context,
        ):
            Source.init(name=SOURCE_IMAP['name'])
        self.assertIn('password\n  Input should be a valid string', str(context.exception))
        self.assertNotIn('12345678', str(context.exception))


class EmailSourceTestCase(TestCase):
    def test_extract_email_source(self):
        for email_source, email_source_output in (
            ('user@example.com', 'user@example.com'),
            ('<user@example.com>', 'user@example.com'),
            ('user <user@example.com>', 'user@example.com'),
            ('No-reply via mailing list <mailing-list@example.com>', 'mailing-list@example.com'),
        ):
            with self.subTest(email_source=email_source):
                self.assertEqual(EmailSource.extract_email_source(email_source), email_source_output)

    def test_process_email_success(self):
        """
        Test successful processing of a single email into a MaintenanceNotification.
        """
        Provider.objects.create(name='zayo', slug='zayo', custom_field_data={PROVIDER_EMAILS_FIELD: 'user@example.com'})

        source = imap_source()

        job = MockedJob()

        email_message = EmailMessage()
        email_message['From'] = 'User <user@example.com>'
        email_message['Subject'] = 'Circuit Maintenance Notification'
        email_message['Date'] = 'Mon, 1 Feb 2021 09:33:34 +0000'
        email_message['Content-Type'] = 'text/html'
        email_message.set_payload(b'Some text goes here')

        notification = source.process_email(job, email_message, b'12345')
        self.assertIsNotNone(notification)
        self.assertEqual(notification.msg_id, b'12345')
        self.assertEqual(notification.source, source)
        self.assertEqual(notification.sender, 'user@example.com')
        self.assertEqual(notification.subject, 'Circuit Maintenance Notification')
        self.assertEqual(notification.provider_type, 'zayo')
        self.assertEqual(notification.raw_payload, email_message.as_bytes())
        self.assertEqual(notification.date, str(email_message['Date']))

    def test_process_email_success_alternate_source_header(self):
        """
        Test successful processing of a single email with a non-standard source header.
        """
        Provider.objects.create(name='zayo', slug='zayo', custom_field_data={PROVIDER_EMAILS_FIELD: 'user@example.com'})

        source = imap_source(source_header='X-Original-Sender')

        job = MockedJob()

        email_message = EmailMessage()
        email_message['From'] = 'Mailing List <mailing-list@example.com>'
        email_message['Date'] = 'Mon, 1 Feb 2021 09:33:34 +0000'
        email_message['X-Original-Sender'] = 'User <user@example.com>'
        email_message['Subject'] = 'Circuit Maintenance Notification'
        email_message['Content-Type'] = 'text/html'
        email_message.set_payload(b'Some text goes here')

        notification = source.process_email(job, email_message, b'12345')
        self.assertIsNotNone(notification)
        self.assertEqual(notification.msg_id, b'12345')
        self.assertEqual(notification.source, source)
        self.assertEqual(notification.sender, 'user@example.com')
        self.assertEqual(notification.subject, 'Circuit Maintenance Notification')
        self.assertEqual(notification.provider_type, 'zayo')
        self.assertEqual(notification.raw_payload, email_message.as_bytes())

    def test_process_email_decoded_headers(self):
        Provider.objects.create(name='zayo', slug='zayo', custom_field_data={PROVIDER_EMAILS_FIELD: 'noc@zayo.example'})
        source = imap_source()

        for subject_header, subject in (
            (b'Subject: =?utf-8?q?Planned_Work_=E2=80=93_Circuit_X?=', 'Planned Work – Circuit X'),
            (b'Subject: A very long\r\n subject, folded', 'A very long subject, folded'),
            # Raw UTF-8, which the message itself returns as an email.header.Header
            ('Subject: Planned Work – Circuit X'.encode(), 'Planned Work – Circuit X'),
        ):
            with self.subTest(subject_header=subject_header):
                # Raw UTF-8 in the sender's name raised TypeError in Nautobot
                email_message = message_from_headers(
                    'From: Zayo NÖC <NOC@zayo.example>'.encode(), subject_header, DATE_HEADER
                )

                notification = source.process_email(MockedJob(), email_message, b'1')
                self.assertEqual(notification.subject, subject)
                self.assertEqual(notification.sender, 'noc@zayo.example')
                self.assertEqual(notification.provider_type, 'zayo')
                # The stored message is the one fetched, headers undecoded
                self.assertEqual(notification.raw_payload, email_message.as_bytes())

    def test_process_email_raw_headers(self):
        # The sender and Date are read as Nautobot read them; decoding them changed the result
        Provider.objects.create(name='zayo', slug='zayo', custom_field_data={PROVIDER_EMAILS_FIELD: 'noc@zayo.example'})
        source = imap_source()

        for from_header in (
            # Decoded, the email library rejects the name (CR LF), which would stop the run
            b'From: =?utf-8?q?NOC=0D=0A?= <noc@zayo.example>',
            # Decoded, the name's "<jose@x.example>" would come first
            b'From: =?utf-8?q?Jos=C3=A9_=3Cjose=40x=2Eexample=3E_via_Zayo?= <noc@zayo.example>',
        ):
            with self.subTest(from_header=from_header):
                email_message = message_from_headers(from_header, b'Subject: Maintenance', DATE_HEADER)
                self.assertEqual(source.process_email(MockedJob(), email_message, b'1').sender, 'noc@zayo.example')

        # A date without a numeric zone stays as sent (decoded, it would read as UTC)
        for date in ('Mon, 1 Feb 2021 09:33:34', 'Mon, 1 Feb 2021 09:33:34 CET'):
            with self.subTest(date=date):
                email_message = message_from_headers(
                    b'From: noc@zayo.example', b'Subject: Maintenance', f'Date: {date}'.encode()
                )
                self.assertEqual(source.process_email(MockedJob(), email_message, b'1').date, date)

    def test_process_email_without_sender(self):
        source = imap_source()
        for headers, value in (
            ((b'Subject: Maintenance', DATE_HEADER), 'None'),
            ((b'From: ', b'Subject: Maintenance', DATE_HEADER), ''),
        ):
            with self.subTest(value=value), patch(f'{SOURCES_MODULE}.IMAP.tag_message') as tag_message:
                job = MockedJob()
                # Nautobot raised ValueError, which stopped the whole run
                self.assertIsNone(source.process_email(job, message_from_headers(*headers), b'1'))
                job.logger.error.assert_called_once_with(
                    f'Not possible to determine the email sender from "From: {value}"', extra=ANY
                )
                tag_message.assert_called_once_with(job, b'1', MessageProcessingStatus.UNKNOWN_PROVIDER)

    def test_process_email_unknown_provider(self):
        source = imap_source()
        job = MockedJob()
        with patch(f'{SOURCES_MODULE}.IMAP.tag_message') as tag_message:
            email_message = message_from_headers(b'From: noc@unknown.example', b'Subject: Maintenance', DATE_HEADER)
            self.assertIsNone(source.process_email(job, email_message, b'1'))
        job.logger.warning.assert_called_once_with(
            'Not possible to determine the provider_type for noc@unknown.example',
            extra={'object': 'noc@unknown.example'},
        )
        tag_message.assert_called_once_with(job, b'1', MessageProcessingStatus.UNKNOWN_PROVIDER)

    def test_process_email_missing_headers(self):
        Provider.objects.create(name='zayo', slug='zayo', custom_field_data={PROVIDER_EMAILS_FIELD: 'noc@zayo.example'})
        source = imap_source()
        for headers, missing in (
            ((b'From: noc@zayo.example', DATE_HEADER), 'Subject'),
            ((b'From: noc@zayo.example', b'Subject: Maintenance'), 'Date'),
            ((b'From: noc@zayo.example',), 'Subject or Date'),
        ):
            with self.subTest(missing=missing), patch(f'{SOURCES_MODULE}.IMAP.tag_message') as tag_message:
                job = MockedJob()
                # Nautobot raised a pydantic ValidationError, which stopped the whole run
                self.assertIsNone(source.process_email(job, message_from_headers(*headers), b'1'))
                job.logger.error.assert_called_once_with(
                    f'Skipping the email from noc@zayo.example because it has no {missing} header.', extra=ANY
                )
                tag_message.assert_called_once_with(job, b'1', MessageProcessingStatus.PARSING_FAILED)

        # An empty Subject is still a Subject
        email_message = message_from_headers(b'From: noc@zayo.example', b'Subject: ', DATE_HEADER)
        self.assertEqual(source.process_email(MockedJob(), email_message, b'1').subject, '')

    def test_get_provider_type_from_email(self):
        Provider.objects.create(
            name='abc d', slug='abc-d', custom_field_data={PROVIDER_EMAILS_FIELD: 'user@example.com'}
        )
        source = imap_source()

        provider_type = source.get_provider_type_from_email('user@example.com')
        self.assertEqual(provider_type, 'abc d')
        provider_type = source.get_provider_type_from_email('unknown')
        self.assertIsNone(provider_type)

    def test_get_provider_type_from_email_field_values(self):
        Provider.objects.bulk_create(
            [
                Provider(name='Bravo', slug='bravo', custom_field_data={PROVIDER_EMAILS_FIELD: 'shared@x.com'}),
                Provider(
                    name='Alpha', slug='alpha', custom_field_data={PROVIDER_EMAILS_FIELD: ' NOC@x.com ,shared@x.com,'}
                ),
                # Values that aren't strings, an unset field, and a deleted one (no key)
                Provider(name='Charlie', slug='charlie', custom_field_data={PROVIDER_EMAILS_FIELD: 123}),
                Provider(name='Delta', slug='delta', custom_field_data={PROVIDER_EMAILS_FIELD: None}),
                Provider(name='Echo', slug='echo'),
            ]
        )

        # Addresses are compared stripped and lower-cased, as the validator compares them, in one query
        with self.assertNumQueries(1):
            self.assertEqual(EmailSource.get_provider_type_from_email('noc@x.com'), 'Alpha')
        # The first provider by name, if the validator was bypassed
        self.assertEqual(EmailSource.get_provider_type_from_email('shared@x.com'), 'Alpha')
        # Empty entries match nothing
        self.assertIsNone(EmailSource.get_provider_type_from_email(''))
        self.assertIsNone(EmailSource.get_provider_type_from_email('123'))


@plugin_settings(notification_sources=[SOURCE_IMAP])
class IMAPSourceTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.provider = Provider.objects.create(
            name='ntt', slug='ntt', custom_field_data={PROVIDER_EMAILS_FIELD: 'sender@example.com'}
        )
        cls.notification_source = NotificationSource.objects.create(name=SOURCE_IMAP['name'])
        cls.notification_source.providers.add(cls.provider)

    def setUp(self):
        self.source = Source.init(name=SOURCE_IMAP['name'])

    def test_imap_restricted_password(self):
        """
        Test successful omit of password in output.
        """
        source = imap_source()
        self.assertNotIn('pass', repr(source))
        self.assertIn('account', repr(source))

    def test_source_factory(self):
        """
        Validate Factory pattern for Source class.
        """
        source_instance = Source.init(name=SOURCE_IMAP['name'])
        self.assertIsInstance(source_instance, IMAP)
        self.assertEqual(source_instance.name, SOURCE_IMAP['name'])
        self.assertEqual(source_instance.url, SOURCE_IMAP['url'])
        self.assertEqual(source_instance.account, SOURCE_IMAP['account'])
        self.assertEqual(source_instance.password, SOURCE_IMAP['secret'])
        self.assertEqual(source_instance.imap_server, 'example.com')
        self.assertEqual(source_instance.imap_port, 993)
        self.assertEqual(source_instance.source_header, 'From')
        self.assertFalse(source_instance.dry_run)
        self.assertEqual(source_instance.get_account_id(), SOURCE_IMAP['account'])

    def test_source_factory_url(self):
        for url, imap_server, imap_port in (
            ('IMAP://Mail.Example.com:1993', 'mail.example.com', 1993),
            # Nautobot took the user info as part of the server name, and an IPv6 literal as "["
            ('imap://user@mail.example.com', 'mail.example.com', 993),
            ('imap://[2001:db8::1]:1993', '2001:db8::1', 1993),
        ):
            with self.subTest(url=url), plugin_settings(notification_sources=[{**SOURCE_IMAP, 'url': url}]):
                source_instance = Source.init(name=SOURCE_IMAP['name'])
                self.assertEqual(source_instance.imap_server, imap_server)
                self.assertEqual(source_instance.imap_port, imap_port)

        with (
            plugin_settings(notification_sources=[{**SOURCE_IMAP, 'url': 'imap://example.com:abc'}]),
            self.assertRaisesMessage(ValueError, "Port could not be cast to integer value as 'abc'"),
        ):
            Source.init(name=SOURCE_IMAP['name'])

        # Without a host the source doesn't validate (Nautobot connected to localhost)
        with (
            plugin_settings(notification_sources=[{**SOURCE_IMAP, 'url': 'imap:///'}]),
            self.assertRaisesMessage(ValidationError, 'imap_server\n  Input should be a valid string'),
        ):
            Source.init(name=SOURCE_IMAP['name'])

    def test_source_factory_options(self):
        with plugin_settings(notification_sources=[{**SOURCE_IMAP, 'source_header': 'X-Original-Sender'}]):
            source_instance = Source.init(name=SOURCE_IMAP['name'], dry_run=True)
        self.assertEqual(source_instance.source_header, 'X-Original-Sender')
        self.assertTrue(source_instance.dry_run)

    def test_source_factory_imap_no_account(self):
        """
        Validate Factory pattern IMAP without account settings.
        """
        with (
            plugin_settings(notification_sources=[without(SOURCE_IMAP, 'account')]),
            self.assertRaises(ValidationError),
        ):
            Source.init(name=SOURCE_IMAP['name'])

    def test_source_factory_imap_no_secret(self):
        """
        Validate Factory pattern IMAP without secret settings.
        """
        with plugin_settings(notification_sources=[without(SOURCE_IMAP, 'secret')]), self.assertRaises(ValidationError):
            Source.init(name=SOURCE_IMAP['name'])

    def test_get_notifications_without_providers(self):
        """
        Test get_notifications when there are no Providers defined.
        """
        self.notification_source.providers.set([])

        job = MockedJob()
        res = get_notifications(job, NotificationSource.objects.all(), SINCE)
        self.assertEqual([], res)
        source_name = SOURCE_IMAP['name']
        job.logger.warning.assert_called_with(
            f"Skipping source '{source_name}' because no providers were defined.", extra=ANY
        )

    @patch(f'{SOURCES_MODULE}.IMAP.receive_notifications')
    def test_get_notifications_without_notifications(self, mock_receive_notifications):
        """
        Test get_notifications with provider without email in one of the provider and without notifications.
        """
        mock_receive_notifications.return_value = []
        original_provider = Provider.objects.all().first()
        new_provider = Provider.objects.create(name='something', slug='something')
        notification_source = NotificationSource.objects.all().first()
        notification_source.providers.add(original_provider)
        notification_source.providers.add(new_provider)

        job = MockedJob()
        res = get_notifications(job, NotificationSource.objects.all(), SINCE)
        self.assertEqual([], res)

        job.logger.warning.assert_called_with(
            f'Skipping {new_provider.name} because these providers have no email configured.', extra=ANY
        )
        job.logger.info.assert_called_with(
            f'No notifications received for {original_provider}, {new_provider} since {SINCE_TXT} '
            f'from {notification_source.name}',
            extra=ANY,
        )

    @patch(f'{SOURCES_MODULE}.IMAP.receive_notifications', autospec=True, return_value=[])
    def test_get_notifications_emails(self, mock_receive_notifications):
        Provider.objects.bulk_create(
            [
                Provider(
                    name='Provider 1', slug='provider-1', custom_field_data={PROVIDER_EMAILS_FIELD: ' NOC@x.com ,'}
                ),
                # Only empty entries count as no email (Nautobot fetched "" for them)
                Provider(name='Provider 2', slug='provider-2', custom_field_data={PROVIDER_EMAILS_FIELD: ' , '}),
                Provider(name='Provider 3', slug='provider-3', custom_field_data={PROVIDER_EMAILS_FIELD: 123}),
            ]
        )
        self.notification_source.providers.add(*Provider.objects.filter(name__startswith='Provider'))

        job = MockedJob()
        get_notifications(job, NotificationSource.objects.all(), SINCE)

        source = mock_receive_notifications.call_args.args[0]
        self.assertEqual(source.emails_to_fetch, ['sender@example.com', 'noc@x.com'])
        job.logger.warning.assert_called_once_with(
            'Skipping Provider 2, Provider 3 because these providers have no email configured.', extra=ANY
        )
        job.logger.info.assert_any_call(
            f'Retrieving notifications from {SOURCE_IMAP["name"]} for ntt, Provider 1 since {SINCE_TXT}',
            extra={'object': self.notification_source},
        )

    def test_get_notifications_no_imap_account(self):
        """
        Test get_notifications without IMAP account.
        """
        job = MockedJob()
        with plugin_settings(notification_sources=[without(SOURCE_IMAP, 'account')]):
            get_notifications(job, NotificationSource.objects.all(), SINCE)

        assert_called_with_substring(
            job.logger.warning,
            f'Notification Source {SOURCE_IMAP["name"]} is not matching class expectations: '
            '1 validation error for IMAP\naccount',
        )

    def test_get_notifications_skipped_source(self):
        # NetBox's job log drops the traceback, so the message says why
        for notification_sources, reason in (
            ([], f'Name {SOURCE_IMAP["name"]} not found in PLUGINS_CONFIG.'),
            ([{**SOURCE_IMAP, 'url': 'imap://example.com:abc'}], "Port could not be cast to integer value as 'abc'"),
        ):
            with self.subTest(reason=reason), plugin_settings(notification_sources=notification_sources):
                job = MockedJob()
                self.assertEqual(get_notifications(job, NotificationSource.objects.all(), SINCE), [])
                job.logger.warning.assert_called_once_with(
                    f'Skipping notification source {SOURCE_IMAP["name"]}: {reason}',
                    extra={'object': self.notification_source},
                    exc_info=True,
                )

    @patch(f'{SOURCES_MODULE}.IMAP.receive_notifications')
    def test_get_notifications(self, mock_receive_notifications):
        """
        Test get_notifications.
        """
        notification = build_notification(self.source)

        mock_receive_notifications.return_value = [notification]

        job = MockedJob()
        res = get_notifications(job, NotificationSource.objects.all(), SINCE)

        self.assertEqual(1, len(res))
        job.logger.warning.assert_not_called()

    @patch(f'{SOURCES_MODULE}.IMAP.receive_notifications')
    def test_get_notifications_multiple(self, mock_receive_notifications):
        """
        Test get_notifications multiple.
        """
        notification = build_notification(self.source)

        mock_receive_notifications.return_value = [notification, notification]

        job = MockedJob()
        res = get_notifications(job, NotificationSource.objects.all(), SINCE)

        self.assertEqual(2, len(res))
        job.logger.warning.assert_not_called()

    @patch(f'{SOURCES_MODULE}.IMAP.receive_notifications', autospec=True, return_value=[])
    def test_get_notifications_since(self, mock_receive_notifications):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                get_notifications(MockedJob(), NotificationSource.objects.all(), SINCE, dry_run=dry_run)

                source, _, since_date = mock_receive_notifications.call_args.args
                self.assertEqual(source.dry_run, dry_run)
                # A day earlier, to catch notifications from the same day, in NetBox's time zone
                self.assertEqual(since_date, datetime(2021, 1, 31, 12, tzinfo=UTC))
                self.assertEqual(since_date.tzinfo, timezone.get_default_timezone())

    @patch(f'{SOURCES_MODULE}.IMAP.receive_notifications', side_effect=RuntimeError('connection lost'))
    def test_get_notifications_error(self, mock_receive_notifications):
        job = MockedJob()
        with self.assertRaisesMessage(RuntimeError, 'connection lost'):
            get_notifications(job, NotificationSource.objects.all(), SINCE)
        job.logger.exception.assert_called_once_with(
            f'Issue fetching notifications from {SOURCE_IMAP["name"]}', extra={'object': self.notification_source}
        )

    @patch(f'{SOURCES_MODULE}.IMAP.close_session')
    @patch(f'{SOURCES_MODULE}.IMAP.open_session')
    def test_imap_test_authentication_ok(self, mock_open, mock_close):
        source = imap_source()
        res, message = source.test_authentication()
        self.assertEqual(res, True)
        self.assertEqual(message, 'Test OK')

    @patch(f'{SOURCES_MODULE}.IMAP.open_session')
    def test_imap_test_authentication_ko(self, mock_open):
        mock_open.side_effect = Exception('error message')
        source = imap_source()
        res, message = source.test_authentication()
        self.assertEqual(res, False)
        self.assertEqual(message, 'error message')

    def test_imap_init(self):
        """
        Test IMAP class init.
        """
        for name, url, account, password, imap_server, imap_port, exception in (
            ('name_1', 'url1', 'user_1', 'password_1', 'imap_server', 993, False),
            ('name_1', 'url1', None, 'password_1', 'imap_server', 993, True),
            ('name_1', 'url1', 'user_1', None, 'imap_server', 993, True),
            ('name_1', 'url1', 'user_1', 'password_1', None, 993, True),
            ('name_1', 'url1', 'user_1', 'password_1', 'imap_server', None, False),
        ):
            kwargs = {}
            if name:
                kwargs['name'] = name
            if url:
                kwargs['url'] = url
            if account:
                kwargs['account'] = account
            if password:
                kwargs['password'] = password
            if imap_server:
                kwargs['imap_server'] = imap_server
            if imap_port:
                kwargs['imap_port'] = imap_port

            with self.subTest(kwargs=kwargs):
                if exception:
                    with self.assertRaises(ValidationError):
                        IMAP(**kwargs)
                else:
                    IMAP(**kwargs)

    def test_open_session(self):
        """
        Test IMAP open_session logic.
        """
        imap_source_instance = imap_source()

        self.assertIsNone(imap_source_instance.session)
        with patch(f'{SOURCES_MODULE}.imaplib.IMAP4_SSL') as mock_session:
            # First time we don't have a session and when the session is created starts with NONAUTH state
            mock_session.return_value.state = 'NONAUTH'
            imap_source_instance.open_session()
            # We expect to have a session created and having called login once.
            self.assertIsNotNone(imap_source_instance.session)
            mock_session.return_value.login.assert_called_once_with('account', 'pass')

            # The server's certificate and hostname are verified (imaplib's default context checks neither)
            mock_session.assert_called_once_with('localhost', 993, ssl_context=ANY)
            ssl_context = mock_session.call_args.kwargs['ssl_context']
            self.assertEqual(ssl_context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(ssl_context.check_hostname)

        with patch(f'{SOURCES_MODULE}.imaplib.IMAP4_SSL') as mock_session:
            # Now, we assume the login succeeded, and state changed to SELECTED
            mock_session.return_value.state = 'SELECTED'
            imap_source_instance.open_session()
            # We expect not extra calls to login
            mock_session.return_value.login.assert_not_called()

    def test_close_session(self):
        """
        Test IMAP close_session logic.
        """
        imap_source_instance = imap_source()

        self.assertIsNone(imap_source_instance.session)
        with patch(f'{SOURCES_MODULE}.imaplib.IMAP4_SSL') as mock_session:
            imap_source_instance.close_session()
            mock_session.return_value.close.assert_not_called()
            mock_session.return_value.logout.assert_not_called()

        with patch(f'{SOURCES_MODULE}.imaplib.IMAP4_SSL') as mock_session:
            # Now we create the session manually
            imap_source_instance.session = mock_session()
            mock_session.return_value.state = 'SELECTED'
            imap_source_instance.close_session()
            mock_session.return_value.close.assert_called_once()
            mock_session.return_value.logout.assert_not_called()

        with patch(f'{SOURCES_MODULE}.imaplib.IMAP4_SSL') as mock_session:
            # Now we create the session manually
            imap_source_instance.session = mock_session()
            mock_session.return_value.state = 'AUTH'
            imap_source_instance.close_session()
            mock_session.return_value.close.assert_not_called()
            mock_session.return_value.logout.assert_called_once()

    @staticmethod
    def mock_session(messages):
        """
        An authenticated IMAP session whose searches find the given messages, keyed by message number.
        """
        session = MagicMock(state='SELECTED')
        session.search.return_value = ('OK', [b' '.join(messages)])
        session.fetch.side_effect = lambda msg_id, parts: ('OK', [(msg_id + b' (RFC822 {1})', messages[msg_id]), b')'])
        return session

    def test_receive_notifications(self):
        messages = {
            b'1': raw_email(subject='Maintenance 1'),
            # Skipped, and the run goes on (Nautobot raised a ValidationError)
            b'2': b'From: sender@example.com\r\nSubject: No date\r\n\r\nbody',
            b'3': raw_email(sender='Sender <SENDER@example.com>', subject='Maintenance 3'),
        }
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                source = imap_source(dry_run=dry_run, emails_to_fetch=['sender@example.com'])
                source.session = session = self.mock_session(messages)
                job = MockedJob()

                notifications = source.receive_notifications(job, datetime(2021, 1, 31, 12, tzinfo=UTC))

                # Fetching marks the messages as read, unless a dry run opens the Inbox read-only
                session.select.assert_called_once_with('Inbox', readonly=dry_run)
                session.search.assert_called_once_with(None, '(FROM "sender@example.com" SINCE "31-Jan-2021")')
                self.assertEqual(
                    [call.args for call in session.fetch.call_args_list],
                    [(b'1', '(RFC822)'), (b'2', '(RFC822)'), (b'3', '(RFC822)')],
                )
                self.assertEqual([n.subject for n in notifications], ['Maintenance 1', 'Maintenance 3'])
                self.assertEqual([n.msg_id for n in notifications], [b'1', b'3'])
                self.assertEqual(notifications[0].raw_payload, email.message_from_bytes(messages[b'1']).as_bytes())
                job.logger.error.assert_called_once_with(
                    'Skipping the email from sender@example.com because it has no Date header.', extra=ANY
                )
                session.close.assert_called_once()

    def test_receive_notifications_source_header(self):
        source = imap_source(source_header='X-Original-Sender', emails_to_fetch=['a@example.com', 'b@example.com'])
        source.session = session = self.mock_session({})

        self.assertEqual(source.receive_notifications(MockedJob()), [])
        self.assertEqual(
            [call.args for call in session.search.call_args_list],
            [
                (None, '(HEADER X-Original-Sender "a@example.com")'),
                (None, '(HEADER X-Original-Sender "b@example.com")'),
            ],
        )
