import datetime
import itertools
import json
import secrets
from collections import defaultdict

import requests
from circuits.models import Circuit
from dcim.models import Cable, CablePath, Device, Interface
from dcim.utils import compile_path_node, decompile_path_node, object_to_path_node
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import Avg, Count, F, Max, Min, Q
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import View
from extras.ui.panels import CustomFieldsPanel, TagsPanel
from google_auth_oauthlib.flow import Flow
from netbox.constants import RQ_QUEUE_DEFAULT
from netbox.object_actions import AddObject, BulkDelete, BulkEdit, BulkExport, BulkImport, DeleteObject, EditObject
from netbox.plugins import get_plugin_config
from netbox.ui import actions, layout
from netbox.ui.panels import CommentsPanel, JSONPanel, ObjectsTablePanel
from netbox.views import generic
from netbox.views.generic.base import BaseObjectView
from oauthlib.oauth2 import OAuth2Error
from utilities.permissions import get_permission_for_model
from utilities.rqworker import any_workers_for_queue
from utilities.views import GetReturnURLMixin, ObjectPermissionRequiredMixin, get_action_url, register_model_view

from . import filtersets, forms, tables
from .handle_notifications.handler import HandleCircuitMaintenanceNotifications
from .handle_notifications.sources import GmailAPIOauth, RedirectAuthorize, Source
from .models import CircuitImpact, CircuitMaintenance, Note, NotificationSource, ParsedNotification, RawNotification
from .object_actions import FindOverlaps, RunNow, ValidateSource
from .site_search import FindSitesWithMaintenanceOverlap
from .ui import panels
from .utils import get_midnight

#
# CircuitMaintenance
#


@register_model_view(CircuitMaintenance, 'list', path='', detail=False)
class CircuitMaintenanceListView(generic.ObjectListView):
    # Most recent first, unlike the model's default ordering; pk keeps pagination stable when start times tie
    queryset = CircuitMaintenance.objects.order_by('-start_time', '-pk')
    filterset = filtersets.CircuitMaintenanceFilterSet
    filterset_form = forms.CircuitMaintenanceFilterForm
    table = tables.CircuitMaintenanceTable
    actions = (AddObject, BulkImport, BulkExport, FindOverlaps, BulkEdit, BulkDelete)


@register_model_view(CircuitMaintenance)
class CircuitMaintenanceView(generic.ObjectView):
    queryset = CircuitMaintenance.objects.all()
    template_name = 'generic/object.html'
    layout = layout.SimpleLayout(
        left_panels=[
            panels.CircuitMaintenancePanel(),
            TagsPanel(),
            CommentsPanel(),
        ],
        right_panels=[
            CustomFieldsPanel(),
        ],
        bottom_panels=[
            ObjectsTablePanel(
                model='netbox_circuit_maintenance.CircuitImpact',
                title=_('Circuits'),
                filters={'maintenance_id': lambda ctx: ctx['object'].pk},
                include_columns=[
                    'circuit',
                    'impact',
                    'provider',
                    'circuit_type',
                    'circuit_status',
                    'termination_a',
                    'termination_z',
                    'circuit_description',
                ],
                exclude_columns=['maintenance'],
                actions=[
                    actions.AddObject(
                        'netbox_circuit_maintenance.CircuitImpact',
                        url_params={'maintenance': lambda ctx: ctx['object'].pk},
                    ),
                ],
            ),
            ObjectsTablePanel(
                model='netbox_circuit_maintenance.Note',
                title=_('Notes'),
                filters={'maintenance_id': lambda ctx: ctx['object'].pk},
                include_columns=['level', 'title', 'last_updated', 'comment'],
                exclude_columns=['maintenance'],
                actions=[
                    actions.AddObject(
                        'netbox_circuit_maintenance.Note',
                        url_params={'maintenance': lambda ctx: ctx['object'].pk},
                    ),
                ],
            ),
            # Newest notification first, from the list view's queryset. Only the notification handler adds them
            ObjectsTablePanel(
                model='netbox_circuit_maintenance.ParsedNotification',
                title=_('Notifications'),
                filters={'maintenance_id': lambda ctx: ctx['object'].pk},
                include_columns=['raw_notification', 'stamp', 'json'],
                exclude_columns=['maintenance', 'provider'],
            ),
        ],
    )


