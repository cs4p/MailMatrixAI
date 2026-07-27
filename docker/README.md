# MailMatrix Local IMAP (Dovecot)

A lightweight Dovecot IMAP container for local development and testing. Plain IMAP on port 143 — no TLS, no complexity. Pair it with `seed_dovecot.py` (in the project root) to pull real email from Fastmail into the container.

## Requirements

- Docker and Docker Compose
- Python 3.9+ (for the seed script)

## Quick start

```bash
# From the docker/ directory
docker compose up -d
```

The container is ready when `docker compose logs` shows:
```
Dovecot v2.3.x starting up for imap
```

Default credentials: `testuser` / `testpass` on `localhost:143`.

## Seeding with real email

Run the seed script from the project root to pull 500 messages from Fastmail and APPEND them to the local container:

```bash
FASTMAIL_USER=you@fastmail.com \
FASTMAIL_PASSWORD=your-app-password \
python seed_dovecot.py
```

Progress is printed every 50 messages. A summary is printed on completion.

### Seed script options

All options are set via environment variables:

| Variable            | Default      | Description                                  |
|---------------------|--------------|----------------------------------------------|
| `FASTMAIL_USER`     | *(required)* | Fastmail login email                         |
| `FASTMAIL_PASSWORD` | *(required)* | Fastmail app password                        |
| `FASTMAIL_COUNT`    | `500`        | Number of messages to fetch                  |
| `SOURCE_FOLDER`     | `INBOX`      | Fastmail folder to pull from                 |
| `DOVECOT_HOST`      | `localhost`  | Local container host                         |
| `DOVECOT_PORT`      | `143`        | Local container port                         |
| `DOVECOT_USER`      | `testuser`   | Local IMAP username                          |
| `DOVECOT_PASSWORD`  | `testpass`   | Local IMAP password                          |

## Pointing MailMatrixAI at the container

Set these in your `.env` (or export them) before running any MailMatrixAI script:

```
IMAP_SERVER=localhost
IMAP_PORT=143
IMAP_USERNAME=testuser
IMAP_PASSWORD=testpass
```

Then run normally:

```bash
python app.py
python sortEmail.py
python emailSummary.py
```

## Configuration

### Custom credentials

Pass `DOVECOT_USER` and `DOVECOT_PASSWORD` to `docker compose`:

```bash
DOVECOT_USER=myuser DOVECOT_PASSWORD=mypass docker compose up -d
```

Or add them to a `.env` file next to `docker-compose.yml`:

```
DOVECOT_USER=myuser
DOVECOT_PASSWORD=mypass
```

### Mail persistence

Mail is stored in a named Docker volume (`maildata`). It survives container restarts. To wipe all mail and start fresh:

```bash
docker compose down -v
docker compose up -d
```

### Port conflict

If port 143 is in use, change the host-side port in `docker-compose.yml`:

```yaml
ports:
  - "1143:143"   # use localhost:1143 instead
```

Then set `DOVECOT_PORT=1143` when running the seed script and `IMAP_PORT=1143` in `.env`.

## How it works

- Base image: `debian:bookworm-slim` + `dovecot-imapd`
- Auth: passwd-file (`/etc/dovecot/users`), PLAIN scheme, no TLS
- Mail storage: Maildir under `/home/<user>/Maildir`
- Folders use `/` as the hierarchy separator (matches Fastmail and Gmail conventions)
- SSL is disabled — suitable for local development only

## Stopping and cleanup

```bash
# Stop (keeps mail volume)
docker compose down

# Stop and delete all mail
docker compose down -v

# Remove the image
docker rmi mailmatrix-imap-test
```
