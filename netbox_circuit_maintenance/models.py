from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from netbox.models import PrimaryModel

from .choices import CircuitMaintenanceStatusChoices
from .constants import MAX_MAINTENANCE_NAME_LENGTH

__all__ = ('CircuitMaintenance',)


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
        ordering = ['start_time']
        indexes = (models.Index(fields=('start_time',)),)  # Default ordering
        verbose_name = _('circuit maintenance')
        verbose_name_plural = _('circuit maintenances')

    def __str__(self):
        return self.name

    @property
    def docs_url(self):
        # The inherited URL points at a NetBox core docs page that doesn't exist; None hides the edit form's Help button
        return None

    def get_status_color(self):
        return CircuitMaintenanceStatusChoices.colors.get(self.status)

    def clean(self):
        super().clean()

        # Either time is None here when its form field failed validation (Django runs clean() regardless). The error is
        # non-field: a CSV update row without an end_time column has no end_time form field to attach it to.
        if self.start_time and self.end_time and self.end_time < self.start_time:
            raise ValidationError(_('End time should be greater than start time.'))
