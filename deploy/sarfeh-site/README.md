# The public website at pendarsolutions.ir/sarfeh/

The website in `site/` (the price page and the tool's landing page), built for the `/sarfeh/` folder of
pendarsolutions.ir and served from Pendar's company server.

```text
visitor ─ Cloudflare ─ mn2-nginx (edge.conf: /sarfeh/) ─ sarfeh-site:80 (nginx, ~/sarfeh/www)
                                                          ▲
                        sarfeh-prices.timer, twice a day ─┘ rewrites ~/sarfeh/www/prices.json
                 sarfeh-stats.timer, every ten minutes ─── counts visits from ~/sarfeh/logs, writes ~/sarfeh/stats
```

## On the server (`~/sarfeh`)

| Path | What |
| --- | --- |
| `src/` | A clone of this repository; the refresh runs `src/scripts/refresh_prices.py` |
| `venv/` | Python for the refresh (`pip install -r src/requirements.txt -c src/constraints.txt`) |
| `state/prices-state.json` | Each provider's last good rows, so a failed read never brings back older prices |
| `www/` | The built site, and `prices.json` |
| `compose.yml`, `site.conf`, `stats.py` | Copied from here by `deploy.sh` |
| `logs/access.log` | The site's requests since the last count; `stats.py` empties it every ten minutes |
| `state/stats.json` | Visit counts per day, and GitHub's numbers per day |
| `stats/` | The owner's panel, served at `/sarfeh/stats/` behind a password |
| `stats-auth/htpasswd`, `state/stats-password.txt` | The panel's password: as nginx checks it, and as the owner types it |

## Deploying a change to the site

```bash
deploy/sarfeh-site/deploy.sh        # build for /sarfeh/, copy it over, pull the repository, reload
```

## Visits: the owner's panel

https://pendarsolutions.ir/sarfeh/stats/ shows how many people opened the site, where they came from, which shared
comparisons were opened, and GitHub's stars and installer downloads. The user name is `sarfeh`, and the password is
on the server:

```bash
ssh ubuntu@159.195.110.103 cat sarfeh/state/stats-password.txt
```

`sarfeh-stats.timer` runs `stats.py` every ten minutes. It takes the access log from nginx, adds it to the counts,
deletes it, and rewrites the pages. No cookie is set and nothing runs in a visitor's browser.

- **A visitor** is counted once a day: the address without its last part plus the browser, hashed with a key that is
  thrown away when the day ends. Only counts are kept, so nobody can be recognised, the next day included.
- **Left out:** crawlers and scripts, apps drawing a link preview (counted apart, as a sign the link was pasted
  somewhere), and whoever opens the panel, for the rest of that day.
- **Installs** are GitHub's download count for each release's `install.sh`. An installation reports nothing back, so
  there is no count of servers running it.
- **GitHub's own traffic** (who looked at the repository) needs a token that may read it. Put a fine-grained token
  with read access to the repository's Administration in `~/sarfeh/state/github-token` (mode 600) and the panel
  adds it; GitHub keeps only two weeks, and the panel keeps every day it has seen.

To set a new password, or the first one on a new server:

```bash
cd ~/sarfeh && umask 077 && openssl rand -base64 18 > state/stats-password.txt \
  && printf 'sarfeh:%s\n' "$(openssl passwd -apr1 -stdin < state/stats-password.txt)" > stats-auth/htpasswd \
  && chmod 644 stats-auth/htpasswd
```

To see what the last runs did, and to run one now:

```bash
journalctl -u sarfeh-stats -n 20
sudo systemctl start sarfeh-stats
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
