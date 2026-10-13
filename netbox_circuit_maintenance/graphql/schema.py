import strawberry
import strawberry_django

from .types import (
    CircuitImpactType,
    CircuitMaintenanceType,
    NoteType,
    NotificationSourceType,
    ParsedNotificationType,
    RawNotificationType,
)


@strawberry.type(name='Query')
class CircuitMaintenanceQuery:
    circuit_maintenance: CircuitMaintenanceType = strawberry_django.field()
    circuit_maintenance_list: list[CircuitMaintenanceType] = strawberry_django.field()

    circuit_impact: CircuitImpactType = strawberry_django.field()
    circuit_impact_list: list[CircuitImpactType] = strawberry_django.field()

    note: NoteType = strawberry_django.field()
    note_list: list[NoteType] = strawberry_django.field()

    notification_source: NotificationSourceType = strawberry_django.field()
    notification_source_list: list[NotificationSourceType] = strawberry_django.field()

    raw_notification: RawNotificationType = strawberry_django.field()
    raw_notification_list: list[RawNotificationType] = strawberry_django.field()

    parsed_notification: ParsedNotificationType = strawberry_django.field()
    parsed_notification_list: list[ParsedNotificationType] = strawberry_django.field()


# NetBox adds each query class in this list to its root Query type
schema = [CircuitMaintenanceQuery]
