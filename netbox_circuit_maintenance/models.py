import logging

from circuits.models import Circuit, Provider
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from netbox.models import NetBoxModel, PrimaryModel
from netbox.plugins import get_plugin_config

from .choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
from .constants import (
    MAX_MAINTENANCE_NAME_LENGTH,
    MAX_NOTE_TITLE_LENGTH,
    MAX_NOTIFICATION_SENDER_LENGTH,
    MAX_NOTIFICATION_SUBJECT_LENGTH,
    MAX_NOTIFICATION_TOTAL_LENGTH,
)

__all__ = (
    'CircuitImpact',
    'CircuitMaintenance',
    'Note',
    'NotificationSource',
    'ParsedNotification',
    'RawNotification',
)

logger = logging.getLogger(__name__)


class CircuitMaintenance(PrimaryModel):
    """
    A provider maintenance, usually created from a parsed notification. It spans circuits through CircuitImpact rather
    than holding a Circuit or Provider directly.
    """

    name = models.CharField(
        verbose_name=_('name'),
        max_length=MAX_MAINTENANCE_NAME_LENGTH,
        unique=True,
    )
    start_time = models.DateTimeField(
        verbose_name=_('start time'),
    )
    end_time = models.DateTimeField(
        verbose_name=_('end time'),
    )
    # Overrides PrimaryModel's 200-character CharField: parser summaries can be longer
    description = models.TextField(
        verbose_name=_('description'),
        blank=True,
    )
    status = models.CharField(
        verbose_name=_('status'),
        max_length=50,
        choices=CircuitMaintenanceStatusChoices,
        default=CircuitMaintenanceStatusChoices.TENTATIVE,
    )
    ack = models.BooleanField(
        verbose_name=_('acknowledged'),
        default=False,
    )

    class Meta:
        ordering = ['start_time', 'pk']
        indexes = (models.Index(fields=('start_time', 'id')),)  # Default ordering
        verbose_name = _('circuit maintenance')
        verbose_name_plural = _('circuit maintenances')

    def __str__(self):
        return self.name

    @property
    def docs_url(self):
        # The inherited URL points at a NetBox core docs page that doesn't exist; None hides the edit form's Help button
        return None

    @property
    def circuits(self):
        return Circuit.objects.filter(maintenance_impacts__maintenance=self)

    @property
    def providers(self):
        # distinct(): the join yields a provider once per impacted circuit
        return Provider.objects.filter(circuits__maintenance_impacts__maintenance=self).distinct()

    def get_status_color(self):
        return CircuitMaintenanceStatusChoices.colors.get(self.status)

    def clean(self):
        super().clean()

        # Either time is None here when its form field failed validation (Django runs clean() regardless). The error is
        # non-field: a CSV update row without an end_time column has no end_time form field to attach it to.
        if self.start_time and self.end_time and self.end_time < self.start_time:
            raise ValidationError(_('End time should be greater than start time.'))


class CircuitImpact(NetBoxModel):
    """
    How a CircuitMaintenance affects one Circuit. A maintenance lists each circuit at most once.
    """

    maintenance = models.ForeignKey(
        to='netbox_circuit_maintenance.CircuitMaintenance',
        on_delete=models.CASCADE,
        related_name='impacts',
        verbose_name=_('maintenance'),
    )
    circuit = models.ForeignKey(
        to='circuits.Circuit',
        on_delete=models.CASCADE,
        related_name='maintenance_impacts',
        verbose_name=_('circuit'),
    )
    impact = models.CharField(
        verbose_name=_('impact'),
        max_length=50,
        choices=CircuitImpactChoices,
        default=CircuitImpactChoices.OUTAGE,
    )

    clone_fields = ('maintenance', 'impact')
    prerequisite_models = (
        'netbox_circuit_maintenance.CircuitMaintenance',
        'circuits.Circuit',
    )

    class Meta:
        ordering = ['maintenance', 'impact', 'pk']
        constraints = (
            models.UniqueConstraint(
                fields=('maintenance', 'circuit'),
                name='%(app_label)s_%(class)s_unique_maintenance_circuit',
            ),
        )
        indexes = (models.Index(fields=('maintenance', 'impact', 'id')),)  # Default ordering
        verbose_name = _('circuit impact')
        verbose_name_plural = _('circuit impacts')

    def __str__(self):
        return f'Circuit {self.circuit} with impact {self.get_impact_display()}'

    @property
    def docs_url(self):
        # Same as CircuitMaintenance.docs_url
        return None

    def get_impact_color(self):
        return CircuitImpactChoices.colors.get(self.impact)

    def to_objectchange(self, action):
        # List impact changes in the maintenance's changelog
        objectchange = super().to_objectchange(action)
        objectchange.related_object = self.maintenance
        return objectchange


