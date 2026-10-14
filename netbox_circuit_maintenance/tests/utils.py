import logging
from unittest.mock import create_autospec

from django.conf import settings
from django.test import override_settings

__all__ = (
    'MockedJob',
    'assert_called_with_substring',
    'plugin_settings',
)


def plugin_settings(**overrides):
    """
    override_settings() for some of the plugin's settings. It replaces the whole PLUGINS_CONFIG setting, so this keeps
    the plugin's other settings, including the defaults NetBox merged in at startup.
    """
    plugin_config = {**settings.PLUGINS_CONFIG['netbox_circuit_maintenance'], **overrides}
    return override_settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, 'netbox_circuit_maintenance': plugin_config})


class MockedJob:
    """
    Stands in for the notification handler's JobRunner, of which the sources use only the logger. The logger is a mock
    with logging.Logger's signatures, so a call that a real logger would reject fails here too.
    """

    def __init__(self):
        self.logger = create_autospec(logging.Logger, instance=True)


def assert_called_with_substring(mock, substring):
    """
    Helper function to check for substrings in mock called args.
    """
    for args, _ in mock.call_args_list:
        for arg in args:
            if substring in arg:
                return
    raise AssertionError(f"Expected substring '{substring}' not found in any call arguments.")
