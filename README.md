# tghoard

*Hoard media from Telegram.*

Self-hosted bulk media downloader for Telegram chats. Web UI, several accounts,
history scan + live watch of new messages, resumable downloads that never leave
half-written files under their final name.

## Run

```sh
cp .env.example .env   # put TELEGRAM_API_ID / TELEGRAM_API_HASH from https://my.telegram.org/apps
docker compose up -d --build
```

Open http://localhost:8000, add an account (phone → code → 2FA password if set),
then pick chats on the **Chats** page.

Without Docker:

```sh
uv venv --python 3.14 && uv pip install -e ".[dev]"
alembic upgrade head
python -m app.main
```

## How it works

- **Accounts** — one Telethon client per account, sessions in `DATA_DIR/sessions/`.
- **Chats → Scan** walks the history with a server-side media filter (videos by default)
  and records every matching file in SQLite with status `new`. `Rescan` starts from the
  beginning; already known messages are skipped. **watch** subscribes to new messages,
  **auto-queue** puts found files straight into the download queue.
- **Files** — queue / unqueue / retry, per chat or in bulk. Each account downloads
  `CONCURRENCY_PER_ACCOUNT` files at a time.
- Downloads go to a per-process temporary directory (`tempfile.TemporaryDirectory`, honours
  `TMPDIR`), are resumed from that offset on retries, verified against the size Telegram
  reports, `fsync`ed and only then moved into place. Existing files are never overwritten.
- The final path is a `str.format` template relative to `DOWNLOAD_DIR` — global default
  `PATH_TEMPLATE`. Placeholders: `{chat}` `{chat_id}` `{msg_id}` `{doc_id}`
  `{date}` (`{date:%Y-%m}` works) `{name}` `{stem}` `{ext}` `{kind}` `{caption}` `{caption_line}`.
  Album captions are copied to every file of the album. Examples:
  `{chat}/{msg_id}_{name}` (default), `{chat}/{date:%Y}/{caption_line} - {name}`.
- `FLOOD_WAIT` under a minute is slept through by Telethon; longer ones pause the account for
  exactly the requested time. Other errors retry with backoff up to `MAX_ATTEMPTS`, then the
  file is marked `failed` with the error.

## Settings (`.env`)

| Variable | Default | |
|---|---|---|
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | — | required |
| `DATA_DIR` | `data` | DB + sessions |
| `DOWNLOAD_DIR` | `downloads` | media root |
| `PATH_TEMPLATE` | `{chat}/{msg_id}_{name}` | default file path template, see above |
| `CONCURRENCY_PER_ACCOUNT` | `3` | parallel files per account |
| `MAX_ATTEMPTS` | `5` | retries before `failed` |
| `PROXY` | — | `socks5://user:pass@host:port`, `http://host:port`, `mtproxy://host:port/secret` |
