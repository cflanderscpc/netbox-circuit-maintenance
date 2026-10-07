import json

import django_tables2 as tables
from django.utils.translation import gettext_lazy as _
from netbox.tables import NetBoxTable, PrimaryModelTable, columns

from .models import CircuitImpact, CircuitMaintenance, Note, ParsedNotification, RawNotification

__all__ = (
    'CircuitImpactTable',
    'CircuitMaintenanceTable',
    'NoteTable',
    'ParsedNotificationTable',
    'RawNotificationTable',
)


class CircuitMaintenanceTable(PrimaryModelTable):
    name = tables.Column(
        verbose_name=_('Name'),
        linkify=True,
    )
    status = columns.ChoiceFieldColumn(
        verbose_name=_('Status'),
    )
    ack = columns.BooleanColumn(
        verbose_name=_('Acknowledged'),
    )
    # Model properties derived through the maintenance's impacts: not sortable, and one query per row each
    providers = columns.ManyToManyColumn(
        verbose_name=_('Providers'),
        linkify_item=True,
        orderable=False,
    )
    circuits = columns.ManyToManyColumn(
        verbose_name=_('Circuits'),
        linkify_item=True,
        orderable=False,
    )
    tags = columns.TagColumn(
        url_name='plugins:netbox_circuit_maintenance:circuitmaintenance_list',
    )

    class Meta(PrimaryModelTable.Meta):
        model = CircuitMaintenance
        fields = (
            'pk',
            'id',
            'ack',
            'name',
            'status',
            'providers',
            'circuits',
            'start_time',
            'end_time',
            'description',
            'comments',
            'tags',
            'created',
            'last_updated',
        )
        default_columns = ('pk', 'ack', 'name', 'status', 'providers', 'circuits', 'start_time', 'end_time')


class CircuitImpactTable(NetBoxTable):
    maintenance = tables.Column(
        verbose_name=_('Maintenance'),
        linkify=True,
    )
    provider = tables.Column(
        accessor='circuit__provider',
        verbose_name=_('Provider'),
        linkify=True,
    )
    circuit = tables.Column(
        verbose_name=_('Circuit'),
        linkify=True,
    )
    # Links to the impact itself, which has no name column
    impact = columns.ChoiceFieldColumn(
        verbose_name=_('Impact'),
        linkify=True,
    )
    # Details of the impacted circuit, shown on the maintenance's page
    circuit_type = columns.ColoredLabelColumn(
        accessor='circuit__type',
        verbose_name=_('Circuit type'),
    )
    circuit_status = columns.ChoiceFieldColumn(
        accessor='circuit__status',
        verbose_name=_('Circuit status'),
        color=lambda record: record.circuit.get_status_color(),
    )
    termination_a = tables.Column(
        accessor='circuit__termination_a__termination',
        verbose_name=_('Side A'),
        linkify=True,
        orderable=False,
    )
    termination_z = tables.Column(
        accessor='circuit__termination_z__termination',
        verbose_name=_('Side Z'),
        linkify=True,
        orderable=False,
    )
    circuit_description = tables.Column(
        accessor='circuit__description',
        verbose_name=_('Circuit description'),
    )
    tags = columns.TagColumn(
        url_name='plugins:netbox_circuit_maintenance:circuitimpact_list',
    )

    class Meta(NetBoxTable.Meta):
        model = CircuitImpact
        fields = (
            'pk',
            'id',
            'maintenance',
            'provider',
            'circuit',
            'impact',
            'circuit_type',
            'circuit_status',
            'termination_a',
            'termination_z',
            'circuit_description',
            'tags',
            'created',
            'last_updated',
            'actions',
        )
        # Also sets the column order on the maintenance's Circuits panel: include_columns shows columns, it doesn't move them
        default_columns = ('pk', 'maintenance', 'circuit', 'impact', 'provider')


class NoteTable(NetBoxTable):
    maintenance = tables.Column(
        verbose_name=_('Maintenance'),
        linkify=True,
    )
    level = columns.ChoiceFieldColumn(
        verbose_name=_('Level'),
    )
    title = tables.Column(
        verbose_name=_('Title'),
        linkify=True,
    )
    comment = columns.MarkdownColumn(
        verbose_name=_('Comment'),
    )
    tags = columns.TagColumn(
        url_name='plugins:netbox_circuit_maintenance:note_list',
    )

    class Meta(NetBoxTable.Meta):
        model = Note
        fields = (
            'pk',
            'id',
            'maintenance',
            'level',
            'title',
            'comment',
            'tags',
            'created',
            'last_updated',
            'actions',
        )
        # The maintenance's Notes panel order, which also sets the list's (include_columns doesn't move columns)
        default_columns = ('pk', 'maintenance', 'level', 'title', 'last_updated', 'comment')


class RawNotificationTable(NetBoxTable):
    subject = tables.Column(
        verbose_name=_('Subject'),
        linkify=True,
    )
    provider = tables.Column(
        verbose_name=_('Provider'),
        linkify=True,
    )
    # Not linked until NotificationSource has a detail view
    source = tables.Column(
        verbose_name=_('Source'),
    )
    parsed = columns.BooleanColumn(
        verbose_name=_('Parsed'),
    )
    tags = columns.TagColumn(
        url_name='plugins:netbox_circuit_maintenance:rawnotification_list',
    )
    # Raw notifications can't be edited
    actions = columns.ActionsColumn(
        actions=('delete', 'changelog'),
    )

    class Meta(NetBoxTable.Meta):
        model = RawNotification
        fields = (
            'pk',
            'id',
            'subject',
            'provider',
            'sender',
            'source',
            'parsed',
            'stamp',
            'tags',
            'created',
            'last_updated',
            'actions',
        )
        default_columns = ('pk', 'subject', 'provider', 'sender', 'source', 'parsed', 'stamp')


class ParsedNotificationTable(NetBoxTable):
    maintenance = tables.Column(
        verbose_name=_('Maintenance'),
        linkify=True,
    )
    # The raw notification's subject
    raw_notification = tables.Column(
        verbose_name=_('Raw notification'),
        linkify=True,
    )
    provider = tables.Column(
        accessor='raw_notification__provider',
        verbose_name=_('Provider'),
        linkify=True,
    )
    stamp = columns.DateTimeColumn(
        accessor='raw_notification__stamp',
        verbose_name=_('Stamp'),
    )
    json = columns.TemplateColumn(
        template_code='<pre class="mb-0">{{ value|json }}</pre>',
        verbose_name=_('JSON'),
        orderable=False,
    )
    tags = columns.TagColumn(
        url_name='plugins:netbox_circuit_maintenance:parsednotification_list',
    )
    # Parsed notifications can't be edited or deleted
    actions = columns.ActionsColumn(
        actions=('changelog',),
    )

    class Meta(NetBoxTable.Meta):
        model = ParsedNotification
        fields = (
            'pk',
            'id',
            'maintenance',
            'raw_notification',
            'provider',
            'stamp',
            'json',
            'tags',
            'created',
            'last_updated',
            'actions',
        )
        # id links to the parsed notification itself
        default_columns = ('pk', 'id', 'maintenance', 'raw_notification', 'provider', 'stamp')

    def value_json(self, value):
        # Exported as JSON, not the rendered (escaped) template
        return json.dumps(value)
