import django_filters
from circuits.models import Circuit, Provider
from django.db.models import Q
from django.utils.translation import gettext as _
from netbox.filtersets import NetBoxModelFilterSet, PrimaryModelFilterSet
from utilities.filters import MultiValueCharFilter
from utilities.filtersets import register_filterset

from .choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
from .models import CircuitImpact, CircuitMaintenance, Note, NotificationSource, ParsedNotification, RawNotification

__all__ = (
    'CircuitImpactFilterSet',
    'CircuitMaintenanceFilterSet',
    'NoteFilterSet',
    'NotificationSourceFilterSet',
    'ParsedNotificationFilterSet',
    'RawNotificationFilterSet',
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


@register_filterset
class NoteFilterSet(NetBoxModelFilterSet):
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
    level = django_filters.MultipleChoiceFilter(
        choices=NoteLevelChoices,
        distinct=False,
        null_value=None,
    )
    # A TextField would otherwise get no filter
    comment = MultiValueCharFilter()

    class Meta:
        model = Note
        fields = ('id', 'title', 'comment')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(title__icontains=value) | Q(comment__icontains=value))


@register_filterset
class NotificationSourceFilterSet(NetBoxModelFilterSet):
    provider_id = django_filters.ModelMultipleChoiceFilter(
        field_name='providers',
        queryset=Provider.objects.all(),
        label=_('Provider (ID)'),
    )
    provider = django_filters.ModelMultipleChoiceFilter(
        field_name='providers__slug',
        queryset=Provider.objects.all(),
        to_field_name='slug',
        label=_('Provider (slug)'),
    )

    class Meta:
        model = NotificationSource
        # The OAuth token is never filterable
        fields = ('id', 'name', 'attach_all_providers')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(name__icontains=value)


@register_filterset
class RawNotificationFilterSet(NetBoxModelFilterSet):
    provider_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Provider.objects.all(),
        distinct=False,
        label=_('Provider (ID)'),
    )
    provider = django_filters.ModelMultipleChoiceFilter(
        field_name='provider__slug',
        queryset=Provider.objects.all(),
        distinct=False,
        to_field_name='slug',
        label=_('Provider (slug)'),
    )
    source_id = django_filters.ModelMultipleChoiceFilter(
        queryset=NotificationSource.objects.all(),
        distinct=False,
        label=_('Notification source (ID)'),
    )
    source = django_filters.ModelMultipleChoiceFilter(
        field_name='source__name',
        queryset=NotificationSource.objects.all(),
        distinct=False,
        to_field_name='name',
        label=_('Notification source (name)'),
    )
    # Nautobot's name for stamp__gte, kept for API clients
    since = django_filters.DateTimeFilter(
        field_name='stamp',
        lookup_expr='gte',
        label=_('Since'),
    )

    class Meta:
        model = RawNotification
        fields = ('id', 'subject', 'sender', 'parsed', 'stamp')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(subject__icontains=value)


@register_filterset
class ParsedNotificationFilterSet(NetBoxModelFilterSet):
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
    raw_notification_id = django_filters.ModelMultipleChoiceFilter(
        queryset=RawNotification.objects.all(),
        distinct=False,
        label=_('Raw notification (ID)'),
    )

    class Meta:
        model = ParsedNotification
        # json can't be filtered with django-filter
        fields = ('id',)

    def search(self, queryset, name, value):
        # Nautobot ran icontains on the raw_notification foreign key itself (Decisions 2026-09-29)
        if not value.strip():
            return queryset
        return queryset.filter(raw_notification__subject__icontains=value)
