# Prices from Sarfeh's price service

From 0.6.0 an installation doesn't read the providers' sites itself. Pendar reads them twice a day (the same readers
and guardrails as before, the ones behind the [public price page](https://pendarsolutions.ir/sarfeh/prices/)),
signs the result, and publishes it on two mirrors, one in Iran and one outside. Your installation:

1. **Starts with the prices of its release day.** Each release carries the newest signed catalog (`data/feed/`),
   applied at the first start.
2. **Takes a token** from the service when it's installed (or the first time it can reach it).
3. **Checks every hour** whether newer prices are out. When they are, admins see it at the top of the dashboard,
   and the alert goes by email and Bale where those are set up. It's said once per new catalog.
4. **Updates when an admin asks**: **Catalog → Update prices**, or on the server:

   ```bash
   sudo sarfeh prices            # where prices come from, and whether new ones are out
   sudo sarfeh prices check      # ask now
   sudo sarfeh prices update     # start using the newest prices
   ```

A catalog that only says the same prices were read again is applied by itself, quietly, so the "read on" dates stay
true without anyone being asked. To apply new prices as soon as they're out as well:
`sudo sarfeh config set PRICE_AUTO_UPDATE=true`.

## What you can rely on

- **No price is taken on trust.** Every catalog is signed with Pendar's key, and your installation checks it against
  the public key built into Sarfeh before using a single row, whichever mirror it came from. A mirror, or anything
  between it and you, can't change a price; a validly signed *older* catalog is refused too, so prices can't be
  rolled back.
- **What the service gets from you:** the token and your Sarfeh version, nothing about your servers, costs, people or
  organization. The service keeps, per token, when it was first and last seen and its version, and no addresses.
- **When the service can't be reached** (no way out, a shutdown), the prices you have stay, the dashboard says when
  the last check failed, and it tries again every hour. It tries the mirror in Iran first, then the one outside.
- **Big moves:** a price move the guardrails would hold (more than 25% at once) is checked by hand at Pendar before
  it's published, so your installation applies it with the rest and records it as reviewed by the publisher.

## Reading the prices yourself instead

```bash
sudo sarfeh config set PRICE_SOURCE=scrape
```

Your server then reads the providers' pages twice a day, as before 0.6.0, and never contacts the price service. Some
providers block automated reading (ArvanCloud): those prices stay as they are until you apply a reviewed price file
(`docs/provider-adapters.md`). To use a signed catalog of your own, see `FINOPS_CATALOG_URL` in `sudo sarfeh config keys`.

The service's HTTP API, with curl examples and how to check a signature: [price-service-api.md](price-service-api.md).
Running the mirrors (for Pendar): [deploy/hub/README.md](../deploy/hub/README.md).