class Note(NetBoxModel):
    """
    A remark on a CircuitMaintenance, such as the handler's warning about a circuit ID it couldn't find. Titles are
    unique per maintenance.
    """

    maintenance = models.ForeignKey(
        to='netbox_circuit_maintenance.CircuitMaintenance',
        on_delete=models.CASCADE,
        related_name='notes',
        verbose_name=_('maintenance'),
    )
    title = models.CharField(
        verbose_name=_('title'),
        max_length=MAX_NOTE_TITLE_LENGTH,
    )
    level = models.CharField(
        verbose_name=_('level'),
        max_length=50,
        choices=NoteLevelChoices,
        default=NoteLevelChoices.INFO,
    )
    comment = models.TextField(
        verbose_name=_('comment'),
    )

    clone_fields = ('maintenance', 'level')
    prerequisite_models = ('netbox_circuit_maintenance.CircuitMaintenance',)

    class Meta:
        ordering = ['last_updated', 'pk']
        constraints = (
            models.UniqueConstraint(
                fields=('maintenance', 'title'),
                name='%(app_label)s_%(class)s_unique_maintenance_title',
            ),
        )
        indexes = (models.Index(fields=('last_updated', 'id')),)  # Default ordering
        verbose_name = _('note')
        verbose_name_plural = _('notes')

    def __str__(self):
        return self.title

    @property
    def docs_url(self):
        # Same as CircuitMaintenance.docs_url
        return None

    def get_level_color(self):
        return NoteLevelChoices.colors.get(self.level)

    def to_objectchange(self, action):
        # List note changes in the maintenance's changelog
        objectchange = super().to_objectchange(action)
        objectchange.related_object = self.maintenance
        return objectchange


class NotificationSource(NetBoxModel):
    """
    A mailbox or API that the notification handler fetches notifications from. Rows mirror the notification_sources
    in PLUGINS_CONFIG, matched by name; users only choose which providers each source applies to.
    """

    name = models.CharField(
        verbose_name=_('name'),
        max_length=100,
        unique=True,
        help_text=_('Notification source name as defined in the plugin configuration'),
    )
    providers = models.ManyToManyField(
        to='circuits.Provider',
        related_name='maintenance_notification_sources',
        blank=True,
        verbose_name=_('providers'),
        help_text=_('The providers that this notification source applies to'),
    )
    # Google OAuth credentials as authorized-user info (google.oauth2.credentials.Credentials.to_json()), None until
    # authorized. Holds a refresh token and client secret, so it stays out of serialize_object() and forms and has no
    # public property. The Jinja2 sandbox hides underscore attributes from export templates and custom links, but their
    # authors (trusted, per NetBox's docs) can still reach it through ORM methods such as values_list().
    _token = models.JSONField(
        blank=True,
        null=True,
        editable=False,
    )
    attach_all_providers = models.BooleanField(
        verbose_name=_('attach all providers'),
        default=False,
        help_text=_('Attach all the providers to this notification source'),
    )

    class Meta:
        ordering = ['name']
        verbose_name = _('notification source')
        verbose_name_plural = _('notification sources')

    def __str__(self):
        return self.name

    @property
    def docs_url(self):
        # Same as CircuitMaintenance.docs_url
        return None

    def serialize_object(self, exclude=None):
        # Keep the OAuth token out of change records and event rule snapshots
        return super().serialize_object(exclude=[*(exclude or []), '_token'])


