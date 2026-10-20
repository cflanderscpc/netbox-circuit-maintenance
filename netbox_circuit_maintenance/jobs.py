from django.core.exceptions import ImproperlyConfigured
from netbox.jobs import system_job
from netbox.plugins import get_plugin_config

from .handle_notifications.handler import HandleCircuitMaintenanceNotifications

__all__ = ('HandleCircuitMaintenanceNotifications',)

# Schedule the notification handler when job_interval is set. NetBox schedules system jobs when the RQ worker starts, so
# the setting is read once, at startup
if (interval := get_plugin_config('netbox_circuit_maintenance', 'job_interval')) is not None:
    # system_job() itself rejects anything but an int, with an error that doesn't name the setting, and a Job's interval
    # must be at least 1
    if type(interval) is not int or interval < 1:
        raise ImproperlyConfigured(
            "PLUGINS_CONFIG['netbox_circuit_maintenance']['job_interval'] must be a whole number of minutes, at least 1, "
            f'or None, not {interval!r}.'
        )
    system_job(interval)(HandleCircuitMaintenanceNotifications)
