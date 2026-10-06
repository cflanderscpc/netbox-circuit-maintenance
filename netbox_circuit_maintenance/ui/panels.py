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


class NotePanel(panels.ObjectAttributesPanel):
    maintenance = attrs.RelatedObjectAttr('maintenance', linkify=True)
    title = attrs.TextAttr('title')
    level = attrs.ChoiceAttr('level')


class ParsedMaintenanceAttr(attrs.RelatedObjectAttr):
    """
    The maintenance of a parsed raw notification's most recently updated parsed notification.
    """

    def get_value(self, obj):
        if not obj.parsed:
            return None
        parsed_notification = obj.parsed_notifications.select_related('maintenance').last()
        return parsed_notification.maintenance if parsed_notification else None


class RawNotificationPanel(panels.ObjectAttributesPanel):
    subject = attrs.TextAttr('subject')
    provider = attrs.RelatedObjectAttr('provider', linkify=True)
    sender = attrs.TextAttr('sender')
    # Not linked until NotificationSource has a detail view
    source = attrs.RelatedObjectAttr('source')
    parsed = attrs.BooleanAttr('parsed')
    maintenance = ParsedMaintenanceAttr('parsed_notifications', linkify=True, label=_('Maintenance'))
    stamp = attrs.DateTimeAttr('stamp')


class RawContentPanel(panels.TextCodePanel):
    """
    The stored notification decoded as UTF-8. Undecodable bytes, such as a character cut in two by the size cap, show
    as U+FFFD instead of hiding the whole body.
    """

    def get_context(self, context):
        ctx = super().get_context(context)
        # bytes() also accepts a memoryview
        return {**ctx, 'value': bytes(ctx['value'] or b'').decode('utf-8', errors='replace')}
