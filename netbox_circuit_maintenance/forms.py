from circuits.models import Circuit, Provider
from django import forms
from django.utils.translation import gettext_lazy as _
from netbox.forms import (
    NetBoxModelBulkEditForm,
    NetBoxModelFilterSetForm,
    NetBoxModelForm,
    NetBoxModelImportForm,
    PrimaryModelBulkEditForm,
    PrimaryModelFilterSetForm,
    PrimaryModelForm,
    PrimaryModelImportForm,
)
from utilities.forms import add_blank_choice
from utilities.forms.constants import BOOLEAN_WITH_BLANK_CHOICES
from utilities.forms.fields import (
    CommentField,
    CSVChoiceField,
    CSVModelChoiceField,
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    TagFilterField,
)
from utilities.forms.rendering import FieldSet
from utilities.forms.widgets import BulkEditNullBooleanSelect, DateTimePicker

from .choices import CircuitImpactChoices, CircuitMaintenanceStatusChoices, NoteLevelChoices
from .constants import MAX_NOTE_TITLE_LENGTH
from .models import CircuitImpact, CircuitMaintenance, Note

__all__ = (
    'CircuitImpactBulkEditForm',
    'CircuitImpactFilterForm',
    'CircuitImpactForm',
    'CircuitImpactImportForm',
    'CircuitMaintenanceBulkEditForm',
    'CircuitMaintenanceFilterForm',
    'CircuitMaintenanceForm',
    'CircuitMaintenanceImportForm',
    'NoteBulkEditForm',
    'NoteFilterForm',
    'NoteForm',
    'NoteImportForm',
)


#
# CircuitMaintenance
#


class CircuitMaintenanceForm(PrimaryModelForm):
    fieldsets = (
        FieldSet('name', 'status', 'start_time', 'end_time', 'ack', 'description', 'tags', name=_('Maintenance')),
    )

    class Meta:
        model = CircuitMaintenance
        fields = ('name', 'status', 'start_time', 'end_time', 'ack', 'description', 'owner', 'comments', 'tags')
        widgets = {
            'start_time': DateTimePicker(),
            'end_time': DateTimePicker(),
        }


class CircuitMaintenanceBulkEditForm(PrimaryModelBulkEditForm):
    status = forms.ChoiceField(
        label=_('Status'),
        choices=add_blank_choice(CircuitMaintenanceStatusChoices),
        required=False,
        initial='',
    )
    ack = forms.NullBooleanField(
        label=_('Acknowledged'),
        required=False,
        widget=BulkEditNullBooleanSelect(),
    )
    # Overrides PrimaryModelBulkEditForm's 100-character limit: the model field is a TextField
    description = forms.CharField(
        label=_('Description'),
        required=False,
        widget=forms.Textarea(),
    )

    model = CircuitMaintenance
    fieldsets = (FieldSet('status', 'ack', 'description'),)
    nullable_fields = ('description', 'comments')


class CircuitMaintenanceImportForm(PrimaryModelImportForm):
    status = CSVChoiceField(
        label=_('Status'),
        choices=CircuitMaintenanceStatusChoices,
        required=False,
        help_text=_('Maintenance status (default: Tentative)'),
    )

    class Meta:
        model = CircuitMaintenance
        fields = ('name', 'status', 'start_time', 'end_time', 'ack', 'description', 'owner', 'comments', 'tags')


class CircuitMaintenanceFilterForm(PrimaryModelFilterSetForm):
    model = CircuitMaintenance
    fieldsets = (
        FieldSet('q', 'filter_id', 'tag'),
        FieldSet('status', 'ack', 'start_time__gte', 'end_time__lte', name=_('Attributes')),
        FieldSet('provider_id', 'circuit_id', name=_('Impact')),
        FieldSet('owner_group_id', 'owner_id', name=_('Ownership')),
    )
    status = forms.MultipleChoiceField(
        label=_('Status'),
        choices=CircuitMaintenanceStatusChoices,
        required=False,
    )
    ack = forms.NullBooleanField(
        label=_('Acknowledged'),
        required=False,
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
    )
    start_time__gte = forms.DateTimeField(
        label=_('Start time after'),
        required=False,
        widget=DateTimePicker(),
    )
    end_time__lte = forms.DateTimeField(
        label=_('End time before'),
        required=False,
        widget=DateTimePicker(),
    )
    provider_id = DynamicModelMultipleChoiceField(
        queryset=Provider.objects.all(),
        required=False,
        label=_('Provider'),
    )
    circuit_id = DynamicModelMultipleChoiceField(
        queryset=Circuit.objects.all(),
        required=False,
        label=_('Circuit'),
    )
    tag = TagFilterField(model)


#
# CircuitImpact
#