@register_model_view(CircuitMaintenance, 'find_overlaps', path='find-overlaps', detail=False)
class CircuitMaintenanceFindOverlapsView(GetReturnURLMixin, BaseObjectView):
    """
    Queue a run of the overlap job, which reports the sites where the maintenances the user can view overlap. Nautobot's
    job was run from Nautobot's job list.
    """

    queryset = CircuitMaintenance.objects.all()

    def get_required_permission(self):
        return get_permission_for_model(self.queryset.model, 'view')

    def get(self, request):
        # Queueing creates a job, so it's a POST
        return redirect(self.get_return_url(request))

    def post(self, request):
        # Exempt view permissions let anonymous users this far, but the run needs a user: without one it would cover
        # every maintenance, circuit and site
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        # As NetBox's script form checks: without a worker, the job would wait unnoticed until one starts
        if not any_workers_for_queue(RQ_QUEUE_DEFAULT):
            messages.error(request, _('Unable to run: RQ worker process not running.'))
            return redirect(self.get_return_url(request))

        # Each run has a name of its own, as Run now's. When the RQ worker starts, it schedules overlap_job_interval's
        # runs with enqueue_once(), which would delete a queued job under the job's name
        job = FindSitesWithMaintenanceOverlap.enqueue(
            name=f'{FindSitesWithMaintenanceOverlap.name} - {secrets.token_hex(4)}',
            user=request.user,
        )
        messages.success(
            request, _('Queued job #{id} to find sites with circuit maintenance overlap.').format(id=job.pk)
        )
        # The job's page, whose Log tab lists the overlaps
        if request.user.has_perm('core.view_job', job):
            return redirect(job.get_absolute_url())
        # The job's log is all it produces
        messages.warning(
            request,
            _("The overlaps will be in the log of job #{id}, which you don't have permission to view.").format(
                id=job.pk
            ),
        )
        return redirect(self.get_return_url(request))


@register_model_view(CircuitMaintenance, 'add', detail=False)
@register_model_view(CircuitMaintenance, 'edit')
class CircuitMaintenanceEditView(generic.ObjectEditView):
    queryset = CircuitMaintenance.objects.all()
    form = forms.CircuitMaintenanceForm


@register_model_view(CircuitMaintenance, 'delete')
class CircuitMaintenanceDeleteView(generic.ObjectDeleteView):
    queryset = CircuitMaintenance.objects.all()


@register_model_view(CircuitMaintenance, 'bulk_import', path='import', detail=False)
class CircuitMaintenanceBulkImportView(generic.BulkImportView):
    queryset = CircuitMaintenance.objects.all()
    model_form = forms.CircuitMaintenanceImportForm


@register_model_view(CircuitMaintenance, 'bulk_edit', path='edit', detail=False)
class CircuitMaintenanceBulkEditView(generic.BulkEditView):
    queryset = CircuitMaintenance.objects.all()
    filterset = filtersets.CircuitMaintenanceFilterSet
    table = tables.CircuitMaintenanceTable
    form = forms.CircuitMaintenanceBulkEditForm


@register_model_view(CircuitMaintenance, 'bulk_delete', path='delete', detail=False)
class CircuitMaintenanceBulkDeleteView(generic.BulkDeleteView):
    queryset = CircuitMaintenance.objects.all()
    filterset = filtersets.CircuitMaintenanceFilterSet
    table = tables.CircuitMaintenanceTable


#
# CircuitImpact
#


