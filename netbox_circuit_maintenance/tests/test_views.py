from datetime import UTC, datetime, timedelta

from circuits.models import Provider
from django.urls import reverse
from netbox.choices import CSVDelimiterChoices, ImportFormatChoices
from utilities.testing import ViewTestCases, create_tags

from netbox_circuit_maintenance.choices import CircuitMaintenanceStatusChoices
from netbox_circuit_maintenance.models import CircuitMaintenance, ParsedNotification, RawNotification

START = datetime(2020, 10, 4, 10, 0, tzinfo=UTC)
END = START + timedelta(hours=2)


class PluginViewTestMixin:
    # NetBox's view test cases reverse '<app_label>:<model>_<action>'; plugin views are in the 'plugins' namespace
    def _get_base_url(self):
        return f'plugins:{super()._get_base_url()}'


class CircuitMaintenanceTestCase(PluginViewTestMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = CircuitMaintenance

    @classmethod
    def setUpTestData(cls):
        # No impacts, notes or parsed notifications: a maintenance delete cascades to them, and under event tracking
        # each deleted object needs a REST serializer, which those models don't have yet
        maintenances = (
            CircuitMaintenance(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance(
                name='UT-TEST-2', start_time=START + timedelta(days=1), end_time=END + timedelta(days=1)
            ),
            CircuitMaintenance(
                name='UT-TEST-3', start_time=START + timedelta(days=2), end_time=END + timedelta(days=2)
            ),
        )
        CircuitMaintenance.objects.bulk_create(maintenances)

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        cls.form_data = {
            'name': 'UT-TEST-10',
            'status': CircuitMaintenanceStatusChoices.CONFIRMED,
            'start_time': datetime(2020, 10, 6, 10, 0, tzinfo=UTC),
            'end_time': datetime(2020, 10, 6, 12, 0, tzinfo=UTC),
            'ack': True,
            'description': 'TEST 0 descr',
            'comments': 'Some comments',
            'tags': [t.pk for t in tags],
        }

        cls.csv_data = {
            'default': (
                'name,start_time,end_time,description,status',
                'UT-TEST-20,2020-10-06T10:00:00Z,2020-10-06T12:00:00Z,TEST 20 descr,TENTATIVE',
                'UT-TEST-21,2020-10-06T10:00:00Z,2020-10-06T12:00:00Z,TEST 21 descr,CONFIRMED',
                'UT-TEST-22,2020-10-06T10:00:00Z,2020-10-06T12:00:00Z,TEST 22 descr,RE-SCHEDULED',
            ),
            # Status defaults to TENTATIVE, as with Nautobot's serializer-based import
            'without_status': (
                'name,start_time,end_time,ack',
                'UT-TEST-30,2020-10-07T10:00:00Z,2020-10-07T12:00:00Z,true',
                'UT-TEST-31,2020-10-07T10:00:00Z,2020-10-07T12:00:00Z,false',
            ),
        }

        cls.csv_update_data = (
            'id,status,ack,description',
            f'{maintenances[0].pk},CONFIRMED,true,New description 1',
            f'{maintenances[1].pk},CANCELLED,true,New description 2',
            f'{maintenances[2].pk},COMPLETED,false,New description 3',
        )

        cls.bulk_edit_data = {
            'status': CircuitMaintenanceStatusChoices.CANCELLED,
            'ack': True,
            # Longer than PrimaryModelBulkEditForm's 100-character description limit
            'description': 'x' * 150,
        }

    def test_bulk_import_default_status(self):
        self.add_permissions('netbox_circuit_maintenance.add_circuitmaintenance')
        data = {
            'data': '\n'.join(self.csv_data['without_status']),
            'format': ImportFormatChoices.CSV,
            'csv_delimiter': CSVDelimiterChoices.AUTO,
        }

        self.assertHttpStatus(self.client.post(self._get_url('bulk_import'), data), 302)
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-30')
        self.assertEqual(maintenance.status, CircuitMaintenanceStatusChoices.TENTATIVE)
        self.assertTrue(maintenance.ack)

    def test_bulk_update_end_before_start(self):
        # A CSV update without an end_time column must report the model's non-field error, not crash
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        self.add_permissions(
            'netbox_circuit_maintenance.add_circuitmaintenance',
            'netbox_circuit_maintenance.change_circuitmaintenance',
        )
        data = {
            'data': f'id,start_time\n{maintenance.pk},2020-10-05T10:00:00Z',
            'format': ImportFormatChoices.CSV,
            'csv_delimiter': CSVDelimiterChoices.AUTO,
        }

        response = self.client.post(self._get_url('bulk_import'), data)
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'End time should be greater than start time.')
        maintenance.refresh_from_db()
        self.assertEqual(maintenance.start_time, START)

    def test_list_objects_ordering(self):
        # Most recent first, as in Nautobot, although the model orders by ascending start time
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        response = self.client.get(self._get_url('list'))
        self.assertHttpStatus(response, 200)
        self.assertEqual(
            [maintenance.name for maintenance in response.context['table'].data],
            ['UT-TEST-3', 'UT-TEST-2', 'UT-TEST-1'],
        )

    def test_global_search(self):
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        maintenance.save()  # bulk_create() skipped the post_save signal that caches search values
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        response = self.client.get(reverse('search'), {'q': 'UT-TEST-1'})
        self.assertHttpStatus(response, 200)
        self.assertContains(response, maintenance.get_absolute_url())

    def test_get_object_notifying_provider(self):
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        providers = (
            Provider.objects.create(name='Provider 1', slug='provider-1'),
            Provider.objects.create(name='Provider 2', slug='provider-2'),
        )
        # The least recently updated parsed notification names the provider, even when another one has an earlier stamp
        for subject, provider, stamp in (('Subject 1', providers[0], END), ('Subject 2', providers[1], START)):
            raw_notification = RawNotification.objects.create(
                raw=b'body', subject=subject, provider=provider, stamp=stamp
            )
            ParsedNotification.objects.create(
                maintenance=maintenance, raw_notification=raw_notification, json={'maintenance_id': 'UT-TEST-1'}
            )
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        response = self.client.get(maintenance.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Notifying provider')
        self.assertContains(response, providers[0].get_absolute_url())
        self.assertNotContains(response, providers[1].get_absolute_url())
