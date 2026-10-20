import base64
import csv
import hashlib
import html
import io
import json
import logging
import os
import re
import tempfile
import uuid
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from circuits.models import Circuit, CircuitTermination, CircuitType, Provider
from core.choices import JobNotificationChoices, JobStatusChoices
from core.models import Job, ObjectChange, ObjectType
from dcim.models import (
    Cable,
    Device,
    DeviceRole,
    DeviceType,
    FrontPort,
    Interface,
    Manufacturer,
    PortMapping,
    RearPort,
    Site,
)
from django.conf import settings
from django.contrib.messages import ERROR, SUCCESS, WARNING, get_messages
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from netbox.choices import CSVDelimiterChoices, ImportFormatChoices
from netbox.context_managers import event_tracking
from netbox.registry import registry
from users.models import ObjectPermission
from utilities.testing import ModelViewTestCase, ViewTestCases, create_tags, post_data

from netbox_circuit_maintenance.choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
from netbox_circuit_maintenance.handle_notifications.handler import HandleCircuitMaintenanceNotifications
from netbox_circuit_maintenance.handle_notifications.sources import IMAP
from netbox_circuit_maintenance.models import (
    CircuitImpact,
    CircuitMaintenance,
    Note,
    NotificationSource,
    ParsedNotification,
    RawNotification,
)
from netbox_circuit_maintenance.site_search import FindSitesWithMaintenanceOverlap
from netbox_circuit_maintenance.views import GOOGLE_OAUTH_SESSION_KEY, CircuitMaintenanceOverviewView

from .test_sources import (
    OAUTH_CLIENT_SECRETS,
    SOURCE_GMAIL_API_OAUTH,
    SOURCE_GMAIL_API_SERVICE_ACCOUNT,
    GmailSourcesMixin,
    write_file,
)
from .utils import plugin_settings

START = datetime(2020, 10, 4, 10, 0, tzinfo=UTC)
END = START + timedelta(hours=2)

GMAIL_READONLY_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
GMAIL_MODIFY_SCOPE = 'https://www.googleapis.com/auth/gmail.modify'


def get_message_list(response):
    """
    The messages of the response's request, as (level, message) pairs: the ones it added, and any earlier ones that no
    page has shown yet.
    """
    return [(message.level, message.message) for message in get_messages(response.wsgi_request)]


def token_response(status=200, **body):
    """
    A response from Google's OAuth token endpoint.
    """
    response = requests.Response()
    response.request = requests.Request('POST', OAUTH_CLIENT_SECRETS['web']['token_uri']).prepare()
    response.status_code = status
    response.headers['Content-Type'] = 'application/json'
    response._content = json.dumps(body).encode()
    return response


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


class CircuitMaintenanceFindOverlapsTestCase(PluginViewTestMixin, ModelViewTestCase):
    """
    The Find Site Overlaps button, which queues the overlap job. Nautobot's job was run from Nautobot's job list.
    """

    model = CircuitMaintenance

    def setUp(self):
        super().setUp()
        patcher = patch('netbox_circuit_maintenance.views.any_workers_for_queue', return_value=True)
        self.any_workers_for_queue = patcher.start()
        self.addCleanup(patcher.stop)

    def find_overlaps(self, query=''):
        """
        Post the button's form, and return the response and the RQ calls it queued: TestCase doesn't run on_commit
        callbacks, so nothing reaches Redis.
        """
        with self.captureOnCommitCallbacks() as callbacks:
            response = self.client.post(f'{self._get_url("find_overlaps")}{query}')
        return response, callbacks

    def test_find_overlaps(self):
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        response, callbacks = self.find_overlaps()

        job = Job.objects.get()
        # The user can't view jobs, so back to the maintenances, told where the results will be
        self.assertRedirects(response, self._get_url('list'), fetch_redirect_response=False)
        self.assertEqual(
            get_message_list(response),
            [
                (SUCCESS, f'Queued job #{job.pk} to find sites with circuit maintenance overlap.'),
                (
                    WARNING,
                    f"The overlaps will be in the log of job #{job.pk}, which you don't have permission to view.",
                ),
            ],
        )
        self.assertRegex(job.name, r'^Find Sites With Circuit Maintenance Overlap - [0-9a-f]{8}$')
        # The run covers what the user can view, and the user gets NetBox's notification when it ends
        self.assertEqual(job.user, self.user)
        self.assertEqual(job.notifications, JobNotificationChoices.NOTIFICATION_ALWAYS)
        self.assertEqual((job.status, job.interval, job.object_type), (JobStatusChoices.STATUS_PENDING, None, None))
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0].args, (FindSitesWithMaintenanceOverlap.handle,))
        self.assertEqual(callbacks[0].keywords['job'], job)
        self.any_workers_for_queue.assert_called_once_with('default')

        # The worker's call reaches run(), which checks the maintenance the user can view
        CircuitMaintenance.objects.create(name='UT-TEST-1', start_time=timezone.now(), end_time=timezone.now())
        logger = logging.getLogger(f'netbox.jobs.{FindSitesWithMaintenanceOverlap.__name__}')
        self.addCleanup(
            lambda: [logger.removeHandler(h) for h in list(logger.handlers) if getattr(h, 'job', None) is job]
        )
        FindSitesWithMaintenanceOverlap.handle(job)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(job.log_entries[-1]['message'], 'Successfully checked through 1 maintenance notification.')

    def test_find_overlaps_job_page(self):
        # A user who can view the job goes to its page, whose Log tab lists the overlaps
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance', 'core.view_job')

        response, _ = self.find_overlaps()
        job = Job.objects.get()
        self.assertRedirects(response, job.get_absolute_url())
        self.assertEqual(
            get_message_list(response),
            [(SUCCESS, f'Queued job #{job.pk} to find sites with circuit maintenance overlap.')],
        )

    def test_find_overlaps_get(self):
        # Queueing creates a job, so a GET only leads back
        url = self._get_url('find_overlaps')
        self.assertHttpStatus(self.client.get(url), 403)

        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')
        self.assertRedirects(self.client.get(url), self._get_url('list'))
        self.assertFalse(Job.objects.exists())

    def test_find_overlaps_permissions(self):
        response, callbacks = self.find_overlaps()
        self.assertHttpStatus(response, 403)
        self.assertFalse(Job.objects.exists())
        self.assertEqual(callbacks, [])

        # Checked for the model: the run covers the maintenances the user can view, whatever the constraints
        obj_perm = ObjectPermission(name='Test permission', constraints={'name': 'UT-TEST-1'}, actions=['view'])
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(CircuitMaintenance))
        response, _ = self.find_overlaps()
        self.assertHttpStatus(response, 302)
        self.assertTrue(Job.objects.exists())

    @override_settings(LOGIN_REQUIRED=False, EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_find_overlaps_anonymous(self):
        # Exempt view permissions let anonymous users see the list, but a run needs a user: without one it would cover
        # everything. They're sent to log in, and don't see the button
        self.client.logout()

        response, callbacks = self.find_overlaps()
        self.assertRedirects(
            response, f'{settings.LOGIN_URL}?next={self._get_url("find_overlaps")}', fetch_redirect_response=False
        )
        self.assertFalse(Job.objects.exists())
        self.assertEqual(callbacks, [])

        response = self.client.get(self._get_url('list'))
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, 'Find Site Overlaps')

    def test_find_overlaps_return_url(self):
        dashboard_url = self._get_url('overview')
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        response, _ = self.find_overlaps(query=f'?return_url={dashboard_url}')
        self.assertRedirects(response, dashboard_url, fetch_redirect_response=False)
        # Only to this site's pages
        response, _ = self.find_overlaps(query='?return_url=https://example.com/')
        self.assertRedirects(response, self._get_url('list'), fetch_redirect_response=False)

    def test_find_overlaps_names(self):
        # Each run has a name of its own. When the RQ worker starts, it schedules overlap_job_interval's runs with
        # enqueue_once(), which deletes a queued job under the job's name and schedules a second series
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')
        self.find_overlaps()
        self.find_overlaps()
        runs = list(Job.objects.all())
        self.assertEqual(len({job.name for job in runs}), 2)

        FindSitesWithMaintenanceOverlap.enqueue_once(interval=60)
        self.assertQuerySetEqual(Job.objects.filter(pk__in=[job.pk for job in runs]), runs, ordered=False)
        self.assertEqual(Job.objects.filter(name=FindSitesWithMaintenanceOverlap.name).count(), 1)

    def test_find_overlaps_no_worker(self):
        # As NetBox's script form
        self.any_workers_for_queue.return_value = False
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        response, callbacks = self.find_overlaps()
        self.assertRedirects(response, self._get_url('list'), fetch_redirect_response=False)
        self.assertEqual(get_message_list(response), [(ERROR, 'Unable to run: RQ worker process not running.')])
        self.assertFalse(Job.objects.exists())
        self.assertEqual(callbacks, [])

    def test_list_objects_button(self):
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')
        filtered_list_url = f'{self._get_url("list")}?status=CONFIRMED&q=UT'

        response = self.client.get(filtered_list_url)
        self.assertHttpStatus(response, 200)
        # Nautobot's job description as a tooltip
        self.assertContains(
            response,
            '<button type="submit" class="btn btn-primary" title="Search for sites with overlapping circuit '
            'maintenances, assuming only two circuits per site"><i class="mdi mdi-calendar-alert" aria-hidden="true">'
            '</i> Find Site Overlaps</button>',
            html=True,
        )
        # A form posting the CSRF token
        match = re.search(
            r'<form action="([^"]+)" method="post">\s*<input type="hidden" name="csrfmiddlewaretoken" value="\w+">\s*'
            r'<button type="submit" class="btn btn-primary" title="Search for sites',
            response.content.decode(),
        )
        self.assertIsNotNone(match)
        # A run covers every maintenance the user can view, so the list's filters don't go with it, only the way back
        action = urlsplit(html.unescape(match[1]))
        self.assertEqual(action.path, self._get_url('find_overlaps'))
        self.assertEqual(parse_qs(action.query), {'return_url': [filtered_list_url]})


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


