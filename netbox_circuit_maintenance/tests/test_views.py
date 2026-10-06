from datetime import UTC, datetime, timedelta

from circuits.models import Circuit, CircuitTermination, CircuitType, Provider
from core.models import ObjectType
from dcim.models import Site
from django.test import override_settings
from django.urls import reverse
from netbox.choices import CSVDelimiterChoices, ImportFormatChoices
from users.models import ObjectPermission
from utilities.testing import ViewTestCases, create_tags, post_data

from netbox_circuit_maintenance.choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices
from netbox_circuit_maintenance.models import CircuitImpact, CircuitMaintenance, ParsedNotification, RawNotification

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
        # No notes or parsed notifications: a maintenance delete cascades to them, and under event tracking each deleted
        # object needs a REST serializer, which those models don't have yet. Impacts are covered by
        # test_delete_object_with_impacts.
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

    def test_get_object_circuits_panel(self):
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        self.add_permissions(
            'netbox_circuit_maintenance.view_circuitmaintenance',
            'netbox_circuit_maintenance.view_circuitimpact',
            'netbox_circuit_maintenance.add_circuitimpact',
        )

        response = self.client.get(maintenance.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Circuits')
        # The table loads over HTMX from the impact list, filtered to this maintenance
        self.assertContains(response, f'maintenance_id={maintenance.pk}')
        # Add pre-fills the maintenance and returns to its page
        add_url = reverse('plugins:netbox_circuit_maintenance:circuitimpact_add')
        self.assertContains(response, f'{add_url}?maintenance={maintenance.pk}&amp;return_url=')

    def test_get_object_circuits_panel_hidden_without_permission(self):
        # Panel and Add button gate on CircuitImpact's own permissions, not the maintenance's
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        response = self.client.get(maintenance.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, f'maintenance_id={maintenance.pk}')
        self.assertNotContains(response, reverse('plugins:netbox_circuit_maintenance:circuitimpact_add'))

    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_delete_object_with_impacts(self):
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
        circuit = Circuit.objects.create(cid='CID-1', provider=provider, type=circuit_type)
        impact = CircuitImpact.objects.create(maintenance=maintenance, circuit=circuit)
        self.add_permissions('netbox_circuit_maintenance.delete_circuitmaintenance')

        # The confirmation lists the impact with a link, and the delete serializes it for events
        response = self.client.get(self._get_url('delete', maintenance))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, impact.get_absolute_url())
        response = self.client.post(self._get_url('delete', maintenance), post_data({'confirm': True}))
        self.assertHttpStatus(response, 302)
        self.assertFalse(CircuitImpact.objects.filter(pk=impact.pk).exists())

    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_get_object_changelog_with_impact(self):
        # An impact added through the UI is listed in the maintenance's changelog, linked to the impact
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
        circuit = Circuit.objects.create(cid='CID-1', provider=provider, type=circuit_type)
        self.add_permissions('netbox_circuit_maintenance.add_circuitimpact')

        response = self.client.post(
            reverse('plugins:netbox_circuit_maintenance:circuitimpact_add'),
            post_data({'maintenance': maintenance.pk, 'circuit': circuit.pk, 'impact': CircuitImpactChoices.OUTAGE}),
        )
        self.assertHttpStatus(response, 302)
        impact = CircuitImpact.objects.get(maintenance=maintenance, circuit=circuit)

        response = self.client.get(self._get_url('changelog', maintenance))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, f'<a href="{impact.get_absolute_url()}">{impact}</a>', html=True)


