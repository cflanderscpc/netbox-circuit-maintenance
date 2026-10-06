from datetime import UTC, datetime, timedelta

from circuits.models import Circuit, CircuitType, Provider
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase, APIViewTestCases

from netbox_circuit_maintenance.choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
from netbox_circuit_maintenance.models import CircuitImpact, CircuitMaintenance, Note

START = datetime(2020, 10, 4, 10, 0, tzinfo=UTC)
END = START + timedelta(hours=2)


class AppTest(APITestCase):
    def test_root(self):
        url = reverse('plugins-api:netbox_circuit_maintenance-api:api-root')
        response = self.client.get(f'{url}?format=api', **self.header)

        self.assertEqual(response.status_code, 200)


# GraphQLTestCase is left out until the GraphQL rows are ported (Decisions 2026-10-06)
class CircuitMaintenanceTest(
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
    APIViewTestCases.CreateObjectViewTestCase,
    APIViewTestCases.UpdateObjectViewTestCase,
    APIViewTestCases.DeleteObjectViewTestCase,
):
    model = CircuitMaintenance
    view_namespace = 'plugins-api:netbox_circuit_maintenance'
    brief_fields = ['description', 'display', 'end_time', 'id', 'name', 'start_time', 'url']
    update_data = {
        'name': 'UT-TEST-7',
        'status': CircuitMaintenanceStatusChoices.CONFIRMED,
        'ack': True,
        'description': 'New description',
    }
    bulk_update_data = {
        'status': CircuitMaintenanceStatusChoices.CANCELLED,
    }

    @classmethod
    def setUpTestData(cls):
        # No parsed notifications: deleting a maintenance cascades to them, and they have no REST serializer yet
        CircuitMaintenance.objects.bulk_create(
            (
                CircuitMaintenance(name='UT-TEST-1', start_time=START, end_time=END),
                CircuitMaintenance(
                    name='UT-TEST-2', start_time=START + timedelta(days=1), end_time=END + timedelta(days=1)
                ),
                CircuitMaintenance(
                    name='UT-TEST-3', start_time=START + timedelta(days=2), end_time=END + timedelta(days=2)
                ),
            )
        )

        cls.create_data = [
            {
                'name': 'UT-TEST-4',
                'start_time': START + timedelta(days=5),
                'end_time': END + timedelta(days=5),
                'description': 'TEST1',
            },
            {
                'name': 'UT-TEST-5',
                'start_time': START + timedelta(days=6),
                'end_time': END + timedelta(days=6),
                'status': CircuitMaintenanceStatusChoices.CONFIRMED,
                'ack': True,
            },
            {
                'name': 'UT-TEST-6',
                'start_time': START + timedelta(days=7),
                'end_time': END + timedelta(days=7),
                # Longer than PrimaryModel's 200-character description
                'description': 'x' * 1000,
            },
        ]

    def test_create_object_default_status(self):
        self.add_permissions('netbox_circuit_maintenance.add_circuitmaintenance')

        response = self.client.post(self._get_list_url(), self.create_data[0], format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status']['value'], CircuitMaintenanceStatusChoices.TENTATIVE)
        self.assertIs(response.data['ack'], False)

    def test_create_object_without_name(self):
        # Nautobot's model default made the name optional in its serializer, and model validation then rejected it
        self.add_permissions('netbox_circuit_maintenance.add_circuitmaintenance')
        data = {key: value for key, value in self.create_data[0].items() if key != 'name'}

        response = self.client.post(self._get_list_url(), data, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(list(response.data), ['name'])

    def test_create_object_end_before_start(self):
        # NetBox's serializer runs the model's full_clean(), so the non-field error from clean() arrives under __all__
        self.add_permissions('netbox_circuit_maintenance.add_circuitmaintenance')
        data = {**self.create_data[0], 'start_time': END, 'end_time': START}

        response = self.client.post(self._get_list_url(), data, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {'__all__': ['End time should be greater than start time.']})


# GraphQLTestCase is left out until the GraphQL rows are ported (Decisions 2026-10-06)
class CircuitImpactTest(
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
    APIViewTestCases.CreateObjectViewTestCase,
    APIViewTestCases.UpdateObjectViewTestCase,
    APIViewTestCases.DeleteObjectViewTestCase,
):
    model = CircuitImpact
    view_namespace = 'plugins-api:netbox_circuit_maintenance'
    brief_fields = ['circuit', 'display', 'id', 'impact', 'maintenance', 'url']
    update_data = {
        'impact': CircuitImpactChoices.DEGRADED,
    }
    bulk_update_data = {
        'impact': CircuitImpactChoices.NO_IMPACT,
    }

    @classmethod
    def setUpTestData(cls):
        providers = (
            Provider(name='Provider 1', slug='provider-1'),
            Provider(name='Provider 2', slug='provider-2'),
        )
        Provider.objects.bulk_create(providers)

        circuit_types = (
            CircuitType(name='Circuit Type 1', slug='circuit-type-1'),
            CircuitType(name='Circuit Type 2', slug='circuit-type-2'),
        )
        CircuitType.objects.bulk_create(circuit_types)

        cls.circuits = (
            Circuit(cid='Circuit 1', provider=providers[0], type=circuit_types[0]),
            Circuit(cid='Circuit 2', provider=providers[1], type=circuit_types[1]),
            Circuit(cid='Circuit 3', provider=providers[1], type=circuit_types[0]),
        )
        Circuit.objects.bulk_create(cls.circuits)

        cls.maintenances = (
            CircuitMaintenance(name='UT-TEST1', start_time=START, end_time=END, description='TEST1'),
            CircuitMaintenance(
                name='UT-TEST2',
                start_time=START + timedelta(days=1),
                end_time=END + timedelta(days=1),
                description='TEST2',
            ),
        )
        CircuitMaintenance.objects.bulk_create(cls.maintenances)

        CircuitImpact.objects.bulk_create(
            (
                CircuitImpact(maintenance=cls.maintenances[0], circuit=cls.circuits[0]),
                CircuitImpact(maintenance=cls.maintenances[0], circuit=cls.circuits[1]),
                CircuitImpact(maintenance=cls.maintenances[0], circuit=cls.circuits[2]),
            )
        )

        cls.create_data = [
            {
                'maintenance': cls.maintenances[1].pk,
                'circuit': cls.circuits[0].pk,
                'impact': CircuitImpactChoices.NO_IMPACT,
            },
            # Nautobot's second case: no impact given
            {
                'maintenance': cls.maintenances[1].pk,
                'circuit': cls.circuits[1].pk,
            },
            {
                'maintenance': cls.maintenances[1].pk,
                'circuit': cls.circuits[2].pk,
                'impact': CircuitImpactChoices.REDUCED_REDUNDANCY,
            },
        ]

    def test_create_object_default_impact(self):
        self.add_permissions('netbox_circuit_maintenance.add_circuitimpact')

        response = self.client.post(self._get_list_url(), self.create_data[1], format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(response.data['impact']['value'], CircuitImpactChoices.OUTAGE)

    def test_create_object_duplicate(self):
        # The (maintenance, circuit) constraint is validated, not left to the database
        self.add_permissions('netbox_circuit_maintenance.add_circuitimpact')
        data = {'maintenance': self.maintenances[0].pk, 'circuit': self.circuits[0].pk}

        response = self.client.post(self._get_list_url(), data, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data, {'__all__': ['Circuit impact with this Maintenance and Circuit already exists.']}
        )


# GraphQLTestCase is left out until the GraphQL rows are ported (Decisions 2026-10-06)
class NoteTest(
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
    APIViewTestCases.CreateObjectViewTestCase,
    APIViewTestCases.UpdateObjectViewTestCase,
    APIViewTestCases.DeleteObjectViewTestCase,
):
    model = Note
    view_namespace = 'plugins-api:netbox_circuit_maintenance'
    brief_fields = ['display', 'id', 'level', 'maintenance', 'title', 'url']
    update_data = {
        'title': 'Note 7',
        'level': NoteLevelChoices.WARNING,
        'comment': 'New comment',
    }
    bulk_update_data = {
        'level': NoteLevelChoices.WARNING,
    }

    @classmethod
    def setUpTestData(cls):
        cls.maintenances = (
            CircuitMaintenance(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance(
                name='UT-TEST-2', start_time=START + timedelta(days=1), end_time=END + timedelta(days=1)
            ),
        )
        CircuitMaintenance.objects.bulk_create(cls.maintenances)

        Note.objects.bulk_create(
            (
                Note(maintenance=cls.maintenances[0], title='Note 1', comment='comment 1'),
                Note(maintenance=cls.maintenances[0], title='Note 2', comment='comment 2'),
                Note(maintenance=cls.maintenances[0], title='Note 3', comment='comment 3'),
            )
        )

        cls.create_data = [
            {
                'maintenance': cls.maintenances[1].pk,
                'title': 'Note 4',
                'level': NoteLevelChoices.WARNING,
                'comment': 'comment 4',
            },
            {
                'maintenance': cls.maintenances[1].pk,
                'title': 'Note 5',
                'comment': 'comment 5',
            },
            # The same title on another maintenance
            {
                'maintenance': cls.maintenances[1].pk,
                'title': 'Note 1',
                'level': NoteLevelChoices.INFO,
                'comment': 'comment 6',
            },
        ]

    def test_create_object_default_level(self):
        self.add_permissions('netbox_circuit_maintenance.add_note')

        response = self.client.post(self._get_list_url(), self.create_data[1], format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(response.data['level']['value'], NoteLevelChoices.INFO)

    def test_create_object_duplicate(self):
        # The (maintenance, title) constraint is validated, not left to the database
        self.add_permissions('netbox_circuit_maintenance.add_note')
        data = {'maintenance': self.maintenances[0].pk, 'title': 'Note 1', 'comment': 'comment 7'}

        response = self.client.post(self._get_list_url(), data, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {'__all__': ['Note with this Maintenance and Title already exists.']})

    def test_list_objects_filtered(self):
        # Nautobot's note API had no filterset
        self.add_permissions('netbox_circuit_maintenance.view_note')

        response = self.client.get(
            self._get_list_url(), {'maintenance_id': self.maintenances[0].pk, 'q': 'Note 2'}, **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual([note['title'] for note in response.data['results']], ['Note 2'])
