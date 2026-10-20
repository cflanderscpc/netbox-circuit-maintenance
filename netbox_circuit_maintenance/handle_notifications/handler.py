import datetime
import json
from collections.abc import Iterable
from contextlib import ExitStack
from enum import Enum

from circuit_maintenance_parser import Maintenance, NotificationData, ProviderError, init_provider
from circuit_maintenance_parser.output import CircuitImpact as ParserCircuitImpact
from circuits.models import Circuit, Provider
from dateutil import parser
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from extras.models import CustomField
from netbox.jobs import JobRunner
from netbox.plugins import get_plugin_config
from utilities.request import NetBoxFakeRequest, apply_request_processors

from ..choices import CircuitMaintenanceStatusChoices, NoteLevelChoices
from ..constants import (
    MAX_MAINTENANCE_NAME_LENGTH,
    MAX_NOTE_TITLE_LENGTH,
    MAX_NOTIFICATION_SENDER_LENGTH,
    MAX_NOTIFICATION_SUBJECT_LENGTH,
    MAX_NOTIFICATION_TOTAL_LENGTH,
    PROVIDER_PARSER_FIELD,
)
from ..enum import MessageProcessingStatus
from ..models import CircuitImpact, CircuitMaintenance, Note, NotificationSource, ParsedNotification, RawNotification
from ..utils import markdown_code_span
from .sources import MaintenanceNotification, get_notifications

__all__ = (
    'HandleCircuitMaintenanceNotifications',
    'create_circuit_maintenance',
    'create_or_update_circuit_maintenance',
    'create_raw_notification',
    'get_maintenances_from_notification',
    'get_since_reference',
    'process_raw_notification',
    'update_circuit_maintenance',
)

# NetBox's job log keeps only each record's message, so every message names its object. The extra={'object': …} of
# Nautobot's job log links still reaches NetBox's other log handlers. For the same reason, errors put the exception
# in the message (noqa: TRY401), and logger.exception() still sends the traceback to the other handlers.


def enum_value(value):
    """
    Return the value of a member of the parser's str Enums (Status, Impact), and any other value as it is. Saving the
    member itself stores its value, but the change log would record 'Impact.OUTAGE'.
    """
    return value.value if isinstance(value, Enum) else value


def get_status(parser_status) -> str:
    """
    Return the CircuitMaintenance status for the parser's status: the same value, or UNKNOWN if there's none.
    """
    status = enum_value(parser_status)
    return status if status in CircuitMaintenanceStatusChoices.values() else CircuitMaintenanceStatusChoices.UNKNOWN


def validate_and_save(obj):
    """
    Validate the object, so that CUSTOM_VALIDATORS rules apply to the handler's writes, then save it. save() fills in
    the defaults of custom fields the object has no value for, but full_clean() runs first and rejects a required one
    without a value, so they're filled in first (the REST API fills them before validating a new object).
    """
    for custom_field in CustomField.objects.get_for_model(obj):
        if custom_field.name not in obj.custom_field_data and custom_field.default is not None:
            obj.custom_field_data[custom_field.name] = custom_field.default
    obj.full_clean()
    obj.save()


def get_stamp(date: str) -> datetime.datetime:
    """
    Return a notification's Date header as a datetime. A date without a time zone is read in TIME_ZONE, as Django
    would read it when saving, without Django's warning.
    """
    stamp = parser.parse(date)
    return timezone.make_aware(stamp) if timezone.is_naive(stamp) else stamp


