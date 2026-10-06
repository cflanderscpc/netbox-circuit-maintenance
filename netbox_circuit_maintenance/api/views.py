from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet

from .. import filtersets
from ..models import CircuitImpact, CircuitMaintenance, Note, RawNotification
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
