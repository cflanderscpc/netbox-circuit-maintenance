from django.test import SimpleTestCase

from netbox_circuit_maintenance.utils import split_provider_emails


class SplitProviderEmailsTestCase(SimpleTestCase):
    def test_split(self):
        for value, emails in (
            ('noc@x.com', ['noc@x.com']),
            (' NOC@X.com , ops@x.com', ['noc@x.com', 'ops@x.com']),
            # Repeats are kept, in order
            ('noc@x.com,NOC@x.com', ['noc@x.com', 'noc@x.com']),
            # Empty entries are skipped
            ('noc@x.com,', ['noc@x.com']),
            (' , ,', []),
            ('', []),
        ):
            with self.subTest(value=value):
                self.assertEqual(split_provider_emails(value), emails)

    def test_not_a_string(self):
        # An unset field is None, and an admin can change the field's type
        for value in (None, 123, ['noc@x.com'], {'email': 'noc@x.com'}):
            with self.subTest(value=value):
                self.assertEqual(split_provider_emails(value), [])
