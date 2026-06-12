"""Tiny .env loader (no external dependency).

Reads KEY=VALUE lines from a `.env` file at the project root and puts them into
os.environ (without overwriting variables already set in the real environment,
so the OS/shell always wins). Call load_env() once at startup before anything
reads configuration.
"""
import os

ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")


def load_env(path=ENV_PATH):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
