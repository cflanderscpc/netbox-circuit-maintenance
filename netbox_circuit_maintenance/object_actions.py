from django.utils.translation import gettext_lazy as _
from netbox.object_actions import ObjectAction

__all__ = ('ValidateSource',)


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
