import runpy
import uuid
import warnings
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple
from unittest.mock import patch

from circuits.models import Circuit, CircuitTermination, CircuitType, Provider, ProviderNetwork
from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from dcim.models import Location, Region, Site, SiteGroup
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.utils import timezone
from netbox.registry import registry
from users.models import ObjectPermission, User

from netbox_circuit_maintenance.choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices
from netbox_circuit_maintenance.models import CircuitImpact, CircuitMaintenance
from netbox_circuit_maintenance.site_search import FindSitesWithMaintenanceOverlap, check_for_overlap

from .utils import plugin_settings

DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# The date the job runs on. Local midnight is 06:00 UTC: the tests run in America/Edmonton, which is UTC-6 in August
TODAY = date(2022, 8, 25)


def parse_time(value, date_format):
    # Nautobot's tests compared naive datetimes; maintenance times are aware
    return datetime.strptime(value, date_format).replace(tzinfo=UTC)


class MockCircuitMaintenance(NamedTuple):
    start_time: datetime
    end_time: datetime


class CheckForOverlapTestCase(TestCase):
    """
    Nautobot's TestOverlap.
    """

    def test_check_for_overlap_false(self):
        record1 = MockCircuitMaintenance(
            start_time=parse_time('2020-10-04 10:00:00', DATE_FORMAT),
            end_time=parse_time('2020-10-04 12:00:00', DATE_FORMAT),
        )
        record2 = MockCircuitMaintenance(
            start_time=parse_time('2020-10-05 10:00:00', DATE_FORMAT),
            end_time=parse_time('2020-10-05 12:00:00', DATE_FORMAT),
        )

        result = check_for_overlap(record1, record2)
        self.assertFalse(result)

    def test_check_for_overlap_true(self):
        record1 = MockCircuitMaintenance(
            start_time=parse_time('2020-10-04 10:00:00', DATE_FORMAT),
            end_time=parse_time('2020-10-04 12:00:00', DATE_FORMAT),
        )
        record2 = MockCircuitMaintenance(
            start_time=parse_time('2020-10-04 11:00:00', DATE_FORMAT),
            end_time=parse_time('2020-10-05 12:00:00', DATE_FORMAT),
        )

        result = check_for_overlap(record1, record2)
        self.assertTrue(result)

    def test_check_for_overlap_same_day_true(self):
        record1 = MockCircuitMaintenance(
            start_time=parse_time('2020-10-04 10:00:00', DATE_FORMAT),
            end_time=parse_time('2020-10-04 12:00:00', DATE_FORMAT),
        )
        record2 = MockCircuitMaintenance(
            start_time=parse_time('2020-10-04 11:00:00', DATE_FORMAT),
            end_time=parse_time('2020-10-04 11:30:00', DATE_FORMAT),
        )

        result = check_for_overlap(record1, record2)
        self.assertTrue(result)

    def test_check_for_overlap_same_day_false(self):
        record1 = MockCircuitMaintenance(
            start_time=parse_time('2020-10-04 10:00:00', DATE_FORMAT),
            end_time=parse_time('2020-10-04 12:00:00', DATE_FORMAT),
        )
        record2 = MockCircuitMaintenance(
            start_time=parse_time('2020-10-04 09:00:00', DATE_FORMAT),
            end_time=parse_time('2020-10-04 09:30:00', DATE_FORMAT),
        )

        result = check_for_overlap(record1, record2)
        self.assertFalse(result)

    def test_check_for_overlap_touching(self):
        # One ending as the other starts overlaps, in either order
        record1 = MockCircuitMaintenance(
            start_time=parse_time('2020-10-04 10:00:00', DATE_FORMAT),
            end_time=parse_time('2020-10-04 12:00:00', DATE_FORMAT),
        )
        record2 = MockCircuitMaintenance(
            start_time=parse_time('2020-10-04 12:00:00', DATE_FORMAT),
            end_time=parse_time('2020-10-04 13:00:00', DATE_FORMAT),
        )

        self.assertTrue(check_for_overlap(record1, record2))
        self.assertTrue(check_for_overlap(record2, record1))


