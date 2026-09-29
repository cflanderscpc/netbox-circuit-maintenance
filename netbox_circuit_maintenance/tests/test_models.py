import uuid
from datetime import UTC, datetime, timedelta

from circuits.models import Circuit, CircuitType, Provider
from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange, ObjectType
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.forms import modelform_factory
from django.test import RequestFactory, TestCase
from netbox.context_managers import event_tracking
from users.models import User

from netbox_circuit_maintenance.choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
from netbox_circuit_maintenance.constants import MAX_NOTE_TITLE_LENGTH
from netbox_circuit_maintenance.models import CircuitImpact, CircuitMaintenance, Note

START = datetime(2020, 10, 4, 10, 0, tzinfo=UTC)
END = START + timedelta(hours=2)

# NetBox counterparts of the Nautobot feature flags (custom_validators -> custom_validation, webhooks -> event_rules),
# plus the rest of the NetBoxModel feature set
NETBOX_MODEL_FEATURES = {
    'bookmarks',
    'change_logging',
    'cloning',
    'custom_fields',
    'custom_links',
    'custom_validation',
    'event_rules',
    'export_templates',
    'journaling',
    'notifications',
    'tags',
}


class CircuitMaintenanceTestCase(TestCase):
    def test_defaults(self):
        maintenance = CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END)

        self.assertEqual(maintenance.status, CircuitMaintenanceStatusChoices.TENTATIVE)
        self.assertFalse(maintenance.ack)
        self.assertEqual(maintenance.description, '')
        self.assertEqual(str(maintenance), 'UT-TEST-1')

    def test_status_values(self):
        # The handler stores parser output verbatim, so the values must match the maintnote standard exactly
        self.assertEqual(
            CircuitMaintenanceStatusChoices.values(),
            ['TENTATIVE', 'CONFIRMED', 'CANCELLED', 'IN-PROCESS', 'COMPLETED', 'RE-SCHEDULED', 'UNKNOWN'],
        )

    def test_get_status_color(self):
        maintenance = CircuitMaintenance(name='UT-TEST-1', start_time=START, end_time=END)

        for status in CircuitMaintenanceStatusChoices.values():
            maintenance.status = status
            self.assertTrue(maintenance.get_status_color(), status)

    def test_clean_end_before_start(self):
        maintenance = CircuitMaintenance(name='UT-TEST-1', start_time=END, end_time=START)

        with self.assertRaises(ValidationError) as cm:
            maintenance.full_clean()
        self.assertEqual(list(cm.exception.message_dict), [NON_FIELD_ERRORS])

    def test_clean_end_before_start_form_without_end_time(self):
        # BulkImportView deletes the form fields a CSV update row doesn't include. A field-keyed error from clean()
        # would then raise ValueError ("has no field named 'end_time'") instead of a form error.
        maintenance = CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END)
        form_class = modelform_factory(CircuitMaintenance, fields=('name', 'start_time'))
        form = form_class(data={'name': 'UT-TEST-1', 'start_time': END + timedelta(hours=1)}, instance=maintenance)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.non_field_errors(), ['End time should be greater than start time.'])

    def test_clean_end_equal_to_start(self):
        CircuitMaintenance(name='UT-TEST-1', start_time=START, end_time=START).full_clean()

    def test_clean_missing_time(self):
        # Django runs clean() even when a field fails validation; the missing field is reported, not a TypeError
        maintenance = CircuitMaintenance(name='UT-TEST-1', start_time=START)

        with self.assertRaises(ValidationError) as cm:
            maintenance.full_clean()
        self.assertEqual(list(cm.exception.message_dict), ['end_time'])

    def test_description_longer_than_primarymodel_limit(self):
        maintenance = CircuitMaintenance(name='UT-TEST-1', start_time=START, end_time=END, description='x' * 1000)
        maintenance.full_clean()
        maintenance.save()

        maintenance.refresh_from_db()
        self.assertEqual(len(maintenance.description), 1000)

    def test_unique_name(self):
        CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END)

        with self.assertRaises(ValidationError) as cm:
            CircuitMaintenance(name='UT-TEST-1', start_time=START, end_time=END).full_clean()
        self.assertEqual(list(cm.exception.message_dict), ['name'])

    def test_ordering(self):
        for name, offset in (('UT-TEST-1', 2), ('UT-TEST-2', 0), ('UT-TEST-3', 1)):
            start = START + timedelta(days=offset)
            CircuitMaintenance.objects.create(name=name, start_time=start, end_time=start + timedelta(hours=2))

        self.assertEqual(
            list(CircuitMaintenance.objects.values_list('name', flat=True)), ['UT-TEST-2', 'UT-TEST-3', 'UT-TEST-1']
        )

    def test_docs_url(self):
        # None hides the edit form's Help button, which would link to a missing NetBox core docs page
        self.assertIsNone(CircuitMaintenance().docs_url)

    def test_features(self):
        features = set(ObjectType.objects.get_for_model(CircuitMaintenance).features)

        self.assertLessEqual(NETBOX_MODEL_FEATURES, features)


class CircuitImpactTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        providers = (
            Provider.objects.create(name='Provider 1', slug='provider-1'),
            Provider.objects.create(name='Provider 2', slug='provider-2'),
        )
        circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
        cls.circuits = (
            Circuit.objects.create(cid='CID-1', provider=providers[0], type=circuit_type),
            Circuit.objects.create(cid='CID-2', provider=providers[1], type=circuit_type),
            Circuit.objects.create(cid='CID-3', provider=providers[1], type=circuit_type),
        )
        cls.maintenances = (
            CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance.objects.create(
                name='UT-TEST-2', start_time=START + timedelta(days=1), end_time=END + timedelta(days=1)
            ),
        )

    def test_default_impact(self):
        # Ported from Nautobot: the same circuit in two maintenances, neither impact set
        CircuitImpact.objects.bulk_create(
            (
                CircuitImpact(maintenance=self.maintenances[0], circuit=self.circuits[0]),
                CircuitImpact(maintenance=self.maintenances[1], circuit=self.circuits[0]),
            )
        )

        self.assertEqual(
            list(CircuitImpact.objects.values_list('impact', flat=True)),
            [CircuitImpactChoices.OUTAGE, CircuitImpactChoices.OUTAGE],
        )

    def test_impact_values(self):
        # The handler stores parser output verbatim, so the values must match the maintnote standard exactly
        self.assertEqual(
            CircuitImpactChoices.values(),
            ['NO-IMPACT', 'REDUCED-REDUNDANCY', 'DEGRADED', 'OUTAGE'],
        )

    def test_get_impact_color(self):
        impact = CircuitImpact(maintenance=self.maintenances[0], circuit=self.circuits[0])

        for value in CircuitImpactChoices.values():
            impact.impact = value
            self.assertTrue(impact.get_impact_color(), value)

    def test_str(self):
        impact = CircuitImpact(
            maintenance=self.maintenances[0], circuit=self.circuits[0], impact=CircuitImpactChoices.REDUCED_REDUNDANCY
        )

        self.assertEqual(str(impact), 'Circuit CID-1 with impact Reduced redundancy')

    def test_unique_maintenance_circuit(self):
        CircuitImpact.objects.create(maintenance=self.maintenances[0], circuit=self.circuits[0])

        with self.assertRaises(ValidationError) as cm:
            CircuitImpact(
                maintenance=self.maintenances[0], circuit=self.circuits[0], impact=CircuitImpactChoices.NO_IMPACT
            ).full_clean()
        self.assertEqual(
            cm.exception.message_dict,
            {NON_FIELD_ERRORS: ['Circuit impact with this Maintenance and Circuit already exists.']},
        )

    def test_ordering(self):
        # Maintenance (by its start_time, not its pk), then impact value, then pk
        later, earlier = (
            CircuitMaintenance.objects.create(name='UT-TEST-9', start_time=START + timedelta(days=9), end_time=END),
            CircuitMaintenance.objects.create(name='UT-TEST-0', start_time=START - timedelta(days=9), end_time=END),
        )
        impacts = (
            CircuitImpact.objects.create(
                maintenance=later, circuit=self.circuits[0], impact=CircuitImpactChoices.NO_IMPACT
            ),
            CircuitImpact.objects.create(maintenance=earlier, circuit=self.circuits[1]),
            CircuitImpact.objects.create(
                maintenance=earlier, circuit=self.circuits[2], impact=CircuitImpactChoices.DEGRADED
            ),
            CircuitImpact.objects.create(
                maintenance=earlier, circuit=self.circuits[0], impact=CircuitImpactChoices.DEGRADED
            ),
        )

        self.assertEqual(list(CircuitImpact.objects.all()), [impacts[2], impacts[3], impacts[1], impacts[0]])

    def test_cascade_delete(self):
        CircuitImpact.objects.create(maintenance=self.maintenances[0], circuit=self.circuits[0])
        CircuitImpact.objects.create(maintenance=self.maintenances[0], circuit=self.circuits[1])
        CircuitImpact.objects.create(maintenance=self.maintenances[1], circuit=self.circuits[1])

        self.circuits[0].delete()
        self.assertEqual(CircuitImpact.objects.count(), 2)

        self.maintenances[0].delete()
        self.assertEqual(list(CircuitImpact.objects.values_list('circuit__cid', flat=True)), ['CID-2'])

    def test_changelog_related_object(self):
        # Impact changes show in the maintenance's changelog. Deletes can't be covered until the REST serializer
        # exists: under event tracking NetBox serializes a deleted object for events straight away, and without a
        # serializer that raises SerializerNotFound.
        request = RequestFactory().get('/')
        request.id = uuid.uuid4()
        request.user = User.objects.create_user(username='user1')
        maintenance = self.maintenances[0]

        with event_tracking(request):
            impact = CircuitImpact.objects.create(maintenance=maintenance, circuit=self.circuits[0])
        with event_tracking(request):
            impact.snapshot()
            impact.impact = CircuitImpactChoices.NO_IMPACT
            impact.save()

        changes = ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(CircuitImpact), changed_object_id=impact.pk
        ).order_by('pk')
        self.assertEqual(
            [(change.action, change.related_object_type.model, change.related_object_id) for change in changes],
            [
                (ObjectChangeActionChoices.ACTION_CREATE, 'circuitmaintenance', maintenance.pk),
                (ObjectChangeActionChoices.ACTION_UPDATE, 'circuitmaintenance', maintenance.pk),
            ],
        )

    def test_maintenance_circuits_and_providers(self):
        for circuit in self.circuits:
            CircuitImpact.objects.create(maintenance=self.maintenances[0], circuit=circuit)
        CircuitImpact.objects.create(maintenance=self.maintenances[1], circuit=self.circuits[0])

        self.assertQuerySetEqual(self.maintenances[0].circuits, self.circuits, ordered=False)
        # Provider 2 has two impacted circuits but is listed once
        self.assertEqual(
            list(self.maintenances[0].providers.values_list('name', flat=True)), ['Provider 1', 'Provider 2']
        )
        self.assertQuerySetEqual(self.maintenances[1].circuits, [self.circuits[0]])
        self.assertEqual(list(self.maintenances[1].providers.values_list('name', flat=True)), ['Provider 1'])

    def test_docs_url(self):
        self.assertIsNone(CircuitImpact().docs_url)

    def test_features(self):
        features = set(ObjectType.objects.get_for_model(CircuitImpact).features)

        self.assertLessEqual(NETBOX_MODEL_FEATURES, features)


class NoteTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.maintenances = (
            CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance.objects.create(
                name='UT-TEST-2', start_time=START + timedelta(days=1), end_time=END + timedelta(days=1)
            ),
        )

    def test_defaults(self):
        note = Note.objects.create(maintenance=self.maintenances[0], title='Note 1', comment='comment 1')

        self.assertEqual(note.level, NoteLevelChoices.INFO)
        self.assertEqual(str(note), 'Note 1')

    def test_level_values(self):
        # The handler creates notes with level 'WARNING'
        self.assertEqual(NoteLevelChoices.values(), ['INFO', 'WARNING'])

    def test_get_level_color(self):
        note = Note(maintenance=self.maintenances[0], title='Note 1', comment='comment 1')

        for level in NoteLevelChoices.values():
            note.level = level
            self.assertTrue(note.get_level_color(), level)

    def test_required_fields(self):
        with self.assertRaises(ValidationError) as cm:
            Note(maintenance=self.maintenances[0]).full_clean()
        self.assertEqual(sorted(cm.exception.message_dict), ['comment', 'title'])

    def test_title_max_length(self):
        # The handler truncates titles to MAX_NOTE_TITLE_LENGTH
        Note(maintenance=self.maintenances[0], title='x' * MAX_NOTE_TITLE_LENGTH, comment='comment 1').full_clean()

        with self.assertRaises(ValidationError) as cm:
            Note(
                maintenance=self.maintenances[0], title='x' * (MAX_NOTE_TITLE_LENGTH + 1), comment='comment 1'
            ).full_clean()
        self.assertEqual(list(cm.exception.message_dict), ['title'])

    def test_unique_maintenance_title(self):
        Note.objects.create(maintenance=self.maintenances[0], title='Note 1', comment='comment 1')

        with self.assertRaises(ValidationError) as cm:
            Note(
                maintenance=self.maintenances[0],
                title='Note 1',
                level=NoteLevelChoices.WARNING,
                comment='comment 2',
            ).full_clean()
        self.assertEqual(
            cm.exception.message_dict,
            {NON_FIELD_ERRORS: ['Note with this Maintenance and Title already exists.']},
        )

        # The same title on another maintenance is fine
        Note(maintenance=self.maintenances[1], title='Note 1', comment='comment 1').full_clean()

    def test_ordering(self):
        # Least recently updated first, then pk, so editing a note moves it to the end
        notes = [
            Note.objects.create(maintenance=self.maintenances[0], title=f'Note {i}', comment=f'comment {i}')
            for i in range(3)
        ]
        Note.objects.filter(pk=notes[0].pk).update(last_updated=START + timedelta(days=1))
        Note.objects.filter(pk__in=(notes[1].pk, notes[2].pk)).update(last_updated=START)
        self.assertEqual(list(Note.objects.all()), [notes[1], notes[2], notes[0]])

        notes[1].save()
        self.assertEqual(list(Note.objects.all()), [notes[2], notes[0], notes[1]])

    def test_cascade_delete(self):
        Note.objects.create(maintenance=self.maintenances[0], title='Note 1', comment='comment 1')
        Note.objects.create(maintenance=self.maintenances[0], title='Note 2', comment='comment 2')
        Note.objects.create(maintenance=self.maintenances[1], title='Note 3', comment='comment 3')
        self.assertEqual(self.maintenances[0].notes.count(), 2)

        self.maintenances[0].delete()
        self.assertEqual(list(Note.objects.values_list('title', flat=True)), ['Note 3'])

    def test_changelog_related_object(self):
        # Same as CircuitImpactTestCase.test_changelog_related_object, including the missing delete
        request = RequestFactory().get('/')
        request.id = uuid.uuid4()
        request.user = User.objects.create_user(username='user1')
        maintenance = self.maintenances[0]

        with event_tracking(request):
            note = Note.objects.create(maintenance=maintenance, title='Note 1', comment='comment 1')
        with event_tracking(request):
            note.snapshot()
            note.level = NoteLevelChoices.WARNING
            note.save()

        changes = ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(Note), changed_object_id=note.pk
        ).order_by('pk')
        self.assertEqual(
            [(change.action, change.related_object_type.model, change.related_object_id) for change in changes],
            [
                (ObjectChangeActionChoices.ACTION_CREATE, 'circuitmaintenance', maintenance.pk),
                (ObjectChangeActionChoices.ACTION_UPDATE, 'circuitmaintenance', maintenance.pk),
            ],
        )

    def test_docs_url(self):
        self.assertIsNone(Note().docs_url)

    def test_features(self):
        features = set(ObjectType.objects.get_for_model(Note).features)

        self.assertLessEqual(NETBOX_MODEL_FEATURES, features)
