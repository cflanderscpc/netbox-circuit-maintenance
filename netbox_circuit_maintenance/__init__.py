from importlib import metadata

from django.db.models.signals import post_migrate
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
    # The default path, graphql.schema, looks for a schema attribute on the graphql package, not the list in schema.py
    graphql_schema = 'graphql.schema.schema'
    min_version = '4.6.6'
    max_version = '4.6.99'
    default_settings = {
        'raw_notification_initial_days_since': 7,
        'raw_notification_size': 8192,
        'dashboard_n_days': 30,
        'overlap_job_exclude_no_impact': False,
        # Minutes between scheduled runs of the site overlap job; None disables the schedule
        'overlap_job_interval': None,
        # Minutes between scheduled runs of the notification handler job; None disables the schedule
        'job_interval': None,
        # Username that scheduled handler runs record their changes as; None means those runs record no change log
        'job_user': None,
        # The mailboxes to fetch notifications from: a dict per source, matched to a NotificationSource by name
        'notification_sources': [],
    }

    def ready(self):
        super().ready()

        # Connect the signal receivers, and register the notification handler and the overlap job as system jobs if
        # their interval settings are set
        from . import jobs, signals

        # The NotificationSource sync, for this plugin's post_migrate only
        post_migrate.connect(signals.import_notification_sources, sender=self)


config = NetBoxCircuitMaintenanceConfig