def link_circuit(
    job: JobRunner,
    notification: MaintenanceNotification,
    circuit_maintenance_entry: CircuitMaintenance,
    parser_circuit: ParserCircuitImpact,
    provider: Provider,
):
    """
    Link the provider's circuit with the parser circuit's ID (compared case-insensitively) to the maintenance, unless
    it's linked already. If the provider has no such circuit, add a warning note to the maintenance instead, once.
    """
    maintenance_id = circuit_maintenance_entry.name
    circuit_entry = Circuit.objects.filter(cid__iexact=parser_circuit.circuit_id, provider=provider).last()
    if circuit_entry:
        if not CircuitImpact.objects.filter(maintenance=circuit_maintenance_entry, circuit=circuit_entry).exists():
            circuit_impact_entry = CircuitImpact(
                maintenance=circuit_maintenance_entry,
                circuit=circuit_entry,
                impact=enum_value(parser_circuit.impact),
            )
            validate_and_save(circuit_impact_entry)
            job.logger.info(
                f'Circuit ID {parser_circuit.circuit_id} linked to Maintenance {maintenance_id}',
                extra={'object': circuit_impact_entry},
            )
        return

    # Looked up by title alone, so a user's edits to the note's comment or level are kept. Nautobot looked up all
    # three, and an edited note made the create fail on the (maintenance, title) unique constraint
    title = f'Nonexistent circuit ID {parser_circuit.circuit_id}'[:MAX_NOTE_TITLE_LENGTH]
    if not Note.objects.filter(maintenance=circuit_maintenance_entry, title=title).exists():
        note_entry = Note(
            maintenance=circuit_maintenance_entry,
            title=title,
            level=NoteLevelChoices.WARNING,
            # The comment renders as Markdown, and the circuit ID is the provider's text
            comment=(
                f'Circuit ID {markdown_code_span(parser_circuit.circuit_id)} referenced was not found in the '
                'database, so omitted from the maintenance.'
            ),
        )
        validate_and_save(note_entry)
        job.logger.warning(
            f'Circuit ID {parser_circuit.circuit_id} referenced in {maintenance_id} is not in the Database, adding a '
            'note',
            extra={'object': note_entry},
        )
    notification.source.tag_message(job, notification.msg_id, MessageProcessingStatus.UNKNOWN_CIDS)


def create_circuit_maintenance(
    job: JobRunner,
    notification: MaintenanceNotification,
    maintenance_id: str,
    parser_maintenance: Maintenance,
    provider: Provider,
) -> CircuitMaintenance:
    """
    Handles the creation of a new circuit maintenance.
    """
    circuit_maintenance_entry = CircuitMaintenance(
        name=maintenance_id[:MAX_MAINTENANCE_NAME_LENGTH],
        start_time=datetime.datetime.fromtimestamp(parser_maintenance.start, tz=datetime.UTC),
        end_time=datetime.datetime.fromtimestamp(parser_maintenance.end, tz=datetime.UTC),
        description=parser_maintenance.summary,
        status=get_status(parser_maintenance.status),
    )
    validate_and_save(circuit_maintenance_entry)
    job.logger.info(
        f'Created Circuit Maintenance {circuit_maintenance_entry.name}.', extra={'object': circuit_maintenance_entry}
    )

    for parser_circuit in parser_maintenance.circuits:
        link_circuit(job, notification, circuit_maintenance_entry, parser_circuit, provider)

    if not circuit_maintenance_entry.impacts.exists():
        job.logger.warning(
            f'Circuit Maintenance {circuit_maintenance_entry.name} has no circuits in the database.',
            extra={'object': circuit_maintenance_entry},
        )

    return circuit_maintenance_entry


def update_circuit_maintenance(
    job: JobRunner,
    notification: MaintenanceNotification,
    circuit_maintenance_entry: CircuitMaintenance,
    parser_maintenance: Maintenance,
    provider: Provider,
):
    """
    Handles the update of an existent circuit maintenance.
    """
    maintenance_id = circuit_maintenance_entry.name
    # Record the maintenance as it was, for the change log
    circuit_maintenance_entry.snapshot()
    circuit_maintenance_entry.description = parser_maintenance.summary
    if enum_value(parser_maintenance.status) != 'NO-CHANGE':
        circuit_maintenance_entry.status = get_status(parser_maintenance.status)
    circuit_maintenance_entry.start_time = datetime.datetime.fromtimestamp(parser_maintenance.start, tz=datetime.UTC)
    circuit_maintenance_entry.end_time = datetime.datetime.fromtimestamp(parser_maintenance.end, tz=datetime.UTC)
    circuit_maintenance_entry.ack = False
    validate_and_save(circuit_maintenance_entry)

    # Circuits by lower-case ID; the first of the parser's circuits with an ID wins
    parser_circuits = {}
    for parser_circuit in parser_maintenance.circuits:
        parser_circuits.setdefault(parser_circuit.circuit_id.lower(), parser_circuit)
    # Only the provider's circuits: impacts that users added on other providers' circuits stay, as in Nautobot
    circuit_impact_entries = list(
        circuit_maintenance_entry.impacts.filter(circuit__provider=provider).select_related('circuit')
    )
    existing_cids = {circuit_impact_entry.circuit.cid.lower() for circuit_impact_entry in circuit_impact_entries}

    for cid, parser_circuit in parser_circuits.items():
        if cid not in existing_cids:
            link_circuit(job, notification, circuit_maintenance_entry, parser_circuit, provider)

    # The circuits linked already get the new impact, or are unlinked if the notification no longer lists them
    for circuit_impact_entry in circuit_impact_entries:
        if parser_circuit := parser_circuits.get(circuit_impact_entry.circuit.cid.lower()):
            circuit_impact_entry.snapshot()
            circuit_impact_entry.impact = enum_value(parser_circuit.impact)
            validate_and_save(circuit_impact_entry)
        else:
            circuit_impact_entry.delete()
            job.logger.info(
                f'Circuit ID {circuit_impact_entry.circuit.cid} unlinked from Maintenance {maintenance_id}',
                extra={'object': circuit_maintenance_entry},
            )

    job.logger.info(
        f'Updated Circuit Maintenance {maintenance_id}',
        extra={'object': circuit_maintenance_entry},
    )


