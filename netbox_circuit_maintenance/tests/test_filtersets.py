from datetime import UTC, datetime, timedelta

from circuits.models import Circuit, CircuitType, Provider
from django.test import TestCase, override_settings
from utilities.testing import ChangeLoggedFilterSetTests

from netbox_circuit_maintenance.choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
from netbox_circuit_maintenance.filtersets import (
    CircuitImpactFilterSet,
    CircuitMaintenanceFilterSet,
    NoteFilterSet,
    RawNotificationFilterSet,
)
from netbox_circuit_maintenance.models import (
    CircuitImpact,
    CircuitMaintenance,
    Note,
    NotificationSource,
    RawNotification,
)

START = datetime(2020, 10, 4, 10, 0, tzinfo=UTC)
END = START + timedelta(hours=2)


# The inherited created/last_updated tests filter on naive datetimes, which NetBox's own test configuration reads as UTC
@override_settings(TIME_ZONE='UTC')
class CircuitMaintenanceTestCase(TestCase, ChangeLoggedFilterSetTests):
    queryset = CircuitMaintenance.objects.all()
    filterset = CircuitMaintenanceFilterSet

    @classmethod
    def setUpTestData(cls):
        cls.providers = (
            Provider.objects.create(name='Provider 1', slug='provider-1'),
            Provider.objects.create(name='Provider 2', slug='provider-2'),
            Provider.objects.create(name='Provider 3', slug='provider-3'),
        )
        circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
        cls.circuits = (
            Circuit.objects.create(cid='CID-1', provider=cls.providers[0], type=circuit_type),
            Circuit.objects.create(cid='CID-2', provider=cls.providers[1], type=circuit_type),
            Circuit.objects.create(cid='CID-3', provider=cls.providers[1], type=circuit_type),
            Circuit.objects.create(cid='CID-4', provider=cls.providers[2], type=circuit_type),
        )

        maintenances = (
            CircuitMaintenance(
                name='UT-TEST-1',
                start_time=START,
                end_time=END,
                status=CircuitMaintenanceStatusChoices.TENTATIVE,
                description='foobar1',
            ),
            CircuitMaintenance(
                name='UT-TEST-2',
                start_time=START + timedelta(days=1),
                end_time=END + timedelta(days=1),
                status=CircuitMaintenanceStatusChoices.CONFIRMED,
                ack=True,
                description='foobar2',
            ),
            CircuitMaintenance(
                name='UT-TEST-3',
                start_time=START + timedelta(days=2),
                end_time=END + timedelta(days=2),
                status=CircuitMaintenanceStatusChoices.RE_SCHEDULED,
                description='foobar3',
                comments='barbaz',
            ),
        )
        CircuitMaintenance.objects.bulk_create(maintenances)

        # UT-TEST-2 impacts two circuits of the same provider; UT-TEST-3 impacts none
        CircuitImpact.objects.bulk_create(
            (
                CircuitImpact(maintenance=maintenances[0], circuit=cls.circuits[0]),
                CircuitImpact(maintenance=maintenances[1], circuit=cls.circuits[1]),
                CircuitImpact(maintenance=maintenances[1], circuit=cls.circuits[2]),
            )
        )

    def test_q(self):
        self.assertEqual(self.filterset({'q': 'UT-TEST-1'}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({'q': 'foobar2'}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({'q': 'barbaz'}, self.queryset).qs.count(), 1)

    def test_name(self):
        params = {'name': ['UT-TEST-1', 'UT-TEST-2']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_status(self):
        params = {'status': [CircuitMaintenanceStatusChoices.TENTATIVE, CircuitMaintenanceStatusChoices.RE_SCHEDULED]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_ack(self):
        self.assertEqual(self.filterset({'ack': 'true'}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({'ack': 'false'}, self.queryset).qs.count(), 2)

    def test_description(self):
        params = {'description': ['foobar1', 'foobar2']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        self.assertEqual(self.filterset({'description__ic': ['bar3']}, self.queryset).qs.count(), 1)

    def test_start_time(self):
        # Bare start_time is an exact match. Nautobot's start_time filter meant "starts at or after", now start_time__gte.
        self.assertEqual(self.filterset({'start_time': ['2020-10-05T10:00:00Z']}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({'start_time__gte': ['2020-10-05T10:00:00Z']}, self.queryset).qs.count(), 2)

    def test_end_time(self):
        # Bare end_time is an exact match. Nautobot's end_time filter meant "ends at or before", now end_time__lte.
        self.assertEqual(self.filterset({'end_time': ['2020-10-05T12:00:00Z']}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({'end_time__lte': ['2020-10-05T12:00:00Z']}, self.queryset).qs.count(), 2)

    def test_provider(self):
        # UT-TEST-2 matches once, although two of its impacted circuits belong to Provider 2
        params = {'provider_id': [self.providers[0].pk, self.providers[1].pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        params = {'provider': [self.providers[1].slug]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)
        params = {'provider_id': [self.providers[2].pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 0)

    def test_circuit(self):
        params = {'circuit_id': [self.circuits[0].pk, self.circuits[1].pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        params = {'circuit': [self.circuits[1].cid, self.circuits[2].cid]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)


@override_settings(TIME_ZONE='UTC')
class CircuitImpactTestCase(TestCase, ChangeLoggedFilterSetTests):
    queryset = CircuitImpact.objects.all()
    filterset = CircuitImpactFilterSet

    @classmethod
    def setUpTestData(cls):
        providers = (
            Provider.objects.create(name='Provider 1', slug='provider-1'),
            Provider.objects.create(name='Provider 2', slug='provider-2'),
        )
        circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
        # The same CID under two providers
        cls.circuits = (
            Circuit.objects.create(cid='CID-1', provider=providers[0], type=circuit_type),
            Circuit.objects.create(cid='CID-2', provider=providers[0], type=circuit_type),
            Circuit.objects.create(cid='CID-2', provider=providers[1], type=circuit_type),
        )
        cls.maintenances = (
            CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance.objects.create(
                name='UT-TEST-2', start_time=START + timedelta(days=1), end_time=END + timedelta(days=1)
            ),
        )

        CircuitImpact.objects.bulk_create(
            (
                CircuitImpact(
                    maintenance=cls.maintenances[0], circuit=cls.circuits[0], impact=CircuitImpactChoices.NO_IMPACT
                ),
                CircuitImpact(
                    maintenance=cls.maintenances[0], circuit=cls.circuits[1], impact=CircuitImpactChoices.DEGRADED
                ),
                CircuitImpact(maintenance=cls.maintenances[1], circuit=cls.circuits[2]),
            )
        )

    def test_q(self):
        self.assertEqual(self.filterset({'q': 'UT-TEST-2'}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({'q': 'CID-2'}, self.queryset).qs.count(), 2)

    def test_maintenance(self):
        params = {'maintenance_id': [self.maintenances[0].pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        params = {'maintenance': [self.maintenances[0].name, self.maintenances[1].name]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 3)

    def test_circuit(self):
        params = {'circuit_id': [self.circuits[0].pk, self.circuits[2].pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        # A CID matches every provider's circuit with that ID
        params = {'circuit': ['CID-2']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_impact(self):
        params = {'impact': [CircuitImpactChoices.NO_IMPACT, CircuitImpactChoices.OUTAGE]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)


@override_settings(TIME_ZONE='UTC')
class NoteTestCase(TestCase, ChangeLoggedFilterSetTests):
    queryset = Note.objects.all()
    filterset = NoteFilterSet

    @classmethod
    def setUpTestData(cls):
        cls.maintenances = (
            CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance.objects.create(
                name='UT-TEST-2', start_time=START + timedelta(days=1), end_time=END + timedelta(days=1)
            ),
        )

        Note.objects.bulk_create(
            (
                Note(maintenance=cls.maintenances[0], title='Note 1', comment='foobar1'),
                Note(
                    maintenance=cls.maintenances[0],
                    title='Note 2',
                    level=NoteLevelChoices.WARNING,
                    comment='foobar2',
                ),
                # The same title on another maintenance
                Note(maintenance=cls.maintenances[1], title='Note 2', comment='foobar3'),
            )
        )

    def test_q(self):
        self.assertEqual(self.filterset({'q': 'Note 2'}, self.queryset).qs.count(), 2)
        self.assertEqual(self.filterset({'q': 'foobar3'}, self.queryset).qs.count(), 1)

    def test_maintenance(self):
        params = {'maintenance_id': [self.maintenances[0].pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        params = {'maintenance': [self.maintenances[1].name]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_title(self):
        params = {'title': ['Note 1', 'Note 2']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 3)
        self.assertEqual(self.filterset({'title__ic': ['te 1']}, self.queryset).qs.count(), 1)

    def test_level(self):
        params = {'level': [NoteLevelChoices.WARNING]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_comment(self):
        params = {'comment': ['foobar1', 'foobar2']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        self.assertEqual(self.filterset({'comment__ic': ['bar3']}, self.queryset).qs.count(), 1)


@override_settings(TIME_ZONE='UTC')
class RawNotificationTestCase(TestCase, ChangeLoggedFilterSetTests):
    queryset = RawNotification.objects.all()
    filterset = RawNotificationFilterSet
    # The notification body isn't filterable
    ignore_fields = ('raw',)

    @classmethod
    def setUpTestData(cls):
        cls.providers = (
            Provider.objects.create(name='Provider 1', slug='provider-1'),
            Provider.objects.create(name='Provider 2', slug='provider-2'),
        )
        cls.sources = (
            NotificationSource.objects.create(name='Source 1'),
            NotificationSource.objects.create(name='Source 2'),
        )

        RawNotification.objects.bulk_create(
            (
                RawNotification(
                    raw=b'foobar',
                    subject='Subject 1',
                    provider=cls.providers[0],
                    sender='noc1@example.com',
                    source=cls.sources[0],
                    parsed=True,
                    stamp=START,
                ),
                RawNotification(
                    raw=b'',
                    subject='Subject 2',
                    provider=cls.providers[1],
                    sender='noc2@example.com',
                    source=cls.sources[1],
                    stamp=START + timedelta(hours=1),
                ),
                # A deleted source leaves source empty
                RawNotification(
                    raw=b'', subject='Other 3', provider=cls.providers[1], stamp=START + timedelta(hours=2)
                ),
            )
        )

    def test_q(self):
        self.assertEqual(self.filterset({'q': 'subject'}, self.queryset).qs.count(), 2)
        # Only the subject is searched, as in Nautobot
        self.assertEqual(self.filterset({'q': 'noc1'}, self.queryset).qs.count(), 0)

    def test_subject(self):
        params = {'subject': ['Subject 1', 'Other 3']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        self.assertEqual(self.filterset({'subject__ic': ['ject']}, self.queryset).qs.count(), 2)

    def test_provider(self):
        params = {'provider_id': [self.providers[1].pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        params = {'provider': [self.providers[0].slug]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_sender(self):
        params = {'sender': ['noc1@example.com', 'noc2@example.com']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_source(self):
        params = {'source_id': [self.sources[0].pk, self.sources[1].pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        params = {'source': [self.sources[1].name]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_parsed(self):
        self.assertEqual(self.filterset({'parsed': 'true'}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({'parsed': 'false'}, self.queryset).qs.count(), 2)

    def test_stamp(self):
        self.assertEqual(self.filterset({'stamp': ['2020-10-04T11:00:00Z']}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({'stamp__gte': ['2020-10-04T11:00:00Z']}, self.queryset).qs.count(), 2)

    def test_since(self):
        # Nautobot's name for stamp__gte
        self.assertEqual(self.filterset({'since': '2020-10-04T11:00:00Z'}, self.queryset).qs.count(), 2)
