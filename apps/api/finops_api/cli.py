"""Operational CLI: `python -m finops_api.cli <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from .alerts import sync_alert
from .catalog_store import import_manual_prices, run_provider_sync, seed_pricing
from .config import get_settings
from .db import Base, SessionLocal, get_engine
from .orm import ProviderRow, Upload, User
from .security import create_user_with_org


def cmd_migrate(_: argparse.Namespace) -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, "head")
    print("migrations applied")


def cmd_create_all(_: argparse.Namespace) -> None:
    """Dev/test shortcut for SQLite. Postgres environments use `migrate`."""
    import finops_api.orm  # noqa: F401

    Base.metadata.create_all(get_engine())
    print("tables created")


def cmd_seed_pricing(_: argparse.Namespace) -> None:
    with SessionLocal() as db:
        stats = seed_pricing(db, get_settings().seed_dir)
    print(json.dumps(stats))


def cmd_seed_demo_user(args: argparse.Namespace) -> None:
    s = get_settings()
    if s.is_prod:
        sys.exit("refusing to create the demo user in staging/prod")
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == args.email)):
            print(f"demo user {args.email} already exists")
            return
        create_user_with_org(db, args.email, args.password, "Demo", "Demo workspace")
        db.commit()
    print(f"created {args.email}")


def cmd_reset_password(args: argparse.Namespace) -> None:
    """For when nobody can sign in: sets a new password and ends that person's sessions."""
    import secrets

    from .orm import AuditLog
    from .security import set_password

    email = args.email.lower().strip()
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else secrets.token_urlsafe(12)
    if len(password) < 10:
        sys.exit("the password needs at least 10 characters")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            sys.exit(f"no account uses {email}")
        set_password(user, password)
        db.add(AuditLog(user_id=user.id, action="auth.password_reset_cli", entity_type="user", entity_id=user.id,
                        detail={}))
        db.commit()
    if args.password_stdin:
        print(f"password changed for {email}; their other sessions have ended")
    else:
        print(f"new password for {email}: {password}\nthey can change it under Settings after signing in")


def cmd_test_alerts(args: argparse.Namespace) -> None:
    """Send one test message through each alert channel that's set up, and say which worked (`sarfeh test-alerts`)."""
    from .alerts import _send_bale, _send_email

    s = get_settings()
    subject = "Sarfeh: test alert"
    body = "This is a test from Sarfeh. Alerts about prices and renewals will arrive here."
    tried = failed = 0
    if s.smtp_host:
        tried += 1
        to = args.to or s.alert_email_to
        if not to:
            print("email: no recipient. Set ALERT_EMAIL_TO, or pass --to")
            failed += 1
        else:
            try:
                _send_email(subject, body, to)
                print(f"email: sent to {to} through {s.smtp_host}:{s.smtp_port}")
            except Exception as exc:  # noqa: BLE001 - report any delivery failure to the person testing
                print(f"email: failed: {type(exc).__name__}: {exc}")
                failed += 1
    if s.bale_bot_token and s.bale_chat_id:
        tried += 1
        try:
            _send_bale(subject, body)
            print(f"bale: sent to chat {s.bale_chat_id}")
        except Exception as exc:  # noqa: BLE001
            # the request URL holds the bot token, so only the kind of error and the status are shown
            status = getattr(getattr(exc, "response", None), "status_code", None)
            print(f"bale: failed: {type(exc).__name__}" + (f" (HTTP {status})" if status else ""))
            failed += 1
    if not tried:
        sys.exit("no alert channel is set up. Email: sarfeh config set SMTP_HOST=... ALERT_EMAIL_TO=...; "
                 "Bale: sarfeh config set BALE_CHAT_ID=... and BALE_BOT_TOKEN")
    if failed:
        sys.exit(1)


