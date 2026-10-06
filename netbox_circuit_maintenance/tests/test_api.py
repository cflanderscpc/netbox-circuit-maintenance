from datetime import UTC, datetime, timedelta

from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase, APIViewTestCases

from netbox_circuit_maintenance.choices import CircuitMaintenanceStatusChoices
from netbox_circuit_maintenance.models import CircuitMaintenance

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
        # No impacts, notes or parsed notifications: deleting a maintenance cascades to them, and they have no REST
        # serializer yet
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
