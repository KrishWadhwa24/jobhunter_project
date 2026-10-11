PYTHON ?= $(shell command -v python3.12 || command -v python3.13 || command -v python3.11 || command -v python3.10 || command -v python3)
VENV := .venv
PY := $(VENV)/bin/python
CLI := $(PY) -m jobhunter.cli

.PHONY: up down ps help setup service-start service-stop service-restart service-status logs check check-problems \
        import reevaluate chat-id test-telegram serve test db-create doctor browser-setup infra-up infra-down

up:
	@./jh up

down:
	@./jh down

ps:
	@./jh ps

help:
	@echo "make up               set up and (re)start every profile in one go (./jh up)"
	@echo "make down / make ps   stop every profile / show what is running"
	@echo "make setup            one-time install (virtualenv, database, company import)"
	@echo "make service-start    start the tracker and dashboard in the background"
	@echo "make service-stop     stop it"
	@echo "make service-status   is it running, what has it found"
	@echo "make logs             follow the log"
	@echo "make check            test every company source now and print a health table"
	@echo "make check-problems   same, but only show sources with problems"
	@echo "make chat-id          finish Telegram setup (send /start to your bot first)"
	@echo "make reevaluate       re-run filters after editing config.yaml"
	@echo "make import FILE=x.csv  add companies from a CSV"
	@echo "make browser-setup    one-time: make sure a Chrome is available for browser-type companies"
	@echo "make db-create        create the jobhunter database in your Postgres (set DATABASE_URL first)"
	@echo "make doctor           check Postgres, Redis and Telegram"
	@echo "make infra-up/down    optional: start/stop a dedicated Postgres+Redis via docker compose"
	@echo "make test             run the test suite"

$(VENV)/bin/python:
	@$(PYTHON) -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" || (echo "Python 3.9 or newer is required"; exit 1)
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade pip
	$(VENV)/bin/pip install --quiet -r requirements.txt

setup: $(VENV)/bin/python
	@test -f .env || (cp .env.example .env && echo "Created .env: add your Telegram bot token there")
	@$(CLI) setup

service-start: $(VENV)/bin/python
	@$(CLI) service-start

service-stop:
	@$(CLI) service-stop

service-restart: service-stop service-start

service-status:
	@$(CLI) service-status

logs:
	@$(CLI) logs

check:
	@$(CLI) check

check-problems:
	@$(CLI) check --problems

import:
	@$(CLI) import $(FILE)

reevaluate:
	@$(CLI) reevaluate

chat-id:
	@$(CLI) chat-id

test-telegram:
	@$(CLI) test-telegram

serve:
	@$(CLI) serve

test:
	@$(PY) -m pytest -q

db-create: $(VENV)/bin/python
	@$(CLI) db-create

doctor: $(VENV)/bin/python
	@$(CLI) doctor

infra-up:
	docker compose up -d

infra-down:
	docker compose down

browser-setup: $(VENV)/bin/python
	@$(VENV)/bin/pip install --quiet -r requirements.txt
	@if [ -d "/Applications/Google Chrome.app" ] || [ -d "/Applications/Brave Browser.app" ] || [ -d "/Applications/Microsoft Edge.app" ]; then \
	  echo "Found an installed Chrome-based browser; it will be used (nothing extra to download)."; \
	else \
	  echo "No Chrome found; downloading Playwright's Chromium (about 150 MB)..."; $(VENV)/bin/playwright install chromium; \
	fi
	@$(CLI) doctor
