from circuits.api.serializers import CircuitSerializer, ProviderSerializer
from netbox.api.fields import ChoiceField
from netbox.api.serializers import NetBoxModelSerializer, PrimaryModelSerializer

from ..choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
from ..models import CircuitImpact, CircuitMaintenance, Note, NotificationSource, ParsedNotification, RawNotification

__all__ = (
    'CircuitImpactSerializer',
    'CircuitMaintenanceSerializer',
    'NoteSerializer',
    'NotificationSourceSerializer',
    'ParsedNotificationSerializer',
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


class NotificationSourceSerializer(NetBoxModelSerializer):
    providers = ProviderSerializer(nested=True, many=True, read_only=True)

    class Meta:
        model = NotificationSource
        # The OAuth token (_token) is never exposed
        fields = (
            'id',
            'url',
            'display_url',
            'display',
            'name',
            'providers',
            'attach_all_providers',
            'tags',
            'custom_fields',
            'created',
            'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name')


class RawNotificationSerializer(NetBoxModelSerializer):
    provider = ProviderSerializer(nested=True, read_only=True)
    # Null once the source is deleted
    source = NotificationSourceSerializer(nested=True, read_only=True, allow_null=True)

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


class ParsedNotificationSerializer(NetBoxModelSerializer):
    maintenance = CircuitMaintenanceSerializer(nested=True, read_only=True)
    # Nested with RawNotificationSerializer's brief fields, which leave out the body
    raw_notification = RawNotificationSerializer(nested=True, read_only=True)

    class Meta:
        model = ParsedNotification
        # json is the parser output as an object; Nautobot returned it as a string (Decisions 2026-10-06)
        fields = (
            'id',
            'url',
            'display_url',
            'display',
            'maintenance',
            'raw_notification',
            'json',
            'tags',
            'custom_fields',
            'created',
            'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'maintenance', 'raw_notification')
