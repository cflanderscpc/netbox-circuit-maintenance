import logging

from django.utils.translation import gettext_lazy as _
from netbox.ui import actions, attrs, panels

from ..handle_notifications.sources import Source
from ..models import CircuitImpact, CircuitMaintenance

logger = logging.getLogger(__name__)


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


class NotificationSourcePanel(panels.ObjectAttributesPanel):
    name = attrs.TextAttr('name')
    providers = attrs.RelatedObjectListAttr('providers', linkify=True, label=_('Providers'))
    attach_all_providers = attrs.BooleanAttr('attach_all_providers')


class NotificationSourceConfigPanel(panels.ObjectPanel):
    """
    The mailbox that the plugin configuration sets up for a notification source: its type and account, or why it
    can't be set up.
    """

    template_name = 'netbox_circuit_maintenance/panels/notificationsource_config.html'
    title = _('Configuration')

    def get_context(self, context):
        ctx = super().get_context(context)
        try:
            source = Source.init(name=ctx['object'].name)
        except ValueError as exc:
            message = f'Failed to initialize source: {exc}'
            logger.warning(message, exc_info=True, extra={'object': ctx['object']})
            return {**ctx, 'source_type': None, 'account': None, 'error': message}
        return {**ctx, 'source_type': type(source).__name__, 'account': source.get_account_id(), 'error': None}


class LastParsedNotificationAttr(attrs.RelatedObjectAttr):
    """
    A parsed raw notification's most recently updated parsed notification, the one Nautobot's page linked.
    """

    def get_value(self, obj):
        if not obj.parsed:
            return None
        # The related manager sets raw_notification to obj; a select_related() copy would load the body again
        return obj.parsed_notifications.select_related('maintenance').last()


class ParsedMaintenanceAttr(LastParsedNotificationAttr):
    """
    The maintenance of a parsed raw notification's most recently updated parsed notification.
    """

    def get_value(self, obj):
        parsed_notification = super().get_value(obj)
        return parsed_notification.maintenance if parsed_notification else None


class RawNotificationPanel(panels.ObjectAttributesPanel):
    subject = attrs.TextAttr('subject')
    provider = attrs.RelatedObjectAttr('provider', linkify=True)
    sender = attrs.TextAttr('sender')
    source = attrs.RelatedObjectAttr('source', linkify=True)
    parsed = attrs.BooleanAttr('parsed')
    parsed_notification = LastParsedNotificationAttr(
        'parsed_notifications', linkify=True, label=_('Parsed notification')
    )
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


class ParsedNotificationPanel(panels.ObjectAttributesPanel):
    maintenance = attrs.RelatedObjectAttr('maintenance', linkify=True)
    raw_notification = attrs.RelatedObjectAttr('raw_notification', linkify=True)


class CircuitMaintenancesPanel(panels.ObjectPanel):
    """
    The latest maintenances impacting a core circuit, newest start first, with their impact on it. Rendered on the
    circuit's page by template_content.py; View All lists every maintenance of the circuit.
    """

    template_name = 'netbox_circuit_maintenance/panels/circuit_maintenances.html'
    title = _('Circuit Maintenances')
    max_items = 5
    actions = [
        actions.LinkAction(
            view_name='plugins:netbox_circuit_maintenance:circuitmaintenance_list',
            url_params={'circuit_id': lambda ctx: ctx['object'].pk},
            label=_('View All'),
            button_icon='arrow-right-thick',
            permissions=['netbox_circuit_maintenance.view_circuitmaintenance'],
        ),
    ]

    def get_context(self, context):
        ctx = super().get_context(context)
        user = ctx['request'].user
        # A row shows the maintenance as well as the impact, so it needs both view permissions
        impacts = (
            CircuitImpact.objects.restrict(user, 'view')
            .filter(circuit=ctx['object'], maintenance__in=CircuitMaintenance.objects.restrict(user, 'view'))
            .select_related('maintenance')
            .order_by('-maintenance__start_time', '-maintenance__pk')[: self.max_items]
        )
        return {**ctx, 'impacts': impacts}

    def should_render(self, context):
        return context['request'].user.has_perms(
            [
                'netbox_circuit_maintenance.view_circuitimpact',
                'netbox_circuit_maintenance.view_circuitmaintenance',
            ]
        )
