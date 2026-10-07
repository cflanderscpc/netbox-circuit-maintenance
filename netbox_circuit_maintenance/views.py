from django.utils.translation import gettext_lazy as _
from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.object_actions import AddObject, BulkDelete, BulkEdit, BulkExport, BulkImport, DeleteObject
from netbox.ui import actions, layout
from netbox.ui.panels import CommentsPanel, JSONPanel, ObjectsTablePanel
from netbox.views import generic
from utilities.views import register_model_view

from . import filtersets, forms, tables
from .models import CircuitImpact, CircuitMaintenance, Note, ParsedNotification, RawNotification
from .ui import panels

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
