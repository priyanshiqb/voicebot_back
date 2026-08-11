from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        # Register scheduling signals (AvailabilityException -> reschedule task).
        import core.signals  # noqa: F401

        # Deploy-time validation for upload settings whose failure mode is silent.
        import core.checks  # noqa: F401