@override_settings(TIME_ZONE='America/Edmonton')
class FindSitesWithMaintenanceOverlapTestCase(TestCase):
    """
    The overlap job, which Nautobot's tests didn't run.
    """

    @classmethod
    def setUpTestData(cls):
        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
        sites = {
            name: Site.objects.create(name=name, slug=name.lower().replace(' ', '-'))
            for name in ('Site 1', 'Site 2', 'Site 3', 'Site 4')
        }
        # Location names are unique only within their site
        locations = {
            site: Location.objects.create(name='Location 1', slug='location-1', site=sites[site])
            for site in ('Site 3', 'Site 4')
        }
        provider_network = ProviderNetwork.objects.create(name='Provider Network 1', provider=provider)
        region = Region.objects.create(name='Region 1', slug='region-1')
        site_group = SiteGroup.objects.create(name='Site Group 1', slug='site-group-1')

        cls.circuits = {}
        for cid, termination_a, termination_z in (
            ('CID-1', sites['Site 1'], sites['Site 2']),
            ('CID-2', sites['Site 1'], None),
            ('CID-3', sites['Site 2'], sites['Site 1']),
            ('CID-4', sites['Site 1'], None),
            ('CID-5', locations['Site 3'], None),
            ('CID-6', sites['Site 3'], None),
            ('CID-7', locations['Site 4'], None),
            ('CID-8', provider_network, region),
            ('CID-9', provider_network, None),
            ('CID-10', None, None),
            ('CID-11', site_group, None),
            ('CID-12', None, sites['Site 1']),
        ):
            circuit = cls.circuits[cid] = Circuit.objects.create(cid=cid, provider=provider, type=circuit_type)
            for side, termination in (('A', termination_a), ('Z', termination_z)):
                if termination:
                    CircuitTermination.objects.create(circuit=circuit, term_side=side, termination=termination)

    def create_maintenance(self, name, start, hours=2, status=CircuitMaintenanceStatusChoices.CONFIRMED, impacts=()):
        """
        Create a maintenance lasting the given hours, impacting each (CID, impact) in impacts.
        """
        maintenance = CircuitMaintenance.objects.create(
            name=name, start_time=start, end_time=start + timedelta(hours=hours), status=status
        )
        for cid, impact in impacts:
            CircuitImpact.objects.create(maintenance=maintenance, circuit=self.circuits[cid], impact=impact)
        return maintenance

    def run_job(self, user=None, **settings):
        """
        Run the job for an unsaved Job on TODAY, with the given plugin settings, and return it.
        """
        job = FindSitesWithMaintenanceOverlap(
            Job(name=FindSitesWithMaintenanceOverlap.name, job_id=uuid.uuid4(), user=user)
        )
        # NetBox adds a log handler for each runner to the runner's logger, which is module-level, and never removes it
        self.addCleanup(job.logger.removeHandler, job.logger.handlers[-1])
        with patch('django.utils.timezone.localdate', return_value=TODAY), plugin_settings(**settings):
            job.run()
        return job

    def get_log(self, job, level=None):
        """
        Return the messages in the job's log, of one level if given.
        """
        return [entry['message'] for entry in job.job.log_entries if level in (None, entry['level'])]

    @staticmethod
    def overlap_message(site, maintenance, other):
        start = timezone.localtime(maintenance.start_time).isoformat(timespec='seconds')
        return f'There is an overlapping maintenance for site: {site} on {start}. Other maintenances: {other}|{maintenance}'

    def test_overlap(self):
        outage = CircuitImpactChoices.OUTAGE
        self.create_maintenance('M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-1', outage)])
        self.create_maintenance('M-2', datetime(2022, 8, 26, 11, tzinfo=UTC), impacts=[('CID-2', outage)])

        job = self.run_job()

        # CID-1 and CID-2 both terminate on Site 1; the start is the first maintenance's, in local time
        self.assertEqual(
            self.get_log(job, 'warning'),
            [
                'There is an overlapping maintenance for site: Site 1 on 2022-08-26T04:00:00-06:00. '
                'Other maintenances: M-2|M-1'
            ],
        )
        # Nautobot also logged "no overlap was found" for M-2, whose overlap was found while checking M-1
        self.assertEqual(self.get_log(job, 'debug'), [])
        self.assertEqual(self.get_log(job, 'info'), ['Successfully checked through 2 maintenance notifications.'])

    def test_no_overlap(self):
        outage = CircuitImpactChoices.OUTAGE
        self.create_maintenance('M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-1', outage)])
        self.create_maintenance('M-2', datetime(2022, 8, 26, 12, 1, tzinfo=UTC), impacts=[('CID-2', outage)])
        # At a site of its own
        self.create_maintenance('M-3', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-6', outage)])

        job = self.run_job()

        self.assertEqual(self.get_log(job, 'warning'), [])
        self.assertEqual(
            self.get_log(job, 'debug'),
            [
                'Checked maintenance M-1 for overlap, no overlap was found.',
                'Checked maintenance M-3 for overlap, no overlap was found.',
                'Checked maintenance M-2 for overlap, no overlap was found.',
            ],
        )
        self.assertEqual(self.get_log(job, 'info'), ['Successfully checked through 3 maintenance notifications.'])

    def test_overlap_at_each_site(self):
        # Reported once for each site the maintenances share, by site name
        outage = CircuitImpactChoices.OUTAGE
        m1 = self.create_maintenance('M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-1', outage)])
        m2 = self.create_maintenance('M-2', datetime(2022, 8, 26, 11, tzinfo=UTC), impacts=[('CID-3', outage)])

        job = self.run_job()

        self.assertEqual(
            self.get_log(job, 'warning'),
            [self.overlap_message('Site 1', m1, m2), self.overlap_message('Site 2', m1, m2)],
        )

    def test_overlap_site_order(self):
        # Shared sites in name order. They're created in reverse, so pk order isn't name order
        provider, circuit_type = self.circuits['CID-1'].provider, self.circuits['CID-1'].type
        sites = {name: Site.objects.create(name=name, slug=name.lower().replace(' ', '-')) for name in ('C', 'B', 'A')}
        for cid, termination_a, termination_z in (
            ('X-1', 'C', 'B'),
            ('X-2', 'A', None),
            ('Y-1', 'A', 'C'),
            ('Y-2', 'B', None),
        ):
            circuit = self.circuits[cid] = Circuit.objects.create(cid=cid, provider=provider, type=circuit_type)
            for side, site in (('A', termination_a), ('Z', termination_z)):
                if site:
                    CircuitTermination.objects.create(circuit=circuit, term_side=side, termination=sites[site])
        outage = CircuitImpactChoices.OUTAGE
        m1 = self.create_maintenance(
            'M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('X-1', outage), ('X-2', outage)]
        )
        m2 = self.create_maintenance(
            'M-2', datetime(2022, 8, 26, 11, tzinfo=UTC), impacts=[('Y-1', outage), ('Y-2', outage)]
        )

        job = self.run_job()

        self.assertEqual(self.get_log(job, 'warning'), [self.overlap_message(site, m1, m2) for site in ('A', 'B', 'C')])

    def test_overlap_pairs(self):
        # Each pair once, from the maintenance that starts first, against the others by start time. M-2 and M-3 don't
        # overlap each other. M-3 is created first, so pk order isn't start order
        outage = CircuitImpactChoices.OUTAGE
        m3 = self.create_maintenance('M-3', datetime(2022, 8, 26, 13, 30, tzinfo=UTC), impacts=[('CID-4', outage)])
        m1 = self.create_maintenance('M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), hours=4, impacts=[('CID-1', outage)])
        m2 = self.create_maintenance('M-2', datetime(2022, 8, 26, 11, tzinfo=UTC), impacts=[('CID-2', outage)])
        # Same start time as M-3: ordered by pk. It overlaps M-1 and M-3 on the circuit M-2 impacts
        m4 = self.create_maintenance('M-4', datetime(2022, 8, 26, 13, 30, tzinfo=UTC), impacts=[('CID-2', outage)])

        job = self.run_job()

        self.assertEqual(
            self.get_log(job, 'warning'),
            [
                self.overlap_message('Site 1', m1, m2),
                self.overlap_message('Site 1', m1, m3),
                self.overlap_message('Site 1', m1, m4),
                self.overlap_message('Site 1', m3, m4),
            ],
        )
        self.assertEqual(self.get_log(job, 'debug'), [])

    def test_one_maintenance_on_two_circuits(self):
        # Overlaps are between maintenances, so one maintenance impacting both of a site's circuits isn't reported, as in
        # Nautobot
        outage = CircuitImpactChoices.OUTAGE
        self.create_maintenance(
            'M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-2', outage), ('CID-4', outage)]
        )

        job = self.run_job()

        self.assertEqual(self.get_log(job, 'warning'), [])
        self.assertEqual(self.get_log(job, 'debug'), ['Checked maintenance M-1 for overlap, no overlap was found.'])

    def test_terminations_without_site(self):
        # Terminations on a provider network, a region or a site group have no site, and CID-10 has no terminations
        outage = CircuitImpactChoices.OUTAGE
        for name, cid in (('M-1', 'CID-8'), ('M-2', 'CID-9'), ('M-3', 'CID-10'), ('M-4', 'CID-11')):
            self.create_maintenance(name, datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[(cid, outage)])

        job = self.run_job()

        self.assertEqual(self.get_log(job, 'warning'), [])
        self.assertEqual(len(self.get_log(job, 'debug')), 4)

    def test_z_side_only(self):
        # CID-12 has only a Z side, on Site 1
        outage = CircuitImpactChoices.OUTAGE
        m1 = self.create_maintenance('M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-12', outage)])
        m2 = self.create_maintenance('M-2', datetime(2022, 8, 26, 11, tzinfo=UTC), impacts=[('CID-2', outage)])

        job = self.run_job()

        self.assertEqual(self.get_log(job, 'warning'), [self.overlap_message('Site 1', m1, m2)])

    def test_location_termination(self):
        # A termination on a location counts at the location's site
        outage = CircuitImpactChoices.OUTAGE
        m1 = self.create_maintenance('M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-5', outage)])
        m2 = self.create_maintenance('M-2', datetime(2022, 8, 26, 11, tzinfo=UTC), impacts=[('CID-6', outage)])

        job = self.run_job()

        self.assertEqual(self.get_log(job, 'warning'), [self.overlap_message('Site 3', m1, m2)])

    def test_same_location_name(self):
        # Nautobot keyed locations by name, so it reported these two locations named Location 1 as one
        outage = CircuitImpactChoices.OUTAGE
        self.create_maintenance('M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-5', outage)])
        self.create_maintenance('M-2', datetime(2022, 8, 26, 11, tzinfo=UTC), impacts=[('CID-7', outage)])

        job = self.run_job()

        self.assertEqual(self.get_log(job, 'warning'), [])

    def test_maintenances_checked(self):
        # Maintenances ending today or later (local time), unless cancelled or completed. Nautobot checked the ones
        # starting today or later, whatever their status
        midnight = datetime(2022, 8, 25, 6, tzinfo=UTC)
        for name, start, end in (
            ('Ended', midnight - timedelta(hours=3), midnight - timedelta(seconds=1)),
            ('Ends at midnight', midnight - timedelta(hours=3), midnight),
            ('Ongoing', midnight - timedelta(days=1), midnight + timedelta(days=1)),
            ('Starts at midnight', midnight, midnight + timedelta(hours=2)),
        ):
            CircuitMaintenance.objects.create(name=name, start_time=start, end_time=end)
        for index, (status, _, _) in enumerate(CircuitMaintenanceStatusChoices.CHOICES):
            self.create_maintenance(status, midnight + timedelta(days=1, hours=index), status=status)

        job = self.run_job()

        self.assertEqual(
            self.get_log(job, 'debug'),
            [
                f'Checked maintenance {name} for overlap, no overlap was found.'
                for name in (
                    'Ongoing',
                    'Ends at midnight',
                    'Starts at midnight',
                    'TENTATIVE',
                    'CONFIRMED',
                    'IN-PROCESS',
                    'RE-SCHEDULED',
                    'UNKNOWN',
                )
            ],
        )
        self.assertEqual(self.get_log(job, 'info'), ['Successfully checked through 8 maintenance notifications.'])

    def test_ongoing_overlap(self):
        # A maintenance that started before today overlaps one starting today
        outage = CircuitImpactChoices.OUTAGE
        m1 = self.create_maintenance(
            'M-1', datetime(2022, 8, 24, 10, tzinfo=UTC), hours=48, impacts=[('CID-2', outage)]
        )
        m2 = self.create_maintenance('M-2', datetime(2022, 8, 25, 10, tzinfo=UTC), impacts=[('CID-4', outage)])

        job = self.run_job()

        self.assertEqual(self.get_log(job, 'warning'), [self.overlap_message('Site 1', m1, m2)])

    def test_inactive_overlap(self):
        # A cancelled or completed maintenance doesn't overlap
        outage = CircuitImpactChoices.OUTAGE
        self.create_maintenance('M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-2', outage)])
        for status in (CircuitMaintenanceStatusChoices.CANCELLED, CircuitMaintenanceStatusChoices.COMPLETED):
            self.create_maintenance(
                status, datetime(2022, 8, 26, 11, tzinfo=UTC), status=status, impacts=[('CID-4', outage)]
            )

        job = self.run_job()

        self.assertEqual(self.get_log(job, 'warning'), [])
        self.assertEqual(self.get_log(job, 'info'), ['Successfully checked through 1 maintenance notification.'])

    def test_no_maintenances(self):
        job = self.run_job()

        self.assertEqual(self.get_log(job), ['Successfully checked through 0 maintenance notifications.'])

    def test_no_impact(self):
        # Without overlap_job_exclude_no_impact, No impact counts like any impact
        m1 = self.create_maintenance(
            'M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-2', CircuitImpactChoices.NO_IMPACT)]
        )
        m2 = self.create_maintenance(
            'M-2', datetime(2022, 8, 26, 11, tzinfo=UTC), impacts=[('CID-4', CircuitImpactChoices.NO_IMPACT)]
        )

        job = self.run_job()

        self.assertEqual(self.get_log(job, 'warning'), [self.overlap_message('Site 1', m1, m2)])

    def test_exclude_no_impact(self):
        # With overlap_job_exclude_no_impact, a maintenance counts at a site only through the circuits it impacts with
        # something other than No impact. Each case is on a day of its own
        no_impact = CircuitImpactChoices.NO_IMPACT
        outage = CircuitImpactChoices.OUTAGE
        # No impact first: Nautobot skipped it too
        self.create_maintenance('Day 1-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-2', no_impact)])
        self.create_maintenance('Day 1-2', datetime(2022, 8, 26, 11, tzinfo=UTC), impacts=[('CID-4', outage)])
        # No impact second: Nautobot checked only the first maintenance's impacts, so it reported this one
        self.create_maintenance('Day 2-1', datetime(2022, 8, 27, 10, tzinfo=UTC), impacts=[('CID-2', outage)])
        self.create_maintenance('Day 2-2', datetime(2022, 8, 27, 11, tzinfo=UTC), impacts=[('CID-4', no_impact)])
        # An outage at Site 3 and no impact at Site 1: Nautobot reported this one at Site 1 too
        self.create_maintenance(
            'Day 3-1', datetime(2022, 8, 28, 10, tzinfo=UTC), impacts=[('CID-6', outage), ('CID-2', no_impact)]
        )
        self.create_maintenance('Day 3-2', datetime(2022, 8, 28, 11, tzinfo=UTC), impacts=[('CID-4', outage)])
        # Every other impact counts
        reported = []
        for day, impact in enumerate(
            (CircuitImpactChoices.REDUCED_REDUNDANCY, CircuitImpactChoices.DEGRADED, outage), start=4
        ):
            maintenance = self.create_maintenance(
                f'Day {day}-1', datetime(2022, 8, 25 + day, 10, tzinfo=UTC), impacts=[('CID-2', impact)]
            )
            other = self.create_maintenance(
                f'Day {day}-2', datetime(2022, 8, 25 + day, 11, tzinfo=UTC), impacts=[('CID-4', impact)]
            )
            reported.append(self.overlap_message('Site 1', maintenance, other))

        job = self.run_job(overlap_job_exclude_no_impact=True)

        self.assertEqual(self.get_log(job, 'warning'), reported)
        self.assertEqual(
            self.get_log(job, 'debug'),
            [
                f'Checked maintenance {name} for overlap, no overlap was found.'
                for name in ('Day 1-1', 'Day 1-2', 'Day 2-1', 'Day 2-2', 'Day 3-1', 'Day 3-2')
            ],
        )

    def create_overlaps(self):
        """
        Create three overlapping maintenances: M-1 and M-2 at Sites 1 and 2, and M-3 at Site 1.
        """
        outage = CircuitImpactChoices.OUTAGE
        return (
            self.create_maintenance('M-1', datetime(2022, 8, 26, 10, tzinfo=UTC), impacts=[('CID-1', outage)]),
            self.create_maintenance('M-2', datetime(2022, 8, 26, 11, tzinfo=UTC), impacts=[('CID-3', outage)]),
            self.create_maintenance('M-3', datetime(2022, 8, 26, 11, 30, tzinfo=UTC), impacts=[('CID-2', outage)]),
        )

    def get_user(self, **constraints):
        """
        Return a new user who can view maintenances, impacts, circuits and sites, each limited by its constraints if
        given.
        """
        user = User.objects.create_user(username=f'user{User.objects.count() + 1}')
        for model, name in (
            (CircuitMaintenance, 'maintenance'),
            (CircuitImpact, 'impact'),
            (Circuit, 'circuit'),
            (Site, 'site'),
        ):
            obj_perm = ObjectPermission.objects.create(
                name=f'View {name}', actions=['view'], constraints=constraints.get(name)
            )
            obj_perm.users.add(user)
            obj_perm.object_types.add(ObjectType.objects.get_for_model(model))
        return user

    def test_without_user(self):
        # A scheduled run has no user: it covers everything
        m1, m2, m3 = self.create_overlaps()

        job = self.run_job()

        self.assertEqual(
            self.get_log(job, 'warning'),
            [
                self.overlap_message('Site 1', m1, m2),
                self.overlap_message('Site 1', m1, m3),
                self.overlap_message('Site 2', m1, m2),
                self.overlap_message('Site 1', m2, m3),
            ],
        )

    def test_user(self):
        # A run from the UI covers what its user can view
        m3 = self.create_overlaps()[-1]

        job = self.run_job(user=self.get_user())

        self.assertEqual(len(self.get_log(job, 'warning')), 4)

        # Everything for a superuser
        job = self.run_job(user=User.objects.create_user(username='admin', is_superuser=True))

        self.assertEqual(len(self.get_log(job, 'warning')), 4)

        # Nothing without permissions
        job = self.run_job(user=User.objects.create_user(username='nobody'))

        self.assertEqual(self.get_log(job), ['Successfully checked through 0 maintenance notifications.'])

        # With overlap_job_exclude_no_impact too
        m3.impacts.update(impact=CircuitImpactChoices.NO_IMPACT)
        job = self.run_job(
            user=self.get_user(maintenance={'name__in': ['M-2', 'M-3']}), overlap_job_exclude_no_impact=True
        )

        self.assertEqual(self.get_log(job, 'warning'), [])
        self.assertEqual(self.get_log(job, 'info'), ['Successfully checked through 2 maintenance notifications.'])

    def test_user_restricted(self):
        m1, m2, m3 = self.create_overlaps()
        for permission, constraints, maintenances, expected in (
            (
                'maintenance',
                {'name__in': ['M-1', 'M-3']},
                2,
                [self.overlap_message('Site 1', m1, m3)],
            ),
            (
                # M-3 is checked, at no site
                'impact',
                {'maintenance__name__in': ['M-1', 'M-2']},
                3,
                [self.overlap_message('Site 1', m1, m2), self.overlap_message('Site 2', m1, m2)],
            ),
            (
                'circuit',
                {'cid__in': ['CID-2', 'CID-3']},
                3,
                [self.overlap_message('Site 1', m2, m3)],
            ),
            (
                'site',
                {'name': 'Site 2'},
                3,
                [self.overlap_message('Site 2', m1, m2)],
            ),
        ):
            with self.subTest(permission=permission):
                job = self.run_job(user=self.get_user(**{permission: constraints}))

                self.assertEqual(self.get_log(job, 'warning'), expected)
                self.assertEqual(
                    self.get_log(job, 'info')[-1],
                    f'Successfully checked through {maintenances} maintenance notifications.',
                )

    def test_query_count(self):
        # One query for the maintenances and one for their impacts, circuits, terminations and sites, however many
        self.create_overlaps()
        job = FindSitesWithMaintenanceOverlap(Job(name=FindSitesWithMaintenanceOverlap.name, job_id=uuid.uuid4()))
        self.addCleanup(job.logger.removeHandler, job.logger.handlers[-1])

        with patch('django.utils.timezone.localdate', return_value=TODAY), self.assertNumQueries(2):
            job.run()

        self.assertEqual(len(self.get_log(job, 'warning')), 4)

    def handle_scheduled_job(self, overlap_job_interval):
        """
        Run a scheduled job with a 15-minute interval through handle(), as the RQ worker does, and return it.
        Scheduling queues nothing in Redis: TestCase doesn't run on_commit callbacks.
        """
        job = FindSitesWithMaintenanceOverlap.enqueue(schedule_at=timezone.now(), interval=15)
        with plugin_settings(overlap_job_interval=overlap_job_interval):
            FindSitesWithMaintenanceOverlap.handle(job)
        logger = FindSitesWithMaintenanceOverlap(job).logger
        self.addCleanup(
            lambda: [logger.removeHandler(h) for h in list(logger.handlers) if getattr(h, 'job', None) is job]
        )
        job.refresh_from_db()
        return job

    def test_scheduled_run(self):
        """
        A scheduled run with overlap_job_interval set checks as usual, and NetBox schedules the next one.
        """
        job = self.handle_scheduled_job(overlap_job_interval=15)

        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(job.log_entries[-1]['message'], 'Successfully checked through 0 maintenance notifications.')
        self.assertEqual(Job.objects.filter(name=job.name, status=JobStatusChoices.STATUS_SCHEDULED).count(), 1)

    def test_unset_overlap_job_interval(self):
        """
        A scheduled run that starts after overlap_job_interval was unset does nothing and ends the schedule: NetBox
        reschedules every job with an interval, as a system job or not.
        """
        job = self.handle_scheduled_job(overlap_job_interval=None)

        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertIsNone(job.interval)
        self.assertEqual(
            [entry['message'] for entry in job.log_entries],
            ['The overlap_job_interval setting is unset, so this scheduled run does nothing and the schedule ends.'],
        )
        # No next run was scheduled
        self.assertEqual(list(Job.objects.filter(name=job.name)), [job])


