import strawberry

from ..choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices

__all__ = (
    'CircuitImpactEnum',
    'CircuitMaintenanceStatusEnum',
    'NoteLevelEnum',
)


# Enum keys replace the hyphens in the stored values, e.g. STATUS_IN_PROCESS for 'IN-PROCESS'. GraphQL names start
# with CircuitMaintenance, as in types.py. The name goes to as_enum() rather than strawberry.enum(), because generated
# filter inputs such as CircuitMaintenanceNoteLevelEnumBaseFilterLookup are named after the Enum class
CircuitImpactEnum = strawberry.enum(
    CircuitImpactChoices.as_enum(name='CircuitMaintenanceCircuitImpactEnum', prefix='impact')
)
CircuitMaintenanceStatusEnum = strawberry.enum(CircuitMaintenanceStatusChoices.as_enum(prefix='status'))
NoteLevelEnum = strawberry.enum(NoteLevelChoices.as_enum(name='CircuitMaintenanceNoteLevelEnum', prefix='level'))
