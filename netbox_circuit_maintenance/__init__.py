from importlib import metadata

from netbox.plugins import PluginConfig

__version__ = metadata.version(__name__)


class NetBoxCircuitMaintenanceConfig(PluginConfig):
    name = 'netbox_circuit_maintenance'
    verbose_name = 'Circuit Maintenance'
    description = metadata.metadata(__name__)['Summary']
    version = __version__
    author = 'Charles Flanders'
    author_email = 'cflanders@capitalpower.com'
    base_url = 'circuit-maintenance'
    min_version = '4.6.6'
    max_version = '4.6.99'
    default_settings = {
        'raw_notification_initial_days_since': 7,
        'raw_notification_size': 8192,
        'dashboard_n_days': 30,
        'overlap_job_exclude_no_impact': False,
        # Minutes between scheduled runs of the notification handler job; None disables the schedule
        'job_interval': None,
        # Username that scheduled handler runs record their changes as; None means those runs record no change log
        'job_user': None,
    }


config = NetBoxCircuitMaintenanceConfig
