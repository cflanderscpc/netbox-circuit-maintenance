from netbox.api.fields import ChoiceField
from netbox.api.serializers import PrimaryModelSerializer

from ..choices import CircuitMaintenanceStatusChoices
from ..models import CircuitMaintenance

__all__ = ('CircuitMaintenanceSerializer',)


class CircuitMaintenanceSerializer(PrimaryModelSerializer):
    status = ChoiceField(
        choices=CircuitMaintenanceStatusChoices,
        required=False,
    )

    class Meta:
        model = CircuitMaintenance
        fields = (
            'id',
            'url',
            'display_url',
            'display',
            'name',
            'status',
            'start_time',
            'end_time',
            'ack',
            'description',
            'owner',
            'comments',
            'tags',
            'custom_fields',
            'created',
            'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'start_time', 'end_time', 'description')
