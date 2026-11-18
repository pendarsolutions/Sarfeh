# Changelog

All notable changes to FinOps agent. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/). Until 1.0, a minor version can change behaviour.

## [Unreleased]

### Added

- **A public demo mode** (`DEMO_MODE=true`, for an installation of its own): anyone who opens the dashboard lands
  in a sample workspace without signing in and can look at every page, and every change is refused in one place
  in the API. See [docs/demo.md](docs/demo.md).
- **A public price page** on the website (`site/`, at `/prices/`): pick a server size and see what each provider
  charges for it, cheapest first, with the plan you get, its traffic terms, the page the price was read from and
  the date. `scripts/build_price_table.py` writes the table (`site/public/prices.json`, also offered as a download)
  from the price catalog, and a test fails when that file is out of date.
- **Spend:** a new page for what you actually paid. Record each payment by provider and month (months are Solar
  Hijri, as Iranian providers invoice), as a payment or a wallet top-up, in Toman or Rial; or name a vendor the
  catalog doesn't have. The page shows each month's total and its change from the month before, a month-by-month
  chart by provider, and the payments behind each month.
- **Invoices go to the spend ledger.** An uploaded invoice, on the Spend page or to build an inventory, becomes a
  payment to check: its provider, the amount due (converted from Rial) and the month it was issued are read off it.
  It counts once someone confirms it, and the same file uploaded twice is one payment.
- **A monthly estimate.** On the 1st of each month (and when an inventory is first confirmed) its confirmed setup is
  priced, with the VAT a provider states, and kept as that month's estimate. Past months' estimates never change;
  this month's can be taken again from the Spend page.
- **Estimated against paid,** per month and per provider. A gap over 10% is flagged (paid more, paid less, a provider
  not in the setup, or a provider with nothing paid), with what usually causes it.
- **Import spend history from CSV** (month, provider, amount; optionally currency, paid on, reference and note), in
  Persian or English headers and digits. Every row is checked first; if one is wrong, nothing is imported and each
  problem is listed with its row number.
- The sample workspace comes with three months of sample payments, and deleting it removes them with its estimates.
- **Teams as owners.** Admins add teams (or projects) in Settings; each resource, server and payment can name the
  team that owns it. A resource's team isn't a pricing input, so setting it never asks for the inventory to be
  confirmed again. Servers pass their team on to the inventory made from them, and a spend CSV can have a team column.
- **Cost by team (showback),** each month: a payment with a team is that team's; one without is shared out by each
  team's part of that provider's estimate; what can't be shared stays on its own **Unallocated** line. Every line
  adds up to exactly what was paid.
- **Budgets** for the whole organization and for each team: paid so far this month against each, and a projected
  month-end (the largest of paid so far, this month's estimate and last month's total).
- **Budget alerts** by email or Bale at 80% and at 100%, each once a month, in the organization's language.
- **Unusual changes:** a provider or team whose month moves by more than a set percent (30% by default) and amount
  (1,000,000 Toman) from the month before is shown on the Spend page, and last month's arrive as an alert on the 5th.
- **The setup estimates come from** is chosen once in Settings (by default the one confirmed last, preferring a
  real setup to the sample), so the Spend page, budgets and alerts all use the same one.

### Changed

- **The public price table is live.** `scripts/refresh_prices.py` reads every provider's page with the same adapters
  and guardrails as an installation, keeps each provider's last good prices when a page can't be read, and writes
  `prices.json`; on pendarsolutions.ir/sarfeh/ it runs twice a day (`deploy/sarfeh-site`). A price read more than 14
  days ago is marked on the page.
- The website is called **Sarfeh** (صرفه), and can be built for a folder (`SITE_BASE=/sarfeh/`).
- **Editing an inventory no longer takes its cost pages away.** Costs, savings, comparison and reports keep using
  the version confirmed last, with a note, until the changes are confirmed.

### Fixed

- **IranServer prices were read with CPU and memory swapped** after its page moved the memory column ahead of the
  CPU column: NGP-large80 came out as 32 vCPU and 4 GB. Columns are now found by their headers.
