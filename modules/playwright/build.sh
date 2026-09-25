#!/usr/bin/env bash
# Runs at `kbx build`, as root: Playwright + Chromium (with its system
# dependencies) under /opt, readable by everyone.
set -euo pipefail
version="${KBX_OPT_PLAYWRIGHT_VERSION:-1.63.0}"
export PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

npm install --prefix /opt/playwright --no-audit --no-fund "@playwright/test@${version}"
/opt/playwright/node_modules/.bin/playwright install --with-deps chromium
ln -sfn /opt/playwright/node_modules/.bin/playwright /usr/local/bin/playwright
install -m 0755 "$KBX_MODULE_DIR/build/chromium" /usr/local/bin/chromium
chmod -R a+rX /opt/playwright /opt/playwright-browsers
rm -rf /var/lib/apt/lists/*
playwright --version
chromium --version
