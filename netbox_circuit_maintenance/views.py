from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.ui import layout
from netbox.ui.panels import CommentsPanel
from netbox.views import generic
from utilities.views import register_model_view

from . import filtersets, forms, tables
from .models import CircuitMaintenance
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
