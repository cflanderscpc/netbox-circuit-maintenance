from django.core.exceptions import ImproperlyConfigured
from netbox.jobs import system_job
from netbox.plugins import get_plugin_config

from .handle_notifications.handler import HandleCircuitMaintenanceNotifications
from .site_search import FindSitesWithMaintenanceOverlap

__all__ = (
    'FindSitesWithMaintenanceOverlap',
    'HandleCircuitMaintenanceNotifications',
)


def get_interval(setting):
    """
    Return the number of minutes in an interval setting, or None. system_job() itself rejects anything but an int, with
    an error that doesn't name the setting, and a Job's interval must be at least 1.
    """
    interval = get_plugin_config('netbox_circuit_maintenance', setting)
    if interval is not None and (type(interval) is not int or interval < 1):
        raise ImproperlyConfigured(
            f"PLUGINS_CONFIG['netbox_circuit_maintenance']['{setting}'] must be a whole number of minutes, at least 1, "
            f'or None, not {interval!r}.'
        )
    return interval


# Schedule the notification handler when job_interval is set, and the overlap job when overlap_job_interval is. NetBox
# schedules system jobs when the RQ worker starts, so the settings are read once, at startup
for job_class, setting in (
    (HandleCircuitMaintenanceNotifications, 'job_interval'),
    (FindSitesWithMaintenanceOverlap, 'overlap_job_interval'),
):
    if (interval := get_interval(setting)) is not None:
        system_job(interval)(job_class)
