import json
import uuid
from datetime import UTC, datetime, timedelta

from circuits.models import Circuit, CircuitType, Provider
from core.choices import ObjectChangeActionChoices
from core.events import OBJECT_DELETED
from core.models import ObjectChange, ObjectType
from django.conf import settings
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.forms import modelform_factory
from django.test import RequestFactory, TestCase
from extras.choices import EventRuleActionChoices
from extras.events import serialize_for_event
from extras.models import EventRule, ExportTemplate, Notification, NotificationGroup
from google.oauth2.credentials import Credentials
from netbox.context_managers import event_tracking
from users.models import User
from utilities.querydict import prepare_cloned_fields

from netbox_circuit_maintenance.choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
from netbox_circuit_maintenance.constants import (
    MAX_NOTE_TITLE_LENGTH,
    MAX_NOTIFICATION_SUBJECT_LENGTH,
    MAX_NOTIFICATION_TOTAL_LENGTH,
)
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

    def test_ordering_tie(self):
        # Equal start times fall back to pk, so REST pages don't overlap or skip rows. The update stores a new row
        # version after the others, which is where an order by start_time alone returns it
        maintenances = [
            CircuitMaintenance.objects.create(name=f'UT-TEST-{i}', start_time=START, end_time=END) for i in range(3)
        ]
        CircuitMaintenance.objects.filter(pk=maintenances[0].pk).update(ack=True)

        self.assertEqual(list(CircuitMaintenance.objects.all()), maintenances)

    def test_docs_url(self):
        # None hides the edit form's Help button, which would link to a missing NetBox core docs page
        self.assertIsNone(CircuitMaintenance().docs_url)

    def test_get_absolute_url(self):
        maintenance = CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END)

        self.assertEqual(
            maintenance.get_absolute_url(), f'/plugins/circuit-maintenance/circuit-maintenances/{maintenance.pk}/'
        )

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
        # Impact changes, including a delete cascaded from the core circuit, show in the maintenance's changelog. Under
        # event tracking NetBox serializes a deleted object for events straight away, so the delete needs the REST
        # serializer.
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
        impact_pk = impact.pk
        with event_tracking(request):
            self.circuits[0].delete()

        changes = ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(CircuitImpact), changed_object_id=impact_pk
        ).order_by('pk')
        self.assertEqual(
            [(change.action, change.related_object_type.model, change.related_object_id) for change in changes],
            [
                (ObjectChangeActionChoices.ACTION_CREATE, 'circuitmaintenance', maintenance.pk),
                (ObjectChangeActionChoices.ACTION_UPDATE, 'circuitmaintenance', maintenance.pk),
                (ObjectChangeActionChoices.ACTION_DELETE, 'circuitmaintenance', maintenance.pk),
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

    def test_get_absolute_url(self):
        impact = CircuitImpact.objects.create(maintenance=self.maintenances[0], circuit=self.circuits[0])

        self.assertEqual(impact.get_absolute_url(), f'/plugins/circuit-maintenance/circuit-impacts/{impact.pk}/')

    def test_clone_fields(self):
        impact = CircuitImpact.objects.create(
            maintenance=self.maintenances[0], circuit=self.circuits[0], impact=CircuitImpactChoices.DEGRADED
        )

        self.assertEqual(
            prepare_cloned_fields(impact).dict(),
            {'maintenance': str(self.maintenances[0].pk), 'impact': CircuitImpactChoices.DEGRADED},
        )

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
        # Same as CircuitImpactTestCase.test_changelog_related_object, with the delete cascaded from the maintenance
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
        note_pk, maintenance_pk = note.pk, maintenance.pk
        with event_tracking(request):
            maintenance.delete()

        changes = ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(Note), changed_object_id=note_pk
        ).order_by('pk')
        self.assertEqual(
            [(change.action, change.related_object_type.model, change.related_object_id) for change in changes],
            [
                (ObjectChangeActionChoices.ACTION_CREATE, 'circuitmaintenance', maintenance_pk),
                (ObjectChangeActionChoices.ACTION_UPDATE, 'circuitmaintenance', maintenance_pk),
                (ObjectChangeActionChoices.ACTION_DELETE, 'circuitmaintenance', maintenance_pk),
            ],
        )

    def test_docs_url(self):
        self.assertIsNone(Note().docs_url)

    def test_get_absolute_url(self):
        note = Note.objects.create(maintenance=self.maintenances[0], title='Note 1', comment='comment 1')

        self.assertEqual(note.get_absolute_url(), f'/plugins/circuit-maintenance/notes/{note.pk}/')

    def test_clone_fields(self):
        # The title is unique per maintenance and the comment is free text, so neither is cloned
        note = Note.objects.create(
            maintenance=self.maintenances[0], title='Note 1', level=NoteLevelChoices.WARNING, comment='comment 1'
        )

        self.assertEqual(
            prepare_cloned_fields(note).dict(),
            {'maintenance': str(self.maintenances[0].pk), 'level': NoteLevelChoices.WARNING},
        )

    def test_features(self):
        features = set(ObjectType.objects.get_for_model(Note).features)

        self.assertLessEqual(NETBOX_MODEL_FEATURES, features)


class NotificationSourceTestCase(TestCase):
    # google-auth keeps expiry as a naive UTC datetime
    TOKEN_EXPIRY = datetime(2030, 1, 1)  # noqa: DTZ001

    @classmethod
    def setUpTestData(cls):
        cls.providers = (
            Provider.objects.create(name='Provider 1', slug='provider-1'),
            Provider.objects.create(name='Provider 2', slug='provider-2'),
        )
        # What the OAuth callback will store: google.oauth2.credentials.Credentials.to_json()
        cls.token = json.loads(
            Credentials(
                token='access-token',
                refresh_token='refresh-token',
                token_uri='https://oauth2.googleapis.com/token',
                client_id='client-id',
                client_secret='client-secret',
                scopes=['https://www.googleapis.com/auth/gmail.readonly'],
                expiry=cls.TOKEN_EXPIRY,
            ).to_json()
        )

    def test_defaults(self):
        source = NotificationSource(name='Source 1')
        source.full_clean()
        source.save()

        self.assertFalse(source.attach_all_providers)
        self.assertIsNone(source._token)
        self.assertFalse(source.providers.exists())
        self.assertEqual(str(source), 'Source 1')

    def test_unique_name(self):
        NotificationSource.objects.create(name='Source 1')

        with self.assertRaises(ValidationError) as cm:
            NotificationSource(name='Source 1').full_clean()
        self.assertEqual(list(cm.exception.message_dict), ['name'])

    def test_ordering(self):
        for name in ('Source B', 'Source C', 'Source A'):
            NotificationSource.objects.create(name=name)

        self.assertEqual(
            list(NotificationSource.objects.values_list('name', flat=True)), ['Source A', 'Source B', 'Source C']
        )

    def test_providers(self):
        sources = (
            NotificationSource.objects.create(name='Source 1'),
            NotificationSource.objects.create(name='Source 2'),
        )
        sources[0].providers.set(self.providers)
        sources[1].providers.add(self.providers[1])

        self.assertQuerySetEqual(sources[0].providers.all(), self.providers)
        self.assertQuerySetEqual(self.providers[0].maintenance_notification_sources.all(), [sources[0]])
        self.assertQuerySetEqual(self.providers[1].maintenance_notification_sources.all(), sources)

    def test_token_round_trip(self):
        NotificationSource.objects.create(name='Source 1', _token=self.token)

        credentials = Credentials.from_authorized_user_info(NotificationSource.objects.get(name='Source 1')._token)
        self.assertEqual(
            (credentials.token, credentials.refresh_token, credentials.client_secret, credentials.expiry),
            ('access-token', 'refresh-token', 'client-secret', self.TOKEN_EXPIRY),
        )

    def test_token_not_editable(self):
        form_class = modelform_factory(NotificationSource, fields='__all__')

        self.assertNotIn('_token', form_class.base_fields)

    def test_serialize_object_excludes_token(self):
        source = NotificationSource.objects.create(name='Source 1', _token=self.token)

        self.assertNotIn('_token', source.serialize_object())
        self.assertNotIn('_token', source.serialize_object(exclude=['last_updated']))
        self.assertEqual(source.serialize_object(exclude=['last_updated'])['name'], 'Source 1')

    def test_changelog(self):
        # Deleting a provider removes it from the source, which NetBox records as an update to the source
        request = RequestFactory().get('/')
        request.id = uuid.uuid4()
        request.user = User.objects.create_user(username='user1')
        provider_pks = [provider.pk for provider in self.providers]

        with event_tracking(request):
            source = NotificationSource.objects.create(name='Source 1', _token=self.token)
            source.providers.set(self.providers)
        request.id = uuid.uuid4()
        with event_tracking(request):
            self.providers[0].delete()

        changes = ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(NotificationSource), changed_object_id=source.pk
        ).order_by('pk')
        self.assertEqual(
            [
                (
                    change.action,
                    change.prechange_data and change.prechange_data['providers'],
                    change.postchange_data['providers'],
                )
                for change in changes
            ],
            [
                (ObjectChangeActionChoices.ACTION_CREATE, None, provider_pks),
                (ObjectChangeActionChoices.ACTION_UPDATE, provider_pks, provider_pks[1:]),
            ],
        )
        for change in changes:
            self.assertNotIn('_token', change.prechange_data or {})
            self.assertNotIn('_token', change.postchange_data)
        source.refresh_from_db()
        self.assertEqual(source._token, self.token)

    def test_changelog_delete_with_raw_notifications(self):
        # Under event tracking NetBox nulls each raw notification's source with save(), recording an update and moving
        # its last_updated (which is why the handler's fetch window will read created; Decisions 2026-10-07). It also
        # serializes the deleted source for events straight away, so the delete needs the REST serializer
        request = RequestFactory().get('/')
        request.id = uuid.uuid4()
        request.user = User.objects.create_user(username='user1')
        source = NotificationSource.objects.create(name='Source 1', _token=self.token)
        raw_notifications = [
            RawNotification.objects.create(
                subject=f'Subject {i}', provider=self.providers[0], source=source, raw=b'body', stamp=START
            )
            for i in range(2)
        ]
        source_pk = source.pk

        with event_tracking(request):
            source.delete()

        change = ObjectChange.objects.get(
            changed_object_type=ObjectType.objects.get_for_model(NotificationSource), changed_object_id=source_pk
        )
        self.assertEqual(
            (change.action, change.prechange_data['name']), (ObjectChangeActionChoices.ACTION_DELETE, 'Source 1')
        )
        self.assertNotIn('_token', change.prechange_data)
        changes = ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(RawNotification)
        ).order_by('changed_object_id')
        self.assertEqual(
            [
                (
                    change.changed_object_id,
                    change.action,
                    change.prechange_data['source'],
                    change.postchange_data['source'],
                )
                for change in changes
            ],
            [
                (raw_notification.pk, ObjectChangeActionChoices.ACTION_UPDATE, source_pk, None)
                for raw_notification in raw_notifications
            ],
        )
        for raw_notification in raw_notifications:
            last_updated = raw_notification.last_updated
            raw_notification.refresh_from_db()
            self.assertIsNone(raw_notification.source)
            self.assertGreater(raw_notification.last_updated, last_updated)

    def test_event_data(self):
        # Event rule and webhook data comes from the REST serializer, which leaves out the token
        source = NotificationSource.objects.create(name='Source 1', _token=self.token)
        source.providers.set(self.providers)

        data = serialize_for_event(source)
        self.assertNotIn('_token', data)
        self.assertEqual(
            [provider['id'] for provider in data['providers']], [provider.pk for provider in self.providers]
        )

    def test_get_absolute_url(self):
        source = NotificationSource.objects.create(name='Source 1')

        self.assertEqual(source.get_absolute_url(), f'/plugins/circuit-maintenance/notification-sources/{source.pk}/')

    def test_export_template_token_attribute(self):
        # The Jinja2 sandbox blocks underscore attributes. ORM methods such as values_list('_token') still reach the
        # token: export template authors are trusted, as they can read any row in NetBox that way
        NotificationSource.objects.create(name='Source 1', _token=self.token)
        export_template = ExportTemplate(
            name='Sources',
            template_code='{% for source in queryset %}{{ source.name }}:{{ source._token }};{% endfor %}',
        )

        self.assertEqual(export_template.render(queryset=NotificationSource.objects.all()), 'Source 1:;')

    def test_docs_url(self):
        self.assertIsNone(NotificationSource().docs_url)

    def test_features(self):
        features = set(ObjectType.objects.get_for_model(NotificationSource).features)

        self.assertLessEqual(NETBOX_MODEL_FEATURES, features)


class RawNotificationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.providers = (
            Provider.objects.create(name='Provider 1', slug='provider-1'),
            Provider.objects.create(name='Provider 2', slug='provider-2'),
        )
        cls.source = NotificationSource.objects.create(name='Source 1')

    def raw_notification(self, **kwargs):
        return RawNotification(
            **{'raw': b'body', 'subject': 'Subject 1', 'provider': self.providers[0], 'stamp': START, **kwargs}
        )

    def plugin_settings(self, **overrides):
        # override_settings replaces the whole setting, so keep the defaults NetBox merged in at startup
        plugin_config = {**settings.PLUGINS_CONFIG['netbox_circuit_maintenance'], **overrides}
        return self.settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, 'netbox_circuit_maintenance': plugin_config})

    def test_defaults(self):
        raw_notification = self.raw_notification()
        raw_notification.full_clean()
        raw_notification.save()

        self.assertEqual(raw_notification.sender, '')
        self.assertIsNone(raw_notification.source)
        self.assertFalse(raw_notification.parsed)
        self.assertEqual(str(raw_notification), 'Subject 1')

    def test_future_stamp_warning(self):
        # Nautobot's test_future_stamp_validation: a future stamp is logged, not rejected
        stamp = datetime.now(UTC) + timedelta(days=1)
        raw_notification = self.raw_notification(raw=b'', subject='something', source=self.source, stamp=stamp)

        with self.assertLogs(logger='netbox_circuit_maintenance.models', level='WARNING') as cm:
            raw_notification.full_clean()
        self.assertEqual(
            cm.output,
            [f"WARNING:netbox_circuit_maintenance.models:Stamp time {stamp} is not consistent, it's in the future."],
        )

    def test_past_stamp(self):
        with self.assertNoLogs(logger='netbox_circuit_maintenance.models', level='WARNING'):
            self.raw_notification().full_clean()

    def test_clean_missing_stamp(self):
        # Django runs clean() after stamp fails validation, so it mustn't compare None with a datetime
        with self.assertRaises(ValidationError) as cm:
            self.raw_notification(stamp=None).full_clean()
        self.assertEqual(list(cm.exception.message_dict), ['stamp'])

    def test_raw_truncated(self):
        raw_notification = self.raw_notification(raw=b'x' * 10000)
        raw_notification.save()

        self.assertEqual(len(RawNotification.objects.get(pk=raw_notification.pk).raw), 8192)  # Default setting

    def test_raw_truncated_to_setting(self):
        raw_notification = self.raw_notification(raw=b'0123456789abcdef')
        with self.plugin_settings(raw_notification_size=10):
            raw_notification.save()

        self.assertEqual(RawNotification.objects.get(pk=raw_notification.pk).raw, b'0123456789')

    def test_raw_truncated_to_max(self):
        raw_notification = self.raw_notification(raw=b'x' * 20000)
        with self.plugin_settings(raw_notification_size=20000):
            raw_notification.save()

        self.assertEqual(len(RawNotification.objects.get(pk=raw_notification.pk).raw), MAX_NOTIFICATION_TOTAL_LENGTH)

    def test_unique_stamp_provider_subject(self):
        self.raw_notification().save()

        with self.assertRaises(ValidationError) as cm:
            self.raw_notification(sender='sender@example.com').full_clean()
        self.assertEqual(
            cm.exception.message_dict,
            {NON_FIELD_ERRORS: ['Raw notification with this Stamp, Provider and Subject already exists.']},
        )

        # Changing any one of the three is fine
        self.raw_notification(subject='Subject 2').full_clean()
        self.raw_notification(provider=self.providers[1]).full_clean()
        self.raw_notification(stamp=END).full_clean()

    def test_ordering(self):
        # Oldest stamp first, then pk. The handler takes the newest with last()
        raw_notifications = [
            RawNotification.objects.create(raw=b'', subject=f'Subject {i}', provider=self.providers[0], stamp=stamp)
            for i, stamp in enumerate((END, START, START))
        ]

        self.assertEqual(
            list(RawNotification.objects.all()), [raw_notifications[1], raw_notifications[2], raw_notifications[0]]
        )
        self.assertEqual(RawNotification.objects.last(), raw_notifications[0])

    def test_provider_cascade_delete(self):
        self.raw_notification().save()
        self.raw_notification(subject='Subject 2', provider=self.providers[1]).save()
        self.assertEqual(self.providers[0].maintenance_raw_notifications.count(), 1)

        self.providers[0].delete()
        self.assertEqual(list(RawNotification.objects.values_list('subject', flat=True)), ['Subject 2'])

    def test_source_delete(self):
        raw_notification = self.raw_notification(source=self.source)
        raw_notification.save()
        self.assertQuerySetEqual(self.source.raw_notifications.all(), [raw_notification])

        self.source.delete()
        raw_notification.refresh_from_db()
        self.assertIsNone(raw_notification.source)

    def test_serialize_object_excludes_raw(self):
        raw_notification = self.raw_notification()
        raw_notification.save()

        self.assertNotIn('raw', raw_notification.serialize_object())
        self.assertNotIn('raw', raw_notification.serialize_object(exclude=['last_updated']))
        self.assertEqual(raw_notification.serialize_object(exclude=['last_updated'])['subject'], 'Subject 1')

    def test_changelog(self):
        # The change log records the handler's create and parsed update, and a delete, without the body. Under event
        # tracking NetBox serializes a deleted object for events straight away, so the delete needs the REST serializer
        request = RequestFactory().get('/')
        request.id = uuid.uuid4()
        request.user = User.objects.create_user(username='user1')

        with event_tracking(request):
            raw_notification = self.raw_notification()
            raw_notification.save()
        with event_tracking(request):
            raw_notification.snapshot()
            raw_notification.parsed = True
            raw_notification.save()
        raw_notification.refresh_from_db()
        self.assertEqual(raw_notification.raw, b'body')
        raw_notification_pk = raw_notification.pk
        with event_tracking(request):
            raw_notification.snapshot()
            raw_notification.delete()

        changes = ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(RawNotification), changed_object_id=raw_notification_pk
        ).order_by('pk')
        self.assertEqual(
            [
                (
                    change.action,
                    change.prechange_data and change.prechange_data['parsed'],
                    change.postchange_data and change.postchange_data['parsed'],
                )
                for change in changes
            ],
            [
                (ObjectChangeActionChoices.ACTION_CREATE, None, False),
                (ObjectChangeActionChoices.ACTION_UPDATE, False, True),
                (ObjectChangeActionChoices.ACTION_DELETE, True, None),
            ],
        )
        for change in changes:
            self.assertNotIn('raw', change.prechange_data or {})
            self.assertNotIn('raw', change.postchange_data or {})

    def test_event_data_includes_raw(self):
        # Event rule and webhook data comes from the REST serializer, which returns the body as base64, although change
        # records leave it out (Decisions 2026-10-06)
        raw_notification = self.raw_notification()
        raw_notification.save()

        self.assertEqual(serialize_for_event(raw_notification)['raw'], 'Ym9keQ==')

    def test_get_absolute_url(self):
        raw_notification = self.raw_notification()
        raw_notification.save()

        self.assertEqual(
            raw_notification.get_absolute_url(),
            f'/plugins/circuit-maintenance/raw-notifications/{raw_notification.pk}/',
        )

    def test_docs_url(self):
        self.assertIsNone(RawNotification().docs_url)

    def test_features(self):
        features = set(ObjectType.objects.get_for_model(RawNotification).features)

        self.assertLessEqual(NETBOX_MODEL_FEATURES, features)


class ParsedNotificationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.providers = (
            Provider.objects.create(name='Provider 1', slug='provider-1'),
            Provider.objects.create(name='Provider 2', slug='provider-2'),
        )
        cls.maintenances = (
            CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=START, end_time=END),
            CircuitMaintenance.objects.create(
                name='UT-TEST-2', start_time=START + timedelta(days=1), end_time=END + timedelta(days=1)
            ),
        )
        cls.raw_notifications = (
            RawNotification.objects.create(raw=b'body 1', subject='Subject 1', provider=cls.providers[0], stamp=START),
            RawNotification.objects.create(raw=b'body 2', subject='Subject 2', provider=cls.providers[1], stamp=END),
        )

    def parsed_notification(self, maintenance=0, raw_notification=0, **kwargs):
        return ParsedNotification(
            maintenance=self.maintenances[maintenance],
            raw_notification=self.raw_notifications[raw_notification],
            **{'json': {'maintenance_id': 'UT-TEST'}, **kwargs},
        )

    def test_str(self):
        self.assertEqual(str(self.parsed_notification()), 'Parsed notification for Subject 1')

    def test_str_truncated(self):
        # Nautobot's test_run_nonexistent_circuit checks this with a subject the handler cut to 200 characters
        raw_notification = RawNotification(raw=b'', subject='x' * MAX_NOTIFICATION_SUBJECT_LENGTH)
        parsed_notification = ParsedNotification(raw_notification=raw_notification)

        self.assertEqual(str(parsed_notification), f'Parsed notification for {"x" * 176}')

    def test_required_fields(self):
        with self.assertRaises(ValidationError) as cm:
            ParsedNotification(json=None).full_clean()
        self.assertEqual(sorted(cm.exception.message_dict), ['json', 'maintenance', 'raw_notification'])

    def test_json_round_trip(self):
        data = {
            'circuits': [{'circuit_id': 'CID-1', 'impact': 'OUTAGE'}],
            'maintenance_id': 'UT-TEST',
            'start': 1601805600,
            'summary': 'Planned work',
        }
        parsed_notification = self.parsed_notification(json=data)
        parsed_notification.full_clean()
        parsed_notification.save()

        self.assertEqual(ParsedNotification.objects.get(pk=parsed_notification.pk).json, data)

    def test_ordering(self):
        # Least recently updated first, then pk. The raw notification's detail page takes the newest with last()
        parsed_notifications = [self.parsed_notification() for _ in range(3)]
        for parsed_notification in parsed_notifications:
            parsed_notification.save()
        ParsedNotification.objects.filter(pk=parsed_notifications[0].pk).update(last_updated=START + timedelta(days=1))
        ParsedNotification.objects.filter(pk__in=(parsed_notifications[1].pk, parsed_notifications[2].pk)).update(
            last_updated=START
        )

        self.assertEqual(
            list(ParsedNotification.objects.all()),
            [parsed_notifications[1], parsed_notifications[2], parsed_notifications[0]],
        )
        self.assertEqual(ParsedNotification.objects.last(), parsed_notifications[0])

    def test_reverse_relations(self):
        # A raw notification can announce several maintenances, and a maintenance collects every notification about
        # it. Nautobot had no uniqueness on the pair, so the same one can be stored twice
        parsed_notifications = (
            self.parsed_notification(),
            self.parsed_notification(maintenance=1),
            self.parsed_notification(raw_notification=1),
            self.parsed_notification(),
        )
        for parsed_notification in parsed_notifications:
            parsed_notification.full_clean()
            parsed_notification.save()

        self.assertQuerySetEqual(
            self.raw_notifications[0].parsed_notifications.all(),
            [parsed_notifications[0], parsed_notifications[1], parsed_notifications[3]],
            ordered=False,
        )
        self.assertQuerySetEqual(
            self.maintenances[0].parsed_notifications.all(),
            [parsed_notifications[0], parsed_notifications[2], parsed_notifications[3]],
            ordered=False,
        )

    def test_cascade_delete(self):
        self.parsed_notification().save()
        self.parsed_notification(maintenance=1).save()
        self.parsed_notification(maintenance=1, raw_notification=1).save()

        self.maintenances[0].delete()
        self.assertEqual(ParsedNotification.objects.count(), 2)

        self.raw_notifications[0].delete()
        self.assertEqual(ParsedNotification.objects.count(), 1)

        # Deleting a provider cascades through its raw notifications
        self.providers[1].delete()
        self.assertFalse(ParsedNotification.objects.exists())

    def test_changelog_related_object(self):
        # Same as CircuitImpactTestCase.test_changelog_related_object, with the delete cascaded from the raw
        # notification. Under event tracking NetBox serializes a deleted object for events straight away, so the delete
        # needs the REST serializer
        request = RequestFactory().get('/')
        request.id = uuid.uuid4()
        request.user = User.objects.create_user(username='user1')
        maintenance = self.maintenances[0]

        with event_tracking(request):
            parsed_notification = self.parsed_notification()
            parsed_notification.save()
        with event_tracking(request):
            parsed_notification.snapshot()
            parsed_notification.json = {'maintenance_id': 'UT-TEST', 'status': 'CONFIRMED'}
            parsed_notification.save()
        parsed_notification_pk = parsed_notification.pk
        with event_tracking(request):
            self.raw_notifications[0].delete()

        changes = ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(ParsedNotification),
            changed_object_id=parsed_notification_pk,
        ).order_by('pk')
        self.assertEqual(
            [
                (change.action, change.related_object_type.model, change.related_object_id, change.object_repr)
                for change in changes
            ],
            [
                (
                    ObjectChangeActionChoices.ACTION_CREATE,
                    'circuitmaintenance',
                    maintenance.pk,
                    'Parsed notification for Subject 1',
                ),
                (
                    ObjectChangeActionChoices.ACTION_UPDATE,
                    'circuitmaintenance',
                    maintenance.pk,
                    'Parsed notification for Subject 1',
                ),
                (
                    ObjectChangeActionChoices.ACTION_DELETE,
                    'circuitmaintenance',
                    maintenance.pk,
                    'Parsed notification for Subject 1',
                ),
            ],
        )
        # The parser output stays in the change record
        self.assertEqual(changes[1].postchange_data['json'], {'maintenance_id': 'UT-TEST', 'status': 'CONFIRMED'})

    def test_delete_event_notification_long_subject(self):
        # An event rule notification stores the REST display in a varchar(200) and doesn't truncate it once the object
        # is gone, so a delete with a 200-character subject relies on __str__ staying within 200 characters
        request = RequestFactory().get('/')
        request.id = uuid.uuid4()
        request.user = User.objects.create_user(username='user1')
        RawNotification.objects.filter(pk=self.raw_notifications[0].pk).update(
            subject='x' * MAX_NOTIFICATION_SUBJECT_LENGTH
        )
        parsed_notification = self.parsed_notification()
        parsed_notification.raw_notification.refresh_from_db()
        parsed_notification.save()
        notification_group = NotificationGroup.objects.create(name='Group 1')
        notification_group.users.add(request.user)
        event_rule = EventRule.objects.create(
            name='Rule 1',
            event_types=[OBJECT_DELETED],
            action_type=EventRuleActionChoices.NOTIFICATION,
            action_object=notification_group,
        )
        event_rule.object_types.add(ObjectType.objects.get_for_model(ParsedNotification))

        with event_tracking(request):
            parsed_notification.snapshot()
            parsed_notification.delete()

        notification = Notification.objects.get(user=request.user)
        self.assertEqual(notification.event_type, OBJECT_DELETED)
        self.assertEqual(notification.object_repr, f'Parsed notification for {"x" * 176}')

    def test_event_data(self):
        # Event rule and webhook data comes from the REST serializer: the parser output as an object, and the raw
        # notification without its body
        parsed_notification = self.parsed_notification()
        parsed_notification.save()

        data = serialize_for_event(parsed_notification)
        self.assertEqual(data['json'], {'maintenance_id': 'UT-TEST'})
        self.assertEqual(data['raw_notification']['subject'], 'Subject 1')
        self.assertNotIn('raw', data['raw_notification'])

    def test_get_absolute_url(self):
        parsed_notification = self.parsed_notification()
        parsed_notification.save()

        self.assertEqual(
            parsed_notification.get_absolute_url(),
            f'/plugins/circuit-maintenance/parsed-notifications/{parsed_notification.pk}/',
        )

    def test_docs_url(self):
        self.assertIsNone(ParsedNotification().docs_url)

    def test_features(self):
        features = set(ObjectType.objects.get_for_model(ParsedNotification).features)

        self.assertLessEqual(NETBOX_MODEL_FEATURES, features)