@register_model_view(CircuitImpact, 'list', path='', detail=False)
class CircuitImpactListView(generic.ObjectListView):
    queryset = CircuitImpact.objects.all()
    filterset = filtersets.CircuitImpactFilterSet
    filterset_form = forms.CircuitImpactFilterForm
    table = tables.CircuitImpactTable
    actions = (AddObject, BulkImport, BulkExport, BulkEdit, BulkDelete)


@register_model_view(CircuitImpact)
class CircuitImpactView(generic.ObjectView):
    queryset = CircuitImpact.objects.all()
    template_name = 'generic/object.html'
    layout = layout.SimpleLayout(
        left_panels=[
            panels.CircuitImpactPanel(),
            TagsPanel(),
        ],
        right_panels=[
            CustomFieldsPanel(),
        ],
    )


@register_model_view(CircuitImpact, 'add', detail=False)
@register_model_view(CircuitImpact, 'edit')
class CircuitImpactEditView(generic.ObjectEditView):
    queryset = CircuitImpact.objects.all()
    form = forms.CircuitImpactForm


@register_model_view(CircuitImpact, 'delete')
class CircuitImpactDeleteView(generic.ObjectDeleteView):
    queryset = CircuitImpact.objects.all()


@register_model_view(CircuitImpact, 'bulk_import', path='import', detail=False)
class CircuitImpactBulkImportView(generic.BulkImportView):
    queryset = CircuitImpact.objects.all()
    model_form = forms.CircuitImpactImportForm


@register_model_view(CircuitImpact, 'bulk_edit', path='edit', detail=False)
class CircuitImpactBulkEditView(generic.BulkEditView):
    queryset = CircuitImpact.objects.all()
    filterset = filtersets.CircuitImpactFilterSet
    table = tables.CircuitImpactTable
    form = forms.CircuitImpactBulkEditForm


@register_model_view(CircuitImpact, 'bulk_delete', path='delete', detail=False)
class CircuitImpactBulkDeleteView(generic.BulkDeleteView):
    queryset = CircuitImpact.objects.all()
    filterset = filtersets.CircuitImpactFilterSet
    table = tables.CircuitImpactTable


#
# Note
#


@register_model_view(Note, 'list', path='', detail=False)
class NoteListView(generic.ObjectListView):
    queryset = Note.objects.all()
    filterset = filtersets.NoteFilterSet
    filterset_form = forms.NoteFilterForm
    table = tables.NoteTable
    actions = (AddObject, BulkImport, BulkExport, BulkEdit, BulkDelete)


@register_model_view(Note)
class NoteView(generic.ObjectView):
    queryset = Note.objects.all()
    template_name = 'generic/object.html'
    layout = layout.SimpleLayout(
        left_panels=[
            panels.NotePanel(),
            CustomFieldsPanel(),
            TagsPanel(),
        ],
        right_panels=[
            CommentsPanel(field_name='comment', title=_('Comment')),
        ],
    )


@register_model_view(Note, 'add', detail=False)
@register_model_view(Note, 'edit')
class NoteEditView(generic.ObjectEditView):
    queryset = Note.objects.all()
    form = forms.NoteForm


@register_model_view(Note, 'delete')
class NoteDeleteView(generic.ObjectDeleteView):
    queryset = Note.objects.all()


@register_model_view(Note, 'bulk_import', path='import', detail=False)
class NoteBulkImportView(generic.BulkImportView):
    queryset = Note.objects.all()
    model_form = forms.NoteImportForm


@register_model_view(Note, 'bulk_edit', path='edit', detail=False)
class NoteBulkEditView(generic.BulkEditView):
    queryset = Note.objects.all()
    filterset = filtersets.NoteFilterSet
    table = tables.NoteTable
    form = forms.NoteBulkEditForm


@register_model_view(Note, 'bulk_delete', path='delete', detail=False)
class NoteBulkDeleteView(generic.BulkDeleteView):
    queryset = Note.objects.all()
    filterset = filtersets.NoteFilterSet
    table = tables.NoteTable