class RawNotification(NetBoxModel):
    """
    A maintenance notification as fetched from a NotificationSource, before parsing. Only the notification handler
    creates them; a provider's notification is stored once per subject and stamp.
    """

    raw = models.BinaryField(
        verbose_name=_('raw'),
        max_length=MAX_NOTIFICATION_TOTAL_LENGTH,
    )
    subject = models.CharField(
        verbose_name=_('subject'),
        max_length=MAX_NOTIFICATION_SUBJECT_LENGTH,
    )
    provider = models.ForeignKey(
        to='circuits.Provider',
        on_delete=models.CASCADE,
        related_name='maintenance_raw_notifications',
        verbose_name=_('provider'),
    )
    sender = models.CharField(
        verbose_name=_('sender'),
        max_length=MAX_NOTIFICATION_SENDER_LENGTH,
        blank=True,
    )
    source = models.ForeignKey(
        to='netbox_circuit_maintenance.NotificationSource',
        on_delete=models.SET_NULL,
        related_name='raw_notifications',
        blank=True,
        null=True,
        verbose_name=_('source'),
    )
    parsed = models.BooleanField(
        verbose_name=_('parsed'),
        default=False,
    )
    stamp = models.DateTimeField(
        verbose_name=_('stamp'),
        help_text=_('Date of the notification as reported by its source'),
    )

    class Meta:
        ordering = ['stamp', 'pk']
        constraints = (
            models.UniqueConstraint(
                fields=('stamp', 'provider', 'subject'),
                name='%(app_label)s_%(class)s_unique_notification',
            ),
        )
        indexes = (models.Index(fields=('stamp', 'id')),)  # Default ordering
        verbose_name = _('raw notification')
        verbose_name_plural = _('raw notifications')

    def __str__(self):
        return self.subject

    @property
    def docs_url(self):
        # Same as CircuitMaintenance.docs_url
        return None

    def clean(self):
        super().clean()

        # stamp is None here when its form field failed validation (Django runs clean() regardless)
        if self.stamp and self.stamp > timezone.now():
            logger.warning("Stamp time %s is not consistent, it's in the future.", self.stamp)

    def save(self, *args, **kwargs):
        # Cap the stored notification. The setting is read per call, so config changes and override_settings apply
        size = get_plugin_config('netbox_circuit_maintenance', 'raw_notification_size')
        self.raw = self.raw[: min(size, MAX_NOTIFICATION_TOTAL_LENGTH)]
        super().save(*args, **kwargs)

    def serialize_object(self, exclude=None):
        # Keep the notification body (up to 16 KB, about 22 KB as base64) out of change records and event rule snapshots
        return super().serialize_object(exclude=[*(exclude or []), 'raw'])


class ParsedNotification(NetBoxModel):
    """
    The parser's output for one maintenance in a RawNotification. Only the notification handler creates them; a raw
    notification announcing several maintenances has one per maintenance.
    """

    maintenance = models.ForeignKey(
        to='netbox_circuit_maintenance.CircuitMaintenance',
        on_delete=models.CASCADE,
        related_name='parsed_notifications',
        verbose_name=_('maintenance'),
    )
    raw_notification = models.ForeignKey(
        to='netbox_circuit_maintenance.RawNotification',
        on_delete=models.CASCADE,
        related_name='parsed_notifications',
        verbose_name=_('raw notification'),
    )
    json = models.JSONField(
        verbose_name=_('JSON'),
        help_text=_('Maintenance as parsed from the raw notification'),
    )

    class Meta:
        ordering = ['last_updated', 'pk']
        indexes = (models.Index(fields=('last_updated', 'id')),)  # Default ordering
        verbose_name = _('parsed notification')
        verbose_name_plural = _('parsed notifications')

    def __str__(self):
        # A 200-character subject would make this 224. Event rule notifications store the REST display in a
        # varchar(200), and only truncate it while the object still exists, so a delete event would fail
        return f'Parsed notification for {self.raw_notification.subject}'[:200]

    @property
    def docs_url(self):
        # Same as CircuitMaintenance.docs_url
        return None

    def to_objectchange(self, action):
        # List the handler's parsed notifications in the maintenance's changelog, next to the changes they caused
        objectchange = super().to_objectchange(action)
        objectchange.related_object = self.maintenance
        return objectchange
