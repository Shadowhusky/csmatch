"""App-level tests for display-mode discoverability: the `w` binding is
visible in the footer, the title names the current mode, and pressing
`w` cycles default → narrative → monitor → default."""

from __future__ import annotations

import pytest

from csmatch.sources.mock import MockSource
from csmatch.tui import CsMatchApp


@pytest.mark.asyncio
async def test_w_binding_is_discoverable_and_cycles_modes():
    app = CsMatchApp(MockSource())
    async with app.run_test(size=(120, 40)) as pilot:
        # Footer hint: the w binding is shown (not hidden).
        w_binding = next(b for b in app.BINDINGS if getattr(b, "key", None) == "w")
        assert w_binding.show is True
        assert w_binding.description == "view"

        # Title names the current mode.
        assert app._view_mode == "default"
        assert "default view" in app.title

        await pilot.press("w")
        assert app._view_mode == "narrative"
        assert "narrative view" in app.title
        assert app._detail is not None and app._detail._view_mode == "narrative"

        await pilot.press("w")
        assert app._view_mode == "monitor"
        assert "monitor view" in app.title
        assert "build-monitor" in app.title  # vocab re-skins the app title

        await pilot.press("w")
        assert app._view_mode == "default"
        assert "default view" in app.title


@pytest.mark.asyncio
async def test_default_theme_is_ansi_dark():
    app = CsMatchApp(MockSource())
    async with app.run_test(size=(100, 30)):
        assert app.theme == "ansi-dark"
        assert app.current_theme.dark is True