def cmd_sync(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        ids = [args.provider] if args.provider else [p.id for p in db.scalars(select(ProviderRow))]
        failed = False
        for pid in ids:
            run = run_provider_sync(db, pid, triggered_by="cli")
            print(json.dumps({"provider": pid, "status": run.status, "seen": run.items_seen,
                              "changed": run.items_changed, "warnings": run.warnings, "error": run.error},
                             ensure_ascii=False))
            failed |= run.status in ("failed", "quarantined")
    if failed and args.strict:
        sys.exit(1)


def cmd_price_import(args: argparse.Namespace) -> None:
    content = Path(args.file).read_text(encoding="utf-8")
    with SessionLocal() as db:
        run = import_manual_prices(db, args.provider, content, triggered_by="manual-file")
        sync_alert(db, run)
        print(json.dumps({"provider": args.provider, "status": run.status, "changed": run.items_changed,
                          "warnings": run.warnings, "error": run.error}, ensure_ascii=False))
    if run.status == "failed":
        sys.exit(1)


def cmd_catalog_keygen(_: argparse.Namespace) -> None:
    from provider_adapters import generate_signing_key

    private, public = generate_signing_key()
    print(f"FINOPS_CATALOG_SIGNING_KEY={private}   # publisher only; keep this secret")
    print(f"FINOPS_CATALOG_PUBLIC_KEY={public}")


def cmd_catalog_publish(args: argparse.Namespace) -> None:
    from .catalog_feed import publish

    s = get_settings()
    out = Path(args.out) if args.out else s.catalog_publish_dir
    if not out or not s.catalog_signing_key:
        sys.exit("needs FINOPS_CATALOG_SIGNING_KEY and --out (or FINOPS_CATALOG_PUBLISH_DIR)")
    with SessionLocal() as db:
        print(publish(db, out, s.catalog_signing_key))


def cmd_catalog_pull(_: argparse.Namespace) -> None:
    from .catalog_feed import pull

    if not get_settings().catalog_url:
        sys.exit("FINOPS_CATALOG_URL is not set")
    with SessionLocal() as db:
        print(json.dumps(pull(db)))


def _tehran(stamp: object) -> str:
    from zoneinfo import ZoneInfo

    if not stamp:
        return "-"
    when = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp))
    if when.tzinfo is None:  # SQLite gives stored times back without their zone; they're UTC
        when = when.replace(tzinfo=UTC)
    return when.astimezone(ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M")


def _provider_names(db) -> dict[str, str]:
    return {p.id: p.name_en for p in db.scalars(select(ProviderRow))}


def _print_price_status(st: dict, names: dict[str, str]) -> None:
    if st["source"] == "scrape":
        print("Prices are read from the providers' pages by this server, twice a day (PRICE_SOURCE=scrape).")
        return
    if st["source"] == "catalog_url":
        print(f"Prices come from your own signed catalog at {get_settings().catalog_url}, twice a day.")
        return
    print("Prices come from Sarfeh's price service" + (f" ({st['mirror']})" if st.get("mirror") else "") +
          (", applied as soon as they're out (PRICE_AUTO_UPDATE)." if st["auto_update"] else "."))
    applied, latest = st.get("applied"), st.get("latest")
    if applied:
        print(f"In use here: published {_tehran(applied['generated_at'])}, applied {_tehran(applied['applied_at'])} "
              f"({applied['by']})")
    if latest:
        print(f"Newest:      published {_tehran(latest['generated_at'])}, providers last read {_tehran(latest['read_on'])}")
    print(f"Last check:  {_tehran(st.get('checked_at'))} (Tehran time)" + (f"; it failed: {st['error']}" if st.get("error") else ""))
    if st["update_available"]:
        changed = ", ".join(names.get(p, p) for p in st.get("changed") or [])
        print(f"New prices are out{' for ' + changed if changed else ''}. Update with: sudo sarfeh prices update")
    elif latest:
        print("Up to date.")


def cmd_prices_status(args: argparse.Namespace) -> None:
    from .price_service import status

    with SessionLocal() as db:
        st, names = status(db), _provider_names(db)
    if args.json:
        print(json.dumps(st, default=str, ensure_ascii=False))
    else:
        _print_price_status(st, names)


def cmd_prices_check(args: argparse.Namespace) -> None:
    from .price_service import check

    if not get_settings().uses_price_service:
        sys.exit("this installation doesn't take its prices from Sarfeh's price service (PRICE_SOURCE)")
    with SessionLocal() as db:
        st, names = check(db), _provider_names(db)
    if args.json:
        print(json.dumps(st, default=str, ensure_ascii=False))
    else:
        _print_price_status(st, names)
    if st.get("error") and not st.get("latest"):
        sys.exit(1)


def cmd_prices_update(_: argparse.Namespace) -> None:
    from provider_adapters import FeedError

    from .price_service import PriceServiceError, update

    if not get_settings().uses_price_service:
        sys.exit("this installation doesn't take its prices from Sarfeh's price service (PRICE_SOURCE)")
    with SessionLocal() as db:
        try:
            out = update(db, by="command line")
        except (PriceServiceError, FeedError) as exc:
            sys.exit(f"prices weren't updated: {exc}")
        names = _provider_names(db)
    changed = ", ".join(names.get(p, p) for p in out["changed"])
    print(f"Prices published {_tehran(out['generated_at'])} (Tehran time) are in use now." +
          (f" Changed: {changed}." if changed else " No price changed."))


def cmd_prices_bundled(_: argparse.Namespace) -> None:
    """At every start: the catalog that came with this release, if it's newer. Never stops the start."""
    from .price_service import apply_bundled

    try:
        with SessionLocal() as db:
            print(f"bundled prices: {apply_bundled(db)}")
    except Exception as exc:  # noqa: BLE001 - the seed's prices are still there; the hourly check catches up
        print(f"bundled prices not applied: {type(exc).__name__}: {exc}", file=sys.stderr)


def cmd_collect_servers(_: argparse.Namespace) -> None:
    from .fleet_service import collect_all

    with SessionLocal() as db:
        print(json.dumps(collect_all(db)))


def cmd_purge_uploads(_: argparse.Namespace) -> None:
    now = datetime.now(UTC)
    n = 0
    with SessionLocal() as db:
        for up in db.scalars(select(Upload).where(Upload.deleted_at.is_(None))):
            due = up.delete_after if up.delete_after.tzinfo else up.delete_after.replace(tzinfo=UTC)
            if due <= now:
                if up.storage_path:
                    Path(up.storage_path).unlink(missing_ok=True)
                up.storage_path, up.deleted_at = None, now
                n += 1
        db.commit()
    print(f"purged {n} raw invoice file(s)")


def main() -> None:
    ap = argparse.ArgumentParser(prog="finops")
    sub = ap.add_subparsers(required=True)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sub.add_parser("create-all").set_defaults(fn=cmd_create_all)
    sub.add_parser("seed-pricing").set_defaults(fn=cmd_seed_pricing)
    d = sub.add_parser("seed-demo-user")
    d.add_argument("--email", default="demo@finops-agent.local")
    d.add_argument("--password", default="demo-password-1")
    d.set_defaults(fn=cmd_seed_demo_user)
    rp = sub.add_parser("reset-password", help="set a new password for an account (prints a generated one)")
    rp.add_argument("--email", required=True)
    rp.add_argument("--password-stdin", action="store_true", help="read the new password from standard input")
    rp.set_defaults(fn=cmd_reset_password)
    ta = sub.add_parser("test-alerts", help="send a test message through each alert channel that's set up")
    ta.add_argument("--to", help="email address for this test (default: FINOPS_ALERT_EMAIL_TO)")
    ta.set_defaults(fn=cmd_test_alerts)
    s = sub.add_parser("sync-prices")
    s.add_argument("--provider")
    s.add_argument("--strict", action="store_true", help="exit 1 if any sync failed or was quarantined")
    s.set_defaults(fn=cmd_sync)
    m = sub.add_parser("price-import", help="apply a reviewed price file (see docs/provider-adapters.md)")
    m.add_argument("--provider", required=True)
    m.add_argument("--file", required=True)
    m.set_defaults(fn=cmd_price_import)
    sub.add_parser("collect-servers").set_defaults(fn=cmd_collect_servers)
    sub.add_parser("catalog-keygen").set_defaults(fn=cmd_catalog_keygen)
    cp = sub.add_parser("catalog-publish")
    cp.add_argument("--out")
    cp.set_defaults(fn=cmd_catalog_publish)
    sub.add_parser("catalog-pull").set_defaults(fn=cmd_catalog_pull)
    ps = sub.add_parser("prices-status", help="where prices come from, and whether new ones are out")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(fn=cmd_prices_status)
    pc = sub.add_parser("prices-check", help="ask Sarfeh's price service now whether new prices are out")
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(fn=cmd_prices_check)
    sub.add_parser("prices-update", help="apply the newest prices from Sarfeh's price service").set_defaults(
        fn=cmd_prices_update)
    sub.add_parser("prices-bundled", help="apply the prices that came with this release, if newer").set_defaults(
        fn=cmd_prices_bundled)
    sub.add_parser("purge-uploads").set_defaults(fn=cmd_purge_uploads)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
