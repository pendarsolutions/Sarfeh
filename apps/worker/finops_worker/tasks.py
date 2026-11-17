"""Celery worker: twice-daily provider price sync (with guardrails), server collection, the monthly cost
snapshot, and invoice retention purge.

Run:  celery -A finops_worker.tasks worker -B --loglevel=INFO
"""

from __future__ import annotations

import logging
import os

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import select

from finops_api.alerts import renewal_alerts, sync_alert
from finops_api.catalog_feed import publish_if_configured, pull
from finops_api.catalog_store import run_provider_sync
from finops_api.config import get_settings
from finops_api.db import SessionLocal
from finops_api.fleet_service import collect_all, purge_samples
from finops_api.orm import ProviderRow
from finops_api.platform import configure_logging
from finops_api.spend import budget_alerts, take_monthly_snapshots, unusual_change_alerts

configure_logging()
log = logging.getLogger("finops.worker")

broker = os.environ.get("FINOPS_REDIS_URL", "redis://localhost:6379/0")
app = Celery("finops", broker=broker, backend=broker)
app.conf.update(
    timezone="Asia/Tehran",
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "price-sync": {"task": "finops_worker.tasks.sync_all", "schedule": crontab(hour="3,15", minute=15)},
        "collect-servers": {"task": "finops_worker.tasks.collect_servers", "schedule": crontab(minute="*/5"),
                            "options": {"expires": 240}},
        "server-maintenance": {"task": "finops_worker.tasks.server_maintenance", "schedule": crontab(hour=9, minute=0)},
        "purge-expired-invoices": {"task": "finops_worker.tasks.purge_uploads", "schedule": crontab(hour=4, minute=0)},
        # hourly, so a month's estimate is taken in the first hour of the 1st (Solar Hijri) and a newly confirmed
        # inventory gets one within the hour; an inventory that already has this month's is left alone
        "monthly-snapshots": {"task": "finops_worker.tasks.monthly_snapshots", "schedule": crontab(minute=20),
                              "options": {"expires": 3000}},
    },
)


@app.task(name="finops_worker.tasks.sync_provider", autoretry_for=(ConnectionError,), retry_backoff=True,
          max_retries=3)
def sync_provider(provider_id: str, triggered_by: str = "schedule") -> dict:
    with SessionLocal() as db:
        run = run_provider_sync(db, provider_id, triggered_by=triggered_by)
        sync_alert(db, run)
        return {"provider_id": provider_id, "status": run.status, "run_id": run.id}


@app.task(name="finops_worker.tasks.sync_all")
def sync_all() -> list[dict]:
    if get_settings().catalog_url:
        with SessionLocal() as db:
            return [{"catalog": pull(db)}]
    with SessionLocal() as db:
        ids = [p.id for p in db.scalars(select(ProviderRow))]
    results = [sync_provider(pid, "schedule") for pid in ids]
    with SessionLocal() as db:
        publish_if_configured(db)
    return results


@app.task(name="finops_worker.tasks.purge_uploads")
def purge_uploads() -> None:
    from argparse import Namespace

    from finops_api.cli import cmd_purge_uploads

    cmd_purge_uploads(Namespace())


@app.task(name="finops_worker.tasks.collect_servers")
def collect_servers() -> dict:
    with SessionLocal() as db:
        return collect_all(db)


@app.task(name="finops_worker.tasks.server_maintenance")
def server_maintenance() -> None:
    with SessionLocal() as db:
        purge_samples(db)
        renewal_alerts(db)


@app.task(name="finops_worker.tasks.monthly_snapshots")
def monthly_snapshots() -> dict:
    """This month's estimates, then the budget alerts (80% and 100%) and last month's unusual changes, which use them."""
    with SessionLocal() as db:
        out = take_monthly_snapshots(db)
        out["budget_alerts"] = budget_alerts(db)
        out["unusual_change_alerts"] = unusual_change_alerts(db)
        return out
