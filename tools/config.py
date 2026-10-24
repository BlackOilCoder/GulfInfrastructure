"""Shared filesystem configuration for native Python and container entry points."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def configured_path(name, fallback):
    return Path(os.environ.get(name) or fallback).expanduser().resolve()


def data_directory():
    return configured_path("BLUEWATER_DATA_DIR", ROOT / "data")


def structures_file():
    return configured_path("BLUEWATER_STRUCTURES_FILE", ROOT / "platform-structures.js")


def production_cache_directory():
    return configured_path("BLUEWATER_PRODUCTION_CACHE_DIR", data_directory() / "production" / "cache")
