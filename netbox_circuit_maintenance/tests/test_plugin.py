from importlib import metadata

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from utilities.testing import APITestCase, TestCase

from netbox_circuit_maintenance import NetBoxCircuitMaintenanceConfig


class PluginTest(TestCase):
    def test_default_settings(self):
        expected = {
            'raw_notification_initial_days_since': 7,
            'raw_notification_size': 8192,
            'dashboard_n_days': 30,
            'overlap_job_exclude_no_impact': False,
            'job_interval': None,
            'job_user': None,
        }
        self.assertEqual(NetBoxCircuitMaintenanceConfig.default_settings, expected)

        # NetBox copies any default the operator didn't set into PLUGINS_CONFIG at startup, so every key is present
        # whatever the local configuration overrides
        for setting in expected:
            self.assertIn(setting, settings.PLUGINS_CONFIG['netbox_circuit_maintenance'])

    @override_settings(ISOLATED_DEPLOYMENT=True)  # Don't fetch the online plugin catalog
    def test_plugin_list(self):
        self.user.is_superuser = True
        self.user.save()

        response = self.client.get(reverse('core:plugin_list'))
        self.assertHttpStatus(response, 200)
        plugins = {plugin.config_name: plugin for plugin in response.context['table'].data}
        self.assertTrue(plugins['netbox_circuit_maintenance'].is_loaded)


class PluginAPITest(APITestCase):
    def test_installed_plugins(self):
        self.user.is_superuser = True
        self.user.save()

        response = self.client.get(reverse('plugins-api:plugins-list'), **self.header)
        self.assertHttpStatus(response, 200)
        plugins = {plugin['package']: plugin for plugin in response.data}
        self.assertEqual(
            plugins['netbox_circuit_maintenance'],
            {
                'name': 'Circuit Maintenance',
                'package': 'netbox_circuit_maintenance',
                'author': 'Charles Flanders',
                'author_email': 'cflanders@capitalpower.com',
                # Both come from the installed package metadata (pyproject.toml)
                'description': metadata.metadata('netbox-circuit-maintenance')['Summary'],
                'version': metadata.version('netbox-circuit-maintenance'),
                'release_track': '',
            },
        )