class OverlapSystemJobTestCase(TestCase):
    """
    jobs.py, which ready() imports, schedules the overlap job when overlap_job_interval is set. Each test runs the
    module again with runpy, as test_handler's SystemJobTestCase does.
    """

    def setUp(self):
        # Restore the registration that startup made, if any
        system_jobs = registry['system_jobs']
        startup = system_jobs.pop(FindSitesWithMaintenanceOverlap, None)

        def restore():
            system_jobs.pop(FindSitesWithMaintenanceOverlap, None)
            if startup is not None:
                system_jobs[FindSitesWithMaintenanceOverlap] = startup

        self.addCleanup(restore)

    def run_jobs_module(self):
        # runpy warns that the module is in sys.modules already: ready() imported it
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', "'netbox_circuit_maintenance.jobs' found in sys.modules", RuntimeWarning)
            runpy.run_module('netbox_circuit_maintenance.jobs')

    def test_overlap_job_interval(self):
        with plugin_settings(overlap_job_interval=60):
            self.run_jobs_module()
        self.assertEqual(registry['system_jobs'][FindSitesWithMaintenanceOverlap], {'interval': 60})

    def test_overlap_job_interval_unset(self):
        with plugin_settings(overlap_job_interval=None):
            self.run_jobs_module()
        self.assertNotIn(FindSitesWithMaintenanceOverlap, registry['system_jobs'])

    def test_overlap_job_interval_invalid(self):
        # system_job() would accept none of the first three, and NetBox's Job model rejects the last two
        for value in ('60', 60.0, True, 0, -5):
            with (
                self.subTest(value=value),
                plugin_settings(overlap_job_interval=value),
                self.assertRaisesMessage(
                    ImproperlyConfigured,
                    "PLUGINS_CONFIG['netbox_circuit_maintenance']['overlap_job_interval'] must be a whole number of "
                    f'minutes, at least 1, or None, not {value!r}.',
                ),
            ):
                self.run_jobs_module()
            self.assertNotIn(FindSitesWithMaintenanceOverlap, registry['system_jobs'])
