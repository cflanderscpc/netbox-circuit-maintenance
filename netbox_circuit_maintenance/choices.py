from utilities.choices import ChoiceSet


class CircuitMaintenanceStatusChoices(ChoiceSet):
    """Valid values for Circuit Maintenance Status.
    See: https://github.com/jda/maintnote-std/blob/master/standard.md
    """

    key = 'CircuitMaintenance.status'

    STATUS_TENTATIVE = "TENTATIVE"
    STATUS_CONFIRMED = "CONFIRMED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_IN_PROCESS = "IN-PROCESS"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_RESCHEDULED = "RESCHEDULED"
    STATUS_UNKNOWN = "UNKNOWN"

    CHOICES = [
        (STATUS_UNKNOWN, "Unknown", "gray"),
        (STATUS_TENTATIVE, "Tentative", "cyan"),
        (STATUS_CONFIRMED, "Confirmed", "blue"),
        (STATUS_IN_PROCESS, "In-Process", "purple"),
        (STATUS_COMPLETED, "Completed", "teal"),
        (STATUS_RESCHEDULED, "Rescheduled", "pink"),
        (STATUS_CANCELLED, "Cancelled", "gray")
    ]


class CircuitMaintenanceImpactChoices(ChoiceSet):
    """Valid values for Circuit Maintenance Impact.

    See: https://github.com/jda/maintnote-std/blob/master/standard.md
    """

    key = 'CircuitMaintenanceImpact.impact'  # adjust to your plugin's namespace

    IMPACT_NO_IMPACT = 'NO-IMPACT'
    IMPACT_REDUCED_REDUNDANCY = 'REDUCED-REDUNDANCY'
    IMPACT_DEGRADED = 'DEGRADED'
    IMPACT_OUTAGE = 'OUTAGE'

    CHOICES = [
        (IMPACT_NO_IMPACT, 'No impact', 'green'),
        (IMPACT_REDUCED_REDUNDANCY, 'Reduced Redundancy', 'yellow'),
        (IMPACT_DEGRADED, 'Degraded', 'orange'),
        (IMPACT_OUTAGE, 'Outage', 'red'),
    ]


class CircuitMaintenanceNoteLevelChoices(ChoiceSet):
    """
    Valid values for Note Level.
    """

    NOTELEVEL_INFO = 'INFO'
    NOTELEVEL_WARNING = 'WARNING'

    CHOICES = [
        (NOTELEVEL_INFO, "Informational"),
        (NOTELEVEL_WARNING, "Warning")
    ]