#
# NotificationSource
#

# Sources mirror the plugin configuration, so they can't be added or imported (Decisions 2026-09-24)


@register_model_view(NotificationSource, 'list', path='', detail=False)
class NotificationSourceListView(generic.ObjectListView):
    queryset = NotificationSource.objects.all()
    filterset = filtersets.NotificationSourceFilterSet
    filterset_form = forms.NotificationSourceFilterForm
    table = tables.NotificationSourceTable
    actions = (RunNow, BulkExport)


@register_model_view(NotificationSource)
class NotificationSourceView(generic.ObjectView):
    queryset = NotificationSource.objects.all()
    template_name = 'generic/object.html'
    actions = (ValidateSource, EditObject, DeleteObject)
    layout = layout.SimpleLayout(
        left_panels=[
            panels.NotificationSourcePanel(),
            panels.NotificationSourceConfigPanel(),
            TagsPanel(),
        ],
        right_panels=[
            CustomFieldsPanel(),
        ],
    )


@register_model_view(NotificationSource, 'edit')
class NotificationSourceEditView(generic.ObjectEditView):
    queryset = NotificationSource.objects.all()
    form = forms.NotificationSourceForm


@register_model_view(NotificationSource, 'delete')
class NotificationSourceDeleteView(generic.ObjectDeleteView):
    queryset = NotificationSource.objects.all()


# The session key of the Google OAuth authorization in progress: the source, and the state and PKCE code verifier that
# the callback sends on to Google
GOOGLE_OAUTH_SESSION_KEY = 'netbox_circuit_maintenance_google_oauth'


def validate_source(request, notification_source):
    """
    Test a source's authentication and report the result as a message. Raises RedirectAuthorize when a Google OAuth
    source has no usable token.
    """
    try:
        source = Source.init(name=notification_source.name)
    except ValueError as exc:
        messages.error(request, _('FAILED: {message}').format(message=exc))
        return
    is_authenticated, message = source.test_authentication()
    if is_authenticated:
        messages.success(request, _('SUCCESS: {message}').format(message=message))
    else:
        messages.error(request, _('FAILED: {message}').format(message=message))


def get_google_oauth_flow(request, notification_source, **kwargs):
    """
    The Google OAuth web flow that authorizes a GmailAPIOauth source, with the source's client secrets file and scopes.
    Raises ValueError for any other source. Google sends the user back to the callback's URL, which must be one of the
    OAuth client's authorized redirect URIs.
    """
    source = Source.init(name=notification_source.name)
    if not isinstance(source, GmailAPIOauth):
        raise ValueError(f'{notification_source} is not a Google OAuth source.')  # noqa: TRY004 (a configuration error, as in Source.init())
    flow = Flow.from_client_secrets_file(
        source.credentials_file,
        scopes=source.SCOPES + source.extra_scopes,
        redirect_uri=request.build_absolute_uri(get_action_url(NotificationSource, action='google_oauth2callback')),
        **kwargs,
    )
    # The flow requires it only once it fetches the token, after the user's consent
    if 'client_secret' not in flow.client_config:
        raise ValueError(f'Credentials_file {source.credentials_file} for {notification_source} has no client_secret.')
    return flow


@register_model_view(NotificationSource, 'validate')
class NotificationSourceValidateView(GetReturnURLMixin, BaseObjectView):
    """
    Test a source's authentication and report the result as a message. A Google OAuth source without a usable token
    goes on to its authorization instead, if the user may change the source.
    """

    queryset = NotificationSource.objects.all()

    def get_required_permission(self):
        return get_permission_for_model(self.queryset.model, 'view')

    def get(self, request, pk):
        # Validating can store or discard the source's OAuth token, so it's a POST, like a data source's sync
        return redirect(self.get_object(pk=pk).get_absolute_url())

    def post(self, request, pk):
        notification_source = self.get_object(pk=pk)
        try:
            validate_source(request, notification_source)
        except RedirectAuthorize as exc:
            # Authorizing stores a token on the source, so it needs permission to change the source
            if request.user.has_perm(get_permission_for_model(NotificationSource, 'change'), notification_source):
                return redirect(
                    get_action_url(notification_source, action=exc.url_name, kwargs={'pk': notification_source.pk})
                )
            messages.error(
                request,
                _('FAILED: {source} needs Google OAuth authorization by a user who can change it.').format(
                    source=notification_source
                ),
            )
        return redirect(self.get_return_url(request, notification_source))


