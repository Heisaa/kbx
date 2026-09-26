"""Untrusted text from a sandbox, made safe for a terminal or a notification."""

from __future__ import annotations

import re

# C0, DEL, C1, and the bidirectional overrides that could reorder a line.
UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def clean(text: object) -> str:
    """Printable text on one line: sandbox output must not drive the terminal."""
    return UNSAFE.sub("?", str(text).replace("\t", "  ").replace("\r", "").replace("\n", " "))
