# ubuntu-home-server

Lightweight, repeatable home server setup for an old laptop (reference hardware: Intel i5-8250U, 8 GB RAM, 240 GB SSD, Ubuntu Server LTS). Applications run in Docker. Two pillars:

- **Cloudflare Tunnel** publishes applications to the internet. No inbound ports on the router or host.
- **Tailscale** gives private access (SSH, administration) from your own devices only.

```
Internet --(Cloudflare Tunnel, outbound only)--> cloudflared --> example-api:3000
You --------(Tailscale: SSH)------------------> server
Firewall: zero open inbound ports (UFW deny incoming; entry only via tailscale0)
```

Default stack is only `cloudflared` plus your applications. No database runs unless you enable one. That keeps memory and disk use low enough for a few services on the reference hardware.

## Contents

| File | Purpose |
|---|---|
| [docker-compose.example.yml](docker-compose.example.yml) | Infrastructure template: `cloudflared`, plus a commented-out optional local Postgres. Setup copies it to `docker-compose.yml` (gitignored, local edits stay local) |
| `docker-compose.override.yml` | **Generated** by deploy from app manifests (gitignored, do not edit) |
| [.env.example](.env.example) | Infrastructure config template; copied to `.env` by setup |
| [scripts/setup-server.sh](scripts/setup-server.sh) | One-time setup of a fresh Ubuntu (Docker, Tailscale, UFW, secrets, cron) |
| [scripts/deploy.sh](scripts/deploy.sh) | Fetches app code, generates services, builds and starts the stack |
| [scripts/generate_compose.py](scripts/generate_compose.py) | Generates app services from `homeserver.env` manifests |
| [scripts/cloudflare_sync.py](scripts/cloudflare_sync.py) | Sets tunnel Public Hostnames via the Cloudflare API |
| [scripts/backup.sh](scripts/backup.sh) | Daily dump of the optional local Postgres to `backups/` (cron, 14-day retention, no-op without a local DB) |
| [examples/](examples) | Example application manifest to copy into an app repo |
| [manifests/](manifests) | Fallback manifests for apps without their own `homeserver.env` |
| `repos/` | Cloned application repositories land here (gitignored) |
| `env/` | Per-app values on the server: domains and secrets (gitignored) |

## Prerequisites

1. **Tailscale account** (free): <https://login.tailscale.com/start>
2. **Cloudflare account** (free) plus **a domain on Cloudflare** (nameservers pointed at Cloudflare).
3. A Git repository for each application you want to run.

---

## Step 0: Install Ubuntu Server

- During installation, select **OpenSSH Server**.
- Note the user name and password.
- After installation, log in (console or SSH from LAN).

## Step 1: Clone this repo

```bash
git clone https://github.com/your-user/ubuntu-home-server.git
cd ubuntu-home-server
```

## Step 2: Run setup

```bash
bash scripts/setup-server.sh
```

The script is idempotent. Run it as many times as needed. It:

1. Updates the system and installs base tools.
2. Installs **Docker** and adds you to the `docker` group.
3. Installs **Tailscale** and shows a link. Open it in a browser to join the server to your tailnet.
4. Enables the **firewall**: everything closed from outside, inbound only via Tailscale. If you are connected by SSH from LAN, port 22 stays temporarily open so you do not lock yourself out. After verifying SSH over Tailscale, remove it: `sudo ufw delete allow OpenSSH`.
5. **Blocks sleep**, so a laptop with a closed lid keeps serving.
6. Creates **`.env`** with a random database password (app secrets come later, in `env/<app>.env`).
7. Creates **`docker-compose.yml`** from docker-compose.example.yml if missing. Local file is gitignored, so enabling optional database or other local edits never dirty repo.
8. Sets a **daily backup cron** (03:30). It only does work when the optional local Postgres is enabled.

**If setup added you to the docker group, log out and back in.** Otherwise `docker` needs `sudo`.

From now on, connect over Tailscale: `ssh <user>@<server-name>` from any device on your tailnet.

## Step 3: Cloudflare Tunnel

1. Open <https://one.dash.cloudflare.com> -> **Networks -> Tunnels -> Add tunnel**.
2. Type: **cloudflared**. Name it, for example, `homeserver`.
3. Copy the **token** (long, starts with `eyJ...`).
4. Paste it into `.env` on the server:

   ```bash
   nano .env   # TUNNEL_TOKEN=eyJ...
   ```

5. Save. **You do not need to add Public Hostnames by hand** if you fill in the Cloudflare API values (`CF_*`) in Step 4. Deploy then sets one hostname per public application from its manifest. Without API values, deploy prints the routes to add manually here.

## Step 4: Fill in `.env`

```bash
nano .env
```

In the main `.env`, set:

- `APP_REPOS`: your applications, format `directory_name=repo_url#branch`, entries separated by commas, **no spaces**. Example: `APP_REPOS=my_app=https://github.com/your-user/your-app.git#main`. Private repo: embed a token, `https://x-access-token:<TOKEN>@github.com/your-user/your-app.git#main`.
- (Recommended) `CF_API_TOKEN`, `CF_ACCOUNT_ID`, `CF_TUNNEL_ID`, so deploy sets tunnel Public Hostnames itself. Create the API token in Cloudflare (My Profile -> API Tokens; permissions: Account: Cloudflare Tunnel Edit, Zone: DNS Edit).

`DB_PASSWORD` is already generated. Do not change it later if you use the local Postgres; Postgres only reads it on first start of an empty volume.

Per-app values do **not** go into the main `.env`. Domain, secrets, and `DATABASE_URL` go into `env/<app>.env` after the first deploy (Step 5). `WEB_URL` and `CORS_ORIGINS` are derived by deploy from the domain. You never type them by hand.

## Step 5: Deploy

```bash
bash scripts/deploy.sh
```

Deploy clones app repositories into `repos/`, generates their compose services from `homeserver.env` manifests, syncs Cloudflare Public Hostnames, builds images, starts the whole stack, and waits for healthchecks where apps have them. App database migrations, if any, are the application's own job at startup.

On the first run, deploy stops after creating `env/*.env` templates. Fill in `PUBLIC_HOSTNAME` (for example `api.example.com`) and every required secret for each app, then run deploy again.

Verify:

```bash
docker compose ps                    # everything "healthy" / "running"
curl https://api.example.com/health  # from any computer, over the internet
```

Done.

---

## Daily operations

Update applications to latest code:

```bash
bash scripts/deploy.sh                  # full deploy
bash scripts/deploy.sh --changed-only   # only apps with new commits
```

Logs:

```bash
docker compose logs -f example-api   # an application
docker compose logs -f cloudflared   # tunnel
docker compose logs -f db            # local Postgres, if enabled
```

Restart and stop:

```bash
docker compose restart example-api
docker compose down       # stop everything (data stays in volumes)
docker compose up -d      # start again
```

## Add an application

One time, in the application repository (committed):

1. Add a **`homeserver.env`** manifest in the repo root. Template: [examples/example-app.homeserver.env](examples/example-app.homeserver.env). It declares the service name, Dockerfile, port, public flag, healthcheck flag, and required variable names. No secrets.

   **Fallback:** if you cannot change the app repo, add `manifests/<directory_name>.homeserver.env` in THIS repo. Deploy uses it only when the cloned repo has no manifest of its own. The app repo manifest always wins.

One time, on the server:

2. Add the repo to `APP_REPOS` in **`.env`** (comma between entries, no spaces).
3. Run `bash scripts/deploy.sh`. Deploy creates an `env/<app>.env` template and stops. Fill in `PUBLIC_HOSTNAME` and the required secrets, then run deploy again.

That is all. Compose service, Cloudflare Public Hostname, `WEB_URL`/`CORS_ORIGINS`, and healthcheck waiting are all derived from the manifest. App variables stay isolated: a container receives only its own `env/<app>.env`, never the main `.env`.

Manifest format (plain `KEY=VALUE`, no spaces in values): `SERVICE`, `DOCKERFILE`, `CONTEXT` (default `.`), `PORT`, `PUBLIC`, `HEALTHCHECK`, `REQUIRED_ENV` (comma-separated names). Values must not contain spaces, `$`, `;`, or quotes. The generator rejects files that do.

## Databases

Deploy does not create databases and does not inject `DATABASE_URL`. Every application that needs a database sets `DATABASE_URL` explicitly in its own `env/<app>.env`, and lists `DATABASE_URL` in the manifest `REQUIRED_ENV`. The same variable works for an external database (managed Postgres, another host) and for the optional local one below.

### Optional local Postgres

One shared Postgres on the server, off by default to save RAM:

1. Uncomment the `db` service and the `volumes:` block in your local `docker-compose.yml` (created from [docker-compose.example.yml](docker-compose.example.yml) by setup; or copy it manually: `cp docker-compose.example.yml docker-compose.yml`).
2. Start it and create one database per application, once:

   ```bash
   docker compose up -d db
   docker compose exec db createdb -U postgres myapp_db
   ```

3. Set the URL in `env/<app>.env`, using the `DB_PASSWORD` value from the main `.env`:

   ```
   DATABASE_URL=postgresql://postgres:<DB_PASSWORD>@db:5432/myapp_db
   ```

4. Run `bash scripts/deploy.sh`.

Use the `postgresql://` scheme. Some validators reject the shorter `postgres://`.

### Existing installation with a running database

No data loss. The databases already live in the `homeserver_pgdata` volume:

1. Back up first: `bash scripts/backup.sh`.
2. Add `DATABASE_URL` (as above, with each app's existing database name) to every `env/<app>.env`.
3. Remove any `DB_NAME=` line from app manifests and add `DATABASE_URL` to `REQUIRED_ENV`.
4. Uncomment the `db` service and `volumes:` block. Keep compose project `name: homeserver` and volume key `pgdata` unchanged, so the existing volume reattaches.
5. Keep `DB_PASSWORD` in `.env` unchanged.
6. Run `bash scripts/deploy.sh`.

## Backups

- With the local Postgres enabled, cron dumps the **whole Postgres** (all databases) daily at 03:30 to `backups/` (gzip, 14-day retention). Log: `backups/backup.log`.
- Without a local database, the backup script prints a skip message and exits cleanly.
- Manual run: `bash scripts/backup.sh`.
- Restore:

  ```bash
  gunzip -c backups/pg-all-YYYYMMDD-HHMM.sql.gz | docker compose exec -T db psql -U postgres -d postgres
  ```

- Backups sit on the same disk as the database. Copy `backups/` elsewhere from time to time (for example `scp` over Tailscale).

Open a shell in the local database:

```bash
docker compose exec db psql -U postgres -d myapp_db
```

## Automatic deploy on new commits

Polling is simplest, with nothing exposed to the internet. Deploy detects new commits by comparing local `HEAD` with `origin/<branch>` and rebuilds only changed apps (`--changed-only`). Cron every few minutes:

```cron
*/5 * * * * cd /path/to/ubuntu-home-server && flock -n /tmp/homeserver-deploy.lock bash scripts/deploy.sh --changed-only >> backups/deploy.log 2>&1
```

The up-to-5-minute delay is the price of simplicity. Instant alternative: GitHub Actions on `push`, connecting over Tailscale (`tailscale/github-action` plus SSH, then the same `deploy.sh --changed-only`). Webhooks from GitHub do not work here: the server has no public inbound endpoint to receive them.

## Troubleshooting

| Problem | What to do |
|---|---|
| `docker: permission denied` | You did not log back in after setup. Log out/in, or run `newgrp docker`. |
| App unhealthy after deploy | `docker compose logs <service> --tail 100`. Usual cause: wrong `DATABASE_URL`, or `DB_PASSWORD` changed after first database start (the password is stored in the volume; see below). |
| Tunnel does not work | `docker compose logs cloudflared --tail 50`: bad or missing `TUNNEL_TOKEN`. Check the Public Hostname in the Cloudflare dashboard: it must be `HTTP` -> `<service>:<port>`. |
| `curl https://api...` does not answer | `docker compose ps` (is cloudflared running?), tunnel status in the Cloudflare dashboard (must be healthy), DNS (the CNAME record is created when the hostname is added). |
| Changed `DB_PASSWORD` and the database broke | Postgres reads the password only on first start of an empty volume. Restore the old password in `.env`, or run: `docker compose exec db psql -U postgres -c "ALTER USER postgres PASSWORD '<new>';"` and update every `DATABASE_URL`. |
| `deploy.sh` cannot clone a repo | Private repo: add a token to the URL in `APP_REPOS` (Step 4). Check the format: `name=url#branch`, entries comma-separated without spaces. |
| Cannot log in over SSH | The server accepts SSH only via Tailscale. Check `tailscale status` on your computer and `sudo tailscale status` on the server (from console). |
| Disk full | `docker system df`, then optionally `docker system prune` (never touches volumes). |

## Design decisions

- **Tailscale is not Cloudflare Tunnel.** Two different roles: Tailscale gives private access (SSH), Cloudflare Tunnel publishes apps publicly without opening router ports.
- **Zero open ports.** The server answers nothing from LAN/WAN except via Tailscale.
- **Build on the server from git** instead of prebuilt registry images. Simpler, works with private repos.
- **No database by default.** Each app states its `DATABASE_URL` explicitly. One optional shared local Postgres exists for apps that want it; external databases work the same way. This keeps the base stack light on 8 GB RAM.
- **Manifest instead of a manual checklist.** The app repo declares its service in `homeserver.env`; this repo derives compose, Cloudflare routing, and derived URLs from it. Secrets and domains live only on the server (`env/`). An app without its own manifest can use a fallback in `manifests/`; the app repo manifest wins.
- **No auto-updater (Watchtower).** Updating means `deploy.sh`; optionally cron with `--changed-only`. Images always build from the branch named in `APP_REPOS`.
- **Laptop with closed lid does not sleep.** Setup masks the systemd `sleep/suspend/hibernate` targets.
- **Container logs are rotated** (max 3 x 10 MB per service). They cannot fill the SSD.

## License

[MIT](LICENSE)
