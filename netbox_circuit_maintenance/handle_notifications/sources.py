import asyncio
import base64
import contextlib
import datetime
import email
import email.policy
import imaplib
import json
import logging
import os
import re
import ssl
import time
from collections.abc import Iterable
from email.message import Message
from email.parser import BytesHeaderParser
from typing import ClassVar
from urllib.parse import urlparse

from circuits.models import Provider
from django.utils import timezone
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from netbox.jobs import JobRunner
from netbox.plugins import get_plugin_config
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..constants import PROVIDER_EMAILS_FIELD
from ..enum import MessageProcessingStatus
from ..models import NotificationSource
from ..utils import split_provider_emails

# msgraph-sdk comes with the plugin's m365 extra
try:
    from azure.identity.aio import ClientSecretCredential
    from kiota_abstractions.base_request_configuration import RequestConfiguration
    from kiota_authentication_azure.azure_identity_authentication_provider import AzureIdentityAuthenticationProvider
    from kiota_http.kiota_client_factory import KiotaClientFactory
    from msgraph import GraphRequestAdapter, GraphServiceClient
    from msgraph.generated.users.item.mail_folders.item.child_folders.child_folders_request_builder import (
        ChildFoldersRequestBuilder,
    )
    from msgraph.generated.users.item.mail_folders.item.mail_folder_item_request_builder import (
        MailFolderItemRequestBuilder,
    )
    from msgraph.generated.users.item.mail_folders.item.messages.messages_request_builder import MessagesRequestBuilder

    MSGRAPH_PRESENT = True
except ImportError:
    MSGRAPH_PRESENT = False

__all__ = (
    'IMAP',
    'EmailSource',
    'GmailAPI',
    'GmailAPIOauth',
    'GmailAPIServiceAccount',
    'MaintenanceNotification',
    'MicrosoftGraph',
    'RedirectAuthorize',
    'Source',
    'get_notifications',
)

logger = logging.getLogger(__name__)

# An email address's characters: RFC 5322 atext, dots and the @. Nautobot allowed only letters, digits, "-", "_", "."
# and "@", so it cut "noc+maint@x.com" down to "noc"
EMAIL_ADDRESS_PATTERN = r"[-A-Za-z0-9!#$%&'*+/=?^_`{|}~.@]+"


