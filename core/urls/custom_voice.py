from rest_framework.routers import DefaultRouter
from core.views.custom_voice import CustomVoiceViewSet

router = DefaultRouter()
router.register(r"custom-voices", CustomVoiceViewSet, basename="custom-voices")

urlpatterns = router.urls
