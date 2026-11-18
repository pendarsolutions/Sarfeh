# The shared Bale bot

One official Bale bot serves every FinOps agent installation that opts in, so teams don't have to make a bot of their
own. Pendar runs it on one server, the **relay**.

```text
 Bale ──getUpdates/sendMessage── relay (Pendar) ◄──outgoing long poll── installation (your server)
```

- **Nothing is sent until an admin opts in.** Under **Settings → Bale bot**, an admin gets a one-time code (eight
  characters, fifteen minutes) and sends `/start CODE` to the bot. That chat is now linked to that organization.
- **No open ports on your server.** The installation registers with the relay the first time, then keeps one
  outgoing request open (the worker, every minute) and answers the commands that arrive on it.
- **Your data stays on your server.** Reports are worked out on the installation; the relay only carries the
  finished text. It keeps which chat is linked to which installation, and messages in transit for at most ten
  minutes. An installation can only send to chats linked to it.
- **Commands are read-only:** `/report` (this month so far), `/last` (last month), `/budgets`, `/review` (invoices
  waiting to be checked), `/help`, and `/unlink`. Budget alerts, unusual changes, price alerts and the monthly report
  (on the 1st) also go to linked chats.

An installation uses the relay at `FINOPS_RELAY_URL` (`sudo sarfeh config set FINOPS_RELAY_URL=https://...`).
Without it, the Bale bot card doesn't appear, and alerts go only by email and the installation's own bot
(`FINOPS_BALE_BOT_TOKEN` and `FINOPS_BALE_CHAT_ID`), as before.

## Running the relay

On the relay server, with Docker, a domain pointing at it, and ports 80 and 443 open (Caddy gets the certificate):

```bash
sudo mkdir -p /opt/finops-relay && cd /opt/finops-relay
sudo git clone https://github.com/pendarsolutions/Sarfeh.git src
sudo cp src/deploy/relay/compose.yml src/deploy/relay/Caddyfile .
printf 'FINOPS_RELAY_BOT_TOKEN=%s\nRELAY_DOMAIN=%s\n' '<token from @botfather>' 'bot.example.ir' | sudo tee .env >/dev/null
sudo chmod 600 .env
sudo docker compose up -d
curl https://bot.example.ir/health      # {"status":"ok","bot":true,"bot_username":"..."}
```

To update: `cd /opt/finops-relay/src && sudo git pull && cd .. && sudo docker compose restart relay`. The relay's
state is one SQLite file in the `relay-data` volume. If it's lost, installations register again on their next link
code, and chats need linking again.
