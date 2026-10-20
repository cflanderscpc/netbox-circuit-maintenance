from collections import defaultdict

from circuits.models import Circuit
from dcim.models import Site
from django.utils import timezone
from netbox.jobs import JobRunner
from netbox.plugins import get_plugin_config

from .choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices
from .models import CircuitImpact, CircuitMaintenance
from .utils import get_midnight

__all__ = (
    'FindSitesWithMaintenanceOverlap',
    'build_sites_to_maintenance_mapper',
    'check_for_overlap',
    'get_sites_from_circuit',
)

# Maintenances that can't take a circuit down anymore
INACTIVE_STATUSES = (CircuitMaintenanceStatusChoices.CANCELLED, CircuitMaintenanceStatusChoices.COMPLETED)


def restrict(queryset, user):
    """
    Return the objects of the queryset that the user can view, or all of them for a run without a user (a scheduled
    run).
    """
    return queryset.restrict(user, 'view') if user else queryset


def check_for_overlap(record1, record2) -> bool:
    """
    Return True if the two maintenances overlap in time. Maintenances that end and start at the same time overlap.
    """
    return not (record1.end_time < record2.start_time or record2.end_time < record1.start_time)


def get_sites_from_circuit(circuit: Circuit) -> set:
    """
    Return the sites of the circuit's A and Z terminations: a termination on a site or a location has one, and a
    termination on a provider network, a region or a site group has none.
    """
    return {term._site for term in (circuit.termination_a, circuit.termination_z) if term and term._site_id}


def build_sites_to_maintenance_mapper(maintenances, user=None) -> dict:
    """
    Map each site to the set of the maintenances that impact a circuit terminating there, covering only the impacts,
    circuits and sites the user can view (all of them without a user). With overlap_job_exclude_no_impact set, a
    maintenance counts at a site only through the circuits it impacts with something other than No impact, so an
    overlap is one between maintenances that both impact circuits at the site.
    """
    impacts = (
        restrict(CircuitImpact.objects.all(), user)
        .filter(maintenance__in=maintenances, circuit__in=restrict(Circuit.objects.all(), user))
        .select_related('maintenance', 'circuit__termination_a___site', 'circuit__termination_z___site')
    )
    if get_plugin_config('netbox_circuit_maintenance', 'overlap_job_exclude_no_impact'):
        impacts = impacts.exclude(impact=CircuitImpactChoices.NO_IMPACT)

    mapper = defaultdict(set)
    for impact in impacts:
        for site in get_sites_from_circuit(impact.circuit):
            mapper[site].add(impact.maintenance)
    if user:
        site_ids = set(
            restrict(Site.objects.filter(pk__in=[site.pk for site in mapper]), user).values_list('pk', flat=True)
        )
        return {site: site_maintenances for site, site_maintenances in mapper.items() if site.pk in site_ids}
    return dict(mapper)


class FindSitesWithMaintenanceOverlap(JobRunner):
    """
    Job to find the sites where circuit maintenances that haven't ended overlap in time, which may take down a site
    connected by two circuits. Nautobot's job searched by location (Site in NetBox) and assumed every location has only
    two circuits, so it reports every overlap of two maintenances at a site. A run started from the UI covers only the
    maintenances, impacts, circuits and sites its user can view; a scheduled run covers all of them.
    """

    class Meta:
        name = 'Find Sites With Circuit Maintenance Overlap'

    def run(self, **kwargs):
        # NetBox reschedules every job with an interval, registered as a system job or not. Clearing it ends a schedule
        # that began while overlap_job_interval was set (handle() reads it after run() returns)
        if self.job.interval and get_plugin_config('netbox_circuit_maintenance', 'overlap_job_interval') is None:
            self.job.interval = None
            self.logger.warning(
                'The overlap_job_interval setting is unset, so this scheduled run does nothing and the schedule ends.'
            )
            return

        user = self.job.user
        # Maintenances that end today or later, ongoing ones included, unless cancelled or completed
        maintenances = list(
            restrict(CircuitMaintenance.objects.all(), user)
            .filter(end_time__gte=get_midnight(timezone.localdate()))
            .exclude(status__in=INACTIVE_STATUSES)
            .order_by('start_time', 'pk')
        )
        site_maintenances = build_sites_to_maintenance_mapper(maintenances, user)
        maintenance_sites = defaultdict(set)
        for site, site_maintenance_set in site_maintenances.items():
            for maintenance in site_maintenance_set:
                maintenance_sites[maintenance].add(site)

        # Each pair of maintenances at a site is checked once, from the one that starts first
        processed = set()
        overlapping = set()
        for maintenance in maintenances:
            processed.add(maintenance)
            for site in sorted(maintenance_sites[maintenance], key=lambda site: site.name):
                others = sorted(site_maintenances[site] - processed, key=lambda other: (other.start_time, other.pk))
                for other in others:
                    if check_for_overlap(maintenance, other):
                        overlapping.update((maintenance, other))
                        self.logger.warning(
                            f'There is an overlapping maintenance for site: {site} on '
                            f'{timezone.localtime(maintenance.start_time).isoformat(timespec="seconds")}. '
                            f'Other maintenances: {other}|{maintenance}',
                            extra={'object': site},
                        )

            # Not for a maintenance that overlaps one checked before it
            if maintenance not in overlapping:
                self.logger.debug(
                    f'Checked maintenance {maintenance} for overlap, no overlap was found.',
                    extra={'object': maintenance},
                )

        count = len(maintenances)
        self.logger.info(f'Successfully checked through {count} maintenance notification{"" if count == 1 else "s"}.')
