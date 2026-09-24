from datetime import UTC, datetime, timedelta

from core.models import ObjectType
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.forms import modelform_factory
from django.test import TestCase

from netbox_circuit_maintenance.choices import CircuitMaintenanceStatusChoices
from netbox_circuit_maintenance.models import CircuitMaintenance

START = datetime(2020, 10, 4, 10, 0, tzinfo=UTC)
END = START + timedelta(hours=2)


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
        # NetBox counterparts of the Nautobot feature flags (custom_validators -> custom_validation,
        # webhooks -> event_rules), plus the rest of the NetBoxModel feature set
        features = set(ObjectType.objects.get_for_model(CircuitMaintenance).features)

        self.assertLessEqual(
            {
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
            },
            features,
        )
