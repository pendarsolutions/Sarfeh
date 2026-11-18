# A public demo

An installation can run as a public, read-only demo. Anyone who opens it lands in a sample workspace without signing
in, can look at every page, and can't change anything. It is for showing the product to people before they install
it, and it needs an installation of its own.

## Turning it on

```bash
sudo sarfeh config set DEMO_MODE=true     # and DEMO_MODE=false to turn it off again
```

The sample workspace is made the first time someone opens the dashboard.

## What a visitor gets

- **A session without an account,** in one shared organization, «شرکت نمونه». It holds the sample setup (five
  resources at four providers, confirmed), three months of sample payments, two teams with budgets, and savings
  suggestions worked out at today's prices.
- **Every page.** Costs, comparisons and the advisor work, because they only calculate.
- **A banner** that says this is a demo, with a link to install it. When a visitor presses something that would
  change the workspace, the banner says it wasn't done.

## What can't happen

- **No change of any kind.** Every request that would change something is refused in one place in the API (it
  answers `403 read_only_demo`), whatever the page or the route. That covers payments, uploads, teams, budgets,
  settings, adding servers, and running the analysis again.
- **No signing in or signing up.** Accounts that existed before demo mode was turned on can't be used while it's on.
- **Admin pages stay closed:** members, the audit log, the command that enrols a server, the Bale bot.
- **No paid AI.** The advisor uses the offline rules, even if an API key is set.
- **Nothing is kept about visitors.** One quote is saved per price catalog, however many people open the Costs page.
  Comparisons leave no audit entry, and the advisor's questions aren't stored.

## Staying current

- **Prices** are updated by the worker twice a day, as on any installation.
- **A new month** gets its sample payments on the first visit of that month, so "this month" is never empty, and the
  monthly estimate is taken as usual.
- **Savings** are worked out again on the first visit after prices change.

## Before making one public

- **Give it its own installation,** never one that holds real data. Demo mode hides other organizations from
  visitors, but they would still share a database, backups and a server.
- **Use a domain and HTTPS** (the installer's `--domain`).
- **Leave servers, email and bots unconnected.** Nothing in a demo needs them.
- Starting a visit is limited to 30 times a minute from one address.
