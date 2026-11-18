# The public website at pendarsolutions.ir/sarfeh/

The website in `site/` (the price page and the tool's landing page), built for the `/sarfeh/` folder of
pendarsolutions.ir and served from Pendar's company server.

```text
visitor ─ Cloudflare ─ mn2-nginx (edge.conf: /sarfeh/) ─ sarfeh-site:80 (nginx, ~/sarfeh/www)
                                                          ▲
                        sarfeh-prices.timer, twice a day ─┘ rewrites ~/sarfeh/www/prices.json
```

## On the server (`~/sarfeh`)

| Path | What |
| --- | --- |
| `src/` | A clone of this repository; the refresh runs `src/scripts/refresh_prices.py` |
| `venv/` | Python for the refresh (`pip install -r src/requirements.txt -c src/constraints.txt`) |
| `state/prices-state.json` | Each provider's last good rows, so a failed read never brings back older prices |
| `www/` | The built site, and `prices.json` |
| `compose.yml`, `site.conf` | Copied from here by `deploy.sh` |

## Deploying a change to the site

```bash
deploy/sarfeh-site/deploy.sh        # build for /sarfeh/, copy it over, pull the repository, reload
```

## Prices

`sarfeh-prices.timer` runs the refresh at 03:15 and 15:15 Tehran time. To see what the last runs did, and to run one:

```bash
journalctl -u sarfeh-prices -n 80
sudo systemctl start sarfeh-prices
```

A provider whose prices moved more than the guardrail allows is held: the log says so, and nothing changes until
someone checks the page and approves it:

```bash
~/sarfeh/venv/bin/python ~/sarfeh/src/scripts/refresh_prices.py --state ~/sarfeh/state/prices-state.json \
  --out ~/sarfeh/www/prices.json --only iranserver --approve iranserver
```

Some providers can't be read from this server (Server.ir doesn't answer foreign addresses reliably, and ArvanCloud
blocks automated reading). Their last good rows stay, with the date they were read, and the page marks a price as
old after 14 days. To refresh them from somewhere that can read them, run the script there with the same state
file and copy both files back.