class CircuitImpactTestCase(PluginViewTestMixin, ViewTestCases.OrganizationalObjectViewTestCase):
    model = CircuitImpact

    @classmethod
    def setUpTestData(cls):
        providers = (
            Provider(name='Provider 3', slug='provider-3'),
            Provider(name='Provider 4', slug='provider-4'),
        )
        Provider.objects.bulk_create(providers)

        circuit_types = (
            CircuitType(name='Circuit Type 3', slug='circuit-type-3'),
            CircuitType(name='Circuit Type 4', slug='circuit-type-4'),
        )
        CircuitType.objects.bulk_create(circuit_types)

        circuits = (
            Circuit(cid='Circuit 4', provider=providers[0], type=circuit_types[0]),
            Circuit(cid='Circuit 5', provider=providers[1], type=circuit_types[1]),
            Circuit(cid='Circuit 6', provider=providers[1], type=circuit_types[0]),
            Circuit(cid='Circuit 7', provider=providers[1], type=circuit_types[0]),
            Circuit(cid='Circuit 8', provider=providers[1], type=circuit_types[0]),
        )
        Circuit.objects.bulk_create(circuits)

        maintenances = (
            CircuitMaintenance(name='UT-TEST-3', start_time=START, end_time=END),
            CircuitMaintenance(
                name='UT-TEST-4', start_time=START + timedelta(days=1), end_time=END + timedelta(days=1)
            ),
        )
        CircuitMaintenance.objects.bulk_create(maintenances)

        impacts = (
            CircuitImpact(maintenance=maintenances[0], circuit=circuits[0]),
            CircuitImpact(maintenance=maintenances[1], circuit=circuits[0]),
            CircuitImpact(maintenance=maintenances[1], circuit=circuits[-1]),
        )
        CircuitImpact.objects.bulk_create(impacts)

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        cls.form_data = {
            'maintenance': maintenances[0].pk,
            'circuit': circuits[1].pk,
            'impact': CircuitImpactChoices.NO_IMPACT,
            'tags': [t.pk for t in tags],
        }

        cls.csv_data = {
            'default': (
                'maintenance,circuit,impact',
                f'{maintenances[0].name},{circuits[2].cid},NO-IMPACT',
                f'{maintenances[0].name},{circuits[3].cid},OUTAGE',
                f'{maintenances[0].name},{circuits[4].cid},DEGRADED',
            ),
            # Impact defaults to OUTAGE, as with Nautobot's serializer-based import
            'without_impact': (
                'maintenance,circuit',
                f'{maintenances[1].name},{circuits[1].cid}',
                f'{maintenances[1].name},{circuits[2].cid}',
            ),
        }

        cls.csv_update_data = (
            'id,impact',
            f'{impacts[0].pk},NO-IMPACT',
            f'{impacts[1].pk},REDUCED-REDUNDANCY',
            f'{impacts[2].pk},DEGRADED',
        )

        # Not Nautobot's OUTAGE, which every impact already has by default
        cls.bulk_edit_data = {
            'impact': CircuitImpactChoices.DEGRADED,
        }

    def _import(self, data):
        return self.client.post(
            self._get_url('bulk_import'),
            {'data': data, 'format': ImportFormatChoices.CSV, 'csv_delimiter': CSVDelimiterChoices.AUTO},
        )

    # The import form only offers maintenances and circuits the user can view
    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_bulk_import_default_impact(self):
        self.add_permissions('netbox_circuit_maintenance.add_circuitimpact')

        self.assertHttpStatus(self._import('\n'.join(self.csv_data['without_impact'])), 302)
        impact = CircuitImpact.objects.get(maintenance__name='UT-TEST-4', circuit__cid='Circuit 5')
        self.assertEqual(impact.impact, CircuitImpactChoices.OUTAGE)

        # A blank cell counts as omitted (NetBox's CSVSelectWidget)
        self.assertHttpStatus(self._import('maintenance,circuit,impact\nUT-TEST-4,Circuit 7,'), 302)
        impact = CircuitImpact.objects.get(maintenance__name='UT-TEST-4', circuit__cid='Circuit 7')
        self.assertEqual(impact.impact, CircuitImpactChoices.OUTAGE)

    # The import form only offers maintenances and circuits the user can view
    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_bulk_import_ambiguous_cid(self):
        # A CID is unique only per provider: an ambiguous one is rejected, and a circuit.id column picks the circuit
        provider = Provider.objects.create(name='Provider 5', slug='provider-5')
        circuit = Circuit.objects.create(cid='Circuit 6', provider=provider, type=CircuitType.objects.first())
        self.add_permissions('netbox_circuit_maintenance.add_circuitimpact')

        response = self._import('maintenance,circuit\nUT-TEST-4,Circuit 6')
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'is not a unique value for this field')

        self.assertHttpStatus(self._import(f'maintenance,circuit.id\nUT-TEST-4,{circuit.pk}'), 302)
        self.assertTrue(CircuitImpact.objects.filter(maintenance__name='UT-TEST-4', circuit=circuit).exists())

    def test_create_object_prefilled_maintenance(self):
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-4')
        self.add_permissions('netbox_circuit_maintenance.add_circuitimpact')

        response = self.client.get(self._get_url('add'), {'maintenance': maintenance.pk})
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context['form']['maintenance'].value(), str(maintenance.pk))

    def test_list_objects_circuit_columns(self):
        # The columns the maintenance's Circuits panel asks for
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-3')
        circuit = Circuit.objects.get(cid='Circuit 4')
        circuit.description = 'Circuit 4 description'
        circuit.save()
        # Saving the termination caches it on the circuit (Circuit.termination_a)
        site = Site.objects.create(name='Site 1', slug='site-1')
        CircuitTermination.objects.create(circuit=circuit, term_side='A', termination=site)
        self.add_permissions('netbox_circuit_maintenance.view_circuitimpact')

        response = self.client.get(
            self._get_url('list'),
            {
                'maintenance_id': maintenance.pk,
                'include_columns': (
                    'circuit,impact,provider,circuit_type,circuit_status,termination_a,termination_z,circuit_description'
                ),
                'exclude_columns': 'maintenance',
            },
        )
        self.assertHttpStatus(response, 200)
        table = response.context['table']
        self.assertEqual([str(impact) for impact in table.data], ['Circuit Circuit 4 with impact Outage'])
        # Nautobot's column order (no pk checkbox: this user has no bulk actions)
        self.assertEqual(
            [column.name for column in table.columns if column.visible],
            [
                'circuit',
                'impact',
                'provider',
                'circuit_type',
                'circuit_status',
                'termination_a',
                'termination_z',
                'circuit_description',
                'actions',
            ],
        )
        for text in ('Side A', 'Circuit status', site.get_absolute_url(), 'Circuit Type 3', 'Circuit 4 description'):
            self.assertContains(response, text)
        self.assertNotContains(response, maintenance.get_absolute_url())

    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_delete_circuit_with_impacts(self):
        # Deleting a core circuit cascades to its impacts, which under event tracking need the REST serializer
        circuit = Circuit.objects.get(cid='Circuit 4')
        obj_perm = ObjectPermission(name='Test permission', actions=['delete'])
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(Circuit))

        response = self.client.get(reverse('circuits:circuit_delete', kwargs={'pk': circuit.pk}))
        self.assertHttpStatus(response, 200)
        response = self.client.post(
            reverse('circuits:circuit_delete', kwargs={'pk': circuit.pk}), post_data({'confirm': True})
        )
        self.assertHttpStatus(response, 302)
        self.assertFalse(CircuitImpact.objects.filter(circuit_id=circuit.pk).exists())
