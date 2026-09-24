from django.db import models
from netbox.models import NetBoxModel

__all__ = ('ScaffoldModel',)


class ScaffoldModel(NetBoxModel):
    """
    Throwaway model proving the plugin migrates (PORTING.md Phase 3). The first Phase 4 model port deletes it and
    regenerates 0001_initial.
    """

    name = models.CharField(max_length=100)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name
