from django.test import override_settings
from django.urls import reverse
from utilities.testing import APITestCase, TestCase


class PluginTest(TestCase):
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
        self.assertIn('netbox_circuit_maintenance', [plugin['package'] for plugin in response.data])
