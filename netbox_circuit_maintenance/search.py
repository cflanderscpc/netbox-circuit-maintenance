from netbox.search import SearchIndex, register_search

from . import models


@register_search
class CircuitMaintenanceIndex(SearchIndex):
    model = models.CircuitMaintenance
    fields = (
        ('name', 100),
        ('description', 500),
        ('comments', 5000),
    )
    display_attrs = ('status', 'start_time', 'end_time', 'description')
