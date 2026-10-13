from datetime import UTC, datetime, timedelta

from circuits.models import Circuit, CircuitType, Provider
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase, APIViewTestCases, GraphQLFilterTest, GraphQLQueryTest

from netbox_circuit_maintenance.choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
from netbox_circuit_maintenance.graphql.types import NotificationSourceType, RawNotificationType
from netbox_circuit_maintenance.models import (
    CircuitImpact,
    CircuitMaintenance,
    Note,
    NotificationSource,
    ParsedNotification,
    RawNotification,
)

START = datetime(2020, 10, 4, 10, 0, tzinfo=UTC)
END = START + timedelta(hours=2)


class AppTest(APITestCase):
    def test_root(self):
        url = reverse('plugins-api:netbox_circuit_maintenance-api:api-root')
        response = self.client.get(f'{url}?format=api', **self.header)

        self.assertEqual(response.status_code, 200)


class CircuitMaintenanceTest(APIViewTestCases.APIViewTestCase):
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
    graphql_filter_tests = (
        # Nautobot's filtered query
        GraphQLFilterTest(name='name', filters='name: {exact: "UT-TEST-1"}', expected={'name': 'UT-TEST-1'}),
        # Enum keys replace the hyphen
        GraphQLFilterTest(
            name='status',
            filters='status: {exact: STATUS_IN_PROCESS}',
            expected={'status': CircuitMaintenanceStatusChoices.IN_PROCESS},
        ),
        # Through the impacts, each maintenance once
        GraphQLFilterTest(
            name='impacts_provider_distinct',
            filters='impacts: {circuit: {provider: {slug: {exact: "provider-1"}}}}, DISTINCT: true',
            expected={'name__in': ('UT-TEST-1', 'UT-TEST-2')},
        ),
    )
    graphql_query_tests = (
        # Without DISTINCT, a maintenance comes once per matching impact (strawberry_django, as for core filters)
        GraphQLQueryTest(
            name='impacts_provider',
            query='{ circuit_maintenance_list(filters: {impacts: {circuit: {provider: {slug: {exact: "provider-1"}}}}}) '
            '{ name } }',
            assert_result=lambda test, data: test.assertEqual(
                [maintenance['name'] for maintenance in data['circuit_maintenance_list']],
                ['UT-TEST-1', 'UT-TEST-1', 'UT-TEST-2'],
            ),
        ),
    )

    @classmethod
    def setUpTestData(cls):
        maintenances = (
            CircuitMaintenance(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance(
                name='UT-TEST-2',
                start_time=START + timedelta(days=1),
                end_time=END + timedelta(days=1),
                status=CircuitMaintenanceStatusChoices.IN_PROCESS,
            ),
            CircuitMaintenance(
                name='UT-TEST-3', start_time=START + timedelta(days=2), end_time=END + timedelta(days=2)
            ),
        )
        CircuitMaintenance.objects.bulk_create(maintenances)

        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
        circuits = (
            Circuit(cid='Circuit 1', provider=provider, type=circuit_type),
            Circuit(cid='Circuit 2', provider=provider, type=circuit_type),
        )
        Circuit.objects.bulk_create(circuits)
        CircuitImpact.objects.bulk_create(
            (
                CircuitImpact(maintenance=maintenances[0], circuit=circuits[0]),
                CircuitImpact(maintenance=maintenances[0], circuit=circuits[1]),
                CircuitImpact(maintenance=maintenances[1], circuit=circuits[0]),
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


class CircuitImpactTest(APIViewTestCases.APIViewTestCase):
    model = CircuitImpact
    view_namespace = 'plugins-api:netbox_circuit_maintenance'
    # GraphQL returns an error for a required relation the user can't view
    user_permissions = ('netbox_circuit_maintenance.view_circuitmaintenance', 'circuits.view_circuit')
    brief_fields = ['circuit', 'display', 'id', 'impact', 'maintenance', 'url']
    update_data = {
        'impact': CircuitImpactChoices.DEGRADED,
    }
    bulk_update_data = {
        'impact': CircuitImpactChoices.NO_IMPACT,
    }
    graphql_filter_tests = (
        # Nautobot's filtered query
        GraphQLFilterTest(
            name='impact', filters='impact: {exact: IMPACT_OUTAGE}', expected={'impact': CircuitImpactChoices.OUTAGE}
        ),
        # Enum keys replace the hyphen
        GraphQLFilterTest(
            name='impact_hyphen',
            filters='impact: {exact: IMPACT_REDUCED_REDUNDANCY}',
            expected={'impact': CircuitImpactChoices.REDUCED_REDUNDANCY},
        ),
        GraphQLFilterTest(
            name='circuit', filters='circuit: {cid: {exact: "Circuit 2"}}', expected={'circuit__cid': 'Circuit 2'}
        ),
    )

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
                # Not NO_IMPACT, which test_bulk_update_objects sets: an unchanged row records no change
                CircuitImpact(
                    maintenance=cls.maintenances[0],
                    circuit=cls.circuits[1],
                    impact=CircuitImpactChoices.REDUCED_REDUNDANCY,
                ),
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


class NoteTest(APIViewTestCases.APIViewTestCase):
    model = Note
    view_namespace = 'plugins-api:netbox_circuit_maintenance'
    # GraphQL returns an error for a required relation the user can't view
    user_permissions = ('netbox_circuit_maintenance.view_circuitmaintenance',)
    brief_fields = ['display', 'id', 'level', 'maintenance', 'title', 'url']
    update_data = {
        'title': 'Note 7',
        'level': NoteLevelChoices.WARNING,
        'comment': 'New comment',
    }
    bulk_update_data = {
        'level': NoteLevelChoices.WARNING,
    }
    graphql_filter_tests = (
        GraphQLFilterTest(
            name='level', filters='level: {exact: LEVEL_WARNING}', expected={'level': NoteLevelChoices.WARNING}
        ),
    )

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
                # Last in the default order, so test_bulk_update_objects, which sets WARNING, doesn't take it
                Note(
                    maintenance=cls.maintenances[0], title='Note 6', level=NoteLevelChoices.WARNING, comment='comment 6'
                ),
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


class NotificationSourceTest(
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
    APIViewTestCases.GraphQLTestCase,
):
    model = NotificationSource
    view_namespace = 'plugins-api:netbox_circuit_maintenance'
    brief_fields = ['display', 'id', 'name', 'url']
    graphql_filter_tests = (
        GraphQLFilterTest(
            name='providers', filters='providers: {slug: {exact: "provider-2"}}', expected={'name': 'Source 2'}
        ),
    )

    @classmethod
    def setUpTestData(cls):
        cls.providers = (
            Provider(name='Provider 1', slug='provider-1'),
            Provider(name='Provider 2', slug='provider-2'),
        )
        Provider.objects.bulk_create(cls.providers)

        sources = (
            NotificationSource(name='Source 1', _token={'refresh_token': 'refresh-token-value'}),
            NotificationSource(name='Source 2', attach_all_providers=True),
            NotificationSource(name='Source 3'),
        )
        NotificationSource.objects.bulk_create(sources)
        sources[0].providers.set(cls.providers[:1])
        sources[1].providers.set(cls.providers)

    def test_get_object_providers_without_token(self):
        source = NotificationSource.objects.get(name='Source 1')
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')

        response = self.client.get(self._get_detail_url(source), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual([provider['id'] for provider in response.data['providers']], [self.providers[0].pk])
        self.assertFalse(response.data['attach_all_providers'])
        self.assertNotIn('_token', response.data)
        self.assertNotContains(response, 'refresh-token-value')

    def test_read_only(self):
        # Sources come from the plugin configuration
        source = NotificationSource.objects.first()
        self.add_permissions(
            'netbox_circuit_maintenance.add_notificationsource',
            'netbox_circuit_maintenance.change_notificationsource',
            'netbox_circuit_maintenance.delete_notificationsource',
        )

        response = self.client.post(self._get_list_url(), {'name': 'Source 4'}, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)
        response = self.client.patch(
            self._get_detail_url(source), {'providers': [self.providers[1].pk]}, format='json', **self.header
        )
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)
        response = self.client.delete(self._get_detail_url(source), **self.header)
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_list_objects_filtered(self):
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')

        response = self.client.get(self._get_list_url(), {'provider': self.providers[1].slug}, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual([item['name'] for item in response.data['results']], ['Source 2'])

    def test_graphql_without_token(self):
        source = NotificationSource.objects.get(name='Source 1')
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')
        self.assertNotIn('_token', {field.name for field in NotificationSourceType.__strawberry_definition__.fields})

        # Every field the type has
        query = self._build_query('notification_source', id=source.pk)
        response = self.client.post(reverse('graphql'), {'query': query}, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertNotIn('errors', response.json())
        self.assertEqual(response.json()['data']['notification_source']['name'], 'Source 1')
        self.assertNotContains(response, 'refresh-token-value')

        query = f'{{ notification_source(id: {source.pk}) {{ _token }} }}'
        response = self.client.post(reverse('graphql'), {'query': query}, format='json', **self.header)
        self.assertIn("Cannot query field '_token'", response.json()['errors'][0]['message'])


class RawNotificationTest(
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
    APIViewTestCases.GraphQLTestCase,
):
    model = RawNotification
    view_namespace = 'plugins-api:netbox_circuit_maintenance'
    # GraphQL returns an error for a required relation the user can't view
    user_permissions = ('circuits.view_provider',)
    brief_fields = ['display', 'id', 'provider', 'stamp', 'subject', 'url']
    graphql_filter_tests = (
        GraphQLFilterTest(
            name='source', filters='source: {name: {exact: "Source 1"}}', expected={'subject': 'Subject 2'}
        ),
        # The REST filterset's since
        GraphQLFilterTest(
            name='stamp_gte',
            filters=f'stamp: {{gte: "{(START + timedelta(hours=1)).isoformat()}"}}',
            expected={'subject__in': ('Subject 2', 'Subject 3')},
        ),
    )

    @classmethod
    def setUpTestData(cls):
        cls.providers = (
            Provider(name='Provider 1', slug='provider-1'),
            Provider(name='Provider 2', slug='provider-2'),
        )
        Provider.objects.bulk_create(cls.providers)
        cls.source = NotificationSource.objects.create(name='Source 1')

        RawNotification.objects.bulk_create(
            (
                RawNotification(raw=b'body 1', subject='Subject 1', provider=cls.providers[0], stamp=START),
                RawNotification(
                    raw=b'body 2',
                    subject='Subject 2',
                    provider=cls.providers[1],
                    source=cls.source,
                    stamp=START + timedelta(hours=1),
                ),
                RawNotification(
                    raw=b'body 3', subject='Subject 3', provider=cls.providers[1], stamp=START + timedelta(hours=2)
                ),
            )
        )

    def test_get_object_raw_and_source(self):
        # raw is base64, as Nautobot's serializer returned it; source is a nested object, or null once deleted
        raw_notification = RawNotification.objects.get(subject='Subject 2')
        self.add_permissions('netbox_circuit_maintenance.view_rawnotification')

        response = self.client.get(self._get_detail_url(raw_notification), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data['raw'], 'Ym9keSAy')
        self.assertEqual(sorted(response.data['source']), ['display', 'id', 'name', 'url'])
        self.assertEqual((response.data['source']['id'], response.data['source']['name']), (self.source.pk, 'Source 1'))
        self.assertEqual(response.data['provider']['id'], self.providers[1].pk)

        response = self.client.get(
            self._get_detail_url(RawNotification.objects.get(subject='Subject 1')), **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertIsNone(response.data['source'])

    def test_read_only(self):
        # Only the notification handler creates raw notifications, and nobody edits them
        raw_notification = RawNotification.objects.first()
        self.add_permissions(
            'netbox_circuit_maintenance.add_rawnotification',
            'netbox_circuit_maintenance.change_rawnotification',
            'netbox_circuit_maintenance.delete_rawnotification',
        )

        response = self.client.post(self._get_list_url(), {}, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)
        response = self.client.patch(
            self._get_detail_url(raw_notification), {'parsed': True}, format='json', **self.header
        )
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)
        response = self.client.delete(self._get_detail_url(raw_notification), **self.header)
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_list_objects_filtered(self):
        self.add_permissions('netbox_circuit_maintenance.view_rawnotification')

        response = self.client.get(
            self._get_list_url(),
            {'provider': self.providers[1].slug, 'since': (START + timedelta(hours=2)).isoformat()},
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual([item['subject'] for item in response.data['results']], ['Subject 3'])

    def test_graphql_without_raw(self):
        # strawberry_django can't map the BinaryField
        self.add_permissions('netbox_circuit_maintenance.view_rawnotification')
        self.assertNotIn('raw', {field.name for field in RawNotificationType.__strawberry_definition__.fields})

        # Every field the type has
        query = self._build_query('raw_notification_list')
        response = self.client.post(reverse('graphql'), {'query': query}, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertNotIn('errors', response.json())
        self.assertEqual(len(response.json()['data']['raw_notification_list']), 3)
        self.assertNotContains(response, 'body 2')
        self.assertNotContains(response, 'Ym9keSAy')


class ParsedNotificationTest(
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
    APIViewTestCases.GraphQLTestCase,
):
    model = ParsedNotification
    view_namespace = 'plugins-api:netbox_circuit_maintenance'
    # GraphQL returns an error for a required relation the user can't view
    user_permissions = (
        'netbox_circuit_maintenance.view_circuitmaintenance',
        'netbox_circuit_maintenance.view_rawnotification',
    )
    brief_fields = ['display', 'id', 'maintenance', 'raw_notification', 'url']
    graphql_filter_tests = (
        GraphQLFilterTest(
            name='json',
            filters='json: {path: "status", lookup: {string_lookup: {exact: "CONFIRMED"}}}',
            expected={'json__status': 'CONFIRMED'},
        ),
    )

    @classmethod
    def setUpTestData(cls):
        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        cls.maintenances = (
            CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance.objects.create(name='UT-TEST-2', start_time=START, end_time=END),
        )
        cls.raw_notifications = (
            RawNotification.objects.create(raw=b'body 1', subject='Subject 1', provider=provider, stamp=START),
            RawNotification.objects.create(
                raw=b'body 2', subject='Subject 2', provider=provider, stamp=START + timedelta(hours=1)
            ),
        )

        ParsedNotification.objects.bulk_create(
            (
                ParsedNotification(
                    maintenance=cls.maintenances[0],
                    raw_notification=cls.raw_notifications[0],
                    json={'maintenance_id': 'UT-TEST-1', 'status': 'TENTATIVE'},
                ),
                ParsedNotification(
                    maintenance=cls.maintenances[0],
                    raw_notification=cls.raw_notifications[1],
                    json={'maintenance_id': 'UT-TEST-1', 'status': 'CONFIRMED'},
                ),
                ParsedNotification(
                    maintenance=cls.maintenances[1],
                    raw_notification=cls.raw_notifications[1],
                    json={'maintenance_id': 'UT-TEST-2', 'status': 'CONFIRMED'},
                ),
            )
        )

    def test_get_object_json_and_raw_notification(self):
        # json is an object, where Nautobot returned a string; the nested raw notification leaves out the body
        parsed_notification = ParsedNotification.objects.get(raw_notification=self.raw_notifications[0])
        self.add_permissions('netbox_circuit_maintenance.view_parsednotification')

        response = self.client.get(self._get_detail_url(parsed_notification), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data['json'], {'maintenance_id': 'UT-TEST-1', 'status': 'TENTATIVE'})
        self.assertEqual(response.data['maintenance']['id'], self.maintenances[0].pk)
        self.assertEqual(response.data['raw_notification']['subject'], 'Subject 1')
        self.assertNotIn('raw', response.data['raw_notification'])

    def test_read_only(self):
        # Only the notification handler creates parsed notifications, and nobody edits them
        parsed_notification = ParsedNotification.objects.first()
        self.add_permissions(
            'netbox_circuit_maintenance.add_parsednotification',
            'netbox_circuit_maintenance.change_parsednotification',
            'netbox_circuit_maintenance.delete_parsednotification',
        )

        response = self.client.post(self._get_list_url(), {}, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)
        response = self.client.patch(
            self._get_detail_url(parsed_notification), {'json': {}}, format='json', **self.header
        )
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)
        response = self.client.delete(self._get_detail_url(parsed_notification), **self.header)
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_list_objects_filtered(self):
        self.add_permissions('netbox_circuit_maintenance.view_parsednotification')

        response = self.client.get(
            self._get_list_url(),
            {'maintenance': 'UT-TEST-1', 'raw_notification_id': self.raw_notifications[1].pk},
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(
            [item['json'] for item in response.data['results']],
            [{'maintenance_id': 'UT-TEST-1', 'status': 'CONFIRMED'}],
        )

    def test_graphql_display(self):
        # The type builds display from an annotation rather than str(): no row loads its raw notification, body
        # included, in queries of its own, with or without a raw_notification selection next to it, and nested lists
        # carry the annotation too
        self.add_permissions(
            'netbox_circuit_maintenance.view_circuitmaintenance',
            'netbox_circuit_maintenance.view_parsednotification',
        )
        queries = {
            '{ parsed_notification_list { id display } }': lambda data: data['parsed_notification_list'],
            '{ parsed_notification_list { id display raw_notification { subject stamp } } }': (
                lambda data: data['parsed_notification_list']
            ),
            '{ circuit_maintenance_list { parsed_notifications { id display } } }': lambda data: [
                item for maintenance in data['circuit_maintenance_list'] for item in maintenance['parsed_notifications']
            ],
            '{ raw_notification_list { parsed_notifications { id display } } }': lambda data: [
                item for raw in data['raw_notification_list'] for item in raw['parsed_notifications']
            ],
        }

        def run(query):
            with CaptureQueriesContext(connection) as context:
                response = self.client.post(reverse('graphql'), {'query': query}, format='json', **self.header)
            self.assertHttpStatus(response, status.HTTP_200_OK)
            self.assertNotIn('errors', response.json())
            return queries[query](response.json()['data']), context.captured_queries

        for query in queries:
            run(query)  # Warm the caches
        counts = {query: len(run(query)[1]) for query in queries}

        # A 200-character subject, which display truncates
        raw_notification = RawNotification.objects.create(
            raw=b'body 3', subject='x' * 200, provider=self.raw_notifications[0].provider, stamp=START
        )
        ParsedNotification.objects.create(
            maintenance=self.maintenances[1], raw_notification=raw_notification, json={'status': 'TENTATIVE'}
        )

        for query, count in counts.items():
            with self.subTest(query=query):
                results, captured = run(query)
                self.assertEqual(len(captured), count)
                self.assertEqual(
                    sorted((int(item['id']), item['display']) for item in results),
                    sorted((item.pk, str(item)) for item in ParsedNotification.objects.all()),
                )
                self.assertIn('Parsed notification for ' + 'x' * 176, {item['display'] for item in results})
                self.assertFalse(
                    [
                        item['sql']
                        for item in captured
                        if '"netbox_circuit_maintenance_rawnotification"."raw"' in item['sql']
                    ]
                )


class GraphQLSchemaCoverageTest(APIViewTestCases.GraphQLSchemaCoverageTestCase):
    # Every root query type of the plugin has a GraphQLTestCase. NetBox's own suite covers the core types
    def get_schema_type_classes(self):
        return {
            type_class: model
            for type_class, model in super().get_schema_type_classes().items()
            if model._meta.app_label == 'netbox_circuit_maintenance'
        }
