import strawberry

from ..choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices

__all__ = (
    'CircuitImpactEnum',
    'CircuitMaintenanceStatusEnum',
    'NoteLevelEnum',
)


# Enum keys replace the hyphens in the stored values, e.g. STATUS_IN_PROCESS for 'IN-PROCESS'
CircuitImpactEnum = strawberry.enum(CircuitImpactChoices.as_enum(prefix='impact'))
CircuitMaintenanceStatusEnum = strawberry.enum(CircuitMaintenanceStatusChoices.as_enum(prefix='status'))
NoteLevelEnum = strawberry.enum(NoteLevelChoices.as_enum(prefix='level'))
