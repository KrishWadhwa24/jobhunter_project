#!/bin/bash
# One-time: install Playwright and make sure a Chrome is available for the browser collector.
cd "$(dirname "$0")" || exit 1
.venv/bin/pip install --quiet -r requirements.txt || exit 1
if [ -d "/Applications/Google Chrome.app" ] || [ -d "/Applications/Brave Browser.app" ] || [ -d "/Applications/Microsoft Edge.app" ]; then
  echo "Found an installed Chrome-based browser; it will be used (nothing extra to download)."
else
  echo "No Chrome found; downloading Playwright's Chromium (about 150 MB)..."
  .venv/bin/playwright install chromium || exit 1
fi
.venv/bin/python -m jobhunter.cli doctor
