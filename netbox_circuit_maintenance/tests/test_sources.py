import base64
import contextlib
import email
import json
import os
import ssl
import tempfile
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from functools import cache
from unittest.mock import ANY, MagicMock, patch
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import ClientSecretCredential
from circuits.models import Provider
from core.models import ObjectChange
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import RequestFactory, TestCase
from django.utils import timezone
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from httplib2 import Response
from kiota_http.kiota_client_factory import KiotaClientFactory
from msgraph.generated.models.o_data_errors.o_data_error import ODataError
from netbox.context_managers import event_tracking
from pydantic import ValidationError
from users.models import User

from netbox_circuit_maintenance.constants import PROVIDER_EMAILS_FIELD
from netbox_circuit_maintenance.enum import MessageProcessingStatus
from netbox_circuit_maintenance.handle_notifications.sources import (
    IMAP,
    EmailSource,
    GmailAPI,
    GmailAPIOauth,
    GmailAPIServiceAccount,
    MaintenanceNotification,
    MicrosoftGraph,
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

# Each test class adds the credentials_file it writes (GmailSourcesMixin)
SOURCE_GMAIL_API_SERVICE_ACCOUNT = {
    'name': 'source gmail api service account',
    'account': 'me@example.com',
    'url': 'https://accounts.google.com/o/oauth2/auth',
}

SOURCE_GMAIL_API_OAUTH = {
    'name': 'source gmail api oauth',
    'account': 'me@example.com',
    'url': 'https://accounts.google.com/o/oauth2/auth',
}

SOURCE_MICROSOFT_GRAPH = {
    'name': 'source microsoft graph',
    'account': 'me@example.com',
    'url': 'https://graph.microsoft.com',
    'tenant_id': 'contoso.onmicrosoft.com',
    'client_id': 'client-id',
    'secret': 'supersecret',
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


def gmail_source(**kwargs):
    return GmailAPI(
        name='whatever',
        url='https://accounts.google.com/o/oauth2/auth',
        account='account',
        credentials_file='path_to_file',
        **kwargs,
    )


def http_error(status, message):
    """
    An HttpError as googleapiclient raises it for a JSON error response.
    """
    return HttpError(resp=Response({'status': status}), content=json.dumps({'error': {'message': message}}).encode())


def write_file(directory, filename, content):
    path = os.path.join(directory, filename)
    with open(path, 'wb' if isinstance(content, bytes) else 'w') as file:
        file.write(content if isinstance(content, (bytes, str)) else json.dumps(content))
    return path


@cache
def service_account_info():
    """
    A service account key file's content, with a real private key so google-auth can load it.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {
        'type': 'service_account',
        'project_id': 'netbox',
        'private_key_id': 'key-id',
        'private_key': private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
        'client_email': 'netbox@netbox.iam.gserviceaccount.com',
        'client_id': '1234567890',
        'token_uri': 'https://oauth2.googleapis.com/token',
    }


# A web application's OAuth client secrets file, as Google Cloud generates it
OAUTH_CLIENT_SECRETS = {
    'web': {
        'client_id': 'client-id',
        'client_secret': 'client-secret',
        'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
        'token_uri': 'https://oauth2.googleapis.com/token',
    }
}


class GmailSourcesMixin:
    """
    Writes a service account key file and an OAuth client secrets file into a temporary directory, and configures a
    Gmail source for each.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.directory = cls.enterClassContext(tempfile.TemporaryDirectory())
        cls.config_service_account = {
            **SOURCE_GMAIL_API_SERVICE_ACCOUNT,
            'credentials_file': write_file(cls.directory, 'service_account.json', service_account_info()),
        }
        cls.config_oauth = {
            **SOURCE_GMAIL_API_OAUTH,
            'credentials_file': write_file(cls.directory, 'oauth.json', OAUTH_CLIENT_SECRETS),
        }
        cls.enterClassContext(plugin_settings(notification_sources=[cls.config_service_account, cls.config_oauth]))


def graph_source(**kwargs):
    return MicrosoftGraph(
        name='whatever',
        url='https://graph.microsoft.com',
        account='me@example.com',
        tenant_id='contoso.onmicrosoft.com',
        client_id='client-id',
        client_secret='client-secret',
        **kwargs,
    )


def graph_error(status, code, message, headers=None):
    """
    An error response from Microsoft Graph.
    """
    return status, {'error': {'code': code, 'message': message}}, headers or {}


# The path of the account's mailbox in Microsoft Graph's v1.0 API
GRAPH_MAILBOX = '/v1.0/users/me@example.com'


class FakeGraph:
    """
    Stands in for Microsoft Graph, below msgraph-sdk and kiota's middleware: an httpx transport answers each request
    from `responses`, keyed by its decoded path and query, and records it. A list holds the responses to successive
    requests, and any other request gets Graph's 404. Getting a token from Microsoft Entra ID is mocked too.

    Each response is a status and a JSON body or bytes, and optionally headers.
    """

    def __init__(self, responses):
        self.responses = responses
        self.requests = []
        self.raw_paths = []
        self.clients = []

    def handle(self, request):
        url = unquote(request.url.raw_path.decode())
        self.requests.append((request.method, url, request.headers['Authorization']))
        self.raw_paths.append(request.url.raw_path.decode())
        response = self.responses.get(url, graph_error(404, 'ErrorItemNotFound', 'The specified object was not found.'))
        if isinstance(response, list):
            response = response.pop(0)
        status, body, *headers = response
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, headers=headers[0] if headers else None)
        return httpx.Response(status, json=body, headers=headers[0] if headers else None)

    def get_default_client(self):
        # The HTTP client that kiota creates, and wraps in its middleware, with this fake instead of the network
        self.clients.append(httpx.AsyncClient(transport=httpx.MockTransport(self.handle)))
        return self.clients[-1]

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(
            patch.object(KiotaClientFactory, 'get_default_client', side_effect=self.get_default_client)
        )
        self.credential = self.stack.enter_context(
            patch(f'{SOURCES_MODULE}.ClientSecretCredential', wraps=ClientSecretCredential)
        )
        self.get_token = self.stack.enter_context(
            patch.object(
                ClientSecretCredential, 'get_token', autospec=True, return_value=AccessToken('access-token', 2**32)
            )
        )
        self.close = self.stack.enter_context(
            patch.object(ClientSecretCredential, 'close', autospec=True, side_effect=ClientSecretCredential.close)
        )
        return self

    def __exit__(self, *exc_info):
        return self.stack.__exit__(*exc_info)

    def closed(self):
        """
        Return whether each HTTP client was closed, and how many times a credential was.
        """
        return [client.is_closed for client in self.clients], self.close.await_count


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
        # EWS is replaced by Microsoft Graph, and isn't coming back; HTTPS is only for Gmail and Microsoft Graph's
        # global service
        for url, scheme in (
            ('ftp://example.com', 'ftp'),
            ('ews://example.com', 'ews'),
            ('https://example.com/o/oauth2/auth', 'https'),
            ('https://graph.microsoft.us', 'https'),
        ):
            with (
                self.subTest(url=url),
                plugin_settings(notification_sources=[{**SOURCE_IMAP, 'url': url}]),
                self.assertRaisesMessage(
                    ValueError,
                    f'Scheme {scheme} not supported as Notification Source '
                    '(only IMAP or HTTPS to accounts.google.com or graph.microsoft.com).',
                ),
            ):
                Source.init(name=SOURCE_IMAP['name'])

    def test_source_factory_url_malformed(self):
        with (
            plugin_settings(notification_sources=[{**SOURCE_IMAP, 'url': 'wrong url'}]),
            self.assertRaisesMessage(
                ValueError,
                'Scheme  not supported as Notification Source '
                '(only IMAP or HTTPS to accounts.google.com or graph.microsoft.com).',
            ),
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
            ('"Doe, John" <John.Doe@Example.com>', 'john.doe@example.com'),
            ('noc@example.com (NOC)', 'noc@example.com'),
            # Every RFC 5322 address character (Nautobot's pattern cut these at the first "+" or "'")
            ('noc+maintenance@example.com', 'noc+maintenance@example.com'),
            ('NOC <noc+maintenance@example.com>', 'noc+maintenance@example.com'),
            ("o'brien@example.com", "o'brien@example.com"),
            ("a!#$%&'*+/=?^_`{|}~-z@example.com", "a!#$%&'*+/=?^_`{|}~-z@example.com"),
            ('', ''),
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


class GmailAPISourceTestCase(GmailSourcesMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.provider = Provider.objects.create(
            name='ntt', slug='ntt', custom_field_data={PROVIDER_EMAILS_FIELD: 'sender@example.com'}
        )
        cls.notification_source = NotificationSource.objects.create(name=SOURCE_GMAIL_API_SERVICE_ACCOUNT['name'])
        cls.notification_source.providers.add(cls.provider)

    def setUp(self):
        self.source = Source.init(name=SOURCE_GMAIL_API_SERVICE_ACCOUNT['name'])
        self.job = MockedJob()

    def test_source_factory_service_account(self):
        """
        Validate Factory pattern for Source class.
        """
        source_instance = Source.init(name=SOURCE_GMAIL_API_SERVICE_ACCOUNT['name'])
        self.assertIsInstance(source_instance, GmailAPIServiceAccount)
        self.assertEqual(source_instance.name, SOURCE_GMAIL_API_SERVICE_ACCOUNT['name'])
        self.assertEqual(source_instance.url, SOURCE_GMAIL_API_SERVICE_ACCOUNT['url'])
        self.assertEqual(source_instance.account, SOURCE_GMAIL_API_SERVICE_ACCOUNT['account'])
        self.assertEqual(source_instance.credentials_file, self.config_service_account['credentials_file'])
        self.assertEqual(source_instance.get_account_id(), SOURCE_GMAIL_API_SERVICE_ACCOUNT['account'])
        self.assertEqual(
            (
                source_instance.source_header,
                source_instance.limit_emails_with_not_header_from,
                source_instance.extra_scopes,
                source_instance.labels,
                source_instance.dry_run,
            ),
            ('From', [], [], {}, False),
        )

    def test_source_factory_oauth(self):
        """
        Validate Factory pattern for Source class.
        """
        source_instance = Source.init(name=SOURCE_GMAIL_API_OAUTH['name'])
        self.assertIsInstance(source_instance, GmailAPIOauth)
        self.assertEqual(source_instance.name, SOURCE_GMAIL_API_OAUTH['name'])
        self.assertEqual(source_instance.url, SOURCE_GMAIL_API_OAUTH['url'])
        self.assertEqual(source_instance.account, SOURCE_GMAIL_API_OAUTH['account'])
        self.assertEqual(source_instance.credentials_file, self.config_oauth['credentials_file'])

    def test_source_factory_options(self):
        options = {
            'source_header': 'X-Original-Sender',
            'limit_emails_with_not_header_from': ['mailinglist@example.com'],
            'extra_scopes': ['https://www.googleapis.com/auth/gmail.modify'],
            'labels': {'parsed': 'Label_1', 'parsing-failed': 'Label_2'},
        }
        with plugin_settings(notification_sources=[{**self.config_service_account, **options}]):
            source_instance = Source.init(name=SOURCE_GMAIL_API_SERVICE_ACCOUNT['name'], dry_run=True)
        for option, value in options.items():
            self.assertEqual(getattr(source_instance, option), value)
        self.assertTrue(source_instance.dry_run)

    def test_source_factory_url(self):
        # The host is compared as a hostname (Nautobot compared the netloc's text, so upper case wasn't Gmail)
        for url in (
            'https://ACCOUNTS.Google.com/o/oauth2/auth',
            'https://accounts.google.com:443/o/oauth2/auth',
            'HTTPS://accounts.google.com',
        ):
            with (
                self.subTest(url=url),
                plugin_settings(notification_sources=[{**self.config_service_account, 'url': url}]),
            ):
                self.assertIsInstance(
                    Source.init(name=SOURCE_GMAIL_API_SERVICE_ACCOUNT['name']), GmailAPIServiceAccount
                )

    def test_source_factory_no_account(self):
        """
        Validate Factory pattern Gmail API without account settings.
        """
        with (
            plugin_settings(notification_sources=[without(self.config_service_account, 'account')]),
            self.assertRaises(ValidationError),
        ):
            Source.init(name=SOURCE_GMAIL_API_SERVICE_ACCOUNT['name'])

    def test_source_factory_no_credentials(self):
        """
        Validate Factory pattern Gmail API without credentials_file.
        """
        with (
            plugin_settings(notification_sources=[without(self.config_service_account, 'credentials_file')]),
            self.assertRaisesMessage(
                ValueError,
                f'Credentials_file for {SOURCE_GMAIL_API_SERVICE_ACCOUNT["name"]} not found in PLUGINS_CONFIG.',
            ),
        ):
            Source.init(name=SOURCE_GMAIL_API_SERVICE_ACCOUNT['name'])

    def test_source_factory_credentials_file_non_existent(self):
        """
        Validate Factory pattern Gmail API with credentials_file unexistent.
        """
        with (
            plugin_settings(notification_sources=[{**self.config_service_account, 'credentials_file': 'fake'}]),
            self.assertRaisesMessage(
                ValueError, f'Credentials_file fake for {SOURCE_GMAIL_API_SERVICE_ACCOUNT["name"]} is not available.'
            ),
        ):
            Source.init(name=SOURCE_GMAIL_API_SERVICE_ACCOUNT['name'])

    def test_source_factory_credentials_file_unsupported(self):
        # Every problem is a ValueError, which the source's page and get_notifications() report (Nautobot raised
        # NotImplementedError, AttributeError or OSError, which neither caught)
        name = SOURCE_GMAIL_API_SERVICE_ACCOUNT['name']
        for filename, content, message in (
            # A desktop application's OAuth client
            ('installed.json', {'installed': {}}, "doesn't contain any supported credentials."),
            ('list.json', [{'type': 'service_account'}], "doesn't contain any supported credentials."),
            ('text.json', 'not JSON', "can't be read: Expecting value: line 1 column 1 (char 0)"),
            ('binary.json', b'\xff', "can't be read: 'utf-8' codec can't decode byte 0xff in position 0"),
        ):
            path = write_file(self.directory, filename, content)
            if message.startswith("doesn't"):
                message = f'File {path} {message}'
            else:
                message = f'Credentials_file {path} for {name} {message}'
            with (
                self.subTest(filename=filename),
                plugin_settings(notification_sources=[{**self.config_service_account, 'credentials_file': path}]),
                self.assertRaisesMessage(ValueError, message),
            ):
                Source.init(name=name)

        with (
            patch(f'{SOURCES_MODULE}.open', side_effect=PermissionError(13, 'Permission denied'), create=True),
            self.assertRaisesMessage(
                ValueError,
                f"Credentials_file {self.config_service_account['credentials_file']} for {name} can't be read: "
                '[Errno 13] Permission denied',
            ),
        ):
            Source.init(name=name)

    def test_get_notifications_without_providers(self):
        """
        Test get_notifications when there are no Providers defined.
        """
        self.notification_source.providers.set([])

        res = get_notifications(self.job, NotificationSource.objects.all(), SINCE)
        self.assertEqual([], res)
        source_name = SOURCE_GMAIL_API_SERVICE_ACCOUNT['name']
        self.job.logger.warning.assert_called_with(
            f"Skipping source '{source_name}' because no providers were defined.", extra=ANY
        )

    @patch(f'{SOURCES_MODULE}.GmailAPIServiceAccount.receive_notifications')
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

        res = get_notifications(self.job, NotificationSource.objects.all(), SINCE)
        self.assertEqual([], res)

        self.job.logger.warning.assert_called_with(
            f'Skipping {new_provider.name} because these providers have no email configured.', extra=ANY
        )
        self.job.logger.info.assert_called_with(
            f'No notifications received for {original_provider}, {new_provider} since {SINCE_TXT} '
            f'from {notification_source.name}',
            extra=ANY,
        )

    def test_get_notifications_no_account(self):
        """
        Test get_notifications without account.
        """
        with plugin_settings(notification_sources=[without(self.config_service_account, 'account')]):
            get_notifications(self.job, NotificationSource.objects.all(), SINCE)

        assert_called_with_substring(
            self.job.logger.warning,
            f'Notification Source {SOURCE_GMAIL_API_SERVICE_ACCOUNT["name"]} is not matching class expectations: '
            '1 validation error for GmailAPIServiceAccount\naccount',
        )

    def test_get_notifications_unsupported_credentials(self):
        # The source is skipped, where Nautobot's NotImplementedError stopped the whole run
        path = write_file(self.directory, 'unsupported.json', {'installed': {}})
        with plugin_settings(notification_sources=[{**self.config_service_account, 'credentials_file': path}]):
            self.assertEqual(get_notifications(self.job, NotificationSource.objects.all(), SINCE), [])
        self.job.logger.warning.assert_called_once_with(
            f'Skipping notification source {SOURCE_GMAIL_API_SERVICE_ACCOUNT["name"]}: '
            f"File {path} doesn't contain any supported credentials.",
            extra={'object': self.notification_source},
            exc_info=True,
        )

    @patch(f'{SOURCES_MODULE}.GmailAPIServiceAccount.receive_notifications')
    def test_get_notifications_unauthorized(self, mock_receive_notifications):
        # An OAuth source without a token is skipped, and the run goes on with the next source (Nautobot stopped)
        oauth_source = NotificationSource.objects.create(name=SOURCE_GMAIL_API_OAUTH['name'])
        oauth_source.providers.add(self.provider)
        mock_receive_notifications.return_value = [build_notification(self.source)]

        sources = NotificationSource.objects.all()
        self.assertEqual(list(sources), [oauth_source, self.notification_source])
        res = get_notifications(self.job, sources, SINCE)

        self.assertEqual(len(res), 1)
        self.job.logger.warning.assert_called_once_with(
            f'Skipping notification source {SOURCE_GMAIL_API_OAUTH["name"]}: it needs Google OAuth authorization. '
            'Validate the source to authorize it.',
            extra={'object': oauth_source},
        )

    @patch(f'{SOURCES_MODULE}.GmailAPI.receive_notifications')
    def test_get_notifications(self, mock_receive_notifications):
        """
        Test get_notifications.
        """
        notification = build_notification(self.source)

        mock_receive_notifications.return_value = [notification]

        res = get_notifications(self.job, NotificationSource.objects.all(), SINCE)

        self.assertEqual(1, len(res))
        self.job.logger.warning.assert_not_called()

    @patch(f'{SOURCES_MODULE}.GmailAPI.receive_notifications')
    def test_get_notifications_multiple(self, mock_receive_notifications):
        """
        Test get_notifications multiple.
        """
        notification = build_notification(self.source)

        mock_receive_notifications.return_value = [notification, notification]

        res = get_notifications(self.job, NotificationSource.objects.all(), SINCE)

        self.assertEqual(2, len(res))
        self.job.logger.warning.assert_not_called()

    def test_gmail_api_service_account_init(self):
        """
        Test Gmail API class init.
        """
        for name, url, account, credentials_file, exception in (
            ('name_1', 'url1', 'user_1', 'credentials_file', False),
            ('name_1', 'url1', None, 'credentials_file', True),
            ('name_1', 'url1', 'user_1', None, True),
        ):
            kwargs = {}
            if name:
                kwargs['name'] = name
            if url:
                kwargs['url'] = url
            if account:
                kwargs['account'] = account
            if credentials_file:
                kwargs['credentials_file'] = credentials_file

            with self.subTest(kwargs=kwargs):
                if exception:
                    with self.assertRaises(ValidationError):
                        GmailAPI(**kwargs)
                else:
                    GmailAPI(**kwargs)

    @patch(f'{SOURCES_MODULE}.GmailAPI.load_credentials')
    def test_gmail_api_test_authentication_ok(self, mock_credentials):
        source = gmail_source()
        res, message = source.test_authentication()
        self.assertEqual(res, True)
        self.assertEqual(message, 'Test OK')
        mock_credentials.assert_called_once_with(force_refresh=True)

    @patch(f'{SOURCES_MODULE}.GmailAPI.load_credentials')
    def test_gmail_api_test_authentication_ko(self, mock_credentials):
        mock_credentials.side_effect = Exception('error message')
        source = gmail_source()
        res, message = source.test_authentication()
        self.assertEqual(res, False)
        self.assertEqual(message, 'error message')

    def test_gmail_api_restricted_credentials(self):
        credentials = Credentials(token='access-token-value', refresh_token='refresh-token-value')
        source = gmail_source(credentials=credentials)
        self.assertNotIn(' credentials=', repr(source))
        self.assertNotIn('token-value', repr(source))
        self.assertIn('account', repr(source))

    def test_get_search_criteria(self):
        """
        Test the get_search_criteria method.
        """
        for since_timestamp, source_header, emails_to_fetch, limit_emails_with_not_header_from, result in (
            (
                None,
                'From',
                ['email1@example.com', 'email2@example.com'],
                [],
                ' {from:email1@example.com from:email2@example.com}',
            ),
            (
                datetime(2021, 9, 20, 17, 49, 50, 0, tzinfo=UTC),
                'From',
                ['email1@example.com', 'email2@example.com'],
                [],
                'after:2021/09/20 {from:email1@example.com from:email2@example.com}',
            ),
            (None, 'X-Original-Sender', ['email1@example.com', 'email2@example.com'], [], ''),
            (
                None,
                'X-Original-Sender',
                ['email1@example.com', 'email2@example.com'],
                ['mailinglist1@example.com', 'mailinglist2@example.com'],
                ' {from:mailinglist1@example.com from:mailinglist2@example.com}',
            ),
            (
                datetime(2021, 9, 20, 17, 49, 50, 0, tzinfo=UTC),
                'X-Original-Sender',
                ['email1@example.com', 'email2@example.com'],
                ['mailinglist1@example.com', 'mailinglist2@example.com'],
                'after:2021/09/20 {from:mailinglist1@example.com from:mailinglist2@example.com}',
            ),
        ):
            with self.subTest(since_timestamp=since_timestamp, source_header=source_header, result=result):
                source = gmail_source(
                    source_header=source_header, limit_emails_with_not_header_from=limit_emails_with_not_header_from
                )

                source.emails_to_fetch = emails_to_fetch

                self.assertEqual(result, source._get_search_criteria(since_timestamp))

    @patch('time.sleep', return_value=None)
    def test_execute_retry_logic(self, mock_sleep):
        """
        Test the googleapi execute retry logic.
        """
        mock_request = MagicMock()

        # Mock the execute method to raise an HttpError the first time and succeed the second time
        mock_request.execute.side_effect = [
            HttpError(resp=Response({'status': 503}), content=b'Service Unavailable'),  # First attempt fails
            'Success!',  # Second attempt succeeds
        ]

        result = self.source._execute_with_retries(mock_request, self.job)
        self.assertEqual(result, 'Success!')
        self.assertEqual(mock_request.execute.call_count, 2)
        mock_sleep.assert_called_once_with(1)  # Initial delay of 1 second

    @patch('time.sleep', return_value=None)
    def test_execute_retry_logic_errors(self, mock_sleep):
        # An error that isn't retryable is raised straight away
        error = http_error(403, 'Forbidden')
        mock_request = MagicMock()
        mock_request.execute.side_effect = error
        with self.assertRaises(HttpError) as context:
            self.source._execute_with_retries(mock_request, self.job)
        self.assertIs(context.exception, error)
        self.assertEqual(mock_request.execute.call_count, 1)
        mock_sleep.assert_not_called()
        self.job.logger.warning.assert_not_called()

        # The last attempt's error is raised without another wait (Nautobot slept again, then raised TypeError,
        # building an HttpError from the str reason)
        errors = [http_error(status, 'Backend Error') for status in (500, 502, 503, 504, 503)]
        mock_request = MagicMock()
        mock_request.execute.side_effect = errors
        with self.assertRaises(HttpError) as context:
            self.source._execute_with_retries(mock_request, self.job)
        self.assertIs(context.exception, errors[-1])
        self.assertEqual(mock_request.execute.call_count, 5)
        self.assertEqual([sleep.args for sleep in mock_sleep.call_args_list], [(1,), (2,), (4,), (8,)])
        self.assertEqual(self.job.logger.warning.call_count, 4)
        self.assertTrue(
            self.job.logger.warning.call_args_list[0].args[0].startswith('Google API attempt 1 failed: <HttpError 500')
        )
        self.assertTrue(self.job.logger.warning.call_args.args[0].endswith('Retrying in 8 seconds...'))

    def test_build_service(self):
        # A Gmail API client for the source's credentials, built from the discovery document bundled with
        # google-api-python-client, without a request to Google
        source = gmail_source(credentials=Credentials(token='access-token'))
        with patch(f'{SOURCES_MODULE}.build', wraps=build) as mock_build:
            source.build_service()
        mock_build.assert_called_once_with('gmail', 'v1', credentials=source.credentials)
        self.assertIsInstance(source.service, Resource)
        self.assertTrue(
            source.service.users()
            .messages()
            .list(userId='account', q='')
            .uri.startswith('https://gmail.googleapis.com/gmail/v1/users/account/messages')
        )

    @staticmethod
    def mock_service(messages, pages):
        """
        A Gmail API client whose message list returns the given pages of message IDs, and whose messages are the given
        raw emails, keyed by ID.
        """
        service = MagicMock()
        api = service.users.return_value.messages.return_value
        requests = [MagicMock() for _ in pages]
        for request, page in zip(requests, pages, strict=True):
            request.execute.return_value = {'messages': [{'id': msg_id} for msg_id in page]} if page else {}
        api.list.return_value = requests[0]
        api.list_next.side_effect = [*requests[1:], None]
        api.get.side_effect = lambda userId, id, format: MagicMock(
            **{'execute.return_value': {'id': id, 'raw': base64.urlsafe_b64encode(messages[id]).decode()}}
        )
        return service

    def test_receive_notifications(self):
        messages = {
            # Some 8-bit body bytes, whose base64url encoding has "-" and "_"
            'msg-1': raw_email(subject='Maintenance 1') + b' ' + b'\xfb\xef\xbe\xff' * 3,
            # Skipped and labelled
            'msg-2': raw_email(sender='noc@unknown.example', subject='Unknown'),
            'msg-3': raw_email(sender='Sender <SENDER@example.com>', subject='Maintenance 3'),
            'msg-4': b'From: sender@example.com\r\nSubject: No date\r\n\r\nbody',
        }
        self.assertTrue({'-', '_'} <= set(base64.urlsafe_b64encode(messages['msg-1']).decode()))
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                source = gmail_source(
                    dry_run=dry_run,
                    emails_to_fetch=['sender@example.com'],
                    labels={'unknown-provider': 'Label_1', 'parsed': 'Label_2', 'parsing-failed': 'Label_3'},
                )
                service = self.mock_service(messages, [['msg-1', 'msg-2'], ['msg-3', 'msg-4']])
                api = service.users.return_value.messages.return_value
                job = MockedJob()

                with (
                    patch(f'{SOURCES_MODULE}.GmailAPI.load_credentials') as load_credentials,
                    patch(f'{SOURCES_MODULE}.build', return_value=service) as mock_build,
                ):
                    notifications = source.receive_notifications(job, datetime(2021, 1, 31, 12, tzinfo=UTC))

                load_credentials.assert_called_once_with()
                mock_build.assert_called_once_with('gmail', 'v1', credentials=source.credentials)
                api.list.assert_called_once_with(userId='account', q='after:2021/01/31 {from:sender@example.com}')
                self.assertEqual(
                    [get.kwargs for get in api.get.call_args_list],
                    [{'userId': 'account', 'id': msg_id, 'format': 'raw'} for msg_id in messages],
                )
                self.assertEqual([n.subject for n in notifications], ['Maintenance 1', 'Maintenance 3'])
                self.assertEqual([n.msg_id for n in notifications], [b'msg-1', b'msg-3'])
                self.assertEqual([n.sender for n in notifications], ['sender@example.com', 'sender@example.com'])
                self.assertEqual(notifications[0].raw_payload, email.message_from_bytes(messages['msg-1']).as_bytes())
                job.logger.error.assert_called_once_with(
                    'Skipping the email from sender@example.com because it has no Date header.', extra=ANY
                )
                # The skipped messages are labelled, except in a dry run
                self.assertEqual(
                    [modify.kwargs for modify in api.modify.call_args_list],
                    []
                    if dry_run
                    else [
                        {'userId': 'account', 'id': 'msg-2', 'body': {'addLabelIds': ['Label_1']}},
                        {'userId': 'account', 'id': 'msg-4', 'body': {'addLabelIds': ['Label_3']}},
                    ],
                )
                service.close.assert_called_once_with()
                # The job log gets counts, not the emails
                self.assertEqual(
                    [debug.args for debug in job.logger.debug.call_args_list],
                    [
                        (
                            'Fetched 4 emails from whatever source using search pattern: '
                            'after:2021/01/31 {from:sender@example.com}.',
                        ),
                        ('Raw notifications created 2 from whatever.',),
                    ],
                )

    def test_tag_message(self):
        source = gmail_source(labels={'parsed': 'Label_2'})
        source.service = MagicMock()
        api = source.service.users.return_value.messages.return_value
        job = MockedJob()

        # The handler passes the notification's msg_id, which MaintenanceNotification made bytes
        source.tag_message(job, b'msg-1', MessageProcessingStatus.PARSED)
        api.modify.assert_called_once_with(userId='account', id='msg-1', body={'addLabelIds': ['Label_2']})
        api.modify.return_value.execute.assert_called_once_with()

        # Tags without a configured label are skipped, and so is everything in a dry run
        api.modify.reset_mock()
        source.tag_message(job, 'msg-1', MessageProcessingStatus.IGNORED)
        source.dry_run = True
        source.tag_message(job, 'msg-1', MessageProcessingStatus.PARSED)
        api.modify.assert_not_called()

        # A failure is logged with its reason, which NetBox's job log would otherwise drop with the traceback
        source.dry_run = False
        api.modify.return_value.execute.side_effect = http_error(403, 'Insufficient Permission')
        source.tag_message(job, 'msg-1', MessageProcessingStatus.PARSED)
        job.logger.warning.assert_called_once_with(ANY, exc_info=True)
        message = job.logger.warning.call_args.args[0]
        self.assertTrue(message.startswith("Error in applying tag 'parsed' (Label_2) to message msg-1: <HttpError 403"))
        self.assertIn('Insufficient Permission', message)


class GmailAPIOauthTestCase(GmailSourcesMixin, TestCase):
    # google-auth keeps expiry as a naive UTC datetime
    VALID_EXPIRY = datetime(2030, 1, 1)  # noqa: DTZ001
    EXPIRED_EXPIRY = datetime(2020, 1, 1)  # noqa: DTZ001
    REFRESHED_EXPIRY = datetime(2031, 1, 1)  # noqa: DTZ001

    @classmethod
    def setUpTestData(cls):
        cls.notification_source = NotificationSource.objects.create(name=SOURCE_GMAIL_API_OAUTH['name'])

    def setUp(self):
        self.source = Source.init(name=SOURCE_GMAIL_API_OAUTH['name'])

    def store_token(self, expiry, **kwargs):
        """
        Store a token as the OAuth callback will: google.oauth2.credentials.Credentials.to_json().
        """
        credentials = {
            'token': 'access-token',
            'refresh_token': 'refresh-token',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'client_id': 'client-id',
            'client_secret': 'client-secret',
            'scopes': ['https://www.googleapis.com/auth/gmail.readonly'],
            'expiry': expiry,
        }
        token = json.loads(Credentials(**{**credentials, **kwargs}).to_json())
        NotificationSource.objects.filter(pk=self.notification_source.pk).update(_token=token)
        return token

    def refresh(self, credentials, request):
        # Stands in for google-auth's request to Google's token endpoint
        self.assertIsInstance(request, Request)
        credentials.token = 'new-access-token'
        credentials.expiry = self.REFRESHED_EXPIRY

    def test_load_credentials_unauthorized(self):
        # No token yet, one that was discarded, or one google-auth can't use
        for token in (None, {}, {'refresh_token': 'refresh-token'}):
            NotificationSource.objects.filter(pk=self.notification_source.pk).update(_token=token)
            with (
                self.subTest(token=token),
                self.assertLogs(SOURCES_MODULE, 'DEBUG') as logs,
                self.assertRaises(RedirectAuthorize) as context,
            ):
                self.source.load_credentials()
            self.assertEqual(
                (context.exception.url_name, context.exception.source_name),
                ('google_authorize', SOURCE_GMAIL_API_OAUTH['name']),
            )
            self.assertIsNone(self.source.credentials)
            self.assertEqual(logs.records[0].getMessage(), 'Google OAuth Token has not been initialized yet.')

        # Validating starts the authorization
        with self.assertRaises(RedirectAuthorize):
            self.source.test_authentication()

    @patch.object(Credentials, 'refresh', autospec=True)
    def test_load_credentials_valid(self, refresh):
        self.store_token(self.VALID_EXPIRY)

        # Read from the source, and not saved again
        with self.assertNumQueries(1):
            self.source.load_credentials()
        refresh.assert_not_called()
        self.assertEqual(
            (
                self.source.credentials.token,
                self.source.credentials.refresh_token,
                self.source.credentials.client_secret,
            ),
            ('access-token', 'refresh-token', 'client-secret'),
        )

    def test_load_credentials_expired(self):
        self.store_token(self.EXPIRED_EXPIRY)
        last_updated = NotificationSource.objects.get(pk=self.notification_source.pk).last_updated
        request = RequestFactory().get('/')
        request.id = uuid.uuid4()
        request.user = User.objects.create_user(username='user1')

        with (
            patch.object(Credentials, 'refresh', autospec=True, side_effect=self.refresh) as refresh,
            patch('extras.events.process_event_queue') as process_event_queue,
            event_tracking(request),
        ):
            self.source.load_credentials()

        refresh.assert_called_once_with(self.source.credentials, ANY)
        source = NotificationSource.objects.get(pk=self.notification_source.pk)
        self.assertEqual(
            (source._token['token'], source._token['refresh_token'], source._token['expiry']),
            ('new-access-token', 'refresh-token', '2031-01-01T00:00:00Z'),
        )
        # A refresh is bookkeeping: no change record, event or new last_updated, even in a request
        self.assertEqual(source.last_updated, last_updated)
        self.assertFalse(
            ObjectChange.objects.filter(
                changed_object_type__model='notificationsource', changed_object_id=source.pk
            ).exists()
        )
        process_event_queue.assert_not_called()

    def test_load_credentials_force_refresh(self):
        self.store_token(self.VALID_EXPIRY)

        with patch.object(Credentials, 'refresh', autospec=True, side_effect=self.refresh) as refresh:
            self.assertEqual(self.source.test_authentication(), (True, 'Test OK'))
        refresh.assert_called_once()
        self.assertEqual(
            NotificationSource.objects.get(pk=self.notification_source.pk)._token['token'], 'new-access-token'
        )

    @staticmethod
    def refresh_error():
        # As google-auth raises it for an error from Google's token endpoint (google/oauth2/_client.py)
        return RefreshError(
            'invalid_grant: Token has been expired or revoked.',
            {'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'},
            retryable=False,
        )

    def test_load_credentials_refresh_error(self):
        # A revoked or expired refresh token is discarded, so the next attempt starts the authorization again
        self.store_token(self.EXPIRED_EXPIRY)
        last_updated = NotificationSource.objects.get(pk=self.notification_source.pk).last_updated
        request = RequestFactory().get('/')
        request.id = uuid.uuid4()
        request.user = User.objects.create_user(username='user1')

        with (
            patch.object(Credentials, 'refresh', autospec=True, side_effect=self.refresh_error()),
            self.assertRaises(RefreshError),
            event_tracking(request),
        ):
            self.source.load_credentials()
        source = NotificationSource.objects.get(pk=self.notification_source.pk)
        self.assertIsNone(source._token)
        # Discarding is bookkeeping too
        self.assertEqual(source.last_updated, last_updated)
        self.assertFalse(
            ObjectChange.objects.filter(
                changed_object_type__model='notificationsource', changed_object_id=source.pk
            ).exists()
        )
        with self.assertRaises(RedirectAuthorize):
            self.source.load_credentials()

        # Validating reports the error, without the response data google-auth adds (Nautobot showed both as a tuple)
        self.store_token(self.VALID_EXPIRY)
        with patch.object(Credentials, 'refresh', autospec=True, side_effect=self.refresh_error()):
            self.assertEqual(
                self.source.test_authentication(), (False, 'invalid_grant: Token has been expired or revoked.')
            )
        self.assertIsNone(NotificationSource.objects.get(pk=self.notification_source.pk)._token)

    def test_get_notifications_refresh_error(self):
        # A failed refresh still stops the run, as other fetch failures do. The token is discarded, so later runs
        # skip the source until it's authorized again
        provider = Provider.objects.create(
            name='ntt', slug='ntt', custom_field_data={PROVIDER_EMAILS_FIELD: 'sender@example.com'}
        )
        self.notification_source.providers.add(provider)
        self.store_token(self.EXPIRED_EXPIRY)

        job = MockedJob()
        with (
            patch.object(Credentials, 'refresh', autospec=True, side_effect=self.refresh_error()),
            self.assertRaises(RefreshError),
        ):
            get_notifications(job, NotificationSource.objects.all(), SINCE)
        job.logger.exception.assert_called_once_with(
            f'Issue fetching notifications from {SOURCE_GMAIL_API_OAUTH["name"]}', extra=ANY
        )

        job = MockedJob()
        self.assertEqual(get_notifications(job, NotificationSource.objects.all(), SINCE), [])
        job.logger.warning.assert_called_once_with(
            f'Skipping notification source {SOURCE_GMAIL_API_OAUTH["name"]}: it needs Google OAuth authorization. '
            'Validate the source to authorize it.',
            extra={'object': self.notification_source},
        )

    def test_receive_notifications_unauthorized(self):
        # Raised before any request to Google
        with patch(f'{SOURCES_MODULE}.build') as mock_build, self.assertRaises(RedirectAuthorize):
            self.source.receive_notifications(MockedJob())
        mock_build.assert_not_called()


class GmailAPIServiceAccountTestCase(GmailSourcesMixin, TestCase):
    def test_load_credentials(self):
        modify_scope = 'https://www.googleapis.com/auth/gmail.modify'
        with plugin_settings(notification_sources=[{**self.config_service_account, 'extra_scopes': [modify_scope]}]):
            source = Source.init(name=SOURCE_GMAIL_API_SERVICE_ACCOUNT['name'])

        # Stands in for google-auth's request to Google's token endpoint
        with patch.object(service_account.Credentials, 'refresh', autospec=True) as refresh:
            source.load_credentials()
            credentials = source.credentials
            self.assertIsInstance(credentials, service_account.Credentials)
            self.assertEqual(credentials.service_account_email, service_account_info()['client_email'])
            self.assertEqual(credentials.scopes, ['https://www.googleapis.com/auth/gmail.readonly', modify_scope])
            # The service account acts as the source's account (domain-wide delegation)
            self.assertEqual(credentials._subject, SOURCE_GMAIL_API_SERVICE_ACCOUNT['account'])
            refresh.assert_called_once_with(credentials, ANY)
            self.assertIsInstance(refresh.call_args.args[1], Request)

            # Loaded once, unless forced
            source.load_credentials()
            self.assertIs(source.credentials, credentials)
            self.assertEqual(source.test_authentication(), (True, 'Test OK'))
            self.assertIsNot(source.credentials, credentials)
            self.assertEqual(refresh.call_count, 2)

    def test_test_authentication_invalid_key_file(self):
        path = write_file(self.directory, 'incomplete.json', {'type': 'service_account'})
        with plugin_settings(notification_sources=[{**self.config_service_account, 'credentials_file': path}]):
            source = Source.init(name=SOURCE_GMAIL_API_SERVICE_ACCOUNT['name'])

        # The init only checks the file's type; google-auth reports what's missing, in set order
        is_authenticated, message = source.test_authentication()
        self.assertFalse(is_authenticated)
        self.assertTrue(message.startswith('Service account info was not in the expected format, missing fields '))
        self.assertIn('client_email', message)


# Nautobot's TestExchangeWebService covered the EWS source that this one replaces
@plugin_settings(notification_sources=[SOURCE_MICROSOFT_GRAPH])
class MicrosoftGraphTestCase(TestCase):
    INBOX = f'{GRAPH_MAILBOX}/mailFolders/inbox?$select=id'
    FOLDER = f"{GRAPH_MAILBOX}/mailFolders/inbox/childFolders?$filter=displayName eq 'Circuit Notifications'&$select=id"
    MESSAGES = f'{GRAPH_MAILBOX}/mailFolders/folder-id/messages'
    # The SINCE that get_notifications() is given, less the day it adds
    RECEIVED = 'receivedDateTime ge 2021-01-31T12:00:00Z'

    @classmethod
    def setUpTestData(cls):
        cls.provider = Provider.objects.create(
            name='ntt', slug='ntt', custom_field_data={PROVIDER_EMAILS_FIELD: 'sender@example.com'}
        )
        cls.notification_source = NotificationSource.objects.create(name=SOURCE_MICROSOFT_GRAPH['name'])
        cls.notification_source.providers.add(cls.provider)

    def test_source_factory(self):
        source = Source.init(name=SOURCE_MICROSOFT_GRAPH['name'])
        self.assertIsInstance(source, MicrosoftGraph)
        self.assertEqual(
            (source.name, source.url, source.account, source.tenant_id, source.client_id, source.client_secret),
            (
                SOURCE_MICROSOFT_GRAPH['name'],
                'https://graph.microsoft.com',
                'me@example.com',
                'contoso.onmicrosoft.com',
                'client-id',
                'supersecret',
            ),
        )
        self.assertEqual((source.folder, source.source_header, source.dry_run), (None, 'From', False))
        self.assertEqual(source.get_account_id(), 'me@example.com')
        self.assertNotIn('supersecret', repr(source))
        self.assertIn('client-id', repr(source))

    def test_source_factory_options(self):
        with plugin_settings(notification_sources=[{**SOURCE_MICROSOFT_GRAPH, 'folder': 'Circuit Notifications'}]):
            source = Source.init(name=SOURCE_MICROSOFT_GRAPH['name'], dry_run=True)
        self.assertEqual((source.folder, source.dry_run), ('Circuit Notifications', True))

    def test_source_factory_url(self):
        # The host is compared as a hostname, as for Gmail; the path doesn't matter
        for url in (
            'https://GRAPH.microsoft.com',
            'https://graph.microsoft.com:443/v1.0',
            'HTTPS://graph.microsoft.com/',
        ):
            with (
                self.subTest(url=url),
                plugin_settings(notification_sources=[{**SOURCE_MICROSOFT_GRAPH, 'url': url}]),
            ):
                self.assertIsInstance(Source.init(name=SOURCE_MICROSOFT_GRAPH['name']), MicrosoftGraph)

    def test_source_factory_missing_keys(self):
        for key, field in (
            ('account', 'account'),
            ('tenant_id', 'tenant_id'),
            ('client_id', 'client_id'),
            ('secret', 'client_secret'),
        ):
            with (
                self.subTest(key=key),
                plugin_settings(notification_sources=[without(SOURCE_MICROSOFT_GRAPH, key)]),
                self.assertRaisesMessage(ValidationError, f'{field}\n  Input should be a valid string'),
            ):
                Source.init(name=SOURCE_MICROSOFT_GRAPH['name'])

    def test_source_factory_invalid_values(self):
        # azure-identity checks the values when a run creates the credential. The init checks them first, so the source
        # is skipped rather than failing the run
        for key, value, message in (
            ('tenant_id', 'contoso tenant', 'Value error, Invalid tenant ID provided.'),
            ('client_id', '', 'Value error, client_id should be the id of a Microsoft Entra application'),
            ('secret', '', "Value error, secret should be a Microsoft Entra application's client secret"),
        ):
            with (
                self.subTest(key=key),
                plugin_settings(notification_sources=[{**SOURCE_MICROSOFT_GRAPH, key: value}]),
                self.assertRaises(ValidationError) as context,
            ):
                Source.init(name=SOURCE_MICROSOFT_GRAPH['name'])
            self.assertIn(message, str(context.exception))
            # The error leaves out the input, the secret included
            self.assertNotIn('supersecret', str(context.exception))

    def test_source_factory_not_installed(self):
        with (
            patch(f'{SOURCES_MODULE}.MSGRAPH_PRESENT', False),
            self.assertRaisesMessage(
                ValueError,
                "You must install 'msgraph-sdk' (the plugin's m365 extra) to use HTTPS to graph.microsoft.com.",
            ),
        ):
            Source.init(name=SOURCE_MICROSOFT_GRAPH['name'])

    def test_get_notifications_invalid_source(self):
        job = MockedJob()
        with (
            plugin_settings(notification_sources=[{**SOURCE_MICROSOFT_GRAPH, 'tenant_id': 'contoso tenant'}]),
            FakeGraph({}) as graph,
        ):
            self.assertEqual(get_notifications(job, NotificationSource.objects.all(), SINCE), [])
        assert_called_with_substring(
            job.logger.warning,
            f'Notification Source {SOURCE_MICROSOFT_GRAPH["name"]} is not matching class expectations: '
            '1 validation error for MicrosoftGraph\n  Value error, Invalid tenant ID provided.',
        )
        self.assertEqual(graph.requests, [])

    def test_test_authentication(self):
        source = Source.init(name=SOURCE_MICROSOFT_GRAPH['name'])

        with FakeGraph({self.INBOX: (200, {'id': 'inbox-id'})}) as graph:
            self.assertEqual(source.test_authentication(), (True, 'Test OK'))
        # The app signs in with the source's credentials, for Microsoft Graph
        graph.credential.assert_called_once_with('contoso.onmicrosoft.com', 'client-id', 'supersecret')
        self.assertEqual(graph.get_token.call_args.args[1:], ('https://graph.microsoft.com/.default',))
        # Reading the Inbox checks the app's access to the mailbox
        self.assertEqual(graph.requests, [('GET', self.INBOX, 'Bearer access-token')])
        self.assertEqual(graph.closed(), ([True], 1))

    def test_test_authentication_folder(self):
        source = graph_source(folder="Bob's notices")
        folder = f"{GRAPH_MAILBOX}/mailFolders/inbox/childFolders?$filter=displayName eq 'Bob''s notices'&$select=id"

        with FakeGraph({folder: (200, {'value': [{'id': 'folder-id'}]})}) as graph:
            self.assertEqual(source.test_authentication(), (True, 'Test OK'))
        self.assertEqual(graph.requests, [('GET', folder, 'Bearer access-token')])

        with FakeGraph({folder: (200, {'value': []})}):
            self.assertEqual(
                source.test_authentication(), (False, "Folder Bob's notices not found in the Inbox of me@example.com.")
            )

    def test_test_authentication_ko(self):
        source = graph_source()

        # The app isn't allowed to read the mailbox
        with FakeGraph(
            {self.INBOX: graph_error(403, 'ErrorAccessDenied', 'Access is denied. Check credentials and try again.')}
        ) as graph:
            self.assertEqual(
                source.test_authentication(), (False, 'Access is denied. Check credentials and try again. (status 403)')
            )
        self.assertEqual(graph.closed(), ([True], 1))

        # Microsoft Entra ID refuses the credentials
        error = ClientAuthenticationError(
            'ClientSecretCredential.get_token failed: AADSTS7000215: Invalid client secret'
        )
        with FakeGraph({self.INBOX: (200, {'id': 'inbox-id'})}) as graph:
            graph.get_token.side_effect = error
            self.assertEqual(source.test_authentication(), (False, str(error)))
        self.assertEqual(graph.requests, [])

    def test_get_filters(self):
        for since_timestamp, emails_to_fetch, filters in (
            (None, [], [None]),
            # In UTC, whatever the time zone of the time given
            (
                datetime(2021, 1, 31, 5, tzinfo=ZoneInfo('America/Edmonton')),
                [],
                ['receivedDateTime ge 2021-01-31T12:00:00Z'],
            ),
            # One search per sender, as IMAP searches; quotes in an address are doubled
            (
                None,
                ['a@example.com', "o'brien@example.com"],
                ["from/emailAddress/address eq 'a@example.com'", "from/emailAddress/address eq 'o''brien@example.com'"],
            ),
            (
                datetime(2021, 1, 31, 12, tzinfo=UTC),
                ['a@example.com'],
                ["receivedDateTime ge 2021-01-31T12:00:00Z and from/emailAddress/address eq 'a@example.com'"],
            ),
        ):
            with self.subTest(since_timestamp=since_timestamp, emails_to_fetch=emails_to_fetch):
                source = graph_source(emails_to_fetch=emails_to_fetch)
                self.assertEqual(source._get_filters(since_timestamp), filters)

    def test_receive_notifications(self):
        sender_messages = (
            f"{self.MESSAGES}?$filter={self.RECEIVED} and from/emailAddress/address eq 'sender@example.com'"
        )
        noc_messages = f"{self.MESSAGES}?$filter={self.RECEIVED} and from/emailAddress/address eq 'noc@example.net'"
        contents = {
            'msg-1': raw_email(subject='Maintenance 1'),
            # Skipped, and the run goes on
            'msg-2': b'From: sender@example.com\r\nSubject: No date\r\n\r\nbody',
            'msg-3': raw_email(sender='Sender <SENDER@example.com>', subject='=?utf-8?q?Maintenance_3?='),
        }
        responses = {
            self.FOLDER: (200, {'value': [{'id': 'folder-id'}]}),
            # The list comes in pages
            f'{sender_messages}&$select=id&$top=100': (
                200,
                {
                    'value': [{'id': 'msg-1'}, {'id': 'msg-2'}],
                    '@odata.nextLink': f'https://graph.microsoft.com{self.MESSAGES}?$skiptoken=page-2',
                },
            ),
            f'{self.MESSAGES}?$skiptoken=page-2': (200, {'value': [{'id': 'msg-3'}]}),
            f'{noc_messages}&$select=id&$top=100': (200, {'value': []}),
            **{f'{GRAPH_MAILBOX}/messages/{msg_id}/$value': (200, content) for msg_id, content in contents.items()},
        }
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                source = graph_source(
                    folder='Circuit Notifications',
                    dry_run=dry_run,
                    emails_to_fetch=['sender@example.com', 'noc@example.net'],
                )
                job = MockedJob()

                with FakeGraph(responses) as graph:
                    notifications = source.receive_notifications(job, datetime(2021, 1, 31, 12, tzinfo=UTC))

                # Read only, whether it's a dry run or not: nothing is tagged or marked as read
                self.assertEqual(
                    [(method, url) for method, url, _ in graph.requests],
                    [
                        ('GET', self.FOLDER),
                        ('GET', f'{sender_messages}&$select=id&$top=100'),
                        ('GET', f'{self.MESSAGES}?$skiptoken=page-2'),
                        ('GET', f'{GRAPH_MAILBOX}/messages/msg-1/$value'),
                        ('GET', f'{GRAPH_MAILBOX}/messages/msg-2/$value'),
                        ('GET', f'{GRAPH_MAILBOX}/messages/msg-3/$value'),
                        ('GET', f'{noc_messages}&$select=id&$top=100'),
                    ],
                )
                # One client and one credential for the run, both closed at its end
                self.assertEqual(graph.closed(), ([True], 1))

                # The MIME content is processed as IMAP and Gmail process their emails
                self.assertEqual([n.msg_id for n in notifications], [b'msg-1', b'msg-3'])
                self.assertEqual([n.subject for n in notifications], ['Maintenance 1', 'Maintenance 3'])
                self.assertEqual({(n.sender, n.provider_type) for n in notifications}, {('sender@example.com', 'ntt')})
                self.assertEqual(notifications[0].date, 'Mon, 1 Feb 2021 09:33:34 +0000')
                self.assertEqual(notifications[0].raw_payload, email.message_from_bytes(contents['msg-1']).as_bytes())
                self.assertIs(notifications[0].source, source)
                job.logger.error.assert_called_once_with(
                    'Skipping the email from sender@example.com because it has no Date header.', extra=ANY
                )
                self.assertEqual(
                    [debug.args for debug in job.logger.debug.call_args_list],
                    [
                        (f'Fetched 3 emails from whatever source using filter: {sender_messages.split("=", 1)[1]}.',),
                        (f'Fetched 0 emails from whatever source using filter: {noc_messages.split("=", 1)[1]}.',),
                        ('Raw notifications created 2 from whatever.',),
                    ],
                )

    def test_receive_notifications_inbox(self):
        # Without a folder, sender or time, every message in the Inbox
        responses = {
            self.INBOX: (200, {'id': 'inbox-id'}),
            f'{GRAPH_MAILBOX}/mailFolders/inbox-id/messages?$select=id&$top=100': (200, {'value': [{'id': 'msg-1'}]}),
            f'{GRAPH_MAILBOX}/messages/msg-1/$value': (200, raw_email()),
        }
        with FakeGraph(responses) as graph:
            notifications = graph_source().receive_notifications(MockedJob())
        self.assertEqual([n.msg_id for n in notifications], [b'msg-1'])
        self.assertEqual([url for _, url, _ in graph.requests], list(responses))

    @patch('kiota_http.middleware.retry_handler.asyncio.sleep')
    def test_receive_notifications_throttled(self, sleep):
        # kiota's middleware retries a throttled request after the time Graph asks for
        responses = {
            self.INBOX: (200, {'id': 'inbox-id'}),
            f'{GRAPH_MAILBOX}/mailFolders/inbox-id/messages?$select=id&$top=100': [
                graph_error(
                    429, 'TooManyRequests', 'Application is over its MailboxConcurrency limit.', {'Retry-After': '2'}
                ),
                (200, {'value': []}),
            ],
        }
        with FakeGraph(responses) as graph:
            self.assertEqual(graph_source().receive_notifications(MockedJob()), [])
        self.assertEqual(len(graph.requests), 3)
        sleep.assert_awaited_once_with(2)

    def test_get_notifications(self):
        job = MockedJob()
        responses = {
            self.INBOX: (200, {'id': 'inbox-id'}),
            f'{GRAPH_MAILBOX}/mailFolders/inbox-id/messages?$filter={self.RECEIVED} and '
            "from/emailAddress/address eq 'sender@example.com'&$select=id&$top=100": (
                200,
                {'value': [{'id': 'msg-1'}]},
            ),
            f'{GRAPH_MAILBOX}/messages/msg-1/$value': (200, raw_email()),
        }
        with FakeGraph(responses) as graph:
            notifications = get_notifications(job, NotificationSource.objects.all(), SINCE)

        self.assertEqual([url for _, url, _ in graph.requests], list(responses))
        self.assertEqual([(n.msg_id, n.provider_type) for n in notifications], [(b'msg-1', 'ntt')])
        self.assertEqual(notifications[0].source.name, SOURCE_MICROSOFT_GRAPH['name'])
        job.logger.warning.assert_not_called()

    def test_get_notifications_address_characters(self):
        # Addresses with any RFC 5322 character are searched for and matched to their provider, as EWS's parsed
        # sender was (Nautobot's pattern read "noc+maint@example.com" as "noc")
        provider = Provider.objects.create(
            name='zayo',
            slug='zayo',
            custom_field_data={PROVIDER_EMAILS_FIELD: "noc+maint@example.com, o'brien@example.com"},
        )
        self.notification_source.providers.set([provider])
        messages = (
            f'{GRAPH_MAILBOX}/mailFolders/inbox-id/messages?$filter={self.RECEIVED} and from/emailAddress/address eq'
        )
        responses = {
            self.INBOX: (200, {'id': 'inbox-id'}),
            f"{messages} 'noc+maint@example.com'&$select=id&$top=100": (200, {'value': [{'id': 'msg-1'}]}),
            f"{messages} 'o''brien@example.com'&$select=id&$top=100": (200, {'value': [{'id': 'msg-2'}]}),
            f'{GRAPH_MAILBOX}/messages/msg-1/$value': (200, raw_email(sender='NOC <NOC+maint@example.com>')),
            f'{GRAPH_MAILBOX}/messages/msg-2/$value': (200, raw_email(sender="o'brien@example.com")),
        }
        job = MockedJob()
        with FakeGraph(responses) as graph:
            notifications = get_notifications(job, NotificationSource.objects.all(), SINCE)

        self.assertEqual(
            [(n.sender, n.provider_type) for n in notifications],
            [('noc+maint@example.com', 'zayo'), ("o'brien@example.com", 'zayo')],
        )
        job.logger.warning.assert_not_called()
        # The + is sent encoded, so Graph can't read it as a space
        self.assertIn('%27noc%2Bmaint%40example.com%27', graph.raw_paths[1])

    def test_get_notifications_error(self):
        # A failure stops the run, as other sources' failures do
        job = MockedJob()
        responses = {
            self.INBOX: graph_error(403, 'ErrorAccessDenied', 'Access is denied. Check credentials and try again.'),
        }
        with (
            FakeGraph(responses) as graph,
            self.assertRaisesMessage(ODataError, 'Access is denied. Check credentials and try again. (status 403)'),
        ):
            get_notifications(job, NotificationSource.objects.all(), SINCE)
        job.logger.exception.assert_called_once_with(
            f'Issue fetching notifications from {SOURCE_MICROSOFT_GRAPH["name"]}',
            extra={'object': self.notification_source},
        )
        self.assertEqual(graph.closed(), ([True], 1))