@register_model_view(NotificationSource, 'google_authorize', path='google-authorize')
class NotificationSourceGoogleAuthorizeView(BaseObjectView):
    """
    Start a Google OAuth source's authorization: Google asks the user to let the plugin read the mailbox, then sends
    them to NotificationSourceGoogleCallbackView.
    """

    queryset = NotificationSource.objects.all()

    def get_required_permission(self):
        return get_permission_for_model(self.queryset.model, 'change')

    def get(self, request, pk):
        notification_source = self.get_object(pk=pk)
        try:
            flow = get_google_oauth_flow(request, notification_source)
        except ValueError as exc:
            messages.error(request, _('FAILED: {message}').format(message=exc))
            return redirect(notification_source.get_absolute_url())
        # Offline access, with consent asked every time, so that Google returns a refresh token even to an OAuth client
        # the user authorized before
        authorization_url, state = flow.authorization_url(access_type='offline', prompt='consent')
        request.session[GOOGLE_OAUTH_SESSION_KEY] = {
            'source': notification_source.pk,
            'state': state,
            'code_verifier': flow.code_verifier,
        }
        return redirect(authorization_url)


@register_model_view(NotificationSource, 'google_oauth2callback', path='google-oauth2callback', detail=False)
class NotificationSourceGoogleCallbackView(BaseObjectView):
    """
    Complete a Google OAuth source's authorization: exchange the code Google sends back for a token, store the token on
    the source and validate the source. The URL is the redirect URI registered with Google, so it doesn't name the
    source; the session does.
    """

    queryset = NotificationSource.objects.all()

    def get_required_permission(self):
        return get_permission_for_model(self.queryset.model, 'change')

    def get(self, request):
        # Each authorization completes once
        authorization = request.session.pop(GOOGLE_OAUTH_SESSION_KEY, None)
        if authorization is None:
            messages.error(request, _('No Google OAuth authorization is in progress.'))
            return redirect(get_action_url(NotificationSource, action='list'))
        notification_source = self.get_object(pk=authorization['source'])

        try:
            flow = get_google_oauth_flow(
                request, notification_source, state=authorization['state'], code_verifier=authorization['code_verifier']
            )
            flow.fetch_token(authorization_response=request.build_absolute_uri())
        except OAuth2Error as exc:
            # Reads like google-auth's RefreshError: "access_denied", "invalid_grant: Bad Request"
            message = f'{exc.error}: {exc.description}' if exc.description else exc.error
            messages.error(request, _('FAILED: {message}').format(message=message))
            return redirect(notification_source.get_absolute_url())
        # oauthlib raises a Warning when Google grants other scopes than the ones requested
        except (ValueError, Warning, requests.RequestException) as exc:
            messages.error(request, _('FAILED: {message}').format(message=exc))
            return redirect(notification_source.get_absolute_url())

        # The source can't load a token without a refresh token, so it keeps the token it has
        if not flow.credentials.refresh_token:
            messages.error(request, _('FAILED: Google returned no refresh token.'))
            return redirect(notification_source.get_absolute_url())
        GmailAPIOauth.store_token(notification_source, json.loads(flow.credentials.to_json()))
        # Validate the new token, as Nautobot's callback did by redirecting to Validate
        validate_source(request, notification_source)
        return redirect(notification_source.get_absolute_url())


