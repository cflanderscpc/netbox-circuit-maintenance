from netbox.api.routers import NetBoxRouter

from . import views

router = NetBoxRouter()
router.register('circuit-maintenances', views.CircuitMaintenanceViewSet)
router.register('circuit-impacts', views.CircuitImpactViewSet)

urlpatterns = router.urls
