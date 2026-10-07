import csv
import io
import json
import uuid
from datetime import UTC, datetime, timedelta

from circuits.models import Circuit, CircuitTermination, CircuitType, Provider
from core.models import ObjectType
from dcim.models import Site
from django.test import RequestFactory, override_settings
from django.urls import reverse
from netbox.choices import CSVDelimiterChoices, ImportFormatChoices
from netbox.context_managers import event_tracking
from users.models import ObjectPermission
from utilities.testing import ViewTestCases, create_tags, post_data

from netbox_circuit_maintenance.choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
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


class PluginViewTestMixin:
    # NetBox's view test cases reverse '<app_label>:<model>_<action>'; plugin views are in the 'plugins' namespace
    def _get_base_url(self):
        return f'plugins:{super()._get_base_url()}'


class CircuitMaintenanceTestCase(PluginViewTestMixin, ViewTestCases.PrimaryObjectViewTestCase):
    model = CircuitMaintenance

    @classmethod
    def setUpTestData(cls):
        # Deleting a maintenance with impacts, notes and parsed notifications is covered by test_delete_object_with_children
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

    def test_get_object_notes_panel(self):
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        self.add_permissions(
            'netbox_circuit_maintenance.view_circuitmaintenance',
            'netbox_circuit_maintenance.view_note',
            'netbox_circuit_maintenance.add_note',
        )

        response = self.client.get(maintenance.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Notes')
        # The table loads over HTMX from the note list, filtered to this maintenance
        list_url = reverse('plugins:netbox_circuit_maintenance:note_list')
        self.assertContains(response, f'{list_url}?embedded=True&maintenance_id={maintenance.pk}')
        # Add pre-fills the maintenance and returns to its page
        add_url = reverse('plugins:netbox_circuit_maintenance:note_add')
        self.assertContains(response, f'{add_url}?maintenance={maintenance.pk}&amp;return_url=')

    def test_get_object_notes_panel_hidden_without_permission(self):
        # Panel and Add button gate on Note's own permissions, not the maintenance's
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        response = self.client.get(maintenance.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, reverse('plugins:netbox_circuit_maintenance:note_list'))
        self.assertNotContains(response, reverse('plugins:netbox_circuit_maintenance:note_add'))

    def test_get_object_notifications_panel(self):
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        self.add_permissions(
            'netbox_circuit_maintenance.view_circuitmaintenance',
            'netbox_circuit_maintenance.view_parsednotification',
        )

        response = self.client.get(maintenance.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Notifications')
        # The table loads over HTMX from the parsed notification list, filtered to this maintenance
        list_url = reverse('plugins:netbox_circuit_maintenance:parsednotification_list')
        self.assertContains(response, f'{list_url}?embedded=True&maintenance_id={maintenance.pk}')

    def test_get_object_notifications_panel_hidden_without_permission(self):
        # The panel gates on ParsedNotification's own permission, not the maintenance's
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        response = self.client.get(maintenance.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, reverse('plugins:netbox_circuit_maintenance:parsednotification_list'))

    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_delete_object_with_children(self):
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
        circuit = Circuit.objects.create(cid='CID-1', provider=provider, type=circuit_type)
        impact = CircuitImpact.objects.create(maintenance=maintenance, circuit=circuit)
        note = Note.objects.create(maintenance=maintenance, title='Note 1', comment='comment 1')
        raw_notification = RawNotification.objects.create(
            raw=b'body', subject='Subject 1', provider=provider, stamp=START
        )
        parsed_notification = ParsedNotification.objects.create(
            maintenance=maintenance, raw_notification=raw_notification, json={'maintenance_id': 'UT-TEST-1'}
        )
        self.add_permissions('netbox_circuit_maintenance.delete_circuitmaintenance')

        # The confirmation lists the impact, the note and the parsed notification with links, and the delete
        # serializes them for events
        response = self.client.get(self._get_url('delete', maintenance))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, impact.get_absolute_url())
        self.assertContains(response, note.get_absolute_url())
        self.assertContains(response, parsed_notification.get_absolute_url())
        response = self.client.post(self._get_url('delete', maintenance), post_data({'confirm': True}))
        self.assertHttpStatus(response, 302)
        self.assertFalse(CircuitImpact.objects.filter(pk=impact.pk).exists())
        self.assertFalse(Note.objects.filter(pk=note.pk).exists())
        self.assertFalse(ParsedNotification.objects.filter(pk=parsed_notification.pk).exists())
        # The raw notification stays
        self.assertTrue(RawNotification.objects.filter(pk=raw_notification.pk).exists())

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

    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_get_object_changelog_with_note(self):
        # A note added through the UI is listed in the maintenance's changelog, linked to the note
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        self.add_permissions('netbox_circuit_maintenance.add_note')

        response = self.client.post(
            reverse('plugins:netbox_circuit_maintenance:note_add'),
            post_data({'maintenance': maintenance.pk, 'title': 'Note 1', 'level': 'INFO', 'comment': 'comment 1'}),
        )
        self.assertHttpStatus(response, 302)
        note = Note.objects.get(maintenance=maintenance, title='Note 1')

        response = self.client.get(self._get_url('changelog', maintenance))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, f'<a href="{note.get_absolute_url()}">{note}</a>', html=True)

    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_get_object_changelog_with_parsed_notification(self):
        # A parsed notification the handler stores under event tracking is listed in the maintenance's changelog,
        # linked to the parsed notification
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        raw_notification = RawNotification.objects.create(
            raw=b'body', subject='Subject 1', provider=provider, stamp=START
        )
        request = RequestFactory().get('/')
        request.id = uuid.uuid4()
        request.user = self.user
        with event_tracking(request):
            parsed_notification = ParsedNotification.objects.create(
                maintenance=maintenance, raw_notification=raw_notification, json={'maintenance_id': 'UT-TEST-1'}
            )

        response = self.client.get(self._get_url('changelog', maintenance))
        self.assertHttpStatus(response, 200)
        self.assertContains(
            response, f'<a href="{parsed_notification.get_absolute_url()}">{parsed_notification}</a>', html=True
        )


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


