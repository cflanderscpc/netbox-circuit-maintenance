from django.utils.translation import gettext_lazy as _
from netbox.ui import attrs, panels


class NotifyingProviderAttr(attrs.RelatedObjectAttr):
    """
    The provider of the maintenance's first parsed notification (parsed notifications are ordered by last update).
    """

    def get_value(self, obj):
        parsed_notification = obj.parsed_notifications.select_related('raw_notification__provider').first()
        return parsed_notification.raw_notification.provider if parsed_notification else None


class CircuitMaintenancePanel(panels.ObjectAttributesPanel):
    name = attrs.TextAttr('name')
    status = attrs.ChoiceAttr('status')
    start_time = attrs.DateTimeAttr('start_time')
    end_time = attrs.DateTimeAttr('end_time')
    ack = attrs.BooleanAttr('ack', label=_('Acknowledged'))
    notifying_provider = NotifyingProviderAttr('parsed_notifications', linkify=True, label=_('Notifying provider'))
    description = attrs.TextAttr('description')


class CircuitImpactPanel(panels.ObjectAttributesPanel):
    maintenance = attrs.RelatedObjectAttr('maintenance', linkify=True)
    provider = attrs.RelatedObjectAttr('circuit.provider', linkify=True)
    circuit = attrs.RelatedObjectAttr('circuit', linkify=True)
    impact = attrs.ChoiceAttr('impact')