# The page's Configuration panel sets up each source from the plugin configuration
@plugin_settings(
    notification_sources=[
        {'name': f'Source {i}', 'url': 'imap://mail.example.com', 'account': f'noc{i}@example.com', 'secret': f'pw-{i}'}
        for i in (1, 2, 3)
    ]
)
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

    def test_get_object_configuration(self):
        source = NotificationSource.objects.get(name='Source 2')
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')

        with self.assertNoLogs('netbox_circuit_maintenance'):
            response = self.client.get(source.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Configuration')
        self.assertContains(response, '<th scope="row">Type</th><td>IMAP</td>', html=True)
        self.assertContains(response, '<th scope="row">Account ID</th><td>noc2@example.com</td>', html=True)
        self.assertNotContains(response, 'Authentication status')
        self.assertNotContains(response, 'pw-2')

    def test_get_object_configuration_error(self):
        source = NotificationSource.objects.get(name='Source 1')
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')

        for notification_sources, error in (
            ([], 'Failed to initialize source: Name Source 1 not found in PLUGINS_CONFIG.'),
            # A secret of the wrong type isn't shown
            (
                [{'name': 'Source 1', 'url': 'imap://mail.example.com', 'secret': 12345678}],
                'Failed to initialize source: 2 validation errors for IMAP<br>account<br>',
            ),
        ):
            with (
                self.subTest(error=error),
                plugin_settings(notification_sources=notification_sources),
                self.assertLogs('netbox_circuit_maintenance.ui.panels', 'WARNING') as logs,
            ):
                response = self.client.get(source.get_absolute_url())
                self.assertHttpStatus(response, 200)
                self.assertContains(response, 'Authentication status')
                self.assertContains(response, error)
                self.assertNotContains(response, '12345678')
                self.assertTrue(logs.output[0].startswith(f'WARNING:netbox_circuit_maintenance.ui.panels:{error[:40]}'))

    def test_get_object_configuration_gmail(self):
        # The credentials file's type picks the Gmail source class
        source = NotificationSource.objects.get(name='Source 1')
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')
        config = {'name': 'Source 1', 'url': 'https://accounts.google.com/o/oauth2/auth', 'account': 'noc@example.com'}

        with tempfile.TemporaryDirectory() as directory:
            paths = {}
            for name, credentials in (
                ('service_account', {'type': 'service_account'}),
                ('oauth', {'web': {}}),
                ('installed', {'installed': {}}),
            ):
                paths[name] = os.path.join(directory, f'{name}.json')
                with open(paths[name], 'w') as file:
                    json.dump(credentials, file)

            for name, source_type in (('service_account', 'GmailAPIServiceAccount'), ('oauth', 'GmailAPIOauth')):
                with (
                    self.subTest(source_type=source_type),
                    plugin_settings(notification_sources=[{**config, 'credentials_file': paths[name]}]),
                    self.assertNoLogs('netbox_circuit_maintenance'),
                ):
                    response = self.client.get(source.get_absolute_url())
                    self.assertHttpStatus(response, 200)
                    self.assertContains(response, f'<th scope="row">Type</th><td>{source_type}</td>', html=True)
                    self.assertContains(response, '<th scope="row">Account ID</th><td>noc@example.com</td>', html=True)
                    self.assertNotContains(response, 'Authentication status')

            # An unsupported credentials file is reported on the page (Nautobot's NotImplementedError was a 500)
            with (
                plugin_settings(notification_sources=[{**config, 'credentials_file': paths['installed']}]),
                self.assertLogs('netbox_circuit_maintenance.ui.panels', 'WARNING'),
            ):
                response = self.client.get(source.get_absolute_url())
            self.assertHttpStatus(response, 200)
            self.assertContains(
                response,
                f'Failed to initialize source: File {paths["installed"]} doesn&#x27;t contain any supported credentials.',
            )

    def test_get_object_configuration_graph(self):
        source = NotificationSource.objects.get(name='Source 1')
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')
        config = {
            'name': 'Source 1',
            'url': 'https://graph.microsoft.com',
            'account': 'noc@example.com',
            'tenant_id': 'contoso.onmicrosoft.com',
            'client_id': 'client-id',
            'secret': 'client-secret-value',
        }

        with (
            plugin_settings(notification_sources=[config]),
            self.assertNoLogs('netbox_circuit_maintenance'),
        ):
            response = self.client.get(source.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, '<th scope="row">Type</th><td>MicrosoftGraph</td>', html=True)
        self.assertContains(response, '<th scope="row">Account ID</th><td>noc@example.com</td>', html=True)
        self.assertNotContains(response, 'Authentication status')
        self.assertNotContains(response, 'client-secret-value')

        # A malformed credential is reported on the page, without the secret
        with (
            plugin_settings(notification_sources=[{**config, 'tenant_id': 'contoso tenant'}]),
            self.assertLogs('netbox_circuit_maintenance.ui.panels', 'WARNING'),
        ):
            response = self.client.get(source.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(
            response,
            'Failed to initialize source: 1 validation error for MicrosoftGraph<br>  Value error, Invalid tenant',
        )
        self.assertNotContains(response, 'client-secret-value')

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
        # Sources can't be added or imported, and have no bulk edit or bulk delete. Run Now needs its own permission
        # (NotificationSourceFetchTestCase)
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

    def test_get_object_validate_button(self):
        # Nautobot's test_custom_actions. Validating needs only view permission, as the page does
        source = NotificationSource.objects.get(name='Source 1')
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')

        response = self.client.get(source.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Validate Authentication')
        # A form posting the CSRF token
        self.assertRegex(
            response.content.decode(),
            rf'<form action="{self._get_url("validate", source)}" method="post">\s*'
            r'<input type="hidden" name="csrfmiddlewaretoken" value="\w+">',
        )

    @patch.object(IMAP, '_authentication_logic', autospec=True)
    def test_validate_object(self, authentication_logic):
        # Nautobot's test_validate_view_ok and test_validate_view_ko. The result is a message on the source's page
        source = NotificationSource.objects.get(name='Source 1')
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')

        response = self.client.post(self._get_url('validate', source))
        self.assertRedirects(response, source.get_absolute_url())
        self.assertEqual(get_message_list(response), [(SUCCESS, 'SUCCESS: Test OK')])
        authentication_logic.assert_called_once()

        authentication_logic.side_effect = ConnectionRefusedError('Some error')
        response = self.client.post(self._get_url('validate', source), follow=True)
        self.assertContains(response, 'FAILED: Some error')
        self.assertEqual(
            [(message.level, message.message) for message in response.context['messages']],
            [(ERROR, 'FAILED: Some error')],
        )

    @patch.object(IMAP, '_authentication_logic', autospec=True)
    def test_validate_object_return_url(self, authentication_logic):
        source = NotificationSource.objects.get(name='Source 1')
        list_url = self._get_url('list')
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')

        response = self.client.post(f'{self._get_url("validate", source)}?return_url={list_url}')
        self.assertRedirects(response, list_url)
        # Only to this site's pages (Nautobot redirected to any return_url)
        response = self.client.post(f'{self._get_url("validate", source)}?return_url=https://example.com/')
        self.assertRedirects(response, source.get_absolute_url())

    @patch.object(IMAP, '_authentication_logic', autospec=True)
    def test_validate_object_get(self, authentication_logic):
        # Only a POST validates
        source = NotificationSource.objects.get(name='Source 1')
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')

        response = self.client.get(self._get_url('validate', source))
        self.assertRedirects(response, source.get_absolute_url())
        authentication_logic.assert_not_called()
        self.assertEqual(get_message_list(response), [])

    @plugin_settings(notification_sources=[])
    def test_validate_object_configuration_error(self):
        source = NotificationSource.objects.get(name='Source 1')
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')

        response = self.client.post(self._get_url('validate', source))
        # The page's Configuration panel reports the error too
        with self.assertLogs('netbox_circuit_maintenance.ui.panels', 'WARNING'):
            self.assertRedirects(response, source.get_absolute_url())
        self.assertEqual(get_message_list(response), [(ERROR, 'FAILED: Name Source 1 not found in PLUGINS_CONFIG.')])

    @patch.object(IMAP, '_authentication_logic', autospec=True)
    def test_validate_object_permissions(self, authentication_logic):
        source = NotificationSource.objects.get(name='Source 1')

        response = self.client.post(self._get_url('validate', source))
        self.assertHttpStatus(response, 403)

        # Only the sources the user can view
        obj_perm = ObjectPermission(name='Test permission', constraints={'name': 'Source 2'}, actions=['view'])
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(NotificationSource))
        response = self.client.post(self._get_url('validate', source))
        self.assertHttpStatus(response, 404)
        authentication_logic.assert_not_called()


class NotificationSourceGoogleOAuthTestCase(GmailSourcesMixin, PluginViewTestMixin, ModelViewTestCase):
    """
    Validate, authorize and the OAuth callback with a Google OAuth source. oauthlib runs for real; only Google's token
    endpoint (requests) and the token refresh are mocked.
    """

    model = NotificationSource
    # Google's token endpoint's answer to the authorization code
    TOKEN = {
        'access_token': 'access-token',
        'expires_in': 3599,
        'refresh_token': 'refresh-token',
        'scope': GMAIL_READONLY_SCOPE,
        'token_type': 'Bearer',
    }

    @classmethod
    def setUpTestData(cls):
        cls.source = NotificationSource.objects.create(name=SOURCE_GMAIL_API_OAUTH['name'])
        cls.service_account_source = NotificationSource.objects.create(name=SOURCE_GMAIL_API_SERVICE_ACCOUNT['name'])

    @staticmethod
    def refresh(credentials, request):
        # Stands in for google-auth's request to Google's token endpoint
        credentials.token = 'new-access-token'
        credentials.expiry = datetime(2031, 1, 1)  # noqa: DTZ001 (google-auth's naive UTC)

    def authorize(self):
        """
        Start the source's authorization over HTTPS, and return the state Google sends back to the callback.
        """
        response = self.client.get(self._get_url('google_authorize', self.source), secure=True)
        self.assertHttpStatus(response, 302)
        return self.client.session[GOOGLE_OAUTH_SESSION_KEY]['state']

    def callback(self, params, token=None, secure=True, refresh=None):
        """
        Send the user back from Google with the given query parameters. The token endpoint answers with the token
        response (by default TOKEN), or raises it if it's an exception. Validating calls refresh() (by default
        self.refresh()) for the token refresh.
        """
        # Not "token or": a Response is false for an error status
        if token is None:
            token = token_response(**self.TOKEN)
        with (
            patch.object(
                requests.Session,
                'request',
                autospec=True,
                return_value=token,
                side_effect=token if isinstance(token, Exception) else None,
            ) as token_request,
            patch.object(Credentials, 'refresh', autospec=True, side_effect=refresh or self.refresh),
        ):
            response = self.client.get(self._get_url('google_oauth2callback'), params, secure=secure)
        return response, token_request

    def get_token(self):
        return NotificationSource.objects.get(pk=self.source.pk)._token

    def test_validate_unauthorized(self):
        # A source without a token goes on to its authorization, which needs permission to change the source
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')
        error = f'FAILED: {self.source} needs Google OAuth authorization by a user who can change it.'

        response = self.client.post(self._get_url('validate', self.source))
        self.assertRedirects(response, self.source.get_absolute_url())
        self.assertEqual(get_message_list(response), [(ERROR, error)])

        obj_perm = ObjectPermission(
            name='Test permission', constraints={'name': self.service_account_source.name}, actions=['change']
        )
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(NotificationSource))
        response = self.client.post(self._get_url('validate', self.source))
        self.assertRedirects(response, self.source.get_absolute_url())
        self.assertEqual(get_message_list(response), [(ERROR, error)])

        # Permission to change this source
        obj_perm.constraints = {'name': self.source.name}
        obj_perm.save()
        response = self.client.post(self._get_url('validate', self.source))
        self.assertRedirects(response, self._get_url('google_authorize', self.source), fetch_redirect_response=False)
        self.assertEqual(get_message_list(response), [])

    def test_authorize(self):
        self.add_permissions('netbox_circuit_maintenance.change_notificationsource')

        with plugin_settings(notification_sources=[{**self.config_oauth, 'extra_scopes': [GMAIL_MODIFY_SCOPE]}]):
            response = self.client.get(self._get_url('google_authorize', self.source), secure=True)
        self.assertHttpStatus(response, 302)
        authorization = self.client.session[GOOGLE_OAUTH_SESSION_KEY]
        url = urlsplit(response.url)
        self.assertEqual(f'{url.scheme}://{url.netloc}{url.path}', OAUTH_CLIENT_SECRETS['web']['auth_uri'])
        code_challenge = base64.urlsafe_b64encode(hashlib.sha256(authorization['code_verifier'].encode()).digest())
        self.assertEqual(
            parse_qs(url.query),
            {
                'response_type': ['code'],
                'client_id': ['client-id'],
                'redirect_uri': [
                    'https://testserver/plugins/circuit-maintenance/notification-sources/google-oauth2callback/'
                ],
                'scope': [f'{GMAIL_READONLY_SCOPE} {GMAIL_MODIFY_SCOPE}'],
                'state': [authorization['state']],
                'code_challenge': [code_challenge.decode().rstrip('=')],
                'code_challenge_method': ['S256'],
                'access_type': ['offline'],
                'prompt': ['consent'],
            },
        )
        self.assertEqual(authorization['source'], self.source.pk)

    def test_authorize_errors(self):
        self.add_permissions(
            'netbox_circuit_maintenance.view_notificationsource',
            'netbox_circuit_maintenance.change_notificationsource',
        )
        incomplete_file = write_file(self.directory, 'incomplete.json', {'web': {'client_id': 'client-id'}})
        web = {key: value for key, value in OAUTH_CLIENT_SECRETS['web'].items() if key != 'client_secret'}
        no_secret_file = write_file(self.directory, 'no_secret.json', {'web': web})

        for source, notification_sources, error in (
            (
                self.service_account_source,
                [self.config_service_account],
                f'FAILED: {self.service_account_source} is not a Google OAuth source.',
            ),
            (
                self.source,
                [{**self.config_oauth, 'credentials_file': incomplete_file}],
                'FAILED: Client secrets is not in the correct format.',
            ),
            # Reported before the user's consent: the token request needs it
            (
                self.source,
                [{**self.config_oauth, 'credentials_file': no_secret_file}],
                f'FAILED: Credentials_file {no_secret_file} for {self.source} has no client_secret.',
            ),
        ):
            with self.subTest(error=error), plugin_settings(notification_sources=notification_sources):
                response = self.client.get(self._get_url('google_authorize', source))
                self.assertRedirects(response, source.get_absolute_url())
                self.assertEqual(get_message_list(response), [(ERROR, error)])
                self.assertNotIn(GOOGLE_OAUTH_SESSION_KEY, self.client.session)

        with plugin_settings(notification_sources=[]):
            response = self.client.get(self._get_url('google_authorize', self.source))
            # The page's Configuration panel reports the error too
            with self.assertLogs('netbox_circuit_maintenance.ui.panels', 'WARNING'):
                self.assertRedirects(response, self.source.get_absolute_url())
        self.assertEqual(
            get_message_list(response), [(ERROR, f'FAILED: Name {self.source} not found in PLUGINS_CONFIG.')]
        )
        self.assertNotIn(GOOGLE_OAUTH_SESSION_KEY, self.client.session)

    def test_authorize_permissions(self):
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')

        response = self.client.get(self._get_url('google_authorize', self.source))
        self.assertHttpStatus(response, 403)

        # Only the sources the user can change
        obj_perm = ObjectPermission(
            name='Test permission', constraints={'name': self.service_account_source.name}, actions=['change']
        )
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(NotificationSource))
        response = self.client.get(self._get_url('google_authorize', self.source))
        self.assertHttpStatus(response, 404)
        self.assertNotIn(GOOGLE_OAUTH_SESSION_KEY, self.client.session)

    def test_authorization(self):
        # Validate, authorize, consent at Google, then the callback stores the token and validates the source
        self.add_permissions(
            'netbox_circuit_maintenance.view_notificationsource',
            'netbox_circuit_maintenance.change_notificationsource',
        )
        last_updated = NotificationSource.objects.get(pk=self.source.pk).last_updated
        response = self.client.post(self._get_url('validate', self.source), secure=True)
        self.assertRedirects(response, self._get_url('google_authorize', self.source), fetch_redirect_response=False)
        state = self.authorize()
        code_verifier = self.client.session[GOOGLE_OAUTH_SESSION_KEY]['code_verifier']

        with patch('extras.events.process_event_queue') as process_event_queue:
            response, token_request = self.callback({'state': state, 'code': 'authorization-code'})
        self.assertRedirects(response, self.source.get_absolute_url())
        self.assertEqual(get_message_list(response), [(SUCCESS, 'SUCCESS: Test OK')])
        # The code is exchanged with the PKCE code verifier, at the redirect URI the authorization used
        self.assertEqual(token_request.call_args.args[1:], ('POST', OAUTH_CLIENT_SECRETS['web']['token_uri']))
        self.assertEqual(
            token_request.call_args.kwargs['data'],
            {
                'grant_type': 'authorization_code',
                'code': 'authorization-code',
                'redirect_uri': 'https://testserver/plugins/circuit-maintenance/notification-sources/google-oauth2callback/',
                'code_verifier': code_verifier,
            },
        )
        # Validating refreshed the stored token
        token = self.get_token()
        self.assertEqual(
            (token['token'], token['refresh_token'], token['client_id'], token['scopes']),
            ('new-access-token', 'refresh-token', 'client-id', [GMAIL_READONLY_SCOPE]),
        )
        # Stored as a refresh is: no change record, event or new last_updated (user's choice)
        self.assertEqual(NotificationSource.objects.get(pk=self.source.pk).last_updated, last_updated)
        self.assertFalse(
            ObjectChange.objects.filter(
                changed_object_type=ObjectType.objects.get_for_model(NotificationSource),
                changed_object_id=self.source.pk,
            ).exists()
        )
        process_event_queue.assert_not_called()
        # An authorization completes once
        self.assertNotIn(GOOGLE_OAUTH_SESSION_KEY, self.client.session)
        response, token_request = self.callback({'state': state, 'code': 'authorization-code'})
        self.assertRedirects(response, self._get_url('list'))
        self.assertEqual(get_message_list(response), [(ERROR, 'No Google OAuth authorization is in progress.')])
        token_request.assert_not_called()

    def test_callback_errors(self):
        # Reported on the source's page (Nautobot's callback answered each with a 500)
        self.add_permissions(
            'netbox_circuit_maintenance.view_notificationsource',
            'netbox_circuit_maintenance.change_notificationsource',
        )
        for description, params, token, secure, error in (
            ('consent denied', {'error': 'access_denied'}, None, True, 'FAILED: access_denied'),
            (
                'another state',
                {'state': 'other-state', 'code': 'authorization-code'},
                None,
                True,
                'FAILED: mismatching_state: CSRF Warning! State not equal in request and response.',
            ),
            (
                'code rejected',
                {'code': 'authorization-code'},
                token_response(400, error='invalid_grant', error_description='Bad Request'),
                True,
                'FAILED: invalid_grant: Bad Request',
            ),
            (
                'token endpoint unreachable',
                {'code': 'authorization-code'},
                requests.ConnectionError('Connection refused'),
                True,
                'FAILED: Connection refused',
            ),
            (
                'over HTTP',
                {'code': 'authorization-code'},
                None,
                False,
                'FAILED: insecure_transport: OAuth 2 MUST utilize https.',
            ),
        ):
            with self.subTest(description):
                state = self.authorize()
                response, _token_request = self.callback({'state': state, **params}, token=token, secure=secure)
                self.assertRedirects(response, self.source.get_absolute_url())
                self.assertEqual(get_message_list(response), [(ERROR, error)])
                self.assertIsNone(self.get_token())
                self.assertNotIn(GOOGLE_OAUTH_SESSION_KEY, self.client.session)

    def test_callback_other_scopes(self):
        # oauthlib refuses a token for other scopes than the requested ones. Without include_granted_scopes, that
        # happens when the user deselects one of them on Google's consent screen
        self.add_permissions(
            'netbox_circuit_maintenance.view_notificationsource',
            'netbox_circuit_maintenance.change_notificationsource',
        )

        with plugin_settings(notification_sources=[{**self.config_oauth, 'extra_scopes': [GMAIL_MODIFY_SCOPE]}]):
            state = self.authorize()
            response, _token_request = self.callback({'state': state, 'code': 'authorization-code'})
        self.assertRedirects(response, self.source.get_absolute_url())
        # oauthlib lists each set of scopes in no particular order
        [(level, message)] = get_message_list(response)
        self.assertEqual(level, ERROR)
        self.assertTrue(message.startswith('FAILED: Scope has changed from "'), message)
        self.assertTrue(message.endswith(f'" to "{GMAIL_READONLY_SCOPE}".'), message)
        self.assertIsNone(self.get_token())

    def test_callback_without_refresh_token(self):
        # The source couldn't load the token, so it keeps the one it has
        self.add_permissions(
            'netbox_circuit_maintenance.view_notificationsource',
            'netbox_circuit_maintenance.change_notificationsource',
        )
        token = {key: value for key, value in self.TOKEN.items() if key != 'refresh_token'}
        previous_token = {'token': 'previous-access-token', 'refresh_token': 'previous-refresh-token'}
        NotificationSource.objects.filter(pk=self.source.pk).update(_token=previous_token)

        state = self.authorize()
        response, _token_request = self.callback(
            {'state': state, 'code': 'authorization-code'}, token=token_response(**token)
        )
        self.assertRedirects(response, self.source.get_absolute_url())
        self.assertEqual(get_message_list(response), [(ERROR, 'FAILED: Google returned no refresh token.')])
        self.assertEqual(self.get_token(), previous_token)

    def test_callback_refresh_error(self):
        # Validating the new token fails: the token is discarded, as a failed refresh in Validate does
        self.add_permissions(
            'netbox_circuit_maintenance.view_notificationsource',
            'netbox_circuit_maintenance.change_notificationsource',
        )
        error = RefreshError(
            'invalid_grant: Token has been expired or revoked.',
            {'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'},
            retryable=False,
        )

        state = self.authorize()
        response, _token_request = self.callback({'state': state, 'code': 'authorization-code'}, refresh=error)
        self.assertRedirects(response, self.source.get_absolute_url())
        self.assertEqual(
            get_message_list(response), [(ERROR, 'FAILED: invalid_grant: Token has been expired or revoked.')]
        )
        self.assertIsNone(self.get_token())

    def test_callback_configuration_changed(self):
        # The source's configuration changed between the authorization's start and the callback
        self.add_permissions(
            'netbox_circuit_maintenance.view_notificationsource',
            'netbox_circuit_maintenance.change_notificationsource',
        )

        state = self.authorize()
        with plugin_settings(notification_sources=[]):
            response, token_request = self.callback({'state': state, 'code': 'authorization-code'})
            # The page's Configuration panel reports the error too
            with self.assertLogs('netbox_circuit_maintenance.ui.panels', 'WARNING'):
                self.assertRedirects(response, self.source.get_absolute_url())
        self.assertEqual(
            get_message_list(response), [(ERROR, f'FAILED: Name {self.source} not found in PLUGINS_CONFIG.')]
        )
        token_request.assert_not_called()
        self.assertIsNone(self.get_token())

    def test_callback_permissions(self):
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')
        response = self.client.get(self._get_url('google_oauth2callback'))
        self.assertHttpStatus(response, 403)

        # The source in the session must be one the user can change
        obj_perm = ObjectPermission(name='Test permission', constraints={'name': self.source.name}, actions=['change'])
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(NotificationSource))
        state = self.authorize()
        obj_perm.constraints = {'name': self.service_account_source.name}
        obj_perm.save()
        response, token_request = self.callback({'state': state, 'code': 'authorization-code'})
        self.assertHttpStatus(response, 404)
        token_request.assert_not_called()
        self.assertIsNone(self.get_token())


