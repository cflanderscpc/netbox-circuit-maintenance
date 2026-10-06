import django_tables2 as tables
from django.utils.translation import gettext_lazy as _
from netbox.tables import NetBoxTable, PrimaryModelTable, columns

from .models import CircuitImpact, CircuitMaintenance

__all__ = (
    'CircuitImpactTable',
    'CircuitMaintenanceTable',
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
