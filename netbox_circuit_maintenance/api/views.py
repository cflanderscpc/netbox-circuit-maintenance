from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet

from .. import filtersets
from ..models import CircuitImpact, CircuitMaintenance, Note, ParsedNotification, RawNotification
from . import serializers


class CircuitMaintenanceViewSet(NetBoxModelViewSet):
    queryset = CircuitMaintenance.objects.all()
    serializer_class = serializers.CircuitMaintenanceSerializer
    filterset_class = filtersets.CircuitMaintenanceFilterSet


class CircuitImpactViewSet(NetBoxModelViewSet):
    queryset = CircuitImpact.objects.all()
    serializer_class = serializers.CircuitImpactSerializer
    filterset_class = filtersets.CircuitImpactFilterSet


class NoteViewSet(NetBoxModelViewSet):
    queryset = Note.objects.all()
    serializer_class = serializers.NoteSerializer
    filterset_class = filtersets.NoteFilterSet


class RawNotificationViewSet(NetBoxReadOnlyModelViewSet):
    queryset = RawNotification.objects.all()
    serializer_class = serializers.RawNotificationSerializer
    filterset_class = filtersets.RawNotificationFilterSet


class ParsedNotificationViewSet(NetBoxReadOnlyModelViewSet):
    # The nested raw notification leaves out its body (up to 16 KB per row). The dynamic prefetch skips the cached
    # relation, so it doesn't load the body either
    queryset = ParsedNotification.objects.select_related('raw_notification').defer('raw_notification__raw')
    serializer_class = serializers.ParsedNotificationSerializer
    filterset_class = filtersets.ParsedNotificationFilterSet
