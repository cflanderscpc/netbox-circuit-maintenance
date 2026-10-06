from circuits.api.serializers import CircuitSerializer
from netbox.api.fields import ChoiceField
from netbox.api.serializers import NetBoxModelSerializer, PrimaryModelSerializer

from ..choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices
from ..models import CircuitImpact, CircuitMaintenance

__all__ = (
    'CircuitImpactSerializer',
    'CircuitMaintenanceSerializer',
)


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


class CircuitImpactSerializer(NetBoxModelSerializer):
    maintenance = CircuitMaintenanceSerializer(nested=True)
    circuit = CircuitSerializer(nested=True)
    impact = ChoiceField(
        choices=CircuitImpactChoices,
        required=False,
    )

    class Meta:
        model = CircuitImpact
        fields = (
            'id',
            'url',
            'display_url',
            'display',
            'maintenance',
            'circuit',
            'impact',
            'tags',
            'custom_fields',
            'created',
            'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'maintenance', 'circuit', 'impact')
