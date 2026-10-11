# Jobhunter

Tracks careers pages and sends you the roles that fit, with three actions per job:
**Apply**, **Find referrers**, **Already applied**. Runs on your Mac, only while you start it.

## First time

```
make setup                 # virtualenv, database, imports data/companies.csv
```

Open `.env` and set `TELEGRAM_BOT_TOKEN` (from @BotFather). Then:

```
make chat-id               # send /start to your bot; this saves TELEGRAM_CHAT_ID for you
make check                 # tests every company source now; shows which ones work
make service-start         # starts everything; dashboard at http://127.0.0.1:8787
```

## Using your existing Docker Postgres and Redis

By default the app uses a local SQLite file and runs without Redis. To use your containers instead, run `docker ps`
to see the host ports, then put these in `.env` (never paste the password anywhere else):

```
DATABASE_URL=postgresql://USER:PASSWORD@127.0.0.1:5432/jobhunter
REDIS_URL=redis://127.0.0.1:6379/0
```

```
make db-create             # creates an empty `jobhunter` database next to your others; touches nothing else
make doctor                # confirms Postgres, Redis and Telegram are reachable
make setup
```

Redis is used for a cross-process lock (so two cycles never run at once) and the apply queue. Postgres holds companies,
jobs and history. No Docker containers are started or changed by this project unless you run `make infra-up`.

## Everyday

| Command | What it does |
|---|---|
| `make service-start` / `service-stop` | start / stop the tracker and dashboard |
| `make service-status` | running or not, last check, what it has found |
| `make logs` | follow the log |
| `make check` | health table for every company (`make check-problems` shows only the bad ones) |
| `make reevaluate` | re-run the filters after you edit `config.yaml` |
| `make import FILE=x.csv` | add companies (or use the Companies page) |
| `make test` | run the tests |

When your Mac wakes from sleep the service notices within about 30 seconds and catches up.

## Looking for other levels (SDE-2, SDE-3 ...)

Every India software role is stored with a level (Intern, SDE-1, SDE-2, SDE-3, Staff+).
To alert on more levels, edit `config.yaml`:

```yaml
targets:
  levels: [sde1, sde2, sde3]
```

then `make reevaluate`. You can also browse any level on the dashboard right now: choose
**All roles** and toggle the level chips. Titles, locations and the batch rules are all in `config.yaml`.

## What works today and what does not

- Reads: Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Jibe, Eightfold, Oracle HCM,
  Recruiterbox, Amazon, Atlassian. About 125 of the 198 active companies.
- Companies on Keka, Darwinbox, TurboHire, Kula and plain HTML pages show as **Needs browser**.
  They are listed but skipped until the browser collector is built.
- Apply does not fill forms yet: it marks the job and gives you the link. Auto-fill and resume
  tailoring are the next build steps.
- Find referrers gives LinkedIn and Google search links plus message drafts. It does not scrape LinkedIn.
- Source adapters were written from each platform's public API shape. `make check` is the real
  test: fix or pause whatever it reports as failing.

## Privacy

Secrets live only in `.env`, which is git-ignored. Nothing leaves your Mac except requests to the
careers sites and Telegram.


## Using a profile from a phone

`./jh up` starts every profile plus one public sign-in port (8780). Add users with `./jh users add EMAIL --profile NAME`,
point a Cloudflare Tunnel at port 8780 (optionally behind a Vercel address) and add the link to the phone's home screen.
See `deploy/README.md`.
