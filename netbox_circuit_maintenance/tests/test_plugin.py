import sys
from importlib import metadata

from django.apps import apps
from django.conf import settings
from django.db import connection
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
            'overlap_job_interval': None,
            'job_interval': None,
            'job_user': None,
            'notification_sources': [],
        }
        self.assertEqual(NetBoxCircuitMaintenanceConfig.default_settings, expected)

        # NetBox copies any default the operator didn't set into PLUGINS_CONFIG at startup, so every key is present
        # whatever the local configuration overrides
        for setting in expected:
            self.assertIn(setting, settings.PLUGINS_CONFIG['netbox_circuit_maintenance'])

    @override_settings(ISOLATED_DEPLOYMENT=True)  # Don't fetch the online plugin catalog
    def test_ready_imports_jobs(self):
        # ready() imports jobs, which registers the system job. No test module imports it (test_handler runs it with
        # runpy), so this fails if ready() stops importing it
        self.assertIn('netbox_circuit_maintenance.jobs', sys.modules)

    def test_plugin_list(self):
        self.user.is_superuser = True
        self.user.save()

        response = self.client.get(reverse('core:plugin_list'))
        self.assertHttpStatus(response, 200)
        plugins = {plugin.config_name: plugin for plugin in response.context['table'].data}
        self.assertTrue(plugins['netbox_circuit_maintenance'].is_loaded)

    def test_constraint_name_lengths(self):
        # PostgreSQL silently truncates longer identifiers, and Django checks only index and column name lengths
        max_length = connection.ops.max_name_length()
        for model in apps.get_app_config('netbox_circuit_maintenance').get_models():
            for constraint in model._meta.constraints:
                with self.subTest(constraint=constraint.name):
                    self.assertLessEqual(len(constraint.name), max_length)


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
