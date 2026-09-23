from netbox.plugins import PluginConfig

class NetBoxCircuitMaintenanceConfig(PluginConfig):
	name = 'netbox_circuit_maintenance'
	verbose_name = 'NetBox Circuit Maintenance'
	description = 'Manage circuit maintenance notifications in NetBox'
	version = '0.1.0'
	base_url = 'circuit-maint'
	min_version = '4.6.0'
	max_version = '4.6.99'

config = NetBoxCircuitMaintenanceConfig
