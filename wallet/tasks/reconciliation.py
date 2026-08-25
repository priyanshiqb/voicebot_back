"""Scheduled assertion that every balance matches its ledger."""

import logging

from celery import shared_task

from core.tasks import ReportedTask

logger = logging.getLogger(__name__)


@shared_task(base=ReportedTask, name="wallet.tasks.reconcile_wallets")
def reconcile_wallets():
    """Assert ``SUM(ledger) == balance`` for every wallet.

    Reports, never repairs — auto-correcting on a timer hides the bug that caused the
    drift, and you would never learn the balance had been wrong. `--fix` exists on the
    management command for a deliberate operator decision.
    """
    from django.core.management import call_command

    try:
        call_command("reconcile_wallets")
        return 0
    except SystemExit as exc:
        # The command exits non-zero on drift; a task should record that as a finding
        # rather than let it surface as a crash.
        logger.error("wallet reconciliation found drift")
        return int(exc.code or 1)
