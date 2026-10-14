import json
from unittest.mock import patch

from circuit_maintenance_parser import SUPPORTED_PROVIDER_NAMES
from circuits.models import Provider
from core.choices import ObjectChangeActionChoices
from core.events import OBJECT_UPDATED
from core.models import ObjectChange, ObjectType
from django.core import serializers
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape
from extras.choices import CustomFieldUIEditableChoices
from extras.models import CustomField
from netbox.choices import CSVDelimiterChoices, ImportFormatChoices
from rest_framework import status
from utilities.testing import APITestCase, post_data
from utilities.testing import TestCase as ViewTestCase

from netbox_circuit_maintenance.models import NotificationSource

# These tests don't import netbox_circuit_maintenance.signals: importing it would connect the receivers whether or not
# PluginConfig.ready() does

EMAILS = 'emails_circuit_maintenances'
PARSER = 'provider_parser_circuit_maintenances'


def email_error(email):
    return f"Invalid value for custom field '{EMAILS}': {email} is already used by another provider."


def parser_error(parser):
    return (
        f"Invalid value for custom field '{PARSER}': {parser} is not one of the supported providers in the "
        f'circuit-maintenance-parser library: {", ".join(SUPPORTED_PROVIDER_NAMES)}.'
    )


class ProviderValidationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.provider = Provider.objects.create(
            name='Provider 1',
            slug='provider-1',
            # The mail sources strip each address, so a space after the comma is common
            custom_field_data={EMAILS: 'noc@provider1.com, ops@provider1.com', PARSER: 'lumen'},
        )
        Provider.objects.create(name='Provider 2', slug='provider-2')

    def new_provider(self, **custom_field_data):
        return Provider(name='Provider 3', slug='provider-3', custom_field_data=custom_field_data)

    def assertErrors(self, provider, errors):
        with self.assertRaises(ValidationError) as cm:
            provider.full_clean()
        self.assertEqual(cm.exception.message_dict, {NON_FIELD_ERRORS: errors})

    def test_unused_emails(self):
        self.new_provider(**{EMAILS: 'noc@provider3.com,ops@provider3.com'}).full_clean()

    def test_used_email(self):
        self.assertErrors(
            self.new_provider(**{EMAILS: 'noc@provider3.com,noc@provider1.com'}),
            [email_error('noc@provider1.com')],
        )

    def test_used_email_case_and_spaces(self):
        # The mail sources match addresses stripped and lower-cased, where Nautobot compared them as entered
        self.assertErrors(self.new_provider(**{EMAILS: ' NOC@Provider1.com '}), [email_error('noc@provider1.com')])
        self.assertErrors(self.new_provider(**{EMAILS: 'ops@provider1.com'}), [email_error('ops@provider1.com')])

    def test_used_emails_each_reported_once(self):
        self.assertErrors(
            self.new_provider(**{EMAILS: 'ops@provider1.com,noc@provider1.com,NOC@provider1.com'}),
            [email_error('ops@provider1.com'), email_error('noc@provider1.com')],
        )

    def test_similar_emails(self):
        # Whole addresses are compared, not substrings
        self.new_provider(**{EMAILS: 'oc@provider1.com,noc@provider1.co,provider1.com'}).full_clean()

    def test_empty_entries(self):
        # Nautobot compared empty entries too, so two values with a trailing comma clashed
        Provider.objects.filter(pk=self.provider.pk).update(custom_field_data={EMAILS: 'noc@provider1.com,'})
        self.new_provider(**{EMAILS: 'noc@provider3.com,, '}).full_clean()

    def test_own_emails(self):
        # Saving a provider again doesn't clash with its own addresses, and one provider may repeat an address
        self.provider.full_clean()
        self.new_provider(**{EMAILS: 'noc@provider3.com,NOC@provider3.com'}).full_clean()

    def test_queries(self):
        # Checking the addresses costs one query however many there are, where Nautobot loaded every provider and its
        # custom fields
        provider = self.new_provider(**{PARSER: 'lumen'})
        provider.full_clean()
        with CaptureQueriesContext(connection) as without_emails:
            provider.full_clean()
        provider.custom_field_data[EMAILS] = 'a@x.com,b@x.com,c@x.com'
        with CaptureQueriesContext(connection) as with_emails:
            provider.full_clean()
        self.assertEqual(len(with_emails) - len(without_emails), 1)

    def test_supported_parser(self):
        for parser in ('lumen', 'Lumen', 'ZAYO', '', None):
            with self.subTest(parser=parser):
                self.new_provider(**{PARSER: parser}).full_clean()

    def test_unsupported_parser(self):
        for parser in ('unknown', ' lumen'):
            with self.subTest(parser=parser):
                self.assertErrors(self.new_provider(**{PARSER: parser}), [parser_error(parser)])

    def test_both_errors(self):
        self.assertErrors(
            self.new_provider(**{EMAILS: 'noc@provider1.com', PARSER: 'unknown'}),
            [email_error('noc@provider1.com'), parser_error('unknown')],
        )

    def test_non_string_values(self):
        # post_clean runs before NetBox validates custom field data, so the receiver skips other types (a TypeError or
        # AttributeError would escape assertRaises). NetBox then rejects them for a text field
        for value in (123, ['noc@provider1.com'], {'parser': 'unknown'}):
            with self.subTest(value=value):
                provider = self.new_provider(**{EMAILS: value, PARSER: value})
                with self.assertRaises(ValidationError) as cm:
                    provider.full_clean()
                self.assertNotIn('is already used', str(cm.exception))
                self.assertNotIn('supported providers', str(cm.exception))

        # Another provider's value of another type isn't an address
        Provider.objects.filter(pk=self.provider.pk).update(custom_field_data={EMAILS: 123})
        self.new_provider(**{EMAILS: '123'}).full_clean()

    def test_deleted_fields(self):
        # An admin can delete the fields, and migrate doesn't recreate them (Decisions 2026-10-13). Nautobot looked the
        # parser field up, so deleting it made every provider save fail
        CustomField.objects.get(name=PARSER).delete()
        self.provider.full_clean()
        self.assertErrors(self.new_provider(**{EMAILS: 'noc@provider1.com'}), [email_error('noc@provider1.com')])

        # NetBox removes a deleted field's values from every provider
        CustomField.objects.get(name=EMAILS).delete()
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.custom_field_data, {})
        self.provider.full_clean()
        self.new_provider().full_clean()