def create_or_update_circuit_maintenance(
    job: JobRunner,
    notification: MaintenanceNotification,
    raw_entry: RawNotification,
    parser_maintenance: Maintenance,
    provider: Provider,
) -> CircuitMaintenance:
    """
    Processes a Maintenance, creating or updating the related Circuit Maintenance.

    It returns the CircuitMaintenance entry created or updated.
    """
    maintenance_id = f'{raw_entry.provider.name}-{parser_maintenance.maintenance_id}'[:MAX_MAINTENANCE_NAME_LENGTH]
    circuit_maintenance_entry = CircuitMaintenance.objects.filter(name=maintenance_id).first()
    if circuit_maintenance_entry is None:
        return create_circuit_maintenance(job, notification, maintenance_id, parser_maintenance, provider)

    # The newest notification applied so far, by RawNotification.stamp, the time as the source reported it. Nautobot's
    # order_by().reverse().last() picked the oldest
    newest_stamp = circuit_maintenance_entry.parsed_notifications.aggregate(newest=Max('raw_notification__stamp'))[
        'newest'
    ]

    # If the notification is older than the latest one used to update the CircuitMaintenance, we skip updating it
    parser_maintenance_datetime = datetime.datetime.fromtimestamp(parser_maintenance.stamp, tz=datetime.UTC)
    if newest_stamp and newest_stamp > parser_maintenance_datetime:
        job.logger.debug(
            f'Not updating CircuitMaintenance {maintenance_id} because the notification is from '
            f'{parser_maintenance_datetime}, older than the most recent notification from {newest_stamp}.',
            extra={'object': circuit_maintenance_entry},
        )
        notification.source.tag_message(job, notification.msg_id, MessageProcessingStatus.OUT_OF_SEQUENCE)
        return circuit_maintenance_entry

    update_circuit_maintenance(job, notification, circuit_maintenance_entry, parser_maintenance, provider)
    return circuit_maintenance_entry


def get_maintenances_from_notification(
    job: JobRunner, notification: MaintenanceNotification, provider: Provider
) -> Iterable[Maintenance] | None:
    """
    Use the `circuit_maintenance_parser` library to get Maintenances from the notification.
    """
    # custom_field_data rather than cf: the field may have been deleted, or hold a value that isn't a string
    parser_name = provider.custom_field_data.get(PROVIDER_PARSER_FIELD)
    provider_type = parser_name.lower() if parser_name and isinstance(parser_name, str) else provider.name

    parser_provider = init_provider(provider_type=provider_type)
    if not parser_provider:
        job.logger.warning(
            f'Notification Parser {provider_type} not found for provider {provider}', extra={'object': notification}
        )
        notification.source.tag_message(job, notification.msg_id, MessageProcessingStatus.PARSING_FAILED)
        return None

    data_to_process = NotificationData.init_from_email_bytes(notification.raw_payload)
    if not data_to_process:
        # The subject rather than the payload (Nautobot's message lacked its f prefix): the job log keeps every message
        job.logger.error(
            f'Notification data of `{notification.subject}` was not accepted by the parser',
            extra={'object': notification},
        )
        notification.source.tag_message(job, notification.msg_id, MessageProcessingStatus.PARSING_FAILED)
        raise ValueError('Notification data was not accepted by the parser')

    # NetBox's job log keeps only the message, not the traceback, so the message carries the reason
    try:
        result = parser_provider.get_maintenances(data_to_process)
    except ProviderError as exc:
        job.logger.exception(
            f'Parsing failed for notification `{notification.subject}`: {exc}',  # noqa: TRY401
            extra={'object': notification},
        )
        notification.source.tag_message(job, notification.msg_id, MessageProcessingStatus.PARSING_FAILED)
        raise
    except Exception as exc:
        job.logger.exception(
            f'Unexpected exception while parsing notification `{notification.subject}`: {exc}',  # noqa: TRY401
            extra={'object': notification},
        )
        notification.source.tag_message(job, notification.msg_id, MessageProcessingStatus.PARSING_FAILED)
        raise

    notification.source.tag_message(job, notification.msg_id, MessageProcessingStatus.PARSED)
    if not result:
        job.logger.info(
            f'No maintenance notifications detected in `{notification.subject}`',
            extra={'object': notification},
        )
        notification.source.tag_message(job, notification.msg_id, MessageProcessingStatus.IGNORED)
    return result


