"""Upgrading an installation: every schema change is a migration, the migrations build exactly the tables the code
uses, and a database from 0.2.0 (revision 0002) keeps its data on the way up."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _alembic(url: str, *args: str) -> str:
    env = {**os.environ, "FINOPS_DATABASE_URL": url,
           "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in ("packages", "apps/api", "apps/worker"))}
    r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    return r.stdout + r.stderr


def test_a_0_2_0_database_upgrades_with_its_data(tmp_path):
    db = tmp_path / "from-0.2.0.db"
    url = f"sqlite:///{db.as_posix()}"
    _alembic(url, "upgrade", "0002")
    con = sqlite3.connect(db)
    con.execute("insert into organizations (id, name, settings, created_at) "
                "values ('o1', 'Acme', '{}', '2026-01-01 00:00:00')")
    con.execute("insert into inventories (id, organization_id, name, currency, status, source_type, source_meta, "
                "created_at, updated_at) values ('i1', 'o1', 'Production', 'TOMAN', 'confirmed', 'builder', '{}', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')")
    con.commit()
    con.close()

    _alembic(url, "upgrade", "head")
    assert "No new upgrade operations detected" in _alembic(url, "check")

    con = sqlite3.connect(db)
    assert con.execute("select name, status, confirmed_resources from inventories").fetchall() == [
        ("Production", "confirmed", None)]
    assert con.execute("select count(*) from spend_entries").fetchone() == (0,)
    assert con.execute("select count(*) from cost_snapshots").fetchone() == (0,)
    con.close()

    # and back down to 0.3.x, for an upgrade that has to be undone
    _alembic(url, "downgrade", "0003")
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
    assert "spend_entries" not in tables and "cost_snapshots" not in tables
    assert con.execute("select name from inventories").fetchall() == [("Production",)]
    con.close()
