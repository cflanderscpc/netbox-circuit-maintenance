from django.http import QueryDict
from django.utils.translation import gettext_lazy as _
from netbox.object_actions import ObjectAction

__all__ = (
    'FindOverlaps',
    'RunNow',
    'ValidateSource',
)


class FindOverlaps(ObjectAction):
    """
    Queue a run of the overlap job, which finds the sites where circuit maintenances overlap.
    """

    name = 'find_overlaps'
    label = _('Find Site Overlaps')
    permissions_required = {'view'}
    template_name = 'netbox_circuit_maintenance/buttons/find_overlaps.html'

    @classmethod
    def get_url_params(cls, context):
        # A run covers every maintenance the user can view, so the list's filters don't go with it; the user comes back
        # to the list as they left it
        params = QueryDict(mutable=True)
        params['return_url'] = context['request'].get_full_path()
        return params

    @classmethod
    def get_context(cls, context, obj):
        # The button posts a form, and render_to_string() has no request to take the CSRF token from
        return {'csrf_token': context['csrf_token']}


class RunNow(ObjectAction):
    """
    Open the form that runs the notification handler, fetching from every notification source.
    """

    name = 'fetch'
    label = _('Run Now')
    permissions_required = {'fetch'}
    template_name = 'netbox_circuit_maintenance/buttons/run_now.html'


class ValidateSource(ObjectAction):
    """
    Test a notification source's authentication.
    """

    name = 'validate'
    label = _('Validate Authentication')
    permissions_required = {'view'}
    url_kwargs = ['pk']
    template_name = 'netbox_circuit_maintenance/buttons/validate.html'

    @classmethod
    def get_context(cls, context, obj):
        # The button posts a form, and render_to_string() has no request to take the CSRF token from
        return {'csrf_token': context['csrf_token']}
