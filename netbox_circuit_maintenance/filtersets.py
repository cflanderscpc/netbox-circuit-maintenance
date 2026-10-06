import django_filters
from circuits.models import Circuit, Provider
from django.db.models import Q
from django.utils.translation import gettext as _
from netbox.filtersets import PrimaryModelFilterSet
from utilities.filters import MultiValueCharFilter
from utilities.filtersets import register_filterset

from .choices import CircuitMaintenanceStatusChoices
from .models import CircuitMaintenance

__all__ = ('CircuitMaintenanceFilterSet',)


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
