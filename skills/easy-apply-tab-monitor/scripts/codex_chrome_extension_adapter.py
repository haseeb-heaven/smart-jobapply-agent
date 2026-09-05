#!/usr/bin/env python3
"""Deprecated Codex Chrome alias for the generic NDJSON stdio bridge.

The implementation lives in :mod:`browser_bridge_adapter` as
``StdioBridgeAdapter``. This module preserves the historical import path so
existing hosts and tests keep resolving.

Listing-URL validation (``canonical_listing_url``) lives in
:mod:`browser_tab_adapter` and is re-exported through
:mod:`browser_bridge_adapter`; it is intentionally not re-exported here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

try:
    from browser_bridge_adapter import BrowserAdapterError, StdioBridgeAdapter
except ModuleNotFoundError:
    _BRIDGE_PATH = Path(__file__).with_name("browser_bridge_adapter.py")
    _BRIDGE_SPEC = importlib.util.spec_from_file_location("browser_bridge_adapter", _BRIDGE_PATH)
    if _BRIDGE_SPEC is None or _BRIDGE_SPEC.loader is None:
        raise RuntimeError("generic browser bridge is unavailable") from None
    _BRIDGE_MODULE = importlib.util.module_from_spec(_BRIDGE_SPEC)
    sys.modules[_BRIDGE_SPEC.name] = _BRIDGE_MODULE
    _BRIDGE_SPEC.loader.exec_module(_BRIDGE_MODULE)
    BrowserAdapterError = _BRIDGE_MODULE.BrowserAdapterError
    StdioBridgeAdapter = _BRIDGE_MODULE.StdioBridgeAdapter


CodexChromeExtensionAdapter = StdioBridgeAdapter

__all__ = ["BrowserAdapterError", "CodexChromeExtensionAdapter", "StdioBridgeAdapter"]
