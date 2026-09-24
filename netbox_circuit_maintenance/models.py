"""
NetBox Circuit Maintenance plugin Models
"""

import logging
import pickle
from datetime import UTC, datetime

from circuits.models import Provider

# from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from netbox.models import OrganizationalModel, PrimaryModel
from netbox.plugins import get_plugin_config

from .choices import (
    CircuitMaintenanceImpactChoices,
    CircuitMaintenanceNoteLevelChoices,
    CircuitMaintenanceStatusChoices,
)

logger = logging.getLogger(__name__)

__all__ = (
    'CircuitMaintenance',
    'MaintenanceImpact',
    'MaintenanceNote',
    'NotificationSource',
    'ParsedNotification',
    'RawNotification',
)


class CircuitMaintenance(PrimaryModel):
    name = models.CharField(max_length=100, unique=True, blank=False)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    description = models.TextField(default='', blank=True)
    status = models.CharField(
        choices=CircuitMaintenanceStatusChoices,
        default=CircuitMaintenanceStatusChoices.STATUS_UNKNOWN,
        max_length=50,
    )
    ack = models.BooleanField(default=False)
    circuit = models.ForeignKey(
        to='circuits.Circuit',
        on_delete=models.CASCADE,
        related_name='maintenance',
    )
    provider = models.ForeignKey(
        to='circuits.Provider',
        on_delete=models.CASCADE,
        related_name='maintenance',
    )

    prerequisite_models = (
        'circuits.Circuit',
        'circuits.Provider',
    )

    class Meta:
        ordering = ['start_time', 'provider']

    def __str__(self):
        return f'{self.name}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_circuit_maintenance:circuitmaintenance', args=[self.pk])


class MaintenanceImpact(OrganizationalModel):
    maintenance = models.ForeignKey(
        to='netbox_circuit_maintenance.CircuitMaintenance',
        on_delete=models.CASCADE,
        related_name='impacts',
    )
    circuit = models.ForeignKey(
        to='circuits.Circuit',
        on_delete=models.CASCADE,
        related_name='impacts',
    )
    impact = models.CharField(
        default=CircuitMaintenanceImpactChoices.IMPACT_OUTAGE,
        max_length=50,
        choices=CircuitMaintenanceImpactChoices,
    )

    class Meta:
        ordering = ['maintenance', 'impact']
        constraints = [
            models.UniqueConstraint(
                name='%(app_label)s_%(class)s_unique_maintenance',
                fields=['maintenance', 'circuit'],
                violation_error_message='Each Maintenance must be for a separate Circuit.',
            )
        ]

    def __str__(self):
        return f'Circuit {self.circuit} with impact {self.impact}'[:200]

    def get_absolute_url(self):
        return reverse('plugins:netbox_circuit_maintenance:circuitimpact', args=[self.pk])


class MaintenanceNote(OrganizationalModel):
    maintenance = models.ForeignKey(
        to='netbox_circuit_maintenance.CircuitMaintenance',
        on_delete=models.CASCADE,
        default=None,
    )
    title = models.CharField(max_length=200)
    level = models.CharField(
        default=CircuitMaintenanceNoteLevelChoices.NOTELEVEL_INFO,
        max_length=50,
        choices=CircuitMaintenanceNoteLevelChoices,
    )
    comment = models.TextField()

    class Meta:
        ordering = ['last_updated']
        constraints = [
            models.UniqueConstraint(
                fields=['maintenance', 'title'],
                name='%(app_label)s_%(class)s_unique_title',
                violation_error_message='Each Maintenance needs a unique Title',
            )
        ]

    def __str__(self):
        return f'{self.title}'[:200]

    def get_absolute_url(self):
        return reverse('plugins:netbox_circuit_maintenance:note', args=[self.pk])


class NotificationSource(OrganizationalModel):
    name = models.CharField(
        max_length=100, unique=True, help_text=_('Notification Source Name as defined in configuration file.')
    )
    providers = models.ManyToManyField(
        to='circuits.Provider', help_text=_('The Provider(s) that this Notification Source applies to.'), blank=True
    )
    _token = models.BinaryField(blank=True)
    attach_all_providers = models.BooleanField(
        default=False, help_text=_('Attach all the Providers to this Notification Source')
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_circuit_maintenance:notificationsource', args=[self.pk])

    @property
    def token(self):
        """Getter for _token"""
        # TODO: Find a way to replace this with something else if Secret storage ever happens
        return pickle.loads(self._token)

    @token.setter
    def token(self, value):
        """Setter for _token."""
        self._token = pickle.dumps(value)


@receiver(post_save, sender=Provider)
def add_provider_to_email_sources(sender, instance, created, **kwargs):
    """Listen to Provider's creation to add them to the NotificationSources that have the attach_all_providers flag"""
    if created:
        for notification_source in NotificationSource.objects.filter(attach_all_providers=True):
            notification_source.providers.add(instance)


class RawNotification(OrganizationalModel):
    """Model for maintenance notifications in raw format."""

    raw = models.BinaryField(max_length=16384)
    subject = models.CharField(max_length=200)
    provider = models.ForeignKey(to='circuits.Provider', on_delete=models.CASCADE, default=None)
    sender = models.CharField(max_length=200, default='', blank=True)
    source = models.ForeignKey(to='netbox_circuit_maintenance.NotificationSource', on_delete=models.SET_NULL, null=True)
    parsed = models.BooleanField(default=False)
    # RawNotification.stamp is the date when the RawNotification was received by the Source
    stamp = models.DateTimeField()
    natural_key_field_names = ['stamp', 'provider', 'subject']

    class Meta:
        ordering = ['stamp']
        unique_together = ('stamp', 'provider', 'subject')

    def save(self, *args, **kwargs):
        """Custom save for RawNotification."""
        # Limiting the size of the notification stored.
        notification_length = min(get_plugin_config('netbox_circuit_maintenance', 'raw_notification_size'), 16384)
        self.raw = self.raw[:notification_length]
        super().save(*args, **kwargs)

    def __str__(self):
        """String value for HTML rendering."""
        return f'{self.subject}'

    def get_absolute_url(self, api=False):
        """Returns reverse loop up URL."""
        return reverse('plugins:netbox_circuit_maintenance:rawnotification', args=[self.pk])

    def clean(self):
        """Add validation when creating a RawNotification."""

        if self.stamp > datetime.now(UTC):
            # - SHOULD BE A ValidationError?
            logger.warning("Stamp time %s is not consistent, it's in the future.", self.stamp)


class ParsedNotification(OrganizationalModel):
    """Model for maintenance notifications after parsing."""

    maintenance = models.ForeignKey(to='netbox_circuit_maintenance.CircuitMaintenance', on_delete=models.CASCADE)
    raw_notification = models.ForeignKey(to='netbox_circuit_maintenance.RawNotification', on_delete=models.CASCADE)
    json = models.JSONField()
    natural_key_field_names = ['maintenance', 'raw_notification']

    class Meta:
        ordering = ['last_updated']

    def __str__(self):
        """String value for HTML rendering."""
        # str(self) is used in change logging, and ObjectChange.object_repr field is limited to 200 characters.
        return f'Parsed notification for {self.raw_notification.subject}'[:200]

    def get_absolute_url(self, api=False):
        """Returns reverse loop up URL."""
        return reverse('plugins:nautobot_circuit_maintenance:parsednotification', args=[self.pk])