class Source(BaseModel):
    """
    Base class to retrieve notifications. To be extended for each scheme.
    """

    # Validation errors are logged and shown on the source's page, so they leave out the input values: a wrongly typed
    # secret would otherwise appear in them
    model_config = ConfigDict(hide_input_in_errors=True)

    name: str
    url: str
    # A dry run leaves the mailbox as it found it: implementations neither tag messages nor mark them as read
    dry_run: bool = False

    def get_account_id(self) -> str:
        """
        Method to get an identifier of the related account.
        """
        raise NotImplementedError

    def receive_notifications(
        self, job: JobRunner, since_timestamp: datetime.datetime | None = None
    ) -> Iterable['MaintenanceNotification']:
        """
        Function to retrieve notifications since one moment in time.

        The `MaintenanceNotification` attributes will contains these attributes:
        * source: self.name
        * sender: it could be the email 'from' or omitted if not relevant
        * subject: it could be the email 'subject' or some meaningful identifier from notification
        * provider_type: mapping to the Provider that is related to this notification
        * raw: the raw_payload from notification
        """
        # TODO: `senders` is used to limit the scope of emails retrieved, this won't have sense depending on the
        # Notification Source.
        raise NotImplementedError

    def validate_providers(self, job: JobRunner, notification_source: NotificationSource, since_txt: str) -> bool:
        """
        Method to validate that the NotificationSource has attached Providers.

        Args:
            job (JobRunner): Job to use its logger
            notification_source (NotificationSource): Notification Source to validate providers
            since_txt (str): Date string to be used to log

        Returns:
            bool: True if there are relevant providers attached or False otherwise
        """
        raise NotImplementedError

    def _authentication_logic(self):
        """
        Inner method to run the custom class validation logic.
        """
        raise NotImplementedError

    def test_authentication(self) -> tuple[bool, str]:
        """
        Method to validate the authentication of the Source.

        Returns:
            Tuple:
                bool: True if authentication was successful, False otherwise
                str: Message from authentication execution
        """
        try:
            self._authentication_logic()
            is_authenticated = True
            message = 'Test OK'
        except RedirectAuthorize:
            raise
        except Exception as exc:  # noqa: BLE001 (any failure is the test's result)
            is_authenticated = False
            # imaplib raises the server's response as bytes, in whatever charset the server chose
            if exc.args and isinstance(exc.args[0], bytes):
                message = exc.args[0].decode(errors='replace')
            # google-auth adds the token endpoint's response data after the message
            elif isinstance(exc, RefreshError) and exc.args:
                message = str(exc.args[0])
            else:
                message = str(exc)

        return is_authenticated, message

    @classmethod
    def init(cls, name: str, dry_run: bool = False) -> 'Source':
        """
        Factory Pattern to get the specific Source Class depending on the scheme.
        """
        for notification_source in get_plugin_config('netbox_circuit_maintenance', 'notification_sources'):
            if notification_source.get('name', '') == name:
                config = notification_source
                break
        else:
            raise ValueError(f'Name {name} not found in PLUGINS_CONFIG.')

        url = config.get('url')
        if not url:
            raise ValueError(f'URL for {name} not found in PLUGINS_CONFIG.')

        url_components = urlparse(url)
        scheme = url_components.scheme.lower()
        if scheme == 'imap':
            return IMAP(
                name=name,
                url=url,
                dry_run=dry_run,
                account=config.get('account'),
                password=config.get('secret'),
                # hostname also handles user info and IPv6 literals
                imap_server=url_components.hostname,
                imap_port=url_components.port or 993,
                source_header=config.get('source_header', 'From'),
            )
        if scheme == 'https' and url_components.hostname == 'graph.microsoft.com':
            if not MSGRAPH_PRESENT:
                raise ValueError(
                    "You must install 'msgraph-sdk' (the plugin's m365 extra) to use HTTPS to graph.microsoft.com."
                )
            return MicrosoftGraph(
                name=name,
                url=url,
                dry_run=dry_run,
                account=config.get('account'),
                tenant_id=config.get('tenant_id'),
                client_id=config.get('client_id'),
                client_secret=config.get('secret'),
                folder=config.get('folder'),
            )
        if scheme == 'https' and url_components.hostname == 'accounts.google.com':
            creds_filename = config.get('credentials_file')
            if not creds_filename:
                raise ValueError(f'Credentials_file for {name} not found in PLUGINS_CONFIG.')

            if not os.path.isfile(creds_filename):
                raise ValueError(f'Credentials_file {creds_filename} for {name} is not available.')

            # Every problem with the file is a ValueError, which the callers report as a source that can't be set up
            try:
                with open(creds_filename, encoding='utf-8') as credentials_file:
                    credentials = json.load(credentials_file)
            except (OSError, ValueError) as exc:
                raise ValueError(f"Credentials_file {creds_filename} for {name} can't be read: {exc}") from exc
            if not isinstance(credentials, dict):
                credentials = {}

            if credentials.get('type') == 'service_account':
                gmail_api_class = GmailAPIServiceAccount
            elif 'web' in credentials:
                gmail_api_class = GmailAPIOauth
            else:
                raise ValueError(f"File {creds_filename} doesn't contain any supported credentials.")
            return gmail_api_class(
                name=name,
                url=url,
                dry_run=dry_run,
                account=config.get('account'),
                credentials_file=creds_filename,
                source_header=config.get('source_header', 'From'),
                limit_emails_with_not_header_from=config.get('limit_emails_with_not_header_from', []),
                extra_scopes=config.get('extra_scopes', []),
                labels=config.get('labels', {}),
            )

        raise ValueError(
            f'Scheme {scheme} not supported as Notification Source '
            '(only IMAP or HTTPS to accounts.google.com or graph.microsoft.com).'
        )

    def tag_message(self, job: JobRunner, msg_id: str | bytes, tag: MessageProcessingStatus):
        """
        If supported, apply the given tag to the given message for future reference and categorization.

        The default implementation of this method is a no-op but specific Source subclasses may implement it. An
        implementation does nothing in a dry run.
        """


