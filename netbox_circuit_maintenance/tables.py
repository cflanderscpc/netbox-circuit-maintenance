import django_tables2 as tables
from netbox.tables import ChoiceFieldColumn, NetBoxTable, PrimaryModelTable, OrganizationalModelTable, columns

from .models import CircuitMaintenance, MaintenanceImpact, MaintenanceNote, NotificationSource, RawNotification


class CircuitMaintenanceTable(PrimaryModelTable):
    name = tables.Column(linkify=True)

    class Meta(PrimaryModelTable.Meta):
        model = CircuitMaintenance
        fields = (
            'pk',
            'id',
            'name',
            'circuit',
            'provider',
            'start_time',
            'end_time',
            'status',
            'description',
            'ack',
            'actions',
        )
        default_columns = ('name', 'circuit', 'provider', 'start_time', 'end_time', 'status', 'actions')
