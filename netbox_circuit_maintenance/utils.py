import re

from circuits.models import Provider
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from netbox.plugins import get_plugin_config
from utilities.exceptions import AbortRequest

from .models import NotificationSource

__all__ = (
    'NotificationSourceSyncError',
    'markdown_code_span',
    'split_provider_emails',
    'sync_notification_sources',
)


def markdown_code_span(text):
    """
    Return the text as a Markdown inline code span, for text the plugin writes into a Markdown field that mustn't
    render as Markdown (a provider's circuit ID, say). The delimiter is one backtick longer than the longest run of
    backticks in the text, and the space inside each end, which Markdown strips, keeps a backtick at either end of the
    text off the delimiter.
    """
    delimiter = '`' * (max(map(len, re.findall('`+', text)), default=0) + 1)
    return f'{delimiter} {text} {delimiter}'


def split_provider_emails(value):
    """
    Return the sender addresses in a Provider's emails custom field value, stripped and lower-cased as the mail
    sources match senders. Empty entries are skipped, and a value that isn't a string (the field's type was changed)
    has no addresses.
    """
    if not isinstance(value, str):
        return []
    return [email for email in (entry.strip().lower() for entry in value.split(',')) if email]


class NotificationSourceSyncError(Exception):
    """
    Raised when the NotificationSources can't be synced with PLUGINS_CONFIG; nothing was changed.
    """


def get_configured_sources():
    """
    Return attach_all_providers by name for the notification_sources in PLUGINS_CONFIG. Raise
    NotificationSourceSyncError for a missing, blank, too long or repeated name, or an attach_all_providers that the
    model field rejects. Entries are named by index or name, never by their contents, which hold secrets.
    """
    max_length = NotificationSource._meta.get_field('name').max_length
    attach_field = NotificationSource._meta.get_field('attach_all_providers')
    sources = {}
    for index, config in enumerate(get_plugin_config('netbox_circuit_maintenance', 'notification_sources')):
        name = config.get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > max_length:
            raise NotificationSourceSyncError(
                f'notification_sources[{index}] in PLUGINS_CONFIG needs a name of up to {max_length} characters.'
            )
        if name in sources:
            raise NotificationSourceSyncError(
                f'Name {name} is used by more than one notification source in PLUGINS_CONFIG.'
            )
        # The values the model field accepts. Nautobot stored the same, but attached every provider for a string
        # such as 'False', which it tested before converting
        try:
            sources[name] = attach_field.to_python(config.get('attach_all_providers', False))
        except ValidationError:
            raise NotificationSourceSyncError(
                f'attach_all_providers for {name} in PLUGINS_CONFIG must be True or False.'
            ) from None
    return sources


def sync_notification_sources():
    """
    Make the NotificationSources match the notification_sources in PLUGINS_CONFIG: create the missing ones, update
    attach_all_providers, attach every Provider to the sources that attach all, and delete the sources no longer
    configured. A source is saved only when it changes, because each save notifies its subscribers. Return a line per
    change, for the caller to report. Raise NotificationSourceSyncError, changing nothing, if the database isn't fully
    migrated, the configuration is invalid or a protection rule stops a delete.
    """
    # The current models are used, the plugin's and core's (a delete reaches bookmarks, journal entries and more), so
    # every app must be fully migrated, as when migrate finishes
    executor = MigrationExecutor(connection)
    if plan := executor.migration_plan(executor.loader.graph.leaf_nodes()):
        raise NotificationSourceSyncError(f'The database has {len(plan)} unapplied migration(s).')

    configured = get_configured_sources()
    messages = []

    with transaction.atomic():
        for name, attach_all_providers in configured.items():
            source, created = NotificationSource.objects.get_or_create(
                name=name, defaults={'attach_all_providers': attach_all_providers}
            )
            if created:
                messages.append(f'Created notification source {name}')
            elif source.attach_all_providers != attach_all_providers:
                source.attach_all_providers = attach_all_providers
                # Leave the OAuth token alone, which the Gmail source stores without saving the source
                source.save(update_fields=['attach_all_providers', 'last_updated'])
                messages.append(f'Set attach_all_providers to {attach_all_providers} on notification source {name}')

            # Including providers that were taken off the source since
            if attach_all_providers and (
                missing := list(
                    Provider.objects.exclude(maintenance_notification_sources=source).values_list('pk', flat=True)
                )
            ):
                source.providers.add(*missing)
                messages.append(f'Added {len(missing)} provider(s) to notification source {name}')

        for source in NotificationSource.objects.exclude(name__in=list(configured)):
            try:
                source.delete()
            except AbortRequest as e:
                raise NotificationSourceSyncError(f"Notification source {source.name} can't be deleted. {e}") from e
            messages.append(f'Deleted notification source {source.name}, which is no longer in PLUGINS_CONFIG')

    return messages
