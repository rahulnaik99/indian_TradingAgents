#!/usr/bin/env python3
"""Report malformed .env lines without ever printing their values."""
from __future__ import annotations
import re, sys, io
from pathlib import Path
from dotenv.parser import parse_stream

path = Path(sys.argv[1] if len(sys.argv) > 1 else ".env")
if not path.exists():
    raise SystemExit(0)
text = path.read_text(encoding="utf-8", errors="replace")
# python-dotenv parser exposes invalid bindings with their source line. Keep output secret-safe.
errors = []
for binding in parse_stream(io.StringIO(text)):
    if binding.error:
        raw = binding.original.string.strip()
        match = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*", raw)
        key = match.group(1) if match else "UNKNOWN_KEY"
        errors.append((binding.original.line, key))
if errors:
    for line, key in errors:
        print(f"ENV SYNTAX WARNING: .env line {line}, key {key}: invalid dotenv syntax (value hidden).")
    print("Check quoting, unescaped # characters, or multiline values. No secret values were printed.")
    raise SystemExit(1)
print(".env syntax check passed.")
