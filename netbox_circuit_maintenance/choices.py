from django.utils.translation import gettext_lazy as _
from utilities.choices import ChoiceSet

# Values follow the maintnote standard (https://github.com/jda/maintnote-std/blob/master/standard.md). The handler
# stores parser output verbatim, so they must match it exactly.

#
# CircuitMaintenance
#


class CircuitMaintenanceStatusChoices(ChoiceSet):
    TENTATIVE = 'TENTATIVE'
    CONFIRMED = 'CONFIRMED'
    CANCELLED = 'CANCELLED'
    IN_PROCESS = 'IN-PROCESS'
    COMPLETED = 'COMPLETED'
    RE_SCHEDULED = 'RE-SCHEDULED'
    UNKNOWN = 'UNKNOWN'

    CHOICES = (
        (TENTATIVE, _('Tentative'), 'cyan'),
        (CONFIRMED, _('Confirmed'), 'blue'),
        (CANCELLED, _('Cancelled'), 'gray'),
        (IN_PROCESS, _('In process'), 'orange'),
        (COMPLETED, _('Completed'), 'green'),
        (RE_SCHEDULED, _('Re-scheduled'), 'purple'),
        (UNKNOWN, _('Unknown'), 'yellow'),
    )


#
# CircuitImpact
#


class CircuitImpactChoices(ChoiceSet):
    NO_IMPACT = 'NO-IMPACT'
    REDUCED_REDUNDANCY = 'REDUCED-REDUNDANCY'
    DEGRADED = 'DEGRADED'
    OUTAGE = 'OUTAGE'

    CHOICES = (
        (NO_IMPACT, _('No impact'), 'green'),
        (REDUCED_REDUNDANCY, _('Reduced redundancy'), 'yellow'),
        (DEGRADED, _('Degraded'), 'orange'),
        (OUTAGE, _('Outage'), 'red'),
    )