def create_raw_notification(
    job: JobRunner,
    notification: MaintenanceNotification,
    provider: Provider,
) -> RawNotification | None:
    """
    Create a RawNotification.

    If it already exists, we return `None` to signal we are skipping it.
    """
    subject = notification.subject[:MAX_NOTIFICATION_SUBJECT_LENGTH]
    stamp = get_stamp(notification.date)
    raw_entry = RawNotification.objects.filter(subject=subject, provider=provider, stamp=stamp).defer('raw').first()
    if raw_entry:
        # If the RawNotification was already created, we ignore it.
        job.logger.debug(
            f'Raw notification `{raw_entry.subject}` already existed with ID: {raw_entry.pk}',
            extra={'object': raw_entry},
        )
        return None

    try:
        raw_entry = RawNotification(
            subject=subject,
            provider=provider,
            # full_clean() checks the field's limit; save() then cuts it to the raw_notification_size setting
            raw=notification.raw_payload[:MAX_NOTIFICATION_TOTAL_LENGTH],
            sender=notification.sender[:MAX_NOTIFICATION_SENDER_LENGTH],
            source=NotificationSource.objects.filter(name=notification.source.name).last(),
            stamp=stamp,
        )
        validate_and_save(raw_entry)
    except Exception as exc:
        job.logger.exception(
            f'Raw notification `{notification.subject}` not created: {exc}',  # noqa: TRY401
            extra={'object': notification},
        )
        raise

    job.logger.info(f'Raw notification `{raw_entry.subject}` created.', extra={'object': raw_entry})
    return raw_entry


def process_raw_notification(job: JobRunner, notification: MaintenanceNotification) -> int | None:
    """
    Processes a raw notification (maybe containing multiple parsed notifications).

    It creates a RawNotification and if it could be parsed, create the corresponding ParsedNotification and the
    related objects. Finally returns the ID of the RawNotification modified.
    """
    provider = Provider.objects.filter(name=notification.provider_type).first()
    if provider is None:
        job.logger.warning(
            f'Raw notification `{notification.subject}` not created because it refers to provider '
            f"{notification.provider_type}, which doesn't exist.",
            extra={'object': notification},
        )
        notification.source.tag_message(job, notification.msg_id, MessageProcessingStatus.UNKNOWN_PROVIDER)
        return None

    raw_entry = create_raw_notification(job, notification, provider)
    if not raw_entry:
        return None

    parser_maintenances = get_maintenances_from_notification(job, notification, provider)
    if not parser_maintenances:
        return raw_entry.pk

    for parser_maintenance in parser_maintenances:
        try:
            circuit_maintenance_entry = create_or_update_circuit_maintenance(
                job, notification, raw_entry, parser_maintenance, provider
            )
            # Update raw notification as properly parsed, once for all its maintenances
            if not raw_entry.parsed:
                raw_entry.snapshot()
                raw_entry.parsed = True
                validate_and_save(raw_entry)

            # Insert parsed notification in DB, the parser's output as a JSON object
            parsed_entry = ParsedNotification(
                maintenance=circuit_maintenance_entry,
                raw_notification=raw_entry,
                json=json.loads(parser_maintenance.to_json()),
            )
            validate_and_save(parsed_entry)
            job.logger.info(
                f'Saved Parsed Notification for {circuit_maintenance_entry.name}.',
                extra={'object': parsed_entry},
            )

        except Exception as exc:
            job.logger.exception(
                f'Unexpected exception while handling parsed notification `{notification.subject}`: {exc}',  # noqa: TRY401
                extra={'object': notification},
            )
            raise

    return raw_entry.pk


def get_since_reference(job: JobRunner, notification_source: NotificationSource) -> float:
    """
    Get the timestamp that the notification source's next fetch starts from: when the newest of its raw notifications
    was created, or `raw_notification_initial_days_since` days ago if it has none. A source skipped in earlier runs
    (one that needs authorization, or with a configuration error) then catches up once it's fixed.
    """
    # created rather than last_updated, which saves that aren't fetches change (a tag edit, a source's deletion)
    since_reference = notification_source.raw_notifications.aggregate(newest=Max('created'))['newest']
    if not since_reference:
        days = get_plugin_config('netbox_circuit_maintenance', 'raw_notification_initial_days_since')
        since_reference = timezone.now() - datetime.timedelta(days=days)
    job.logger.info(
        f'Processing notifications from {notification_source} since '
        f'{timezone.localtime(since_reference).isoformat(timespec="seconds")}',
        extra={'object': notification_source},
    )
    return since_reference.timestamp()


