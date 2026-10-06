from circuits.api.serializers import CircuitSerializer, ProviderSerializer
from netbox.api.fields import ChoiceField
from netbox.api.serializers import NetBoxModelSerializer, PrimaryModelSerializer
from rest_framework import serializers

from ..choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
from ..models import CircuitImpact, CircuitMaintenance, Note, RawNotification

__all__ = (
    'CircuitImpactSerializer',
    'CircuitMaintenanceSerializer',
    'NoteSerializer',
    'RawNotificationSerializer',
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


class NoteSerializer(NetBoxModelSerializer):
    maintenance = CircuitMaintenanceSerializer(nested=True)
    level = ChoiceField(
        choices=NoteLevelChoices,
        required=False,
    )

    class Meta:
        model = Note
        fields = (
            'id',
            'url',
            'display_url',
            'display',
            'maintenance',
            'title',
            'level',
            'comment',
            'tags',
            'custom_fields',
            'created',
            'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'maintenance', 'title', 'level')


class RawNotificationSerializer(NetBoxModelSerializer):
    provider = ProviderSerializer(nested=True, read_only=True)
    # The source's ID until NotificationSource has a serializer to nest
    source = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = RawNotification
        # raw (a non-editable BinaryField) is read-only base64
        fields = (
            'id',
            'url',
            'display_url',
            'display',
            'subject',
            'provider',
            'sender',
            'source',
            'parsed',
            'stamp',
            'raw',
            'tags',
            'custom_fields',
            'created',
            'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'provider', 'subject', 'stamp')
