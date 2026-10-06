from netbox.api.viewsets import NetBoxModelViewSet

from .. import filtersets
from ..models import CircuitImpact, CircuitMaintenance
from . import serializers


class CircuitMaintenanceViewSet(NetBoxModelViewSet):
    queryset = CircuitMaintenance.objects.all()
    serializer_class = serializers.CircuitMaintenanceSerializer
    filterset_class = filtersets.CircuitMaintenanceFilterSet


class CircuitImpactViewSet(NetBoxModelViewSet):
    queryset = CircuitImpact.objects.all()
    serializer_class = serializers.CircuitImpactSerializer
    filterset_class = filtersets.CircuitImpactFilterSet