class MaintenanceNotification(BaseModel):
    """
    Representation of all the data related to a Maintenance Notification.
    """

    msg_id: bytes
    source: Source
    sender: str
    subject: str
    provider_type: str
    raw_payload: bytes
    date: str


class EmailSource(Source):
    """
    Abstract class that shares some methods and attributes across email based sources.
    """

    account: str
    emails_to_fetch: list[str] = []
    source_header: str = 'From'

    def get_account_id(self) -> str:
        """
        Method to get an identifier of the related account.
        """
        return self.account

    def validate_providers(self, job: JobRunner, notification_source: NotificationSource, since_txt: str) -> bool:
        """
        Method to validate that the NotificationSource has attached Providers.

        Args:
            job (JobRunner): Job to use its logger
            notification_source (NotificationSource): Notification Source to validate providers
            since_txt (str): Date string to be used to log

        Returns:
            bool: True if there are relevant providers attached or False otherwise
        """
        providers_with_email = []
        providers_without_email = []
        providers = notification_source.providers.all()
        if not providers:
            job.logger.warning(
                f"Skipping source '{notification_source.name}' because no providers were defined.",
                extra={'object': notification_source},
            )
            return False

        for provider in providers:
            # custom_field_data rather than cf, which costs a custom field query per provider outside a request
            if provider_emails := split_provider_emails(provider.custom_field_data.get(PROVIDER_EMAILS_FIELD)):
                self.emails_to_fetch.extend(provider_emails)
                providers_with_email.append(provider.name)
            else:
                providers_without_email.append(provider.name)

        if providers_without_email:
            job.logger.warning(
                f'Skipping {", ".join(providers_without_email)} because these providers have no email configured.',
                extra={'object': notification_source},
            )

        if not providers_with_email:
            job.logger.warning(
                (
                    f'Skipping Notification Source {notification_source.name} because none of the related providers '
                    'have emails defined.'
                ),
                extra={'object': notification_source},
            )
            return False
        job.logger.debug(f'Fetching emails from {self.emails_to_fetch}')
        job.logger.info(
            (
                f'Retrieving notifications from {notification_source.name} for '
                f'{", ".join(providers_with_email)} since {since_txt}'
            ),
            extra={'object': notification_source},
        )

        return True

    @staticmethod
    def extract_email_source(email_source: str) -> str:
        """
        Method to get the sender email address.
        """
        try:
            email_source = re.search(rf'\<({EMAIL_ADDRESS_PATTERN})\>', email_source).group(1)
        except AttributeError:
            try:
                email_source = re.search(f'({EMAIL_ADDRESS_PATTERN})', email_source).group(1)
            except AttributeError:
                return ''
        return email_source.lower()

    @staticmethod
    def get_provider_type_from_email(email_source: str) -> str | None:
        """
        Return the `Provider` type related to the source: the name of the first Provider, by name, whose emails custom
        field lists the address.
        """
        for name, provider_emails in Provider.objects.values_list(
            'name', f'custom_field_data__{PROVIDER_EMAILS_FIELD}'
        ):
            if email_source in split_provider_emails(provider_emails):
                return name
        return None

    def process_email(
        self, job: JobRunner, email_message: Message, msg_id: str | bytes
    ) -> MaintenanceNotification | None:
        """
        Process an EmailMessage to create the MaintenanceNotification. An email without a sender, Subject or Date is
        logged and skipped.
        """
        # Raw UTF-8 in a header comes back as an email.header.Header, which str() turns into text; the sender's ASCII
        # address survives it
        email_source = None
        if email_message[self.source_header]:
            email_source = self.extract_email_source(str(email_message[self.source_header]))

        if not email_source:
            job.logger.error(
                f'Not possible to determine the email sender from "{self.source_header}: '
                f'{email_message[self.source_header]}"',
                extra={'object': email_message},
            )
            self.tag_message(job, msg_id, MessageProcessingStatus.UNKNOWN_PROVIDER)
            return None

        provider_type = self.get_provider_type_from_email(email_source)
        if not provider_type:
            job.logger.warning(
                f'Not possible to determine the provider_type for {email_source}',
                extra={'object': email_source},
            )
            self.tag_message(job, msg_id, MessageProcessingStatus.UNKNOWN_PROVIDER)
            return None

        if missing_headers := [header for header in ('Subject', 'Date') if email_message[header] is None]:
            job.logger.error(
                f'Skipping the email from {email_source} because it has no {" or ".join(missing_headers)} header.',
                extra={'object': email_message},
            )
            self.tag_message(job, msg_id, MessageProcessingStatus.PARSING_FAILED)
            return None

        raw_payload = email_message.as_bytes()
        return MaintenanceNotification(
            source=self,
            sender=email_source,
            subject=self.decode_subject(raw_payload),
            provider_type=provider_type,
            raw_payload=raw_payload,
            date=str(email_message['Date']),
            msg_id=msg_id,
        )

    @staticmethod
    def decode_subject(raw_payload: bytes) -> str:
        """
        Return the message's Subject as text: encoded words decoded, folding removed and raw UTF-8 read, where the
        message's own (compat32) header keeps all three as they were sent.
        """
        headers = BytesHeaderParser(policy=email.policy.default).parsebytes(raw_payload)
        return str(headers['Subject'])


