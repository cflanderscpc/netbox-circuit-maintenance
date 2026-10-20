from django.utils.translation import gettext_lazy as _
from netbox.object_actions import ObjectAction

__all__ = (
    'RunNow',
    'ValidateSource',
)


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