class NoteTestCase(PluginViewTestMixin, ViewTestCases.OrganizationalObjectViewTestCase):
    model = Note

    @classmethod
    def setUpTestData(cls):
        maintenances = (
            CircuitMaintenance(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance(
                name='UT-TEST-2', start_time=START + timedelta(days=1), end_time=END + timedelta(days=1)
            ),
        )
        CircuitMaintenance.objects.bulk_create(maintenances)

        notes = (
            Note(maintenance=maintenances[0], title='Note 0', comment='comment 0'),
            Note(maintenance=maintenances[0], title='Note 1', comment='comment 1'),
            Note(maintenance=maintenances[0], title='Note 2', comment='comment 2'),
        )
        Note.objects.bulk_create(notes)

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        cls.form_data = {
            'maintenance': maintenances[0].pk,
            'title': 'Note 3',
            'level': NoteLevelChoices.INFO,
            'comment': 'comment 3',
            'tags': [t.pk for t in tags],
        }

        cls.csv_data = {
            'default': (
                'maintenance,title,level,comment',
                f'{maintenances[0].name},Note 4,INFO,comment 4',
                f'{maintenances[0].name},Note 5,INFO,comment 5',
                f'{maintenances[0].name},Note 6,WARNING,comment 6',
            ),
            # Level defaults to INFO, as with Nautobot's serializer-based import
            'without_level': (
                'maintenance,title,comment',
                f'{maintenances[1].name},Note 7,comment 7',
                f'{maintenances[1].name},Note 8,comment 8',
            ),
        }

        cls.csv_update_data = (
            'id,level,comment',
            f'{notes[0].pk},WARNING,New comment 0',
            f'{notes[1].pk},WARNING,New comment 1',
            f'{notes[2].pk},INFO,New comment 2',
        )

        cls.bulk_edit_data = {
            'level': NoteLevelChoices.WARNING,
        }

    def _import(self, data):
        return self.client.post(
            self._get_url('bulk_import'),
            {'data': data, 'format': ImportFormatChoices.CSV, 'csv_delimiter': CSVDelimiterChoices.AUTO},
        )

    # The import form only offers maintenances the user can view
    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_bulk_import_default_level(self):
        self.add_permissions('netbox_circuit_maintenance.add_note')

        self.assertHttpStatus(self._import('\n'.join(self.csv_data['without_level'])), 302)
        note = Note.objects.get(maintenance__name='UT-TEST-2', title='Note 7')
        self.assertEqual(note.level, NoteLevelChoices.INFO)

        # A blank cell counts as omitted (NetBox's CSVSelectWidget)
        self.assertHttpStatus(self._import('maintenance,title,level,comment\nUT-TEST-2,Note 9,,comment 9'), 302)
        note = Note.objects.get(maintenance__name='UT-TEST-2', title='Note 9')
        self.assertEqual(note.level, NoteLevelChoices.INFO)

    # The import form only offers maintenances the user can view
    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_bulk_import_duplicate_title(self):
        self.add_permissions('netbox_circuit_maintenance.add_note')

        response = self._import('maintenance,title,comment\nUT-TEST-1,Note 0,comment')
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Note with this Maintenance and Title already exists.')

    def test_bulk_edit_duplicate_title(self):
        # One title for two notes of the same maintenance fails validation and changes nothing
        notes = Note.objects.filter(title__in=('Note 0', 'Note 1'))
        self.add_permissions('netbox_circuit_maintenance.view_note', 'netbox_circuit_maintenance.change_note')

        response = self.client.post(
            self._get_url('bulk_edit'),
            {'pk': [note.pk for note in notes], '_apply': True, 'title': 'Same title'},
        )
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Note with this Maintenance and Title already exists.')
        self.assertFalse(Note.objects.filter(title='Same title').exists())

    def test_create_object_prefilled_maintenance(self):
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-2')
        self.add_permissions('netbox_circuit_maintenance.add_note')

        response = self.client.get(self._get_url('add'), {'maintenance': maintenance.pk})
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context['form']['maintenance'].value(), str(maintenance.pk))

    def test_get_object_comment_markdown(self):
        note = Note.objects.get(title='Note 0')
        note.comment = 'Circuit **CID-1** <script>alert(1)</script>'
        note.save()
        self.add_permissions('netbox_circuit_maintenance.view_note')

        response = self.client.get(note.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, '<strong>CID-1</strong>', html=True)
        self.assertNotContains(response, '<script>alert(1)</script>')

    def test_list_objects_panel_columns(self):
        # The columns the maintenance's Notes panel asks for, in Nautobot's panel order
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-1')
        self.add_permissions('netbox_circuit_maintenance.view_note')

        response = self.client.get(
            self._get_url('list'),
            {
                'maintenance_id': maintenance.pk,
                'include_columns': 'level,title,last_updated,comment',
                'exclude_columns': 'maintenance',
            },
        )
        self.assertHttpStatus(response, 200)
        table = response.context['table']
        # Least recently updated first (no pk checkbox: this user has no bulk actions)
        self.assertEqual([note.title for note in table.data], ['Note 0', 'Note 1', 'Note 2'])
        self.assertEqual(
            [column.name for column in table.columns if column.visible],
            ['level', 'title', 'last_updated', 'comment', 'actions'],
        )
        self.assertNotContains(response, maintenance.get_absolute_url())


