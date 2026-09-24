from core.models import ObjectType
from django.test import TestCase
from extras.models import Tag

from netbox_circuit_maintenance.models import ScaffoldModel


class ScaffoldModelTest(TestCase):
    """
    Proves the plugin's migration reaches the test database (PORTING.md Phase 3). Deleted with ScaffoldModel.
    """

    def test_model(self):
        tag = Tag.objects.create(name='Tag 1', slug='tag-1')
        instance = ScaffoldModel.objects.create(name='Instance 1')
        instance.tags.add(tag)

        self.assertEqual(list(instance.tags.all()), [tag])
        self.assertTrue(
            ObjectType.objects.filter(app_label='netbox_circuit_maintenance', model='scaffoldmodel').exists()
        )
