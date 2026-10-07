from netbox.api.routers import NetBoxRouter

from . import views

router = NetBoxRouter()
router.register('circuit-maintenances', views.CircuitMaintenanceViewSet)
router.register('circuit-impacts', views.CircuitImpactViewSet)
router.register('notes', views.NoteViewSet)
router.register('notification-sources', views.NotificationSourceViewSet)
router.register('raw-notifications', views.RawNotificationViewSet)
router.register('parsed-notifications', views.ParsedNotificationViewSet)

urlpatterns = router.urls
