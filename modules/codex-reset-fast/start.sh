#!/bin/sh
# Codex's /fast toggle persists `service_tier = "fast"` in config.toml, so
# every later session would silently start in Fast mode.
exec python3 "$KBX_MODULE_DIR/reset_fast.py"
