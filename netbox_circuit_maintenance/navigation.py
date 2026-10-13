from django.utils.translation import gettext_lazy as _
from netbox.plugins import PluginMenu, PluginMenuItem

menu = PluginMenu(
    label=_('Circuit Maintenance'),
    groups=(
        (
            _('Maintenances'),
            (
                PluginMenuItem(
                    link='plugins:netbox_circuit_maintenance:circuitmaintenance_overview',
                    link_text=_('Dashboard'),
                    permissions=['netbox_circuit_maintenance.view_circuitmaintenance'],
                ),
                PluginMenuItem(
                    link='plugins:netbox_circuit_maintenance:circuitmaintenance_list',
                    link_text=_('Circuit Maintenances'),
                    permissions=['netbox_circuit_maintenance.view_circuitmaintenance'],
                ),
            ),
        ),
        (
            _('Notifications'),
            (
                PluginMenuItem(
                    link='plugins:netbox_circuit_maintenance:rawnotification_list',
                    link_text=_('Notifications'),
                    permissions=['netbox_circuit_maintenance.view_rawnotification'],
                ),
                PluginMenuItem(
                    link='plugins:netbox_circuit_maintenance:notificationsource_list',
                    link_text=_('Notification Sources'),
                    permissions=['netbox_circuit_maintenance.view_notificationsource'],
                ),
            ),
        ),
    ),
    icon_class='mdi mdi-wrench-clock',
)