class ProviderValidationViewTestCase(ViewTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.providers = (
            Provider.objects.create(name='Provider 1', slug='provider-1', custom_field_data={EMAILS: 'noc@x.com'}),
            Provider.objects.create(name='Provider 2', slug='provider-2'),
            Provider.objects.create(name='Provider 3', slug='provider-3'),
        )

    def get_emails(self, provider):
        provider.refresh_from_db()
        return provider.custom_field_data.get(EMAILS)

    def test_edit(self):
        provider = self.providers[1]
        self.add_permissions('circuits.change_provider')
        url = reverse('circuits:provider_edit', kwargs={'pk': provider.pk})
        data = {'name': provider.name, 'slug': provider.slug, f'cf_{EMAILS}': 'NOC@x.com', f'cf_{PARSER}': 'unknown'}

        response = self.client.post(url, post_data(data))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, escape(email_error('noc@x.com')))
        self.assertContains(response, 'unknown is not one of the supported providers')
        self.assertIsNone(self.get_emails(provider))

        data.update({f'cf_{EMAILS}': 'noc@y.com', f'cf_{PARSER}': 'Lumen'})
        self.assertHttpStatus(self.client.post(url, post_data(data)), 302)
        self.assertEqual(self.get_emails(provider), 'noc@y.com')

    def test_import(self):
        # Rows are saved one at a time, so the second row clashes with the first, and the import is rolled back
        self.add_permissions('circuits.add_provider')
        data = {
            'data': f'name,slug,cf_{EMAILS}\nProvider 4,provider-4,new@x.com\nProvider 5,provider-5,NEW@x.com',
            'format': ImportFormatChoices.CSV,
            'csv_delimiter': CSVDelimiterChoices.AUTO,
        }

        response = self.client.post(reverse('circuits:provider_bulk_import'), data)
        self.assertHttpStatus(response, 200)
        self.assertContains(response, escape(email_error('new@x.com')))
        self.assertFalse(Provider.objects.filter(slug__in=('provider-4', 'provider-5')).exists())

    def test_edit_hidden_field(self):
        # Forms leave out a custom field hidden from the UI, and a model error keyed on a field the form lacks raises
        # ValueError, so the error is a non-field one on any NetBox version
        CustomField.objects.filter(name=EMAILS).update(ui_editable=CustomFieldUIEditableChoices.HIDDEN)
        provider = self.providers[1]
        Provider.objects.filter(pk=provider.pk).update(custom_field_data={EMAILS: 'noc@x.com'})
        self.add_permissions('circuits.change_provider')
        data = {'name': provider.name, 'slug': provider.slug, 'description': 'New description'}

        response = self.client.post(reverse('circuits:provider_edit', kwargs={'pk': provider.pk}), post_data(data))
        self.assertHttpStatus(response, 200)
        self.assertNotIn(f'cf_{EMAILS}', response.context['form'].fields)
        self.assertContains(response, escape(email_error('noc@x.com')))
        provider.refresh_from_db()
        self.assertEqual(provider.description, '')

    def test_import_update_without_custom_fields(self):
        # A CSV update without the custom field columns drops those form fields. Before NetBox 4.6.9 (#22683), a model
        # error keyed on a missing form field raised ValueError, so the error is a non-field one
        provider = self.providers[1]
        Provider.objects.filter(pk=provider.pk).update(custom_field_data={EMAILS: 'noc@x.com'})
        self.add_permissions('circuits.add_provider', 'circuits.change_provider')
        data = {
            'data': f'id,description\n{provider.pk},New description',
            'format': ImportFormatChoices.CSV,
            'csv_delimiter': CSVDelimiterChoices.AUTO,
        }

        response = self.client.post(reverse('circuits:provider_bulk_import'), data)
        self.assertHttpStatus(response, 200)
        self.assertContains(response, escape(email_error('noc@x.com')))
        provider.refresh_from_db()
        self.assertEqual(provider.description, '')

    def test_bulk_edit(self):
        # Objects are saved one at a time, so the second provider clashes with the first, and the edit is rolled back
        self.add_permissions('circuits.view_provider', 'circuits.change_provider')
        data = {'pk': [self.providers[1].pk, self.providers[2].pk], '_apply': True, f'cf_{EMAILS}': 'same@x.com'}

        response = self.client.post(reverse('circuits:provider_bulk_edit'), data)
        self.assertHttpStatus(response, 200)
        self.assertContains(response, escape(email_error('same@x.com')))
        self.assertIsNone(self.get_emails(self.providers[1]))
        self.assertIsNone(self.get_emails(self.providers[2]))


class ProviderValidationAPITestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.provider = Provider.objects.create(
            name='Provider 1', slug='provider-1', custom_field_data={EMAILS: 'noc@x.com'}
        )

    def test_create(self):
        self.add_permissions('circuits.add_provider')
        data = {'name': 'Provider 2', 'slug': 'provider-2', 'custom_fields': {EMAILS: 'noc@x.com'}}

        response = self.client.post(reverse('circuits-api:provider-list'), data, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {NON_FIELD_ERRORS: [email_error('noc@x.com')]})
        self.assertFalse(Provider.objects.filter(slug='provider-2').exists())

    def test_update(self):
        provider = Provider.objects.create(name='Provider 2', slug='provider-2')
        self.add_permissions('circuits.change_provider')
        url = reverse('circuits-api:provider-detail', kwargs={'pk': provider.pk})

        response = self.client.patch(url, {'custom_fields': {PARSER: 'unknown'}}, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {NON_FIELD_ERRORS: [parser_error('unknown')]})

        response = self.client.patch(url, {'custom_fields': {PARSER: 'zayo'}}, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)

    def test_bulk_update_same_email(self):
        # A bulk update validates and saves one object at a time, so the second provider clashes with the first, and
        # the update is rolled back
        providers = (
            Provider.objects.create(name='Provider 2', slug='provider-2'),
            Provider.objects.create(name='Provider 3', slug='provider-3'),
        )
        self.add_permissions('circuits.change_provider')
        data = [{'id': provider.pk, 'custom_fields': {EMAILS: 'new@x.com'}} for provider in providers]

        response = self.client.patch(reverse('circuits-api:provider-list'), data, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {NON_FIELD_ERRORS: [email_error('new@x.com')]})
        self.assertFalse(Provider.objects.filter(custom_field_data__emails_circuit_maintenances='new@x.com').exists())

    def test_bulk_create_same_email(self):
        # Pinned gap (Decisions 2026-10-14): a bulk create validates every object before saving any, so two new
        # providers can share an address in one request. NetBox's unique custom fields behave the same way
        self.add_permissions('circuits.add_provider')
        data = [
            {'name': f'Provider {i}', 'slug': f'provider-{i}', 'custom_fields': {EMAILS: 'new@x.com'}} for i in (2, 3)
        ]

        response = self.client.post(reverse('circuits-api:provider-list'), data, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(Provider.objects.filter(custom_field_data__emails_circuit_maintenances='new@x.com').count(), 2)


def get_source_changes(source):
    return ObjectChange.objects.filter(
        changed_object_type=ObjectType.objects.get_for_model(NotificationSource), changed_object_id=source.pk
    )


class AddProviderToSourcesTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.sources = (
            NotificationSource.objects.create(name='Source 1', attach_all_providers=True),
            NotificationSource.objects.create(name='Source 2'),
            NotificationSource.objects.create(name='Source 3', attach_all_providers=True),
        )

    def test_create(self):
        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        self.assertEqual(list(provider.maintenance_notification_sources.all()), [self.sources[0], self.sources[2]])

    def test_update(self):
        # Only a new provider is added, so one taken off a source stays off when it's saved again
        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        self.sources[0].providers.remove(provider)
        provider.description = 'New description'
        provider.save()
        self.assertEqual(list(provider.maintenance_notification_sources.all()), [self.sources[2]])

    def test_fixture(self):
        # loaddata saves each object with raw=True, so nothing is added; Nautobot added it
        data = json.dumps([{'model': 'circuits.provider', 'fields': {'name': 'Provider 1', 'slug': 'provider-1'}}])
        for obj in serializers.deserialize('json', data):
            obj.save()
        provider = Provider.objects.get(slug='provider-1')
        self.assertFalse(provider.maintenance_notification_sources.exists())


class AddProviderToSourcesViewTestCase(ViewTestCase):
    def test_create_changelog(self):
        # The source records an update with its providers before and after, under the user who added the provider, who
        # needs no permission on sources
        source = NotificationSource.objects.create(name='Source 1', attach_all_providers=True)
        provider1 = Provider.objects.create(name='Provider 1', slug='provider-1')
        self.add_permissions('circuits.add_provider')

        response = self.client.post(
            reverse('circuits:provider_add'), post_data({'name': 'Provider 2', 'slug': 'provider-2'})
        )
        self.assertHttpStatus(response, 302)
        provider2 = Provider.objects.get(slug='provider-2')
        self.assertEqual(
            [
                (change.action, change.user, change.prechange_data['providers'], change.postchange_data['providers'])
                for change in get_source_changes(source)
            ],
            [(ObjectChangeActionChoices.ACTION_UPDATE, self.user, [provider1.pk], [provider1.pk, provider2.pk])],
        )


class AddProviderToSourcesAPITestCase(APITestCase):
    def test_bulk_create_changelog(self):
        # NetBox merges a source's changes within one request into one record
        sources = (
            NotificationSource.objects.create(name='Source 1', attach_all_providers=True),
            NotificationSource.objects.create(name='Source 2', attach_all_providers=True),
        )
        self.add_permissions('circuits.add_provider')
        data = [{'name': f'Provider {i}', 'slug': f'provider-{i}'} for i in (1, 2)]

        response = self.client.post(reverse('circuits-api:provider-list'), data, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        provider_pks = [provider['id'] for provider in response.data]
        for source in sources:
            with self.subTest(source=source.name):
                self.assertEqual(
                    [
                        (change.action, change.prechange_data['providers'], change.postchange_data['providers'])
                        for change in get_source_changes(source)
                    ],
                    [(ObjectChangeActionChoices.ACTION_UPDATE, [], provider_pks)],
                )

    def test_bulk_create_events(self):
        # Event rules on source updates get one event per source, with its providers before and after. The default
        # EVENTS_PIPELINE entry is patched rather than queueing jobs in Redis
        source = NotificationSource.objects.create(name='Source 1', attach_all_providers=True)
        self.add_permissions('circuits.add_provider')
        data = [{'name': f'Provider {i}', 'slug': f'provider-{i}'} for i in (1, 2)]

        with patch('extras.events.process_event_queue') as process_event_queue:
            response = self.client.post(reverse('circuits-api:provider-list'), data, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        events = [
            (
                event['event_type'],
                event['snapshots']['prechange']['providers'],
                event['snapshots']['postchange']['providers'],
            )
            for event in process_event_queue.call_args.args[0]
            if event['object'] == source
        ]
        self.assertEqual(events, [(OBJECT_UPDATED, [], [provider['id'] for provider in response.data])])