- **Chabokan's cheapest plan («ارزان», 1 vCPU, 2 GB) was missing:** a hosting package just before it on the page made
  both unreadable. Chabokan's Mashhad plans are gone from its page, so they no longer count as expected.
- **MobinHost plans whose capacity is used up** («ظرفیت به پایان رسید») counted as available. They are sold out, and
  the price page leaves out plans that aren't available.

## [0.3.1] — 2026-11-16

### Fixed

- **Background jobs failed on 0.3.0 installations:** price updates (so "Update now" under Integrations returned an
  error) and server collection. The 0.3.0 image picked up a new release of Celery's queue library, kombu 5.7.0,
  which needs a package it doesn't install. Every dependency is now locked in `constraints.txt`, and CI builds the
  image and has its worker take a job through Redis.
- **An inventory couldn't be deleted.** It now has a Delete button. Deleting the sample workspace also removes the
  sample engineer day rate it set, so you really start over.
- **The sample workspace showed a developer's note** in English. It now explains itself in Persian or English and has
  a Persian name, and Settings labels its day rate in the reader's language.
- **A 100% confidence showed as 00%** on the review page: the number now fits inside its ring.

### Added

- `finops-agent doctor` checks that the background worker answers, not only that its container runs.
- Starting a price update while the worker can't be reached says so, instead of failing with error 500.

## [0.3.0] — 2026-11-09

A team can now run its own installation without touching the database, and the interface follows the Pendar design
system.

### Added

- **One-line install:** `curl -fsSL https://github.com/pendarsolutions/FinOps/releases/latest/download/install.sh | sudo bash`.
  Each release attaches a one-file, readable `install.sh` that needs nothing else. In a terminal it asks for the
  domain, port, nightly backups, email and Bale; with options (`--domain`, `--set KEY=VALUE`, `--yes`...) it asks
  nothing, for scripts and Ansible.
- **The `finops-agent` command** runs an installation: `status`, `doctor` (checks DNS, the certificate, disk,
  backups and updates, and says how to fix each problem), `config set/get/unset/keys` (checks values, keeps secrets
  out of shell history, restarts only what changed), `address`, `upgrade` (backs up first; works offline with
  `--bundle`), `logs`, `restart`, `backup`, `restore`, `reset-password`, `test-alerts`, `uninstall`.
- **Behind your own reverse proxy:** `--proxy https://finops.example.ir --listen 127.0.0.1:8080` serves plain HTTP to
  nginx, Traefik or a load balancer that already handles HTTPS.
- **First-account setup:** a fresh installation opens on creating the owner's account.
- **Backups.** A nightly backup at 03:30 that keeps the newest 14, and one command to back up or restore
  (`/opt/finops-agent/backup.sh`). A backup holds the database, the uploaded invoices and the settings with the keys
  that decrypt stored SSH keys. A restore asks first, backs up the current data before changing anything, and keeps
  the settings that belong to the machine. `--no-daily-backup` turns the nightly backup off.
- **Getting back in.** With email set up, "Forgot your password?" sends a one-time link that works for 30 minutes.
  Without email, an admin sets a temporary password, and a server command resets a locked-out owner's password.
- **Members.** Admins change roles and remove members. They manage only roles below their own, and nobody changes
  their own membership. Removed members lose access at once and can be added back.
- **Change your password** under Settings. It signs you out everywhere else.
- **Motion** from Pendar's motion guideline. The wordmark is laid brick by brick, page blocks arrive in reading
  order, and chart bars are laid instead of stretched. With reduced motion turned on, everything is in place at once.

### Changed

- **An installation serves the dashboard only.** The landing page and the public "how it works" page moved to the
  public website, a separate project in `site/` with the install command and links to the code. Signed-out visitors
  get sign-in, and old `/welcome` and `/how-it-works` addresses redirect.
- Every `FINOPS_` setting in `/opt/finops-agent/.env` now reaches the app, so new settings need no compose change.
- **Pendar design system** (version 3): Estedad replaces Vazirmatn, with Pendar's colours, icons, wordmark and
  loader. Dark mode follows the system.