class CircuitImpactForm(NetBoxModelForm):
    # Pre-filled from ?maintenance=<pk> when added from a maintenance's page
    maintenance = DynamicModelChoiceField(
        label=_('Maintenance'),
        queryset=CircuitMaintenance.objects.all(),
    )
    circuit = DynamicModelChoiceField(
        label=_('Circuit'),
        queryset=Circuit.objects.all(),
        selector=True,
    )

    fieldsets = (FieldSet('maintenance', 'circuit', 'impact', 'tags', name=_('Circuit Impact')),)

    class Meta:
        model = CircuitImpact
        fields = ('maintenance', 'circuit', 'impact', 'tags')


class CircuitImpactBulkEditForm(NetBoxModelBulkEditForm):
    impact = forms.ChoiceField(
        label=_('Impact'),
        choices=add_blank_choice(CircuitImpactChoices),
        required=False,
        initial='',
    )

    model = CircuitImpact
    fieldsets = (FieldSet('impact'),)


class CircuitImpactImportForm(NetBoxModelImportForm):
    maintenance = CSVModelChoiceField(
        label=_('Maintenance'),
        queryset=CircuitMaintenance.objects.all(),
        to_field_name='name',
        help_text=_('Name of the maintenance'),
    )
    circuit = CSVModelChoiceField(
        label=_('Circuit'),
        queryset=Circuit.objects.all(),
        to_field_name='cid',
        help_text=_('Circuit ID (use a "circuit.id" column when the same CID exists under several providers)'),
    )
    impact = CSVChoiceField(
        label=_('Impact'),
        choices=CircuitImpactChoices,
        required=False,
        help_text=_('Impact on the circuit (default: Outage)'),
    )

    class Meta:
        model = CircuitImpact
        fields = ('maintenance', 'circuit', 'impact', 'tags')


class CircuitImpactFilterForm(NetBoxModelFilterSetForm):
    model = CircuitImpact
    fieldsets = (
        FieldSet('q', 'filter_id', 'tag'),
        FieldSet('maintenance_id', 'circuit_id', 'impact', name=_('Attributes')),
    )
    maintenance_id = DynamicModelMultipleChoiceField(
        queryset=CircuitMaintenance.objects.all(),
        required=False,
        label=_('Maintenance'),
    )
    circuit_id = DynamicModelMultipleChoiceField(
        queryset=Circuit.objects.all(),
        required=False,
        label=_('Circuit'),
    )
    impact = forms.MultipleChoiceField(
        label=_('Impact'),
        choices=CircuitImpactChoices,
        required=False,
    )
    tag = TagFilterField(model)


#
# Note
#


class NoteForm(NetBoxModelForm):
    # Pre-filled from ?maintenance=<pk> when added from a maintenance's page
    maintenance = DynamicModelChoiceField(
        label=_('Maintenance'),
        queryset=CircuitMaintenance.objects.all(),
    )
    comment = CommentField(
        label=_('Comment'),
        required=True,
    )

    fieldsets = (FieldSet('maintenance', 'title', 'level', 'comment', 'tags', name=_('Note')),)

    class Meta:
        model = Note
        fields = ('maintenance', 'title', 'level', 'comment', 'tags')


class NoteBulkEditForm(NetBoxModelBulkEditForm):
    title = forms.CharField(
        label=_('Title'),
        max_length=MAX_NOTE_TITLE_LENGTH,
        required=False,
    )
    level = forms.ChoiceField(
        label=_('Level'),
        choices=add_blank_choice(NoteLevelChoices),
        required=False,
        initial='',
    )
    comment = CommentField(
        label=_('Comment'),
    )

    model = Note
    fieldsets = (FieldSet('title', 'level', 'comment'),)


class NoteImportForm(NetBoxModelImportForm):
    maintenance = CSVModelChoiceField(
        label=_('Maintenance'),
        queryset=CircuitMaintenance.objects.all(),
        to_field_name='name',
        help_text=_('Name of the maintenance'),
    )
    level = CSVChoiceField(
        label=_('Level'),
        choices=NoteLevelChoices,
        required=False,
        help_text=_('Note level (default: Info)'),
    )

    class Meta:
        model = Note
        fields = ('maintenance', 'title', 'level', 'comment', 'tags')


class NoteFilterForm(NetBoxModelFilterSetForm):
    model = Note
    fieldsets = (
        FieldSet('q', 'filter_id', 'tag'),
        FieldSet('maintenance_id', 'title', 'level', 'comment', name=_('Attributes')),
    )
    maintenance_id = DynamicModelMultipleChoiceField(
        queryset=CircuitMaintenance.objects.all(),
        required=False,
        label=_('Maintenance'),
    )
    title = forms.CharField(
        label=_('Title'),
        required=False,
    )
    level = forms.MultipleChoiceField(
        label=_('Level'),
        choices=NoteLevelChoices,
        required=False,
    )
    comment = forms.CharField(
        label=_('Comment'),
        required=False,
    )
    tag = TagFilterField(model)