class NotificationSourceTestCase(
    PluginViewTestMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    model = NotificationSource

    @classmethod
    def setUpTestData(cls):
        cls.providers = (
            Provider(name='Provider 3', slug='provider-3'),
            Provider(name='Provider 4', slug='provider-4'),
            Provider(name='Provider 5', slug='provider-5'),
        )
        Provider.objects.bulk_create(cls.providers)

        sources = (
            NotificationSource(name='Source 1'),
            NotificationSource(name='Source 2', attach_all_providers=True),
            NotificationSource(name='Source 3'),
        )
        NotificationSource.objects.bulk_create(sources)
        sources[0].providers.set(cls.providers[:2])
        sources[1].providers.set(cls.providers)

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        # The name and attach_all_providers come from the plugin configuration, so only these can be edited
        cls.form_data = {
            'providers': [cls.providers[2].pk],
            'tags': [t.pk for t in tags],
        }

    def test_get_object_attributes(self):
        source = NotificationSource.objects.get(name='Source 2')
        source._token = {'refresh_token': 'refresh-token-value'}
        source.save()
        self.add_permissions(
            'netbox_circuit_maintenance.view_notificationsource',
            'netbox_circuit_maintenance.add_notificationsource',
            'netbox_circuit_maintenance.change_notificationsource',
            'netbox_circuit_maintenance.delete_notificationsource',
        )

        response = self.client.get(source.get_absolute_url())
        self.assertHttpStatus(response, 200)
        for provider in self.providers:
            self.assertContains(response, provider.get_absolute_url())
        self.assertContains(response, 'Attach all providers')
        self.assertContains(response, 'mdi-check-bold')
        self.assertNotContains(response, 'refresh-token-value')
        # Sources can be edited and deleted, but not added or cloned, whatever the user's permissions
        self.assertContains(response, self._get_url('edit', source))
        self.assertContains(response, self._get_url('delete', source))
        self.assertNotContains(response, f'{self._get_url("list")}add/')

    # The form only offers providers the user can view
    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_edit_object_configured_fields(self):
        # The form ignores the fields that come from the plugin configuration
        source = NotificationSource.objects.get(name='Source 1')
        self.add_permissions('netbox_circuit_maintenance.change_notificationsource')

        response = self.client.post(
            self._get_url('edit', source),
            post_data({'name': 'Renamed', 'attach_all_providers': True, 'providers': [self.providers[2].pk]}),
        )
        self.assertHttpStatus(response, 302)
        source.refresh_from_db()
        self.assertEqual((source.name, source.attach_all_providers), ('Source 1', False))
        self.assertQuerySetEqual(source.providers.all(), [self.providers[2]])

    def test_list_objects_filtered(self):
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')

        response = self.client.get(self._get_url('list'), {'provider_id': self.providers[2].pk})
        self.assertHttpStatus(response, 200)
        self.assertEqual([str(source) for source in response.context['table'].data], ['Source 2'])

    def test_list_objects_actions(self):
        # Export only: sources can't be added or imported, and have no bulk edit or bulk delete
        self.add_permissions(
            'netbox_circuit_maintenance.view_notificationsource',
            'netbox_circuit_maintenance.add_notificationsource',
            'netbox_circuit_maintenance.change_notificationsource',
            'netbox_circuit_maintenance.delete_notificationsource',
        )

        response = self.client.get(self._get_url('list'))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Export')
        for action in ('add', 'import', 'edit', 'delete'):
            self.assertNotContains(response, f'{self._get_url("list")}{action}/')

    def test_menu_item(self):
        list_url = self._get_url('list')
        self.add_permissions('netbox_circuit_maintenance.view_rawnotification')
        response = self.client.get(reverse('home'))
        self.assertNotContains(response, f'href="{list_url}"')

        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')
        response = self.client.get(reverse('home'))
        self.assertContains(response, f'href="{list_url}"')

    def test_delete_object_with_raw_notifications(self):
        # The raw notifications stay, without a source
        source = NotificationSource.objects.get(name='Source 1')
        raw_notification = RawNotification.objects.create(
            subject='Subject 1', provider=self.providers[0], source=source, raw=b'body', stamp=START
        )
        self.add_permissions('netbox_circuit_maintenance.delete_notificationsource')

        response = self.client.post(self._get_url('delete', source), post_data({'confirm': True}))
        self.assertHttpStatus(response, 302)
        self.assertFalse(NotificationSource.objects.filter(pk=source.pk).exists())
        raw_notification.refresh_from_db()
        self.assertIsNone(raw_notification.source)


class RawNotificationTestCase(
    PluginViewTestMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = RawNotification

    @classmethod
    def setUpTestData(cls):
        # Deletes cascading to parsed notifications are covered by the *_with_parsed_notifications tests
        cls.providers = (
            Provider(name='Provider 3', slug='provider-3'),
            Provider(name='Provider 4', slug='provider-4'),
        )
        Provider.objects.bulk_create(cls.providers)

        cls.source = NotificationSource.objects.create(name='Source 1')

        # Distinct stamps: Nautobot's test relied on separate datetime.now() calls
        RawNotification.objects.create(
            subject='Subject 1',
            provider=cls.providers[0],
            sender='noc@example.com',
            source=cls.source,
            raw=b'whatever 1',
            stamp=START,
        )
        RawNotification.objects.create(
            subject='Subject 2',
            provider=cls.providers[1],
            source=cls.source,
            raw=b'whatever 2',
            stamp=START + timedelta(hours=1),
        )
        RawNotification.objects.create(
            subject='Subject 3',
            provider=cls.providers[1],
            source=cls.source,
            raw=b'whatever 3',
            stamp=START + timedelta(hours=2),
        )

    def test_get_object_attributes(self):
        raw_notification = RawNotification.objects.get(subject='Subject 1')
        self.add_permissions(
            'netbox_circuit_maintenance.view_rawnotification',
            'netbox_circuit_maintenance.add_rawnotification',
            'netbox_circuit_maintenance.change_rawnotification',
            'netbox_circuit_maintenance.delete_rawnotification',
        )

        response = self.client.get(raw_notification.get_absolute_url())
        self.assertHttpStatus(response, 200)
        for text in (
            'Subject 1',
            'noc@example.com',
            self.providers[0].get_absolute_url(),
            self.source.get_absolute_url(),
        ):
            self.assertContains(response, text)
        # Raw notifications can only be deleted, whatever the user's permissions
        self.assertContains(response, self._get_url('delete', raw_notification))
        self.assertNotContains(response, f'{raw_notification.get_absolute_url()}edit/')
        self.assertNotContains(response, f'{self._get_url("list")}add/')

    def test_get_object_without_source(self):
        # A deleted source leaves source empty
        raw_notification = RawNotification.objects.get(subject='Subject 1')
        raw_notification.source = None
        raw_notification.save()
        self.add_permissions('netbox_circuit_maintenance.view_rawnotification')

        response = self.client.get(raw_notification.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, 'Source 1')

    def test_get_object_content(self):
        # The body is escaped, and a character the size cap cut in two doesn't hide the rest of it
        raw_notification = RawNotification.objects.get(subject='Subject 1')
        raw_notification.raw = '<b>Maintenance</b> café'.encode()[:-1]
        raw_notification.save()
        self.add_permissions('netbox_circuit_maintenance.view_rawnotification')

        response = self.client.get(raw_notification.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, '&lt;b&gt;Maintenance&lt;/b&gt; caf\N{REPLACEMENT CHARACTER}')
        self.assertNotContains(response, '<b>Maintenance</b>')

    def test_get_object_parsed_notification(self):
        # A parsed notification links to its most recently updated parsed notification and that one's maintenance
        raw_notification = RawNotification.objects.get(subject='Subject 1')
        maintenances = (
            CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance.objects.create(name='UT-TEST-2', start_time=START, end_time=END),
        )
        parsed_notifications = [
            ParsedNotification.objects.create(
                maintenance=maintenance, raw_notification=raw_notification, json={'maintenance_id': maintenance.name}
            )
            for maintenance in maintenances
        ]
        self.add_permissions('netbox_circuit_maintenance.view_rawnotification')

        response = self.client.get(raw_notification.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, parsed_notifications[1].get_absolute_url())
        self.assertNotContains(response, maintenances[1].get_absolute_url())

        raw_notification.parsed = True
        raw_notification.save()
        response = self.client.get(raw_notification.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Parsed notification')
        self.assertContains(response, parsed_notifications[1].get_absolute_url())
        self.assertContains(response, maintenances[1].get_absolute_url())
        self.assertNotContains(response, parsed_notifications[0].get_absolute_url())
        self.assertNotContains(response, maintenances[0].get_absolute_url())

    def test_list_objects_filtered(self):
        self.add_permissions('netbox_circuit_maintenance.view_rawnotification')

        response = self.client.get(
            self._get_url('list'), {'provider_id': self.providers[1].pk, 'since': START + timedelta(hours=2)}
        )
        self.assertHttpStatus(response, 200)
        self.assertEqual([str(raw_notification) for raw_notification in response.context['table'].data], ['Subject 3'])

    def test_list_objects_source(self):
        # The source links to its page, and the filter form offers it (a dynamic field, which reverses the source's
        # REST endpoint when it renders)
        other_source = NotificationSource.objects.create(name='Source 2')
        RawNotification.objects.filter(subject='Subject 3').update(source=other_source)
        self.add_permissions('netbox_circuit_maintenance.view_rawnotification')

        response = self.client.get(self._get_url('list'), {'source_id': other_source.pk})
        self.assertHttpStatus(response, 200)
        self.assertEqual([str(raw_notification) for raw_notification in response.context['table'].data], ['Subject 3'])
        self.assertContains(response, f'href="{other_source.get_absolute_url()}"')
        self.assertIn('source_id', response.context['filter_form'].fields)

    def test_menu_item(self):
        # Gated on the raw notifications' own permission, not view_circuitmaintenance as in Nautobot
        list_url = self._get_url('list')
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')
        response = self.client.get(reverse('home'))
        self.assertNotContains(response, f'href="{list_url}"')

        self.add_permissions('netbox_circuit_maintenance.view_rawnotification')
        response = self.client.get(reverse('home'))
        self.assertContains(response, f'href="{list_url}"')

    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_provider_related_objects(self):
        # Core's Provider page lists the provider's raw notifications under Related objects
        self.add_permissions('circuits.view_provider')

        response = self.client.get(self.providers[1].get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, f'{self._get_url("list")}?provider_id={self.providers[1].pk}')

    def parse(self, subject):
        maintenance = CircuitMaintenance.objects.create(
            name=f'Maintenance for {subject}', start_time=START, end_time=END
        )
        raw_notification = RawNotification.objects.get(subject=subject)
        return ParsedNotification.objects.create(
            maintenance=maintenance, raw_notification=raw_notification, json={'maintenance_id': maintenance.name}
        )

    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_delete_object_with_parsed_notifications(self):
        parsed_notification = self.parse('Subject 1')
        raw_notification = parsed_notification.raw_notification
        self.add_permissions('netbox_circuit_maintenance.delete_rawnotification')

        # The confirmation lists the parsed notification with a link, and the delete serializes it for events
        response = self.client.get(self._get_url('delete', raw_notification))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, parsed_notification.get_absolute_url())
        response = self.client.post(self._get_url('delete', raw_notification), post_data({'confirm': True}))
        self.assertHttpStatus(response, 302)
        self.assertFalse(ParsedNotification.objects.exists())
        # The maintenance stays
        self.assertTrue(CircuitMaintenance.objects.filter(pk=parsed_notification.maintenance.pk).exists())

    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_bulk_delete_objects_with_parsed_notifications(self):
        self.parse('Subject 1')
        self.parse('Subject 2')
        self.add_permissions('netbox_circuit_maintenance.delete_rawnotification')

        response = self.client.post(
            self._get_url('bulk_delete'),
            {'pk': list(RawNotification.objects.values_list('pk', flat=True)), 'confirm': True, '_confirm': True},
        )
        self.assertHttpStatus(response, 302)
        self.assertFalse(RawNotification.objects.exists())
        self.assertFalse(ParsedNotification.objects.exists())

    @override_settings(EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_delete_provider_with_raw_notifications(self):
        # Deleting a core provider cascades to its raw notifications and their parsed notifications, which under event
        # tracking need their REST serializers
        provider = self.providers[1]
        raw_notification = RawNotification.objects.get(subject='Subject 2')
        parsed_notification = self.parse('Subject 2')
        obj_perm = ObjectPermission(name='Test permission', actions=['delete'])
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(Provider))

        # The confirmation lists the raw and parsed notifications with links
        response = self.client.get(reverse('circuits:provider_delete', kwargs={'pk': provider.pk}))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, raw_notification.get_absolute_url())
        self.assertContains(response, parsed_notification.get_absolute_url())
        response = self.client.post(
            reverse('circuits:provider_delete', kwargs={'pk': provider.pk}), post_data({'confirm': True})
        )
        self.assertHttpStatus(response, 302)
        self.assertEqual(list(RawNotification.objects.values_list('subject', flat=True)), ['Subject 1'])
        self.assertFalse(ParsedNotification.objects.exists())


class ParsedNotificationTestCase(
    PluginViewTestMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    model = ParsedNotification

    @classmethod
    def setUpTestData(cls):
        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        cls.maintenances = (
            CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance.objects.create(name='UT-TEST-2', start_time=START, end_time=END),
        )
        # Stamps in the opposite order of creation, so the list order can't come from last_updated or pk
        cls.raw_notifications = tuple(
            RawNotification.objects.create(
                raw=f'body {i}'.encode(), subject=f'Subject {i}', provider=provider, stamp=START - timedelta(hours=i)
            )
            for i in range(1, 4)
        )

        ParsedNotification.objects.bulk_create(
            (
                ParsedNotification(
                    maintenance=cls.maintenances[0],
                    raw_notification=cls.raw_notifications[0],
                    json={'maintenance_id': 'UT-TEST-1', 'status': 'CONFIRMED'},
                ),
                ParsedNotification(
                    maintenance=cls.maintenances[0],
                    raw_notification=cls.raw_notifications[1],
                    json={'maintenance_id': 'UT-TEST-1', 'status': 'TENTATIVE'},
                ),
                ParsedNotification(
                    maintenance=cls.maintenances[1],
                    raw_notification=cls.raw_notifications[2],
                    json={'maintenance_id': 'UT-TEST-2', 'status': 'TENTATIVE'},
                ),
            )
        )

    def test_get_object_attributes(self):
        parsed_notification = ParsedNotification.objects.get(raw_notification=self.raw_notifications[0])
        parsed_notification.json = {'maintenance_id': 'UT-TEST-1', 'summary': '<b>Planned</b> work'}
        parsed_notification.save()
        self.add_permissions(
            'netbox_circuit_maintenance.view_parsednotification',
            'netbox_circuit_maintenance.add_parsednotification',
            'netbox_circuit_maintenance.change_parsednotification',
            'netbox_circuit_maintenance.delete_parsednotification',
        )

        response = self.client.get(parsed_notification.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, self.maintenances[0].get_absolute_url())
        # Breadcrumbs: Parsed Notifications, then the raw notification, as Nautobot's page led back to it
        self.assertContains(
            response,
            f'<li class="breadcrumb-item"><a href="{self.raw_notifications[0].get_absolute_url()}">Subject 1</a></li>',
            html=True,
        )
        self.assertContains(response, f'href="{self._get_url("list")}"')
        # The parser output, formatted and escaped
        self.assertContains(response, '&quot;summary&quot;: &quot;&lt;b&gt;Planned&lt;/b&gt; work&quot;')
        self.assertNotContains(response, '<b>Planned</b>')
        # Parsed notifications can't be cloned, edited or deleted, whatever the user's permissions
        self.assertNotContains(response, f'{parsed_notification.get_absolute_url()}edit/')
        self.assertNotContains(response, f'{parsed_notification.get_absolute_url()}delete/')
        self.assertNotContains(response, f'{self._get_url("list")}add/')

    def test_list_objects_ordering(self):
        # Newest raw notification first, as on Nautobot's maintenance page, although the model orders by last update
        self.add_permissions('netbox_circuit_maintenance.view_parsednotification')

        response = self.client.get(self._get_url('list'))
        self.assertHttpStatus(response, 200)
        self.assertEqual(
            [parsed_notification.raw_notification.subject for parsed_notification in response.context['table'].data],
            ['Subject 1', 'Subject 2', 'Subject 3'],
        )

    def test_list_objects_panel_columns(self):
        # What the maintenance's Notifications panel asks for: Nautobot's columns in its order, plus the ID linking the
        # parsed notification
        self.add_permissions('netbox_circuit_maintenance.view_parsednotification')

        response = self.client.get(
            self._get_url('list'),
            {
                'maintenance_id': self.maintenances[0].pk,
                'include_columns': 'raw_notification,stamp,json',
                'exclude_columns': 'maintenance,provider',
            },
        )
        self.assertHttpStatus(response, 200)
        table = response.context['table']
        self.assertEqual(
            [parsed_notification.raw_notification.subject for parsed_notification in table.data],
            ['Subject 1', 'Subject 2'],
        )
        self.assertEqual(
            [column.name for column in table.columns if column.visible],
            ['id', 'raw_notification', 'stamp', 'json', 'actions'],
        )
        self.assertContains(response, self.raw_notifications[0].get_absolute_url())
        self.assertContains(response, '&quot;status&quot;: &quot;CONFIRMED&quot;')
        self.assertNotContains(response, self.maintenances[0].get_absolute_url())

    def test_export_json(self):
        # The JSON column exports the parser output as JSON
        self.add_permissions('netbox_circuit_maintenance.view_parsednotification')

        response = self.client.get(
            self._get_url('list'),
            {'export': 'table', 'maintenance_id': self.maintenances[1].pk, 'include_columns': 'json'},
        )
        self.assertHttpStatus(response, 200)
        rows = list(csv.DictReader(io.StringIO(response.content.decode())))
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]['JSON']), {'maintenance_id': 'UT-TEST-2', 'status': 'TENTATIVE'})

    def test_list_objects_actions(self):
        # Export only: parsed notifications can't be added, imported, edited or deleted
        self.add_permissions(
            'netbox_circuit_maintenance.view_parsednotification',
            'netbox_circuit_maintenance.add_parsednotification',
            'netbox_circuit_maintenance.change_parsednotification',
            'netbox_circuit_maintenance.delete_parsednotification',
        )

        response = self.client.get(self._get_url('list'))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Export')
        for action in ('add', 'import', 'edit', 'delete'):
            self.assertNotContains(response, f'{self._get_url("list")}{action}/')

    def test_menu(self):
        # The list isn't in the menu
        self.add_permissions('netbox_circuit_maintenance.view_parsednotification')

        response = self.client.get(reverse('home'))
        self.assertNotContains(response, f'href="{self._get_url("list")}"')
