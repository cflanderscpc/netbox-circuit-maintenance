from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.filters import NetBoxModelFilter, PrimaryModelFilter
from strawberry.scalars import ID
from strawberry_django import BaseFilterLookup, DatetimeFilterLookup, FilterLookup, StrFilterLookup

from .. import models

if TYPE_CHECKING:
    from circuits.graphql.filters import CircuitFilter, ProviderFilter
    from netbox.graphql.filter_lookups import JSONFilter

    from .enums import CircuitImpactEnum, CircuitMaintenanceStatusEnum, NoteLevelEnum

__all__ = (
    'CircuitImpactFilter',
    'CircuitMaintenanceFilter',
    'NoteFilter',
    'NotificationSourceFilter',
    'ParsedNotificationFilter',
    'RawNotificationFilter',
)


@strawberry_django.filter_type(models.CircuitMaintenance, lookups=True)
class CircuitMaintenanceFilter(PrimaryModelFilter):
    name: StrFilterLookup | None = strawberry_django.filter_field()
    start_time: DatetimeFilterLookup | None = strawberry_django.filter_field()
    end_time: DatetimeFilterLookup | None = strawberry_django.filter_field()
    status: (
        BaseFilterLookup[
            Annotated['CircuitMaintenanceStatusEnum', strawberry.lazy('netbox_circuit_maintenance.graphql.enums')]
        ]
        | None
    ) = strawberry_django.filter_field()
    ack: FilterLookup[bool] | None = strawberry_django.filter_field()
    # A maintenance reaches circuits and providers only through its impacts
    impacts: Annotated['CircuitImpactFilter', strawberry.lazy('netbox_circuit_maintenance.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )


# GraphQL names start with CircuitMaintenance, as in types.py
@strawberry_django.filter_type(models.CircuitImpact, lookups=True, name='CircuitMaintenanceCircuitImpactFilter')
class CircuitImpactFilter(NetBoxModelFilter):
    maintenance: (
        Annotated['CircuitMaintenanceFilter', strawberry.lazy('netbox_circuit_maintenance.graphql.filters')] | None
    ) = strawberry_django.filter_field()
    maintenance_id: ID | None = strawberry_django.filter_field()
    circuit: Annotated['CircuitFilter', strawberry.lazy('circuits.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
    circuit_id: ID | None = strawberry_django.filter_field()
    impact: (
        BaseFilterLookup[Annotated['CircuitImpactEnum', strawberry.lazy('netbox_circuit_maintenance.graphql.enums')]]
        | None
    ) = strawberry_django.filter_field()


@strawberry_django.filter_type(models.Note, lookups=True, name='CircuitMaintenanceNoteFilter')
class NoteFilter(NetBoxModelFilter):
    maintenance: (
        Annotated['CircuitMaintenanceFilter', strawberry.lazy('netbox_circuit_maintenance.graphql.filters')] | None
    ) = strawberry_django.filter_field()
    maintenance_id: ID | None = strawberry_django.filter_field()
    title: StrFilterLookup | None = strawberry_django.filter_field()
    level: (
        BaseFilterLookup[Annotated['NoteLevelEnum', strawberry.lazy('netbox_circuit_maintenance.graphql.enums')]] | None
    ) = strawberry_django.filter_field()
    comment: StrFilterLookup | None = strawberry_django.filter_field()


@strawberry_django.filter_type(
    models.NotificationSource, lookups=True, name='CircuitMaintenanceNotificationSourceFilter'
)
class NotificationSourceFilter(NetBoxModelFilter):
    name: StrFilterLookup | None = strawberry_django.filter_field()
    providers: Annotated['ProviderFilter', strawberry.lazy('circuits.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
    attach_all_providers: FilterLookup[bool] | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.RawNotification, lookups=True, name='CircuitMaintenanceRawNotificationFilter')
class RawNotificationFilter(NetBoxModelFilter):
    # No filter on raw, which the GraphQL type leaves out
    subject: StrFilterLookup | None = strawberry_django.filter_field()
    provider: Annotated['ProviderFilter', strawberry.lazy('circuits.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
    provider_id: ID | None = strawberry_django.filter_field()
    sender: StrFilterLookup | None = strawberry_django.filter_field()
    source: (
        Annotated['NotificationSourceFilter', strawberry.lazy('netbox_circuit_maintenance.graphql.filters')] | None
    ) = strawberry_django.filter_field()
    source_id: ID | None = strawberry_django.filter_field()
    parsed: FilterLookup[bool] | None = strawberry_django.filter_field()
    stamp: DatetimeFilterLookup | None = strawberry_django.filter_field()


@strawberry_django.filter_type(
    models.ParsedNotification, lookups=True, name='CircuitMaintenanceParsedNotificationFilter'
)
class ParsedNotificationFilter(NetBoxModelFilter):
    maintenance: (
        Annotated['CircuitMaintenanceFilter', strawberry.lazy('netbox_circuit_maintenance.graphql.filters')] | None
    ) = strawberry_django.filter_field()
    maintenance_id: ID | None = strawberry_django.filter_field()
    raw_notification: (
        Annotated['RawNotificationFilter', strawberry.lazy('netbox_circuit_maintenance.graphql.filters')] | None
    ) = strawberry_django.filter_field()
    raw_notification_id: ID | None = strawberry_django.filter_field()
    json: Annotated['JSONFilter', strawberry.lazy('netbox.graphql.filter_lookups')] | None = (
        strawberry_django.filter_field()
    )
