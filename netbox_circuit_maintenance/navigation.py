from django.utils.translation import gettext_lazy as _
from netbox.plugins import PluginMenu, PluginMenuItem

menu = PluginMenu(
    label=_('Circuit Maintenance'),
    groups=(
        (
            _('Maintenances'),
            (
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
            ),
        ),
    ),
    icon_class='mdi mdi-wrench-clock',
)