- **Solar Hijri dates** throughout the Persian interface (۱۴۰۵/۰۸/۱۷), with moments shown in the reader's time.
- **The Persian report is Persian:** headers, status words, Persian digits and separators, and Solar Hijri dates.
- Price update statuses are written out ("Updated", "Held for review") instead of `OK` and `FAILED`, and common
  error messages appear in Persian.
- Form fields line up at the top, with each form's button on its own row.
- The installer writes `FINOPS_PUBLIC_URL` (used for links in emails) and remembers the address in
  `FINOPS_ADDRESS`.

### Fixed

- Running the 0.2.0 installer again without `--domain` switched a domain installation to its IP address.
- `sudo ./install.sh` failed with "Permission denied" on a fresh clone: the scripts weren't executable in git.
- A password change didn't end sessions that were already signed in.
- On a development server, the welcome tour opened on every page load, even after it was dismissed.

### Upgrading

Run the new installer as before. The database is upgraded when the app starts, the nightly backup is set up, and
`FINOPS_PUBLIC_URL` is added to your settings from your domain or IP address.

## [0.2.0] — 2026-11-05

First public release.

### Added

- **Server monitoring.** Add servers from the dashboard and their real CPU, memory and disk use is read over SSH. The
  key can run only one read-only script, server identities are pinned, and a changed identity stops collection until
  an admin approves it. The Servers page shows each server's cost, unused capacity and renewal date.
- **Installer that works next to other services.** If ports 80 and 443 are taken, it picks a free port and leaves
  the existing services alone. It also takes a registry mirror, and an offline bundle is attached to every release.
- **More providers with automatic prices.** Scrapers for Chabokan, MobinHost and ParsPack, checked twice a day.
  ParsPack is priced exactly from a server's specs using the daily price table behind its official calculator.
  ArvanCloud prices are updated from reviewed price files, because its site blocks scraping.
- **Alerts** by email and Bale for price changes, failed updates, price moves held for review, and renewals.
- **Shared, signed price catalog,** so one installation can scrape for the others.
- **"How it works" page** that explains every calculation with worked examples. Tests check every number on it.
- **Plain-language suggestions.** Each one says what changes, what to ask the new provider before moving, and how
  long a move takes to pay for itself.
- Release workflow: tests, an offline bundle with checksums, and images on GitHub Container Registry for every tag.

### Changed

- Renamed to FinOps agent. Consulting referrals and unused credential storage were removed.
- Apache-2.0 license, contributor guide, security policy and an issue template for reporting a wrong price.
- Safer defaults: development ports listen only on your own computer, sign-up closes after the first account, and
  sign-in is rate limited.
- The Persian interface no longer shows raw field names, internal codes or JSON, and the Vazirmatn font is bundled
  instead of loaded from Google Fonts, which is unreliable from Iran and doesn't work offline.
- The Compare table shows only the rows where an option differs from your current server.
- CI runs on every push and pull request.

### Fixed

- Re-importing the starting catalog on restart overwrote newer scraped prices.
- A sync that found unchanged prices didn't count as a check, so prices read twice a day were shown as getting old.
- Upgrades kept serving the old Caddy configuration.
- An offline bundle couldn't be built on Windows, and a bundle built at another version or image prefix looked for
  images it didn't contain.

### Known limitations

- Prices are public list prices. Discounts, negotiated rates and fees not on the price page aren't included, and the
  totals haven't yet been checked against many real invoices. Please [report a wrong price](../../issues/new?template=wrong-price.md).
- When a provider changes a plan's specs but keeps its name, a server bought on the old plan is priced at the new
  plan's price, and the app doesn't warn about the mismatch yet.
- Price guardrails hold a sync for review when the median price moves more than 25%. A single plan that jumps is
  applied without review.
- ArvanCloud: no hourly proration rule, traffic direction unverified, prices updated by hand.
- Plan-based providers rarely publish prices for extra traffic, extra disk or extra IPv4 addresses. Those parts show
  as price unavailable, and one IPv4 address is assumed to be included (the quote says so).
