from circuits.models import Provider
from core.models import ObjectChange
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from extras.models import Bookmark, JournalEntry, Notification, Subscription, Tag
from users.models import User
from utilities.templatetags.builtins.filters import render_markdown

from netbox_circuit_maintenance.models import NotificationSource, RawNotification
from netbox_circuit_maintenance.utils import (
    NotificationSourceSyncError,
    markdown_code_span,
    split_provider_emails,
    sync_notification_sources,
)

from .utils import plugin_settings


class SplitProviderEmailsTestCase(SimpleTestCase):
    def test_split(self):
        for value, emails in (
            ('noc@x.com', ['noc@x.com']),
            (' NOC@X.com , ops@x.com', ['noc@x.com', 'ops@x.com']),
            # Repeats are kept, in order
            ('noc@x.com,NOC@x.com', ['noc@x.com', 'noc@x.com']),
            # Empty entries are skipped
            ('noc@x.com,', ['noc@x.com']),
            (' , ,', []),
            ('', []),
        ):
            with self.subTest(value=value):
                self.assertEqual(split_provider_emails(value), emails)

    def test_not_a_string(self):
        # An unset field is None, and an admin can change the field's type
        for value in (None, 123, ['noc@x.com'], {'email': 'noc@x.com'}):
            with self.subTest(value=value):
                self.assertEqual(split_provider_emails(value), [])


class MarkdownCodeSpanTestCase(SimpleTestCase):
    def test_code_span(self):
        for text, span in (
            ('ABC-1', '` ABC-1 `'),
            ('A`B', '`` A`B ``'),
            ('``A`', '``` ``A` ```'),
            ('', '`  `'),
        ):
            with self.subTest(text=text):
                self.assertEqual(markdown_code_span(text), span)

    def test_rendered(self):
        # NetBox's Markdown rendering shows the text verbatim, HTML and Markdown syntax included
        for text, html in (
            ('<ABC-1>', '&lt;ABC-1&gt;'),
            ('A*B*C', 'A*B*C'),
            ('_X_', '_X_'),
            ('A`B', 'A`B'),
            ('`A', '`A'),
            ('A``', 'A``'),
            ('A\\', 'A\\'),
            ('[x](javascript:alert(1))', '[x](javascript:alert(1))'),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    render_markdown(markdown_code_span(text)),
                    f'<div class="rendered-markdown"><p><code>{html}</code></p></div>',
                )


class SyncNotificationSourcesTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.providers = (
            Provider.objects.create(name='Provider 1', slug='provider-1'),
            Provider.objects.create(name='Provider 2', slug='provider-2'),
        )

    def sync(self, *sources):
        with plugin_settings(notification_sources=list(sources)):
            return sync_notification_sources()

    def assertSources(self, expected):
        """
        Assert the sources' names, attach_all_providers and providers' names.
        """
        self.assertEqual(
            [
                (source.name, source.attach_all_providers, sorted(source.providers.values_list('name', flat=True)))
                for source in NotificationSource.objects.all()
            ],
            expected,
        )

    def test_create(self):
        # Keys other than these two are for the sources
        messages = self.sync(
            {'name': 'Source 1', 'url': 'imap://example.com', 'attach_all_providers': True},
            {'name': 'Source 2', 'url': 'imap://example.com'},
        )
        self.assertSources([('Source 1', True, ['Provider 1', 'Provider 2']), ('Source 2', False, [])])
        self.assertEqual(
            messages,
            [
                'Created notification source Source 1',
                'Added 2 provider(s) to notification source Source 1',
                'Created notification source Source 2',
            ],
        )

    def test_unchanged(self):
        # A source is saved only when it changes, so its subscribers aren't notified on every migrate
        source = NotificationSource.objects.create(name='Source 1', attach_all_providers=True)
        source.providers.set(self.providers)
        last_updated = source.last_updated
        user = User.objects.create(username='User 1')
        Subscription.objects.create(user=user, object=source)

        self.assertEqual(self.sync({'name': 'Source 1', 'attach_all_providers': True}), [])
        source.refresh_from_db()
        self.assertEqual(source.last_updated, last_updated)
        self.assertFalse(Notification.objects.exists())

    def test_update(self):
        source = NotificationSource.objects.create(name='Source 1')
        source.providers.add(self.providers[0])
        user = User.objects.create(username='User 1')
        Subscription.objects.create(user=user, object=source)

        messages = self.sync({'name': 'Source 1', 'attach_all_providers': True})
        self.assertSources([('Source 1', True, ['Provider 1', 'Provider 2'])])
        self.assertEqual(
            messages,
            [
                'Set attach_all_providers to True on notification source Source 1',
                'Added 1 provider(s) to notification source Source 1',
            ],
        )
        # Saving the source notifies its subscribers, but without a request nothing is change-logged
        self.assertEqual(list(Notification.objects.values_list('event_type', flat=True)), ['object_updated'])
        self.assertFalse(ObjectChange.objects.exists())
        last_updated = source.last_updated
        source.refresh_from_db()
        self.assertGreater(source.last_updated, last_updated)

        # Turning it off keeps the providers, as in Nautobot
        messages = self.sync({'name': 'Source 1'})
        self.assertSources([('Source 1', False, ['Provider 1', 'Provider 2'])])
        self.assertEqual(messages, ['Set attach_all_providers to False on notification source Source 1'])

    def test_reattach_removed_provider(self):
        # As in Nautobot, a provider taken off an attach-all source is added back on the next sync
        source = NotificationSource.objects.create(name='Source 1', attach_all_providers=True)
        source.providers.add(self.providers[1])

        messages = self.sync({'name': 'Source 1', 'attach_all_providers': True})
        self.assertSources([('Source 1', True, ['Provider 1', 'Provider 2'])])
        self.assertEqual(messages, ['Added 1 provider(s) to notification source Source 1'])

    def test_delete(self):
        kept = NotificationSource.objects.create(name='Source 1')
        kept.providers.set(self.providers)
        deleted = NotificationSource.objects.create(name='Source 2')
        deleted.providers.set(self.providers)
        deleted.tags.add(Tag.objects.create(name='Tag 1', slug='tag-1'))
        user = User.objects.create(username='User 1')
        Bookmark.objects.create(user=user, object=deleted)
        Subscription.objects.create(user=user, object=deleted)
        JournalEntry.objects.create(assigned_object=deleted, comments='Comment')
        raw_notification = RawNotification.objects.create(
            subject='Subject', provider=self.providers[0], source=deleted, raw=b'body', stamp=timezone.now()
        )
        last_updated = raw_notification.last_updated

        messages = self.sync({'name': 'Source 1'})
        self.assertSources([('Source 1', False, ['Provider 1', 'Provider 2'])])
        self.assertEqual(messages, ['Deleted notification source Source 2, which is no longer in PLUGINS_CONFIG'])

        # What hangs off the source goes with it, its subscribers are notified, and its raw notifications are kept, as
        # when it's deleted in the UI. Without a request nothing is change-logged, so the raw notifications aren't
        # re-saved
        source_type = ContentType.objects.get_for_model(NotificationSource)
        for model in (Bookmark, Subscription):
            self.assertFalse(model.objects.filter(object_type=source_type, object_id=deleted.pk).exists())
        self.assertFalse(
            JournalEntry.objects.filter(assigned_object_type=source_type, assigned_object_id=deleted.pk).exists()
        )
        self.assertFalse(Tag.objects.get(slug='tag-1').extras_taggeditem_items.exists())
        self.assertEqual(
            list(Notification.objects.values_list('object_repr', 'event_type')), [('Source 2', 'object_deleted')]
        )
        raw_notification.refresh_from_db()
        self.assertIsNone(raw_notification.source)
        self.assertEqual(raw_notification.last_updated, last_updated)
        self.assertFalse(ObjectChange.objects.exists())

    def test_empty(self):
        # The default deletes every source
        NotificationSource.objects.create(name='Source 1')
        messages = self.sync()
        self.assertFalse(NotificationSource.objects.exists())
        self.assertEqual(messages, ['Deleted notification source Source 1, which is no longer in PLUGINS_CONFIG'])

    def test_attach_all_providers_values(self):
        # Whatever the model field accepts, as Nautobot stored. Nautobot attached every provider for a string such as
        # 'False' too
        for value, attach_all_providers in (
            (True, True),
            ('True', True),
            (1, True),
            (False, False),
            ('False', False),
            ('f', False),
            ('0', False),
            (0, False),
        ):
            with self.subTest(value=value):
                NotificationSource.objects.all().delete()
                self.sync({'name': 'Source 1', 'attach_all_providers': value})
                source = NotificationSource.objects.get()
                self.assertIs(source.attach_all_providers, attach_all_providers)
                self.assertEqual(source.providers.count(), 2 if attach_all_providers else 0)

    def test_invalid(self):
        # Nothing changes: the configured source isn't created and the other isn't deleted
        NotificationSource.objects.create(name='Source 1')
        source = {'name': 'Source 2', 'secret': 'password'}
        for sources, message in (
            ([source, {'secret': 'password'}], 'notification_sources[1] in PLUGINS_CONFIG needs a name of up to 100'),
            ([source, {'name': ''}], 'notification_sources[1] in PLUGINS_CONFIG needs a name'),
            ([source, {'name': '  '}], 'notification_sources[1] in PLUGINS_CONFIG needs a name'),
            ([source, {'name': 123}], 'notification_sources[1] in PLUGINS_CONFIG needs a name'),
            ([source, {'name': 'x' * 101}], 'notification_sources[1] in PLUGINS_CONFIG needs a name'),
            ([source, {'name': 'Source 2'}], 'Name Source 2 is used by more than one notification source'),
            (
                [source, {'name': 'Source 3', 'attach_all_providers': 'yes'}],
                'attach_all_providers for Source 3 in PLUGINS_CONFIG must be True or False.',
            ),
            (
                [source, {'name': 'Source 3', 'attach_all_providers': None}],
                'attach_all_providers for Source 3 in PLUGINS_CONFIG must be True or False.',
            ),
        ):
            with self.subTest(sources=sources):
                with self.assertRaisesMessage(NotificationSourceSyncError, message) as context:
                    self.sync(*sources)
                self.assertNotIn('password', str(context.exception))
                self.assertSources([('Source 1', False, [])])

    def test_protected(self):
        # A protection rule on sources stops the sync, and its changes so far are undone: Source 2 isn't created
        NotificationSource.objects.create(name='Source 1')
        protection_rules = {'netbox_circuit_maintenance.notificationsource': [{'name': {'eq': 'Unprotected'}}]}
        with (
            self.settings(PROTECTION_RULES=protection_rules),
            self.assertRaisesMessage(
                NotificationSourceSyncError,
                "Notification source Source 1 can't be deleted. Deletion is prevented by a protection rule",
            ),
        ):
            self.sync({'name': 'Source 2'})
        self.assertSources([('Source 1', False, [])])

    def test_unapplied_migrations(self):
        # Before migrate finishes, the current models may not match the database
        NotificationSource.objects.create(name='Source 1')
        # The plugin's latest migration: an earlier one with a later one applied isn't planned again
        [(app_label, name)] = MigrationLoader(connection).graph.leaf_nodes('netbox_circuit_maintenance')
        MigrationRecorder(connection).migration_qs.filter(app=app_label, name=name).delete()
        with self.assertRaisesMessage(NotificationSourceSyncError, 'The database has 1 unapplied migration(s).'):
            self.sync({'name': 'Source 2'})
        self.assertSources([('Source 1', False, [])])

    def test_name_max_length(self):
        name = 'x' * 100
        self.sync({'name': name})
        self.assertEqual(NotificationSource.objects.get().name, name)
