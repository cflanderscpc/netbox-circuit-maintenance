import django_filters
from circuits.models import Circuit, Provider
from django.db.models import Q
from django.utils.translation import gettext as _
from netbox.filtersets import NetBoxModelFilterSet, PrimaryModelFilterSet
from utilities.filters import MultiValueCharFilter
from utilities.filtersets import register_filterset

from .choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices
from .models import CircuitImpact, CircuitMaintenance

__all__ = (
    'CircuitImpactFilterSet',
    'CircuitMaintenanceFilterSet',
)


@register_filterset
class CircuitMaintenanceFilterSet(PrimaryModelFilterSet):
    # Multi-valued like other NetBox description filters; a TextField would otherwise get a single-value CharFilter
    description = MultiValueCharFilter()
    status = django_filters.MultipleChoiceFilter(
        choices=CircuitMaintenanceStatusChoices,
        distinct=False,
        null_value=None,
    )
    # A maintenance reaches circuits and providers only through its impacts
    provider_id = django_filters.ModelMultipleChoiceFilter(
        field_name='impacts__circuit__provider',
        queryset=Provider.objects.all(),
        label=_('Provider (ID)'),
    )
    provider = django_filters.ModelMultipleChoiceFilter(
        field_name='impacts__circuit__provider__slug',
        queryset=Provider.objects.all(),
        to_field_name='slug',
        label=_('Provider (slug)'),
    )
    circuit_id = django_filters.ModelMultipleChoiceFilter(
        field_name='impacts__circuit',
        queryset=Circuit.objects.all(),
        label=_('Circuit (ID)'),
    )
    circuit = django_filters.ModelMultipleChoiceFilter(
        field_name='impacts__circuit__cid',
        queryset=Circuit.objects.all(),
        to_field_name='cid',
        label=_('Circuit (CID)'),
    )

    class Meta:
        model = CircuitMaintenance
        fields = ('id', 'name', 'start_time', 'end_time', 'description', 'ack')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) | Q(description__icontains=value) | Q(comments__icontains=value)
        )


@register_filterset
class CircuitImpactFilterSet(NetBoxModelFilterSet):
    maintenance_id = django_filters.ModelMultipleChoiceFilter(
        queryset=CircuitMaintenance.objects.all(),
        distinct=False,
        label=_('Maintenance (ID)'),
    )
    maintenance = django_filters.ModelMultipleChoiceFilter(
        field_name='maintenance__name',
        queryset=CircuitMaintenance.objects.all(),
        distinct=False,
        to_field_name='name',
        label=_('Maintenance (name)'),
    )
    circuit_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Circuit.objects.all(),
        distinct=False,
        label=_('Circuit (ID)'),
    )
    circuit = django_filters.ModelMultipleChoiceFilter(
        field_name='circuit__cid',
        queryset=Circuit.objects.all(),
        distinct=False,
        to_field_name='cid',
        label=_('Circuit (CID)'),
    )
    impact = django_filters.MultipleChoiceFilter(
        choices=CircuitImpactChoices,
        distinct=False,
        null_value=None,
    )

    class Meta:
        model = CircuitImpact
        fields = ('id',)

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(maintenance__name__icontains=value) | Q(circuit__cid__icontains=value))
