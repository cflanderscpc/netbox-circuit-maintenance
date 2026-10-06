from netbox.api.viewsets import NetBoxModelViewSet

from .. import filtersets
from ..models import CircuitMaintenance
from . import serializers


class CircuitMaintenanceViewSet(NetBoxModelViewSet):
    queryset = CircuitMaintenance.objects.all()
    serializer_class = serializers.CircuitMaintenanceSerializer
    filterset_class = filtersets.CircuitMaintenanceFilterSet