class DryRunTransactionSkip(Exception):
    """
    Exception to handle dryrun mode.
    """


class HandleCircuitMaintenanceNotifications(JobRunner):
    """
    Job to handle external circuit maintenance notifications and turn them into Circuit Maintenances.
    """

    class Meta:
        name = 'Update Circuit Maintenances'

    def get_request(self) -> NetBoxFakeRequest | None:
        """
        Return the request to record the run's changes under: as the user who ran the job, else the job_user setting's
        user (scheduled runs have no user), with the job's ID as the request ID. Without a user, return None: the run
        records no changes and sends no events.
        """
        user = self.job.user
        if user is None and (username := get_plugin_config('netbox_circuit_maintenance', 'job_user')):
            user = get_user_model().objects.filter(username=username).first()
            if user is None:
                self.logger.warning(
                    f"Changes aren't recorded in the change log and send no events: user {username} in the job_user "
                    "setting doesn't exist."
                )
                return None
        if user is None:
            self.logger.warning(
                "Changes aren't recorded in the change log and send no events: the job has no user, and the job_user "
                'setting is unset.'
            )
            return None

        # As NetBox's runscript command builds it
        return NetBoxFakeRequest(
            {
                'META': {},
                'COOKIES': {},
                'POST': {},
                'GET': {},
                'FILES': {},
                'user': user,
                'method': 'POST',
                'path': '',
                'id': self.job.job_id,
            }
        )

    def run(self, dry_run=False, **kwargs):
        """
        Fetch notifications, process them and update Circuit Maintenance accordingly. The IDs of the raw notifications
        stored go in the job's data.
        """
        self.logger.debug('Starting Handle Notifications job.')
        raw_notification_ids = []
        self.job.data = {'dry_run': dry_run, 'raw_notifications': raw_notification_ids}

        # NetBox reschedules every job with an interval, registered as a system job or not. Clearing it ends a schedule
        # that began while job_interval was set (handle() reads it after run() returns)
        if self.job.interval and get_plugin_config('netbox_circuit_maintenance', 'job_interval') is None:
            self.job.interval = None
            self.logger.warning(
                'The job_interval setting is unset, so this scheduled run does nothing and the schedule ends.'
            )
            return

        notification_sources = NotificationSource.objects.all()
        if not notification_sources:
            self.logger.warning('No notification sources configured to retrieve notifications from.')
            return

        # A dry run records nothing
        request = None if dry_run else self.get_request()

        notifications = []
        for notification_source in notification_sources:
            since = get_since_reference(self, notification_source)
            try:
                notifications.extend(get_notifications(self, [notification_source], since, dry_run=dry_run))
            except Exception as exc:
                self.logger.error(
                    f'Unexpected exception when retrieving notifications from {notification_source}: {exc}'
                )
                raise

        if not notifications:
            self.logger.info('No notifications received.')
            return

        processed = 0
        for notification in notifications:
            self.logger.info(
                f'Processing notification `{notification.subject}` with ID '
                f'`{notification.msg_id.decode(errors="replace")}` from source `{notification.source.name}`.'
            )
            committed = False
            try:
                with ExitStack() as stack:
                    # As NetBox's background jobs do: event_tracking records the changes as the request's user, and
                    # sends events once the notification's transaction has committed
                    if request:
                        stack.enter_context(apply_request_processors(request))
                    with transaction.atomic():
                        raw_id = process_raw_notification(self, notification)
                        if dry_run:
                            raise DryRunTransactionSkip()
                    committed = True
            except DryRunTransactionSkip:
                self.logger.info('DRYRUN mode, nothing has been committed.')
            except Exception as exc:
                if not committed:
                    self.logger.exception(
                        f'Unexpected exception when parsing notification `{notification.subject}`: {exc}',  # noqa: TRY401
                        extra={'object': notification},
                    )
                    continue
                # The notification is stored, but sending its events failed
                self.logger.exception(
                    f'Unexpected exception when sending the events of notification `{notification.subject}`: {exc}',  # noqa: TRY401
                    extra={'object': notification},
                )

            if raw_id:
                processed += 1
                # A dry run's raw notifications were rolled back
                if not dry_run:
                    raw_notification_ids.append(raw_id)

        self.logger.info(f'{processed} notifications processed.')
