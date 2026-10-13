from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from django.db.models import F
from netbox.graphql.types import NetBoxObjectType, PrimaryObjectType

from .. import models
from .filters import (
    CircuitImpactFilter,
    CircuitMaintenanceFilter,
    NoteFilter,
    NotificationSourceFilter,
    ParsedNotificationFilter,
    RawNotificationFilter,
)

if TYPE_CHECKING:
    from circuits.graphql.types import CircuitType, ProviderType

__all__ = (
    'CircuitImpactType',
    'CircuitMaintenanceType',
    'NoteType',
    'NotificationSourceType',
    'ParsedNotificationType',
    'RawNotificationType',
)


@strawberry_django.type(
    models.CircuitMaintenance,
    fields='__all__',
    filters=CircuitMaintenanceFilter,
    pagination=True,
)
class CircuitMaintenanceType(PrimaryObjectType):
    impacts: list[Annotated['CircuitImpactType', strawberry.lazy('netbox_circuit_maintenance.graphql.types')]]
    notes: list[Annotated['NoteType', strawberry.lazy('netbox_circuit_maintenance.graphql.types')]]
    parsed_notifications: list[
        Annotated['ParsedNotificationType', strawberry.lazy('netbox_circuit_maintenance.graphql.types')]
    ]


@strawberry_django.type(
    models.CircuitImpact,
    fields='__all__',
    filters=CircuitImpactFilter,
    pagination=True,
)
class CircuitImpactType(NetBoxObjectType):
    maintenance: Annotated['CircuitMaintenanceType', strawberry.lazy('netbox_circuit_maintenance.graphql.types')]
    circuit: Annotated['CircuitType', strawberry.lazy('circuits.graphql.types')]


@strawberry_django.type(
    models.Note,
    fields='__all__',
    filters=NoteFilter,
    pagination=True,
)
class NoteType(NetBoxObjectType):
    maintenance: Annotated['CircuitMaintenanceType', strawberry.lazy('netbox_circuit_maintenance.graphql.types')]


@strawberry_django.type(
    models.NotificationSource,
    # The OAuth token stays out of GraphQL, as out of REST and the change log
    exclude=['_token'],
    filters=NotificationSourceFilter,
    pagination=True,
)
class NotificationSourceType(NetBoxObjectType):
    providers: list[Annotated['ProviderType', strawberry.lazy('circuits.graphql.types')]]
    raw_notifications: list[
        Annotated['RawNotificationType', strawberry.lazy('netbox_circuit_maintenance.graphql.types')]
    ]


@strawberry_django.type(
    models.RawNotification,
    # strawberry_django can't map a BinaryField
    exclude=['raw'],
    filters=RawNotificationFilter,
    pagination=True,
)
class RawNotificationType(NetBoxObjectType):
    provider: Annotated['ProviderType', strawberry.lazy('circuits.graphql.types')]
    source: Annotated['NotificationSourceType', strawberry.lazy('netbox_circuit_maintenance.graphql.types')] | None
    parsed_notifications: list[
        Annotated['ParsedNotificationType', strawberry.lazy('netbox_circuit_maintenance.graphql.types')]
    ]


@strawberry_django.type(
    models.ParsedNotification,
    fields='__all__',
    filters=ParsedNotificationFilter,
    pagination=True,
)
class ParsedNotificationType(NetBoxObjectType):
    maintenance: Annotated['CircuitMaintenanceType', strawberry.lazy('netbox_circuit_maintenance.graphql.types')]
    raw_notification: Annotated['RawNotificationType', strawberry.lazy('netbox_circuit_maintenance.graphql.types')]

    # str(self) would load each row's raw notification, body included, in queries of its own. An only= hint on the
    # raw notification would replace the fields a nested raw_notification selection asks for, so annotate instead
    @strawberry_django.field(annotate={'raw_notification_subject': F('raw_notification__subject')})
    def display(self) -> str:
        # ParsedNotification.__str__ from the annotation
        return f'Parsed notification for {self.raw_notification_subject}'[:200]