@register_model_view(NotificationSource, 'fetch', detail=False)
class NotificationSourceFetchView(GetReturnURLMixin, BaseObjectView):
    """
    Run now: queue a run of the notification handler, which fetches from every source and updates circuit maintenances.
    The run records its changes as the requesting user. Nautobot's run_job redirected to the job's run form.
    """

    queryset = NotificationSource.objects.all()
    template_name = 'netbox_circuit_maintenance/notificationsource_fetch.html'

    def get_required_permission(self):
        return get_permission_for_model(self.queryset.model, 'fetch')

    def get(self, request):
        return self.render_form(request, forms.NotificationSourceFetchForm())

    def post(self, request):
        form = forms.NotificationSourceFetchForm(request.POST)
        # As NetBox's script form checks: without a worker, the job would wait unnoticed until one starts
        if not any_workers_for_queue(RQ_QUEUE_DEFAULT):
            messages.error(request, _('Unable to run: RQ worker process not running.'))
        elif form.is_valid():
            # Each run has a name of its own. When the RQ worker starts, it schedules job_interval's runs with
            # enqueue_once(), which would delete a queued job under the handler's name
            job = HandleCircuitMaintenanceNotifications.enqueue(
                name=f'{HandleCircuitMaintenanceNotifications.name} - {secrets.token_hex(4)}',
                user=request.user,
                dry_run=form.cleaned_data['dry_run'],
            )
            messages.success(request, _('Queued job #{id} to update circuit maintenances.').format(id=job.pk))
            # The job's page, which has its data and a Log tab
            if request.user.has_perm('core.view_job', job):
                return redirect(job.get_absolute_url())
            return redirect(self.get_return_url(request))
        return self.render_form(request, form)

    def render_form(self, request, form):
        return render(
            request,
            self.template_name,
            {
                'form': form,
                'job_name': HandleCircuitMaintenanceNotifications.name,
                'return_url': self.get_return_url(request),
            },
        )


#
# RawNotification
#


@register_model_view(RawNotification, 'list', path='', detail=False)
class RawNotificationListView(generic.ObjectListView):
    # The table doesn't show the body (up to 16 KB per row)
    queryset = RawNotification.objects.defer('raw')
    filterset = filtersets.RawNotificationFilterSet
    filterset_form = forms.RawNotificationFilterForm
    table = tables.RawNotificationTable
    actions = (BulkExport, BulkDelete)


@register_model_view(RawNotification)
class RawNotificationView(generic.ObjectView):
    queryset = RawNotification.objects.all()
    template_name = 'generic/object.html'
    actions = (DeleteObject,)
    layout = layout.SimpleLayout(
        left_panels=[
            panels.RawNotificationPanel(),
            TagsPanel(),
        ],
        right_panels=[
            CustomFieldsPanel(),
        ],
        bottom_panels=[
            panels.RawContentPanel('raw', title=_('Content')),
        ],
    )


@register_model_view(RawNotification, 'delete')
class RawNotificationDeleteView(generic.ObjectDeleteView):
    queryset = RawNotification.objects.all()


@register_model_view(RawNotification, 'bulk_delete', path='delete', detail=False)
class RawNotificationBulkDeleteView(generic.BulkDeleteView):
    # Not deferring raw: each object's snapshot() and delete event read it anyway, one query per object if deferred
    queryset = RawNotification.objects.all()
    filterset = filtersets.RawNotificationFilterSet
    table = tables.RawNotificationTable


#
# ParsedNotification
#


# Not in the menu: reached through the detail page's breadcrumb, and loaded by the maintenance's Notifications panel
@register_model_view(ParsedNotification, 'list', path='', detail=False)
class ParsedNotificationListView(generic.ObjectListView):
    # Newest notification first, as on Nautobot's maintenance page; pk keeps pagination stable when stamps tie. The
    # table doesn't show the raw notification's body (up to 16 KB per row)
    queryset = (
        ParsedNotification.objects.select_related('raw_notification')
        .defer('raw_notification__raw')
        .order_by('-raw_notification__stamp', '-pk')
    )
    filterset = filtersets.ParsedNotificationFilterSet
    table = tables.ParsedNotificationTable
    actions = (BulkExport,)


