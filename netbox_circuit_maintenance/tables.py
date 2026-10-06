import django_tables2 as tables
from django.utils.translation import gettext_lazy as _
from netbox.tables import PrimaryModelTable, columns

from .models import CircuitMaintenance

__all__ = ('CircuitMaintenanceTable',)


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
