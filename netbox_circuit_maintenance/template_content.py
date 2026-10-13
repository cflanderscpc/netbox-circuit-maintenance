from netbox.plugins import PluginTemplateExtension

from .ui.panels import CircuitMaintenancesPanel


class CircuitMaintenanceContent(PluginTemplateExtension):
    models = ['circuits.circuit']

    def right_page(self):
        return CircuitMaintenancesPanel().render(self.context)


template_extensions = [CircuitMaintenanceContent]