class NotificationSourceFetchTestCase(PluginViewTestMixin, ModelViewTestCase):
    """
    Run now, which Nautobot's tests didn't cover: its run_job redirected to the job's run form.
    """

    model = NotificationSource

    def setUp(self):
        super().setUp()
        patcher = patch('netbox_circuit_maintenance.views.any_workers_for_queue', return_value=True)
        self.any_workers_for_queue = patcher.start()
        self.addCleanup(patcher.stop)

    def run_now(self, data=None, query=''):
        """
        Post the Run now form, and return the response and the RQ calls it queued: TestCase doesn't run on_commit
        callbacks, so nothing reaches Redis.
        """
        with self.captureOnCommitCallbacks() as callbacks:
            response = self.client.post(f'{self._get_url("fetch")}{query}', data or {})
        return response, callbacks

    def test_permission_registered(self):
        # NetBox registers the model's Meta.permissions, so the ObjectPermission form offers the action
        actions = registry['model_actions']['netbox_circuit_maintenance.notificationsource']
        self.assertEqual(
            {action.name: str(action.help_text) for action in actions},
            {'fetch': 'Fetch notifications from all sources and update circuit maintenances'},
        )

    def test_get_form(self):
        url = self._get_url('fetch')
        self.assertHttpStatus(self.client.get(url), 403)
        # Viewing sources isn't enough
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')
        self.assertHttpStatus(self.client.get(url), 403)

        self.add_permissions('netbox_circuit_maintenance.fetch_notificationsource')
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Update Circuit Maintenances')
        self.assertContains(response, 'name="dry_run"')
        self.assertFalse(response.context['form']['dry_run'].value())
        self.assertContains(
            response, f'<a href="{self._get_url("list")}" class="btn btn-outline-secondary">Cancel</a>', html=True
        )
        self.assertFalse(Job.objects.exists())

    def test_run_now(self):
        self.add_permissions('netbox_circuit_maintenance.fetch_notificationsource')

        response, callbacks = self.run_now()

        job = Job.objects.get()
        # The user can't view jobs, so back to the sources
        self.assertRedirects(response, self._get_url('list'), fetch_redirect_response=False)
        self.assertEqual(
            get_message_list(response), [(SUCCESS, f'Queued job #{job.pk} to update circuit maintenances.')]
        )
        self.assertRegex(job.name, r'^Update Circuit Maintenances - [0-9a-f]{8}$')
        # The run records its changes as the user, who gets NetBox's notification when it ends
        self.assertEqual(job.user, self.user)
        self.assertEqual(job.notifications, JobNotificationChoices.NOTIFICATION_ALWAYS)
        self.assertEqual((job.status, job.interval, job.object_type), (JobStatusChoices.STATUS_PENDING, None, None))
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0].args, (HandleCircuitMaintenanceNotifications.handle,))
        self.assertEqual(callbacks[0].keywords['job'], job)
        self.assertIs(callbacks[0].keywords['dry_run'], False)
        self.any_workers_for_queue.assert_called_once_with('default')

    def test_run_now_dry_run(self):
        self.add_permissions('netbox_circuit_maintenance.fetch_notificationsource')

        response, callbacks = self.run_now({'dry_run': 'on'})
        self.assertHttpStatus(response, 302)
        self.assertIs(callbacks[0].keywords['dry_run'], True)

        # The worker's call reaches run()
        job = callbacks[0].keywords['job']
        logger = logging.getLogger(f'netbox.jobs.{HandleCircuitMaintenanceNotifications.__name__}')
        self.addCleanup(
            lambda: [logger.removeHandler(h) for h in list(logger.handlers) if getattr(h, 'job', None) is job]
        )
        HandleCircuitMaintenanceNotifications.handle(job, dry_run=callbacks[0].keywords['dry_run'])
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(job.data, {'dry_run': True, 'raw_notifications': []})

    def test_run_now_job_page(self):
        # A user who can view the job goes to its page, which has its data and a Log tab
        self.add_permissions('netbox_circuit_maintenance.fetch_notificationsource', 'core.view_job')

        response, _ = self.run_now()
        self.assertRedirects(response, Job.objects.get().get_absolute_url())

    def test_run_now_job_page_constrained(self):
        # Permission to view only other jobs leads back to the sources
        self.add_permissions('netbox_circuit_maintenance.fetch_notificationsource')
        obj_perm = ObjectPermission(name='Test permission', constraints={'name': 'Other job'}, actions=['view'])
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(Job))

        response, _ = self.run_now()
        self.assertRedirects(response, self._get_url('list'), fetch_redirect_response=False)

    def test_run_now_return_url(self):
        url = self._get_url('fetch')
        raw_notifications_url = reverse('plugins:netbox_circuit_maintenance:rawnotification_list')
        self.add_permissions('netbox_circuit_maintenance.fetch_notificationsource')

        response = self.client.get(f'{url}?return_url={raw_notifications_url}')
        self.assertContains(
            response, f'<a href="{raw_notifications_url}" class="btn btn-outline-secondary">Cancel</a>', html=True
        )
        response, _ = self.run_now(query=f'?return_url={raw_notifications_url}')
        self.assertRedirects(response, raw_notifications_url, fetch_redirect_response=False)
        # Only to this site's pages
        response, _ = self.run_now(query='?return_url=https://example.com/')
        self.assertRedirects(response, self._get_url('list'), fetch_redirect_response=False)

    def test_run_now_names(self):
        # Each run has a name of its own. When the RQ worker starts, it schedules job_interval's runs with
        # enqueue_once(), which deletes a queued job under the handler's name and schedules a second series
        self.add_permissions('netbox_circuit_maintenance.fetch_notificationsource')
        self.run_now()
        self.run_now()
        runs = list(Job.objects.all())
        self.assertEqual(len({job.name for job in runs}), 2)

        HandleCircuitMaintenanceNotifications.enqueue_once(interval=60)
        self.assertQuerySetEqual(Job.objects.filter(pk__in=[job.pk for job in runs]), runs, ordered=False)
        self.assertEqual(Job.objects.filter(name=HandleCircuitMaintenanceNotifications.name).count(), 1)

    def test_run_now_no_worker(self):
        # As NetBox's script form
        self.any_workers_for_queue.return_value = False
        self.add_permissions('netbox_circuit_maintenance.fetch_notificationsource')

        response, callbacks = self.run_now()
        self.assertHttpStatus(response, 200)
        self.assertEqual(get_message_list(response), [(ERROR, 'Unable to run: RQ worker process not running.')])
        self.assertFalse(Job.objects.exists())
        self.assertEqual(callbacks, [])

    def test_run_now_permissions(self):
        response, _ = self.run_now()
        self.assertHttpStatus(response, 403)
        self.assertFalse(Job.objects.exists())

        # Checked for the model, as a run fetches from every source: a permission's constraints don't narrow it
        obj_perm = ObjectPermission(name='Test permission', constraints={'name': 'Source 2'}, actions=['fetch'])
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(NotificationSource))
        response, _ = self.run_now()
        self.assertHttpStatus(response, 302)
        self.assertTrue(Job.objects.exists())

    def test_list_objects_button(self):
        list_url = self._get_url('list')
        run_now_button = (
            f'<a href="{self._get_url("fetch")}" class="btn btn-primary" role="button">'
            '<i class="mdi mdi-play" aria-hidden="true"></i> Run Now</a>'
        )
        self.add_permissions('netbox_circuit_maintenance.view_notificationsource')
        response = self.client.get(list_url)
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, run_now_button, html=True)

        self.add_permissions('netbox_circuit_maintenance.fetch_notificationsource')
        response = self.client.get(list_url)
        self.assertContains(response, run_now_button, html=True)


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


