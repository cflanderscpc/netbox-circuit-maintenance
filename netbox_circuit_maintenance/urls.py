from django.urls import include, path
from utilities.urls import get_model_urls

from . import views  # noqa: F401 (registers the model views)

urlpatterns = [
    path(
        'circuit-maintenances/',
        include(get_model_urls('netbox_circuit_maintenance', 'circuitmaintenance', detail=False)),
    ),
    path(
        'circuit-maintenances/<int:pk>/',
        include(get_model_urls('netbox_circuit_maintenance', 'circuitmaintenance')),
    ),
    path(
        'circuit-impacts/',
        include(get_model_urls('netbox_circuit_maintenance', 'circuitimpact', detail=False)),
    ),
    path(
        'circuit-impacts/<int:pk>/',
        include(get_model_urls('netbox_circuit_maintenance', 'circuitimpact')),
    ),
    path(
        'notes/',
        include(get_model_urls('netbox_circuit_maintenance', 'note', detail=False)),
    ),
    path(
        'notes/<int:pk>/',
        include(get_model_urls('netbox_circuit_maintenance', 'note')),
    ),
    path(
        'raw-notifications/',
        include(get_model_urls('netbox_circuit_maintenance', 'rawnotification', detail=False)),
    ),
    path(
        'raw-notifications/<int:pk>/',
        include(get_model_urls('netbox_circuit_maintenance', 'rawnotification')),
    ),
]
