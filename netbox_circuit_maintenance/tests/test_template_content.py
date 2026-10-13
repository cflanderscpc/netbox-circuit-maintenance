from datetime import UTC, datetime, timedelta

from circuits.models import Circuit, CircuitType, Provider
from core.models import ObjectType
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from netbox.registry import registry
from users.models import ObjectPermission
from utilities.testing import TestCase

from netbox_circuit_maintenance.choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices
from netbox_circuit_maintenance.models import CircuitImpact, CircuitMaintenance
from netbox_circuit_maintenance.template_content import CircuitMaintenanceContent

START = datetime(2020, 10, 4, 10, 0, tzinfo=UTC)
PANEL_BEGIN = '<!-- begin CircuitMaintenancesPanel -->'
PANEL_END = '<!-- end CircuitMaintenancesPanel -->'


class CircuitMaintenanceContentTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.providers = (
            Provider.objects.create(name='Provider 1', slug='provider-1'),
            Provider.objects.create(name='Provider 2', slug='provider-2'),
        )
        cls.circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
        # A CID is unique only per provider, and Nautobot matched impacts by CID
        cls.circuit = Circuit.objects.create(cid='CID-1', provider=cls.providers[0], type=cls.circuit_type)
        cls.other_circuit = Circuit.objects.create(cid='CID-1', provider=cls.providers[1], type=cls.circuit_type)

        # MAINT-7 starts with MAINT-6. MAINT-OTHER, the newest, impacts only the other provider's circuit
        cls.maintenances = {}
        for name, days, circuit, impact in (
            ('MAINT-1', 1, cls.circuit, CircuitImpactChoices.OUTAGE),
            ('MAINT-2', 2, cls.circuit, CircuitImpactChoices.DEGRADED),
            ('MAINT-3', 3, cls.circuit, CircuitImpactChoices.OUTAGE),
            ('MAINT-4', 4, cls.circuit, CircuitImpactChoices.OUTAGE),
            ('MAINT-5', 5, cls.circuit, CircuitImpactChoices.NO_IMPACT),
            ('MAINT-6', 6, cls.circuit, CircuitImpactChoices.OUTAGE),
            ('MAINT-7', 6, cls.circuit, CircuitImpactChoices.OUTAGE),
            ('MAINT-OTHER', 10, cls.other_circuit, CircuitImpactChoices.OUTAGE),
        ):
            start = START + timedelta(days=days)
            maintenance = CircuitMaintenance.objects.create(
                name=name, start_time=start, end_time=start + timedelta(hours=2)
            )
            CircuitImpact.objects.create(maintenance=maintenance, circuit=circuit, impact=impact)
            cls.maintenances[name] = maintenance
        CircuitMaintenance.objects.filter(name='MAINT-7').update(
            status=CircuitMaintenanceStatusChoices.CONFIRMED, ack=True
        )

    def get_circuit_page(self, circuit=None):
        response = self.client.get((circuit or self.circuit).get_absolute_url())
        self.assertHttpStatus(response, 200)
        # NetBox shows an extension's exception in place of its content, with a 200
        self.assertTemplateNotUsed(response, 'ui/exception.html')
        return response

    def get_panel(self, response):
        content = response.content.decode()
        return content[content.index(PANEL_BEGIN) : content.index(PANEL_END)]

    def get_names(self, response):
        return [impact.maintenance.name for impact in response.context['impacts']]

    def add_view_permissions(self):
        self.add_permissions(
            'circuits.view_circuit',
            'netbox_circuit_maintenance.view_circuitimpact',
            'netbox_circuit_maintenance.view_circuitmaintenance',
        )

    def test_registered(self):
        self.assertIn(CircuitMaintenanceContent, registry['plugins']['template_extensions']['circuits.circuit'])
        # An extension without models renders on every page
        self.assertNotIn(CircuitMaintenanceContent, registry['plugins']['template_extensions'].get(None, []))

    def test_latest_maintenances(self):
        self.add_view_permissions()

        response = self.get_circuit_page()
        # Newest start first, the newest maintenance first on a tie; Nautobot showed the five oldest
        self.assertEqual(self.get_names(response), ['MAINT-7', 'MAINT-6', 'MAINT-5', 'MAINT-4', 'MAINT-3'])
        panel = self.get_panel(response)
        self.assertIn('Circuit Maintenances', panel)
        maintenance = self.maintenances['MAINT-7']
        self.assertInHTML(f'<a href="{maintenance.get_absolute_url()}">MAINT-7</a>', panel)
        self.assertInHTML('<span class="badge text-bg-red">Outage</span>', panel, count=4)
        self.assertInHTML('<span class="badge text-bg-green">No impact</span>', panel, count=1)
        self.assertInHTML('<span class="badge text-bg-blue">Confirmed</span>', panel, count=1)
        self.assertInHTML('<span class="badge text-bg-cyan">Tentative</span>', panel, count=4)
        self.assertInHTML('<i class="mdi mdi-check-bold text-success" title="Yes"></i>', panel, count=1)
        self.assertInHTML('<i class="mdi mdi-close-thick text-danger" title="No"></i>', panel, count=4)

    @override_settings(TIME_ZONE='America/Edmonton')
    def test_start_time(self):
        self.add_view_permissions()

        panel = self.get_panel(self.get_circuit_page())
        self.assertIn('Start time', panel)
        # MAINT-7 starts at 2020-10-10 10:00 UTC: shown in local time (MDT), to the minute
        self.assertIn('2020-10-10</span> 04:00</span>', panel)

    def test_view_all(self):
        self.add_view_permissions()

        panel = self.get_panel(self.get_circuit_page())
        list_url = reverse('plugins:netbox_circuit_maintenance:circuitmaintenance_list')
        self.assertIn(f'href="{list_url}?circuit_id={self.circuit.pk}&amp;return_url=', panel)
        self.assertIn('View All', panel)

        # The link lists every maintenance of the circuit, in the panel's order
        response = self.client.get(list_url, {'circuit_id': self.circuit.pk})
        self.assertHttpStatus(response, 200)
        self.assertEqual(
            [maintenance.name for maintenance in response.context['table'].data],
            ['MAINT-7', 'MAINT-6', 'MAINT-5', 'MAINT-4', 'MAINT-3', 'MAINT-2', 'MAINT-1'],
        )

    def test_circuit_with_same_cid(self):
        self.add_view_permissions()

        response = self.get_circuit_page(self.other_circuit)
        self.assertEqual(self.get_names(response), ['MAINT-OTHER'])

    def test_no_maintenances(self):
        circuit = Circuit.objects.create(cid='CID-2', provider=self.providers[0], type=self.circuit_type)
        self.add_view_permissions()

        response = self.get_circuit_page(circuit)
        self.assertEqual(self.get_names(response), [])
        self.assertInHTML('<td colspan="5" class="text-muted">None</td>', self.get_panel(response))

    def test_hidden_without_permission(self):
        # A row shows both the impact and its maintenance
        self.add_permissions('circuits.view_circuit')
        self.assertNotContains(self.get_circuit_page(), PANEL_BEGIN)

        self.add_permissions('netbox_circuit_maintenance.view_circuitimpact')
        self.assertNotContains(self.get_circuit_page(), PANEL_BEGIN)

        self.remove_permissions('netbox_circuit_maintenance.view_circuitimpact')
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')
        self.assertNotContains(self.get_circuit_page(), PANEL_BEGIN)

        self.add_permissions('netbox_circuit_maintenance.view_circuitimpact')
        self.assertContains(self.get_circuit_page(), PANEL_BEGIN)

    def test_restricted(self):
        self.add_permissions('circuits.view_circuit', 'netbox_circuit_maintenance.view_circuitimpact')
        obj_perm = ObjectPermission(
            name='Test permission', actions=['view'], constraints={'name__in': ['MAINT-1', 'MAINT-2', 'MAINT-7']}
        )
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(CircuitMaintenance))

        # Older maintenances fill the places of those the user can't view
        response = self.get_circuit_page()
        self.assertEqual(self.get_names(response), ['MAINT-7', 'MAINT-2', 'MAINT-1'])

        self.remove_permissions('netbox_circuit_maintenance.view_circuitimpact')
        obj_perm = ObjectPermission(
            name='Test permission 2', actions=['view'], constraints={'impact': CircuitImpactChoices.OUTAGE}
        )
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(CircuitImpact))

        response = self.get_circuit_page()
        self.assertEqual(self.get_names(response), ['MAINT-7', 'MAINT-1'])

    def test_query_count(self):
        # One query for the rows, however many there are
        circuit = Circuit.objects.create(cid='CID-2', provider=self.providers[0], type=self.circuit_type)
        CircuitImpact.objects.create(maintenance=self.maintenances['MAINT-1'], circuit=circuit)
        self.add_view_permissions()
        # The first request in a process fills caches that later ones read
        self.get_circuit_page(circuit)

        with CaptureQueriesContext(connection) as queries:
            self.get_circuit_page(circuit)
        query_count = len(queries)

        for name in ('MAINT-2', 'MAINT-3', 'MAINT-4', 'MAINT-5'):
            CircuitImpact.objects.create(maintenance=self.maintenances[name], circuit=circuit)
        with CaptureQueriesContext(connection) as queries:
            response = self.get_circuit_page(circuit)
        self.assertEqual(len(self.get_names(response)), 5)
        self.assertEqual(len(queries), query_count)