class IMAP(EmailSource):
    """
    IMAP class, extending Source class.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    password: str = Field(..., repr=False)
    imap_server: str
    imap_port: int = 993

    session: imaplib.IMAP4_SSL | None = None

    def open_session(self):
        """
        Open session to IMAP server, verifying its certificate and hostname (imaplib's default context checks neither).

        See states: https://github.com/python/cpython/blob/3.12/Lib/imaplib.py#L58
        """
        if not self.session:
            self.session = imaplib.IMAP4_SSL(self.imap_server, self.imap_port, ssl_context=ssl.create_default_context())
        if self.session.state == 'NONAUTH':
            self.session.login(self.account, self.password)

    def close_session(self):
        """
        Close session to IMAP server.

        See states: https://github.com/python/cpython/blob/3.12/Lib/imaplib.py#L58
        """
        if self.session:
            if self.session.state == 'SELECTED':
                self.session.close()
            if self.session.state == 'AUTH':
                self.session.logout()

    def _authentication_logic(self):
        """
        Inner method to run the custom class validation logic.
        """
        self.open_session()
        self.close_session()

    def fetch_email(self, job: JobRunner, msg_id: bytes) -> MaintenanceNotification | None:
        """
        Fetch an specific email ID.
        """
        _, data = self.session.fetch(msg_id, '(RFC822)')
        email_message = email.message_from_bytes(data[0][1])

        return self.process_email(job, email_message, msg_id)

    def receive_notifications(
        self, job: JobRunner, since_timestamp: datetime.datetime | None = None
    ) -> Iterable[MaintenanceNotification]:
        """
        Retrieve emails since an specific time, if provided.
        """
        self.open_session()

        # Define searching criteria. Fetching a message marks it as read, unless a dry run opens the Inbox read-only
        # (IMAP EXAMINE)
        self.session.select('Inbox', readonly=self.dry_run)

        # TODO: find the right way to search messages from several senders
        # Maybe extend filtering options, for instance, to discard some type of notifications
        msg_ids = []

        # TODO: define a similar function to _get_search_criteria
        since_date = ''
        if since_timestamp:
            since_txt = since_timestamp.strftime('%d-%b-%Y')
            since_date = f'SINCE "{since_txt}"'

        if self.emails_to_fetch:
            for sender in self.emails_to_fetch:
                if self.source_header == 'From':
                    search_items = (f'FROM "{sender}"', since_date)
                else:
                    search_items = (f'HEADER {self.source_header} "{sender}"', since_date)
                search_text = ' '.join(search_items).strip()
                search_criteria = f'({search_text})'
                messages = self.session.search(None, search_criteria)[1][0]
                msg_ids.extend(messages.split())
                job.logger.debug(
                    f'Fetched {len(messages.split())} emails from {self.name}'
                    f' source using search pattern: {search_criteria}.'
                )
        else:
            search_criteria = f'({since_date})'
            messages = self.session.search(None, search_criteria)[1][0]
            msg_ids.extend(messages.split())
            job.logger.debug(
                f'Fetched {len(messages.split())} emails from {self.name} '
                f'source using search pattern: {search_criteria}.'
            )

        received_notifications = []
        for msg_id in msg_ids:
            raw_notification = self.fetch_email(job, msg_id)
            if raw_notification:
                received_notifications.append(raw_notification)

        job.logger.debug(f'Raw notifications created {len(received_notifications)} from {self.name}.')

        self.close_session()
        return received_notifications


class MicrosoftGraph(EmailSource):
    """
    Microsoft Graph class, extending Source class, for an Exchange Online mailbox. It replaces Nautobot's Exchange Web
    Services source.

    The source signs in as an application (OAuth client credentials) with the Mail.Read application permission, so it
    reads messages, and neither tags them nor marks them as read. msgraph-sdk is async only: each method runs its
    requests in an event loop of its own, and uses the ORM, which refuses to run inside one, outside it.
    """

    tenant_id: str
    client_id: str
    client_secret: str = Field(..., repr=False)
    # A sub-folder of the Inbox, by name, as in Nautobot's EWS source
    folder: str | None = None

    # Client credentials ask for every application permission granted to the app registration
    SCOPES: ClassVar[list[str]] = ['https://graph.microsoft.com/.default']

    @model_validator(mode='after')
    def check_credential(self):
        """
        Check the credential's values as azure-identity does when a run creates the credential, so that a malformed
        one is a source that can't be set up, which the callers skip, rather than a failed run.
        """
        ClientSecretCredential(self.tenant_id, self.client_id, self.client_secret)
        return self

    @staticmethod
    def _quote(value: str) -> str:
        """
        Return the value as an OData string literal.
        """
        return "'" + value.replace("'", "''") + "'"

    @contextlib.asynccontextmanager
    async def _mailbox(self):
        """
        Yield the account's mailbox, through a Graph client that is closed on exit.
        """
        # kiota's HTTP client rather than msgraph-core's, whose middleware (retries when throttled among it) doesn't run
        # with the kiota-http it allows
        async with (
            ClientSecretCredential(self.tenant_id, self.client_id, self.client_secret) as credential,
            KiotaClientFactory.create_with_default_middleware() as http_client,
        ):
            auth_provider = AzureIdentityAuthenticationProvider(credential, scopes=self.SCOPES)
            client = GraphServiceClient(request_adapter=GraphRequestAdapter(auth_provider, client=http_client))
            yield client.users.by_user_id(self.account)

    async def _get_folder_id(self, mailbox) -> str:
        """
        Return the ID of the Inbox, or of its sub-folder named `folder`.
        """
        inbox = mailbox.mail_folders.by_mail_folder_id('inbox')
        if not self.folder:
            query = MailFolderItemRequestBuilder.MailFolderItemRequestBuilderGetQueryParameters(select=['id'])
            return (await inbox.get(RequestConfiguration(query_parameters=query))).id

        query = ChildFoldersRequestBuilder.ChildFoldersRequestBuilderGetQueryParameters(
            filter=f'displayName eq {self._quote(self.folder)}', select=['id']
        )
        folders = await inbox.child_folders.get(RequestConfiguration(query_parameters=query))
        if not folders.value:
            raise ValueError(f'Folder {self.folder} not found in the Inbox of {self.account}.')
        return folders.value[0].id

    def _authentication_logic(self):
        """
        Inner method to run the custom class validation logic. Finding the folder checks the credentials, the
        application's access to the mailbox and the folder's name.
        """

        async def find_folder():
            async with self._mailbox() as mailbox:
                await self._get_folder_id(mailbox)

        asyncio.run(find_folder())

    def _get_filters(self, since_timestamp: datetime.datetime | None = None) -> list[str | None]:
        """
        Build the $filter of each message search: one per sender, as IMAP searches, and by received time if provided.
        """
        received = None
        if since_timestamp:
            received = f'receivedDateTime ge {since_timestamp.astimezone(datetime.UTC):%Y-%m-%dT%H:%M:%SZ}'
        if not self.emails_to_fetch:
            return [received]
        return [
            ' and '.join(filter(None, (received, f'from/emailAddress/address eq {self._quote(sender)}')))
            for sender in self.emails_to_fetch
        ]

    async def _fetch_emails(self, filters: list[str | None]) -> list[tuple[str | None, list[tuple[str, bytes]]]]:
        """
        Run each search in the folder, and fetch the messages it finds: their ID and MIME content, per search.
        """
        searches = []
        async with self._mailbox() as mailbox:
            messages = mailbox.mail_folders.by_mail_folder_id(await self._get_folder_id(mailbox)).messages
            for search_filter in filters:
                query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
                    filter=search_filter, select=['id'], top=100
                )
                page = await messages.get(RequestConfiguration(query_parameters=query))
                msg_ids = [message.id for message in page.value]
                while page.odata_next_link:
                    page = await messages.with_url(page.odata_next_link).get()
                    msg_ids.extend(message.id for message in page.value)
                emails = [(msg_id, await mailbox.messages.by_message_id(msg_id).content.get()) for msg_id in msg_ids]
                searches.append((search_filter, emails))
        return searches

    def receive_notifications(
        self, job: JobRunner, since_timestamp: datetime.datetime | None = None
    ) -> Iterable[MaintenanceNotification]:
        """
        Retrieve emails since an specific time, if provided.
        """
        searches = asyncio.run(self._fetch_emails(self._get_filters(since_timestamp)))

        received_notifications = []
        for search_filter, emails in searches:
            job.logger.debug(f'Fetched {len(emails)} emails from {self.name} source using filter: {search_filter}.')
            for msg_id, content in emails:
                raw_notification = self.process_email(job, email.message_from_bytes(content), msg_id)
                if raw_notification:
                    received_notifications.append(raw_notification)

        job.logger.debug(f'Raw notifications created {len(received_notifications)} from {self.name}.')
        return received_notifications


class GmailAPI(EmailSource):
    """
    GmailAPI class.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    credentials_file: str
    account: str
    service: Resource | None = None
    credentials: service_account.Credentials | Credentials | None = Field(None, repr=False)

    # The required scope for baseline functionality (add gmail.modify permission in extra_scopes to enable tagging)
    SCOPES: list[str] = ['https://www.googleapis.com/auth/gmail.readonly']

    extra_scopes: list[str] = []
    limit_emails_with_not_header_from: list[str] = []
    labels: dict[str, str] = {}

    def load_credentials(self, force_refresh=False):
        """
        Load Credentials for Gmail API.
        """
        raise NotImplementedError

    def build_service(self):
        """
        Build API service.
        """
        self.service = build('gmail', 'v1', credentials=self.credentials)

    def close_service(self):
        """
        Close API Service.
        """
        if self.service:
            self.service.close()

    def _authentication_logic(self):
        """
        Inner method to run the custom class validation logic.
        """
        self.load_credentials(force_refresh=True)

    @staticmethod
    def _execute_with_retries(request, job, retries=5, delay=1, backoff=2):
        """
        Executes a Google API request with retries and exponential backoff.

        Args:
            request: The Google API request object.
            job: Job object to log.
            retries (int): Maximum number of attempts.
            delay (int/float): Initial delay between retries.
            backoff (int/float): Backoff multiplier to increase delay.

        Returns:
            The result of the successful execution of the request.

        Raises:
            HttpError: If all retries fail, it raises the last HttpError.
        """
        for attempt in range(1, retries + 1):
            try:
                return request.execute()
            except HttpError as http_error:
                # Raise the error if it's not retryable (e.g., 400, 403) or this was the last attempt
                if http_error.resp.status not in (500, 502, 503, 504) or attempt == retries:
                    raise
                job.logger.warning(f'Google API attempt {attempt} failed: {http_error}. Retrying in {delay} seconds...')
                time.sleep(delay)
                delay *= backoff
        return None

    def fetch_email(self, job: JobRunner, msg_id: str) -> MaintenanceNotification | None:
        """
        Fetch an specific email ID.

        See data format:  https://developers.google.com/gmail/api/reference/rest/v1/users.messages#Message
        """
        request = self.service.users().messages().get(userId=self.account, id=msg_id, format='raw')

        received_email = self._execute_with_retries(request, job)

        raw_email_string = base64.urlsafe_b64decode(received_email['raw'].encode('utf8'))
        email_message = email.message_from_bytes(raw_email_string)
        return self.process_email(job, email_message, msg_id)

    def _get_search_criteria(self, since_timestamp: datetime.datetime | None = None) -> str:
        """
        Build "search" criteria to filter emails, from date of from sender.
        """
        search_criteria = ''
        if since_timestamp:
            since_txt = since_timestamp.strftime('%Y/%m/%d')
            search_criteria = f'after:{since_txt}'

        # If source_header is not "From" but some other custom header such as X-Original-Sender,
        # the Gmail API doesn't let us filter by that, but if we provided via config a list of
        # source via `limit_emails_with_not_header_from`, we filter by that.
        if self.emails_to_fetch and self.source_header == 'From':
            emails_with_from = [f'from:{email}' for email in self.emails_to_fetch]
            search_criteria += ' {' + f'{" ".join(emails_with_from)}' + '}'
        elif self.emails_to_fetch and self.limit_emails_with_not_header_from:
            emails_with_from = [f'from:{email}' for email in self.limit_emails_with_not_header_from]
            search_criteria += ' {' + f'{" ".join(emails_with_from)}' + '}'

        return search_criteria

    def tag_message(self, job: JobRunner, msg_id: str | bytes, tag: MessageProcessingStatus):
        """
        Apply the given Gmail label to the given message, unless this is a dry run.
        """
        # Do we have a configured label ID corresponding to the given tag?
        if self.dry_run or tag.value not in self.labels:
            return

        # Gmail API expects a str msg_id, but MaintenanceNotification coerces it to bytes - change it back if needed
        if isinstance(msg_id, bytes):
            msg_id = msg_id.decode()

        try:
            self.service.users().messages().modify(
                userId=self.account, id=msg_id, body={'addLabelIds': [self.labels[tag.value]]}
            ).execute()
        except HttpError as exc:
            # NetBox's job log keeps only the message, not the traceback, so the message carries the reason
            job.logger.warning(
                f"Error in applying tag '{tag.value}' ({self.labels[tag.value]}) to message {msg_id}: {exc}",
                exc_info=True,
            )

    def receive_notifications(
        self, job: JobRunner, since_timestamp: datetime.datetime | None = None
    ) -> Iterable[MaintenanceNotification]:
        """
        Retrieve emails since an specific time, if provided.
        """
        self.load_credentials()
        self.build_service()

        search_criteria = self._get_search_criteria(since_timestamp)

        # messages.list() returns 100 emails at a time;
        # we need to loop with list_next() until we have all relevant messages
        request = self.service.users().messages().list(userId=self.account, q=search_criteria)
        msg_ids = []
        while request is not None:
            response = request.execute()
            msg_ids.extend(msg['id'] for msg in response.get('messages', []))
            request = self.service.users().messages().list_next(request, response)

        job.logger.debug(
            f'Fetched {len(msg_ids)} emails from {self.name} source using search pattern: {search_criteria}.'
        )

        received_notifications = []
        for msg_id in msg_ids:
            raw_notification = self.fetch_email(job, msg_id)
            if raw_notification:
                received_notifications.append(raw_notification)

        # Only the count: NetBox's job log keeps debug messages, and the notifications hold whole emails
        job.logger.debug(f'Raw notifications created {len(received_notifications)} from {self.name}.')

        self.close_service()
        return received_notifications


class RedirectAuthorize(Exception):
    """
    Custom class to signal a redirect to trigger OAuth autorization workflow for a specific source_name.
    """

    def __init__(self, url_name, source_name):
        """
        Init for RedirectAuthorize.
        """
        self.url_name = url_name
        self.source_name = source_name
        super().__init__()


class GmailAPIOauth(GmailAPI):
    """
    GmailAPIOauth class.

    See: https://developers.google.com/identity/protocols/oauth2/web-server
    """

    def load_credentials(self, force_refresh=False):
        """
        Load Gmail API OAuth credentials, the ones the OAuth callback stored on the NotificationSource.
        """
        notification_source = NotificationSource.objects.get(name=self.name)
        try:
            # None until the OAuth callback stores a token, and after a refresh failed
            self.credentials = Credentials.from_authorized_user_info(notification_source._token or {})
        except ValueError:
            self.credentials = None
            logger.debug('Google OAuth Token has not been initialized yet.')

        if force_refresh or not self.credentials or not self.credentials.valid:
            if self.credentials and self.credentials.refresh_token and (self.credentials.expired or force_refresh):
                try:
                    self.credentials.refresh(Request())
                except RefreshError:
                    # Bad token, discard it
                    self.store_token(notification_source, None)
                    raise

                self.store_token(notification_source, json.loads(self.credentials.to_json()))
            else:
                raise RedirectAuthorize(url_name='google_authorize', source_name=self.name)

    @staticmethod
    def store_token(notification_source, token):
        """
        Save the token without a change record, an event or a new last_updated, as core saves a data source's sync
        status: a refresh is bookkeeping, and the token is left out of the change data anyway. The OAuth callback
        stores the first token the same way.
        """
        notification_source._token = token
        NotificationSource.objects.filter(pk=notification_source.pk).update(_token=token)


class GmailAPIServiceAccount(GmailAPI):
    """
    GmailAPIServiceAccount class.
    """

    def load_credentials(self, force_refresh=False):
        """
        Load Gmail API Service Account credentials.
        """
        if force_refresh or not self.credentials:
            self.credentials = service_account.Credentials.from_service_account_file(self.credentials_file)
            self.credentials = self.credentials.with_scopes(self.SCOPES + self.extra_scopes)
            self.credentials = self.credentials.with_subject(self.account)
            self.credentials.refresh(Request())


def get_notifications(
    job: JobRunner,
    notification_sources: Iterable[NotificationSource],
    since: float,
    dry_run: bool = False,
) -> Iterable[MaintenanceNotification]:
    """
    Method to fetch notifications from multiple sources and return MaintenanceNotification objects.
    """
    received_notifications = []

    for notification_source in notification_sources:
        try:
            since_date = datetime.datetime.fromtimestamp(since, tz=timezone.get_default_timezone())
            since_txt = since_date.strftime('%d-%b-%Y')

            try:
                source = Source.init(name=notification_source.name, dry_run=dry_run)
            except ValidationError as validation_error:
                job.logger.warning(
                    (
                        f'Notification Source {notification_source.name} '
                        f'is not matching class expectations: {validation_error}'
                    ),
                    extra={'object': notification_source},
                    exc_info=True,
                )
                continue
            except ValueError as exc:
                # NetBox's job log keeps only the message, not the traceback, so the message carries the reason
                job.logger.warning(
                    f'Skipping notification source {notification_source}: {exc}',
                    extra={'object': notification_source},
                    exc_info=True,
                )
                continue

            if source.validate_providers(job, notification_source, since_txt):
                # When using the SINCE filter, we add one extra day to check for notifications received
                # on the very same day since last notification.
                since_date -= datetime.timedelta(days=1)

                try:
                    raw_notifications = source.receive_notifications(job, since_date)
                except RedirectAuthorize:
                    # Only a user can authorize a Google OAuth source, so the run goes on with the other sources
                    job.logger.warning(
                        f'Skipping notification source {notification_source.name}: it needs Google OAuth '
                        'authorization. Validate the source to authorize it.',
                        extra={'object': notification_source},
                    )
                    continue
                received_notifications.extend(raw_notifications)

                if not raw_notifications:
                    job.logger.info(
                        (
                            f'No notifications received for '
                            f'{", ".join(notification_source.providers.all().values_list("name", flat=True))} since '
                            f'{since_txt} from {notification_source.name}'
                        ),
                        extra={'object': notification_source},
                    )

        except Exception:
            job.logger.exception(
                f'Issue fetching notifications from {notification_source.name}',
                extra={'object': notification_source},
            )
            raise

    return received_notifications
