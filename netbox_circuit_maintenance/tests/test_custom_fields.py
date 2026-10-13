from importlib import import_module

from circuits.forms import ProviderForm
from circuits.models import Provider
from dcim.models import Site
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from extras.choices import CustomFieldTypeChoices
from extras.models import CustomField
from utilities.templatetags.builtins.filters import render_markdown

MIGRATION = ('netbox_circuit_maintenance', '0009_provider_custom_fields')
migration = import_module(f'{MIGRATION[0]}.migrations.{MIGRATION[1]}')

FIELDS = {
    'emails_circuit_maintenances': (
        'Circuit maintenance emails',
        "Comma-separated sender addresses of this provider's maintenance notifications",
    ),
    'provider_parser_circuit_maintenances': (
        'Circuit maintenance parser',
        "circuit-maintenance-parser provider name (e.g. lumen), if not the provider's name",
    ),
}


class ProviderCustomFieldsTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The models as the migration sees them. Not in setUpTestData, which deep-copies its attributes for each test
        cls.apps = MigrationLoader(connection).project_state(MIGRATION).apps

    def run_migration(self, operation='code'):
        # Only the database alias is read from the schema editor
        getattr(migration.Migration.operations[0], operation)(self.apps, connection.schema_editor())

    def test_fields(self):
        # migrate created both fields in the test database
        self.assertEqual(
            {cf.name: (cf.label, cf.description) for cf in CustomField.objects.get_for_model(Provider)},
            FIELDS,
        )
        for name in FIELDS:
            with self.subTest(name=name):
                custom_field = CustomField.objects.get(name=name)
                self.assertEqual(custom_field.type, CustomFieldTypeChoices.TYPE_TEXT)
                self.assertEqual(list(custom_field.object_types.all()), [ContentType.objects.get_for_model(Provider)])
                # get_or_create() skips validation, so this is what checks the name and the label and description
                # lengths
                custom_field.full_clean()

    def test_provider_values(self):
        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        self.assertIsNone(provider.cf['emails_circuit_maintenances'])
        self.assertIsNone(provider.cf['provider_parser_circuit_maintenances'])

        # clean() would drop these values if the fields weren't assigned to Provider
        provider.custom_field_data = {
            'emails_circuit_maintenances': 'noc@example.com,maintenance@example.com',
            'provider_parser_circuit_maintenances': 'lumen',
        }
        provider.full_clean()
        provider.save()

        provider = Provider.objects.get(pk=provider.pk)
        self.assertEqual(provider.cf['emails_circuit_maintenances'], 'noc@example.com,maintenance@example.com')
        self.assertEqual(provider.cf['provider_parser_circuit_maintenances'], 'lumen')

    def test_provider_form(self):
        # Where an admin sets them: the Provider edit form
        form = ProviderForm()
        for name, (label, description) in FIELDS.items():
            with self.subTest(name=name):
                self.assertEqual(form.fields[f'cf_{name}'].label, label)
                # NetBox renders the description as Markdown help text
                self.assertEqual(form.fields[f'cf_{name}'].help_text, render_markdown(description))

    def test_existing_field(self):
        # A field created before the migration ran keeps its settings and other object types, and gains Provider
        CustomField.objects.filter(name__in=FIELDS).delete()
        existing = CustomField.objects.create(
            name='emails_circuit_maintenances',
            type=CustomFieldTypeChoices.TYPE_LONGTEXT,
            label='Maintenance senders',
        )
        existing.object_types.set([ContentType.objects.get_for_model(Site)])

        self.run_migration()

        existing.refresh_from_db()
        self.assertEqual(existing.type, CustomFieldTypeChoices.TYPE_LONGTEXT)
        self.assertEqual(existing.label, 'Maintenance senders')
        self.assertEqual(
            set(existing.object_types.all()),
            {ContentType.objects.get_for_model(Provider), ContentType.objects.get_for_model(Site)},
        )
        created = CustomField.objects.get(name='provider_parser_circuit_maintenances')
        self.assertEqual(created.type, CustomFieldTypeChoices.TYPE_TEXT)
        self.assertEqual((created.label, created.description), FIELDS['provider_parser_circuit_maintenances'])
        self.assertEqual(list(created.object_types.all()), [ContentType.objects.get_for_model(Provider)])

    def test_rerun(self):
        # Edits made in the UI survive the migration running again
        custom_field = CustomField.objects.get(name='emails_circuit_maintenances')
        custom_field.label = 'Maintenance senders'
        custom_field.save()
        custom_field.object_types.add(ContentType.objects.get_for_model(Site))

        self.run_migration()

        custom_field.refresh_from_db()
        self.assertEqual(custom_field.label, 'Maintenance senders')
        self.assertEqual(custom_field.object_types.count(), 2)
        self.assertEqual(CustomField.objects.filter(name__in=FIELDS).count(), 2)

    def test_reverse(self):
        # Pins the no-op reverse: migrating backwards leaves the fields and their values in place. (Doesn't run a
        # backwards migrate, which would undo the plugin's other migrations too)
        provider = Provider.objects.create(
            name='Provider 1',
            slug='provider-1',
            custom_field_data={'emails_circuit_maintenances': 'noc@example.com'},
        )

        self.run_migration('reverse_code')

        self.assertEqual(CustomField.objects.get_for_model(Provider).count(), 2)
        provider.refresh_from_db()
        self.assertEqual(provider.cf['emails_circuit_maintenances'], 'noc@example.com')
