from netbox.api.routers import NetBoxRouter

from . import views

router = NetBoxRouter()
router.register('circuit-maintenances', views.CircuitMaintenanceViewSet)

urlpatterns = router.urls
