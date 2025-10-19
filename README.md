# EL Estate Telegram Bot

Production-ready Telegram bot to fetch images from OLX and Otodom listings.
Features include whitelist-based access control, admin utilities, daily/weekly
usage stats, Docker/Compose setup, and CI workflow.

## Getting started

1. Create environment file:
   - `BOT_TOKEN`
   - `ADMIN_IDS` (comma-separated numeric IDs)
   - `REDIS_URL` (e.g., `redis://redis:6379/0`)
   - `SELENIUM_URL` (e.g., `http://selenium:4444/wd/hub`)
2. Run the stack:
   - `docker compose up -d --build`
3. Verify health:
   - `curl http://localhost:8080/healthz`

## Usage

- User:
  - `/start` — introduction
  - `/crop` — choose crop percentage via inline buttons
  - `/retry` — resend last processed link
- Admin:
  - `/admin` — admin menu (commands list and quick buttons)
  - `/allow id`, `/allow_username @nick`, `/allow_from_forward`
  - `/deny id`, `/allowed`, `/stats`, `/setname id Full Name`

## Implementation notes

- Remote Selenium is used; ensure the URL is reachable from the bot container.
- State and metadata are stored in Redis (FSM + whitelist/stats).
- Logs are structured JSON to stdout and `/app/logs/app.log`.
- Image directories are cleaned up after sending to limit disk usage.