# Template parsednotification.html adds the raw notification to the breadcrumbs
@register_model_view(ParsedNotification)
class ParsedNotificationView(generic.ObjectView):
    queryset = ParsedNotification.objects.all()
    actions = ()
    layout = layout.SimpleLayout(
        left_panels=[
            panels.ParsedNotificationPanel(),
            CustomFieldsPanel(),
            TagsPanel(),
        ],
        right_panels=[
            JSONPanel('json', title=_('JSON')),
        ],
    )


#
# Dashboard
#


def get_date_range(day, days):
    """
    Return the datetimes of local midnight on the given date and local midnight the given number of days later. The
    dashboard includes both ends, as Nautobot's did.
    """
    midnight = get_midnight(day)
    return midnight, midnight + datetime.timedelta(days=days)


def get_connected_devices(terminations, user):
    """
    Map the pk of each circuit termination to the devices the user can view that are connected to it: the devices of
    the interfaces whose cable path reaches the termination over the termination's own cable, through any patch panels.
    Stands in for Nautobot's connected_endpoint.parent, since a NetBox CircuitTermination isn't a PathEndpoint.
    """
    cable_type = ContentType.objects.get_for_model(Cable)
    interface_type = ContentType.objects.get_for_model(Interface)
    # Path node of each cabled termination: (termination pk, path node of its cable)
    termination_nodes = {
        object_to_path_node(termination): (termination.pk, compile_path_node(cable_type.pk, termination.cable_id))
        for termination in terminations
        if termination.cable_id
    }
    if not termination_nodes:
        return {}

    interface_ids = defaultdict(set)
    for cable_path in CablePath.objects.filter(_nodes__overlap=list(termination_nodes)).only('path'):
        origins = cable_path.path[0]
        if decompile_path_node(origins[0])[0] != interface_type.pk:
            continue
        for previous_step, step in itertools.pairwise(cable_path.path):
            for node in termination_nodes.keys() & set(step):
                termination_pk, cable_node = termination_nodes[node]
                # A path that crosses the circuit reaches its far termination from the near one, not over a cable
                if cable_node in previous_step:
                    interface_ids[termination_pk].update(decompile_path_node(origin)[1] for origin in origins)
    if not interface_ids:
        return {}

    device_ids = dict(
        Interface.objects.filter(pk__in=set().union(*interface_ids.values())).values_list('pk', 'device_id')
    )
    # An unnamed device's str() reads its device type and virtual chassis
    devices = list(
        Device.objects.restrict(user, 'view')
        .filter(pk__in=set(device_ids.values()))
        .select_related('site', 'device_type__manufacturer', 'virtual_chassis')
    )
    connected_devices = {}
    for termination_pk, pks in interface_ids.items():
        termination_device_ids = {device_ids.get(pk) for pk in pks}
        connected_devices[termination_pk] = [device for device in devices if device.pk in termination_device_ids]
    return connected_devices