class CircuitMaintenanceOverviewTestCase(PluginViewTestMixin, ModelViewTestCase):
    # Ports Nautobot's DashboardTest and DashboardTestZeroMaintenances, which called the view's helpers with this date;
    # the requests here patch timezone.localdate, which the view reads per request
    model = CircuitMaintenance
    today = date(2022, 8, 25)

    @classmethod
    def setUpTestData(cls):
        maintenances = {
            name: CircuitMaintenance.objects.create(name=name, start_time=start, end_time=start + timedelta(hours=2))
            for name, start in (
                ('UT-TEST-1', datetime(2022, 8, 24, 10, tzinfo=UTC)),  # In the past 7 days
                ('UT-TEST-4', datetime(2022, 8, 16, 10, tzinfo=UTC)),  # In the past 30 days
                ('UT-TEST-2', datetime(2022, 8, 26, 10, tzinfo=UTC)),  # In the next 7 days
                ('UT-TEST-3', datetime(2022, 8, 27, 10, tzinfo=UTC)),  # In the next 7 days
                ('UT-TEST-5', datetime(2022, 3, 27, 10, tzinfo=UTC)),  # In the past 365 days
            )
        }

        cls.sites = {name: Site.objects.create(name=name, slug=name.lower()) for name in ('Site A', 'Site B', 'Site Z')}
        manufacturer = Manufacturer.objects.create(name='Manufacturer 1', slug='manufacturer-1')
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model='Device Type 1', slug='device-type-1')
        role = DeviceRole.objects.create(name='Device Role 1', slug='device-role-1')
        cls.devices = {
            name: Device.objects.create(name=name, site=cls.sites[site], device_type=device_type, role=role)
            for name, site in (
                ('Device A', 'Site A'),
                ('Patch Panel', 'Site A'),
                ('Device B', 'Site B'),
                ('Device Z', 'Site Z'),
            )
        }
        interfaces = {
            name: Interface.objects.create(device=cls.devices[name], name='eth0', type='1000base-t')
            for name in ('Device A', 'Device B', 'Device Z')
        }
        front_port = FrontPort.objects.create(device=cls.devices['Patch Panel'], name='Front Port 1', type='8p8c')
        rear_port = RearPort.objects.create(device=cls.devices['Patch Panel'], name='Rear Port 1', type='8p8c')
        PortMapping.objects.create(
            device=cls.devices['Patch Panel'],
            front_port=front_port,
            front_port_position=1,
            rear_port=rear_port,
            rear_port_position=1,
        )

        provider = Provider.objects.create(name='Provider 1', slug='provider-1')
        circuit_type = CircuitType.objects.create(name='Circuit Type 1', slug='circuit-type-1')
        cls.circuits = {
            cid: Circuit.objects.create(cid=cid, provider=provider, type=circuit_type)
            for cid in ('CID-1', 'CID-2', 'CID-3')
        }
        terminations = {
            (cid, side): CircuitTermination.objects.create(
                circuit=cls.circuits[cid], term_side=side, termination=cls.sites[site]
            )
            for cid, side, site in (
                ('CID-1', 'A', 'Site A'),
                ('CID-1', 'Z', 'Site Z'),
                ('CID-2', 'A', 'Site A'),
                ('CID-2', 'Z', 'Site B'),
            )
        }
        # CID-1: Device A through a patch panel on the A side, Device Z on the Z side. CID-2: Device B on the Z side
        # only. CID-3 has no terminations
        Cable(a_terminations=[interfaces['Device A']], b_terminations=[front_port]).save()
        Cable(a_terminations=[rear_port], b_terminations=[terminations['CID-1', 'A']]).save()
        Cable(a_terminations=[terminations['CID-1', 'Z']], b_terminations=[interfaces['Device Z']]).save()
        Cable(a_terminations=[terminations['CID-2', 'Z']], b_terminations=[interfaces['Device B']]).save()

        CircuitImpact.objects.bulk_create(
            CircuitImpact(maintenance=maintenances[name], circuit=cls.circuits[cid])
            for name, cid in (('UT-TEST-2', 'CID-1'), ('UT-TEST-3', 'CID-2'), ('UT-TEST-1', 'CID-3'))
        )

    def get_dashboard(self, today=None):
        with patch('django.utils.timezone.localdate', return_value=today or self.today):
            response = self.client.get(self._get_url('overview'))
        self.assertHttpStatus(response, 200)
        return response

    def get_metrics(self, today=None):
        return {str(label): value for label, value in self.get_dashboard(today).context['metrics']}

    def plugin_settings(self, **overrides):
        # override_settings replaces the whole setting, so keep the defaults NetBox merged in at startup
        plugin_config = {**settings.PLUGINS_CONFIG['netbox_circuit_maintenance'], **overrides}
        return self.settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, 'netbox_circuit_maintenance': plugin_config})

    def test_get_maintenances_next_n_days(self):
        view = CircuitMaintenanceOverviewView()

        maintenances = view.get_maintenances_next_n_days(self.today, 7)
        self.assertEqual([maintenance.name for maintenance in maintenances], ['UT-TEST-2', 'UT-TEST-3'])

    def test_metrics(self):
        # Nautobot's past-days, historical-matrix, future-maintenances and per-month tests
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance', 'circuits.view_circuit')

        self.assertEqual(
            self.get_metrics(),
            {
                'Upcoming Maintenances': 2,
                'Historical - 7 Days': 1,
                'Historical - 30 Days': 2,
                'Historical - 365 Days': 3,
                'Average Duration of Maintenances': '120.0 minutes',
                'Future Maintenances': 2,
                'Average Number of Maintenances Per Month': 0.8,  # 5 from March to August
                'Future Maintenance to Circuit Ratio': 0.67,  # 2 upcoming, 3 circuits
            },
        )

    def test_metrics_zero_maintenances(self):
        CircuitMaintenance.objects.all().delete()
        Circuit.objects.all().delete()
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance', 'circuits.view_circuit')

        self.assertEqual(
            self.get_metrics(),
            {
                'Upcoming Maintenances': 0,
                'Historical - 7 Days': 0,
                'Historical - 30 Days': 0,
                'Historical - 365 Days': 0,
                'Average Duration of Maintenances': 'No maintenances found.',
                'Future Maintenances': 0,
                'Average Number of Maintenances Per Month': 0,
                'Future Maintenance to Circuit Ratio': 0,
            },
        )

    def test_metrics_average_duration(self):
        # The exact average; Nautobot rounded each duration to whole minutes first, giving 60.0
        CircuitMaintenance.objects.all().delete()
        for name, duration in (('UT-TEST-1', timedelta(minutes=90, seconds=30)), ('UT-TEST-2', timedelta(minutes=30))):
            CircuitMaintenance.objects.create(name=name, start_time=START, end_time=START + duration)
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        self.assertEqual(self.get_metrics()['Average Duration of Maintenances'], '60.25 minutes')

    def test_metrics_per_month_one_maintenance(self):
        # Nautobot returned 0 for fewer than two maintenances
        CircuitMaintenance.objects.exclude(name='UT-TEST-1').delete()
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        self.assertEqual(self.get_metrics()['Average Number of Maintenances Per Month'], 1.0)

    def test_metrics_restricted(self):
        # Counts cover only the maintenances and circuits the user can view
        obj_perm = ObjectPermission(
            name='Test permission', actions=['view'], constraints={'name__in': ['UT-TEST-2', 'UT-TEST-5']}
        )
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(CircuitMaintenance))
        obj_perm = ObjectPermission(name='Test permission 2', actions=['view'], constraints={'cid': 'CID-1'})
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(Circuit))

        metrics = self.get_metrics()
        self.assertEqual(metrics['Upcoming Maintenances'], 1)
        self.assertEqual(metrics['Historical - 365 Days'], 1)
        self.assertEqual(metrics['Future Maintenances'], 1)
        self.assertEqual(metrics['Average Number of Maintenances Per Month'], 0.3)  # 2 from March to August
        self.assertEqual(metrics['Future Maintenance to Circuit Ratio'], 1.0)  # 1 upcoming, 1 circuit

    def test_date_per_request(self):
        # Nautobot evaluated today once, at import
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        self.assertEqual(self.get_metrics()['Future Maintenances'], 2)
        self.assertEqual(self.get_metrics(date(2022, 8, 28))['Future Maintenances'], 0)

    def test_dashboard_n_days(self):
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        with self.plugin_settings(dashboard_n_days=2):
            response = self.get_dashboard()
            metrics = self.get_metrics()
        self.assertContains(response, 'Circuits With Maintenances in the Next 2 Days')
        self.assertContains(response, 'Devices With Maintenances in the Next 2 Days')
        self.assertEqual(metrics['Upcoming Maintenances'], 1)

    def test_affected_circuits_and_devices(self):
        self.add_permissions(
            'netbox_circuit_maintenance.view_circuitmaintenance',
            'netbox_circuit_maintenance.view_circuitimpact',
            'circuits.view_circuit',
            'dcim.view_device',
        )

        response = self.get_dashboard()
        # A side first; CID-2 has a device on the Z side only, which Nautobot's Location column left out
        self.assertEqual(
            [
                (row['maintenance'].name, row['circuit'].cid, [site.name for site in row['sites']])
                for row in response.context['circuit_rows']
            ],
            [('UT-TEST-2', 'CID-1', ['Site A', 'Site Z']), ('UT-TEST-3', 'CID-2', ['Site B'])],
        )
        # Device A is behind the patch panel, which isn't listed
        self.assertEqual(
            [(row['maintenance'].name, row['device'].name) for row in response.context['device_rows']],
            [('UT-TEST-2', 'Device A'), ('UT-TEST-2', 'Device Z'), ('UT-TEST-3', 'Device B')],
        )
        for obj in (self.circuits['CID-1'], self.sites['Site A'], self.devices['Device A']):
            self.assertContains(response, obj.get_absolute_url())
        self.assertNotContains(response, self.circuits['CID-3'].get_absolute_url())
        self.assertNotContains(response, self.devices['Patch Panel'].get_absolute_url())

    def test_window_boundaries(self):
        # Both ends of each window are included: a maintenance starting at local midnight today is upcoming and in the
        # past 7 days, and one starting at local midnight 30 days later is upcoming too
        CircuitMaintenance.objects.all().delete()
        midnight = timezone.make_aware(datetime.combine(self.today, time()))
        for name, start in (('UT-TEST-1', midnight), ('UT-TEST-2', midnight + timedelta(days=30))):
            CircuitMaintenance.objects.create(name=name, start_time=start, end_time=start + timedelta(hours=2))
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        metrics = self.get_metrics()
        self.assertEqual(metrics['Upcoming Maintenances'], 2)
        self.assertEqual(metrics['Historical - 7 Days'], 1)
        self.assertEqual(metrics['Future Maintenances'], 2)

    def test_dashboard_n_days_negative(self):
        # Nothing upcoming, as in Nautobot
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')

        with self.plugin_settings(dashboard_n_days=-5):
            metrics = self.get_metrics()
        self.assertEqual(metrics['Upcoming Maintenances'], 0)
        self.assertEqual(metrics['Historical - 7 Days'], 1)

    def test_affected_devices_circuits_in_series(self):
        # Device S1 -- CID-S1 -- CID-S2 -- Device S2: each termination's device is the one reached over its own cable,
        # beyond the other circuit where there is one
        provider = Provider.objects.get(slug='provider-1')
        circuit_type = CircuitType.objects.get(slug='circuit-type-1')
        maintenance = CircuitMaintenance.objects.get(name='UT-TEST-2')
        device_type = DeviceType.objects.get(slug='device-type-1')
        role = DeviceRole.objects.get(slug='device-role-1')
        interfaces = []
        for name, site in (('Device S1', 'Site A'), ('Device S2', 'Site Z')):
            device = Device.objects.create(name=name, site=self.sites[site], device_type=device_type, role=role)
            interfaces.append(Interface.objects.create(device=device, name='eth0', type='1000base-t'))
        terminations = []
        for cid in ('CID-S1', 'CID-S2'):
            circuit = Circuit.objects.create(cid=cid, provider=provider, type=circuit_type)
            CircuitImpact.objects.create(maintenance=maintenance, circuit=circuit)
            for side in ('A', 'Z'):
                terminations.append(
                    CircuitTermination.objects.create(circuit=circuit, term_side=side, termination=self.sites['Site A'])
                )
        Cable(a_terminations=[interfaces[0]], b_terminations=[terminations[0]]).save()
        Cable(a_terminations=[terminations[1]], b_terminations=[terminations[2]]).save()
        Cable(a_terminations=[terminations[3]], b_terminations=[interfaces[1]]).save()
        self.add_permissions(
            'netbox_circuit_maintenance.view_circuitmaintenance',
            'netbox_circuit_maintenance.view_circuitimpact',
            'circuits.view_circuit',
            'dcim.view_device',
        )

        response = self.get_dashboard()
        self.assertEqual(
            [
                (row['circuit'].cid, [site.name for site in row['sites']])
                for row in response.context['circuit_rows']
                if row['circuit'].cid.startswith('CID-S')
            ],
            [('CID-S1', ['Site A', 'Site Z']), ('CID-S2', ['Site A', 'Site Z'])],
        )
        self.assertEqual(
            [
                row['device'].name
                for row in response.context['device_rows']
                if row['device'].name.startswith('Device S')
            ],
            ['Device S1', 'Device S2', 'Device S1', 'Device S2'],
        )

    def test_affected_circuits_and_devices_restricted(self):
        # Circuits need the impacts' and the circuits' view permissions, devices their own
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance', 'circuits.view_circuit')
        response = self.get_dashboard()
        self.assertEqual(response.context['circuit_rows'], [])
        self.assertEqual(response.context['device_rows'], [])

        self.add_permissions('netbox_circuit_maintenance.view_circuitimpact')
        self.remove_permissions('circuits.view_circuit')
        response = self.get_dashboard()
        self.assertEqual(response.context['circuit_rows'], [])

        self.add_permissions('circuits.view_circuit')
        response = self.get_dashboard()
        self.assertEqual([row['circuit'].cid for row in response.context['circuit_rows']], ['CID-1', 'CID-2'])
        self.assertEqual([row['sites'] for row in response.context['circuit_rows']], [[], []])
        self.assertEqual(response.context['device_rows'], [])

        obj_perm = ObjectPermission(name='Test permission', actions=['view'], constraints={'name': 'Device Z'})
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(Device))
        response = self.get_dashboard()
        self.assertEqual(
            [[site.name for site in row['sites']] for row in response.context['circuit_rows']], [['Site Z'], []]
        )
        self.assertEqual([row['device'].name for row in response.context['device_rows']], ['Device Z'])

    def test_permission_required(self):
        response = self.client.get(self._get_url('overview'))
        self.assertHttpStatus(response, 403)

    def test_menu_item(self):
        overview_url = self._get_url('overview')
        response = self.client.get(reverse('home'))
        self.assertNotContains(response, f'href="{overview_url}"')

        # First in the menu group, before the maintenance list
        self.add_permissions('netbox_circuit_maintenance.view_circuitmaintenance')
        content = self.client.get(reverse('home')).content.decode()
        self.assertIn(f'href="{overview_url}"', content)
        self.assertLess(content.index(f'href="{overview_url}"'), content.index(f'href="{self._get_url("list")}"'))
