"""Test-session environment. get_settings() has required fields (the real
Gemini key and MCP URL) with no defaults -- tests never make a real Gemini
call or a real MCP connection, but the settings object still has to
construct successfully wherever an endpoint reads it."""

from __future__ import annotations

import os

os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")
os.environ.setdefault("ASSAY_MCP_SERVER_URL", "http://mcp.test/mcp")