@register_model_view(CircuitMaintenance, 'overview', detail=False)
class CircuitMaintenanceOverviewView(ObjectPermissionRequiredMixin, View):
    """
    Dashboard: the circuits and devices affected by the maintenances starting in the next dashboard_n_days days, and
    maintenance metrics. Lists and counts cover only the objects the user can view.
    """

    queryset = CircuitMaintenance.objects.all()

    def get_required_permission(self):
        return get_permission_for_model(self.queryset.model, 'view')

    def get(self, request):
        n_days = get_plugin_config('netbox_circuit_maintenance', 'dashboard_n_days')
        # Per request: Nautobot's was a class attribute, evaluated once at import (Decisions 2026-09-29)
        today = timezone.localdate()
        circuit_rows, device_rows = self.get_affected_objects(self.get_maintenances_next_n_days(today, n_days))

        return render(
            request,
            'netbox_circuit_maintenance/circuitmaintenance_overview.html',
            {
                'n_days': n_days,
                'circuit_rows': circuit_rows,
                'device_rows': device_rows,
                'metrics': self.get_metrics(today, n_days),
            },
        )

    def get_maintenances_next_n_days(self, today, n_days):
        return self.queryset.filter(start_time__range=get_date_range(today, n_days))

    def get_affected_objects(self, maintenances):
        """
        Return the rows of the Circuits and Devices cards: each circuit the maintenances impact, with the sites of its
        connected devices, and each of those devices, A side first.
        """
        user = self.request.user
        impacts = list(
            CircuitImpact.objects.restrict(user, 'view')
            .filter(maintenance__in=maintenances, circuit__in=Circuit.objects.restrict(user, 'view'))
            .select_related('maintenance', 'circuit__termination_a', 'circuit__termination_z')
            .order_by('maintenance__start_time', 'maintenance__pk', 'circuit')
        )
        terminations = [
            termination
            for impact in impacts
            for termination in (impact.circuit.termination_a, impact.circuit.termination_z)
            if termination
        ]
        connected_devices = get_connected_devices(terminations, user)

        circuit_rows = []
        device_rows = []
        for impact in impacts:
            circuit = impact.circuit
            devices = [
                device
                for termination in (circuit.termination_a, circuit.termination_z)
                if termination
                for device in connected_devices.get(termination.pk, ())
            ]
            circuit_rows.append(
                {
                    'circuit': circuit,
                    'sites': list(dict.fromkeys(device.site for device in devices)),
                    'maintenance': impact.maintenance,
                }
            )
            device_rows.extend({'device': device, 'maintenance': impact.maintenance} for device in devices)
        return circuit_rows, device_rows

    def get_metrics(self, today, n_days):
        """
        Return the Metrics card's rows as (label, value) pairs.
        """
        stats = self.queryset.aggregate(
            upcoming=Count('pk', filter=Q(start_time__range=get_date_range(today, n_days))),
            past_7_days=Count('pk', filter=Q(start_time__range=get_date_range(today - datetime.timedelta(7), 7))),
            past_30_days=Count('pk', filter=Q(start_time__range=get_date_range(today - datetime.timedelta(30), 30))),
            past_365_days=Count('pk', filter=Q(start_time__range=get_date_range(today - datetime.timedelta(365), 365))),
            future=Count('pk', filter=Q(start_time__gte=get_midnight(today))),
            total=Count('pk'),
            first_start=Min('start_time'),
            last_start=Max('start_time'),
            average_duration=Avg(F('end_time') - F('start_time')),
        )
        if stats['total']:
            minutes = round(stats['average_duration'].total_seconds() / 60, 2)
            average_duration = _('{minutes} minutes').format(minutes=minutes)
            # Months from the first start to the last, counting both
            first_start = timezone.localtime(stats['first_start'])
            last_start = timezone.localtime(stats['last_start'])
            months = (last_start.year - first_start.year) * 12 + last_start.month - first_start.month + 1
            per_month = round(stats['total'] / months, 1)
        else:
            average_duration = _('No maintenances found.')
            per_month = 0
        circuit_count = Circuit.objects.restrict(self.request.user, 'view').count()
        circuit_ratio = round(stats['upcoming'] / circuit_count, 2) if circuit_count else 0

        return (
            (_('Upcoming Maintenances'), stats['upcoming']),
            (_('Historical - 7 Days'), stats['past_7_days']),
            (_('Historical - 30 Days'), stats['past_30_days']),
            (_('Historical - 365 Days'), stats['past_365_days']),
            (_('Average Duration of Maintenances'), average_duration),
            (_('Future Maintenances'), stats['future']),
            (_('Average Number of Maintenances Per Month'), per_month),
            (_('Future Maintenance to Circuit Ratio'), circuit_ratio),
        )