- ParsPack's calculator doesn't price traffic or IPv4, and its one-time setup fee isn't part of the monthly figure.
- Liara PaaS and DBaaS traffic and Liara extra disk prices are unknown. Server.ir term discounts are unverified and
  never applied. Providers' invoice rounding rules are unknown, so the engine doesn't round.
- Downsizing suggestions need 14 days of measured usage.

## [0.1.0] — 2026-10-04

Internal MVP build. It was never published; the sections below record how it was built.

### Phase 0 — Bootstrap ✅
Monorepo layout, Docker Compose (Postgres 16, Redis, API, worker, beat, web), Alembic migration
(+ Postgres immutability triggers), idempotent seed import, JWT auth with orgs and roles, GitLab CI
(lint, tests, Postgres migration + double seed, web build, images), `.env.example`, health/readiness,
JSON logs.

### Phase 1 — Pricing core ✅
Versioned catalog (close + insert, never overwrite), Decimal-only engine with component / Liara plan /
fixed-SKU / discovery-only strategies, tax as a separate line, PRICE_UNAVAILABLE semantics, golden tests
for every seeded provider family and hypothesis property tests.

### Phase 2 — Inventory ✅
Guided builder API, review/confirmation gate with required-field rules per pricing shape and
"I don't know" acknowledgement, immutable quotes, source freshness (incl. Jalali price dates).

### Phase 3 — Comparison + recommendations ✅
Equivalence engine (hard constraints, weighted fit, confidence caps, explicit differences, Iran-only
default), REC-001..005, non-double-counted avoidable spend.

### Phase 4 — AI ✅
Chat → InventoryDraft (Claude structured output, or deterministic fa/en parser offline), invoice
(PDF/CSV) → draft with Rial/Toman detection and redaction, tool-calling advisor with numeric guard and
template fallback, `ai_runs` logging, per-user rate limits.

### Phase 5 — Provider sync ✅ (with one documented blocker)
Live parsers for Liara, Server.ir, IranServer against real pages; ArvanCloud blocked by a bot
challenge (fails visibly); guardrails + quarantine approval; nightly Celery schedule.
Live sync on 2026-10-04: Liara 23 rows / 0 changes, IranServer 5 / 0, Server.ir 24 / 5 changes
(IRFH download now metered at 1,600 Toman/GB; IRFH80612 delisted).

### Phase 6 — Pilot hardening (partial)
Done: bilingual HTML report, audit log, demo workspace, upload validation/signed URLs/retention,
encrypted read-only credentials, structured logs, deployment docs.
Web UI: all 9 sections, fa (RTL) / en, review table, compare matrix, catalog with history, advisor panel; smoke-tested against the live API and rendered at 390 px width.
Not done: PDF report (phase 2 by spec), OIDC, external alert routing (alerts are structured ERROR logs),
load testing. (pytest runs on SQLite; the Postgres path — migration, triggers, seed, full API flow, Celery sync — was verified via docker compose.)

### Definition of Done

| # | Item | Status |
|---|---|---|
| 1 | Import seed catalog | ✅ `make seed-pricing` (idempotent; tested) |
| 2 | Manually create an Iranian inventory | ✅ builder API + UI |
| 3 | Must confirm before analysis | ✅ 409 until confirmed; edits un-confirm |
| 4 | Exact costs for supported rows | ✅ golden tests |
| 5 | Compare a VM against alternatives | ✅ |
| 6 | Show why an alternative is / is not equivalent | ✅ constraints + differences + rejected list |
| 7 | Evidence-backed opportunities | ✅ |
| 8 | Chat → draft in Persian or English | ✅ (offline parser and Claude path) |
| 9 | Invoice → draft with uncertainty | ✅ PDF/CSV; images need OCR (not available — returns a clear 422) |
| 10 | AI cannot produce an unsupported number | ✅ guard + tests |
| 11 | Every price has source/freshness | ✅ |
| 12 | Sync failures visible | ✅ |
| 13 | Unit/golden tests pass | ✅ 117 Python tests + web typecheck/build |
| 14 | Full stack with one command | ✅ `docker compose up --build` verified: Postgres 16, Redis, api, worker, beat, web |
| 15 | README: setup, architecture, production deployment | ✅ |
