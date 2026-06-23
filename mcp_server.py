"""
Stealth browser MCP server — expose CloakBrowser tools over MCP.

Usage:
    uv run python mcp_server.py              # stdio (for Claude Desktop / agent integration)
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.session import ServerSession

from main import RefStore, TabManager, take_snapshot, _wrap_page_content

load_dotenv()


class BrowserState:
    """Mutable container for the browser, created/destroyed on demand."""

    def __init__(self) -> None:
        self.browser: Any | None = None
        self.tabs: TabManager | None = None
        self.refs: RefStore | None = None

    @property
    def is_open(self) -> bool:
        return self.browser is not None

    async def open(self, headed: bool = False) -> str:
        if self.is_open:
            return "Browser is already open."
        from cloakbrowser import launch_async

        self.browser = await launch_async(headless=not headed)
        context = await self.browser.new_context(viewport={"width": 1280, "height": 720})
        first_page = await context.new_page()
        await first_page.goto("about:blank")
        self.tabs = TabManager(context)
        self.tabs.add(first_page)
        self.refs = RefStore()
        mode = "headed" if headed else "headless"
        return f"Browser opened ({mode})."

    async def close(self) -> str:
        if not self.is_open:
            return "Browser is not open."
        await self.browser.close()
        self.browser = None
        self.tabs = None
        self.refs = None
        return "Browser closed."

    def require(self) -> tuple[TabManager, RefStore]:
        if not self.is_open:
            raise RuntimeError("Browser is not open. Call open_browser first.")
        return self.tabs, self.refs  # type: ignore[return-value]


_state = BrowserState()

mcp = FastMCP("stealth-browser")


# ---------------------------------------------------------------------------
# Browser lifecycle
# ---------------------------------------------------------------------------

@mcp.tool()
async def open_browser(headed: bool = False) -> str:
    """Launch the stealth browser. Must be called before any other browser tool.
    Set headed=true to show the browser window."""
    return await _state.open(headed=headed)


@mcp.tool()
async def close_browser() -> str:
    """Close the browser and free resources. Call when you're done with browser tasks."""
    return await _state.close()


# ---------------------------------------------------------------------------
# Snapshot & content
# ---------------------------------------------------------------------------

@mcp.tool()
async def snapshot() -> str:
    """Get a compact accessibility snapshot of the page with short refs (@e1, @e2...)
    for each interactive element. Use these refs in click, fill, select, check,
    get_text. This is your PRIMARY tool for understanding the page — much cheaper
    than a screenshot. Refs are invalidated after page changes; re-snapshot to get
    fresh ones."""
    tabs, refs = _state.require()
    tree = await take_snapshot(tabs.page, refs)
    header = f"Page: {tabs.page.url} | {refs.count} interactive refs"
    return f"{header}\n\n{_wrap_page_content(tree)}"


@mcp.tool()
async def get_text(ref: str) -> str:
    """Get the text content of an element by ref (@e1) or CSS/XPath selector."""
    tabs, refs = _state.require()
    selector = refs.resolve(ref)
    loc = tabs.page.locator(selector).first
    text = await loc.text_content(timeout=5000)
    return _wrap_page_content((text or "").strip() or "(empty)")


@mcp.tool()
async def get_page_content(
    selector: str | None = None,
    max_length: int = 10000,
) -> str:
    """Extract readable text from the page or a section. Use for articles,
    emails, posts, docs, search results. Optionally scope to a CSS selector."""
    tabs, refs = _state.require()
    page = tabs.page
    if selector:
        text = await page.locator(selector).first.inner_text(timeout=5000)
    else:
        text = await page.inner_text("body", timeout=5000)
    text = text.strip()
    if len(text) > max_length:
        text = text[:max_length] + "\n\n... (truncated)"
    return _wrap_page_content(text or "(no text content)")


# ---------------------------------------------------------------------------
# DOM interaction (ref-based)
# ---------------------------------------------------------------------------

@mcp.tool()
async def click(ref: str) -> str:
    """Click an element by ref (@e1) or selector. Use snapshot first to get refs."""
    tabs, refs = _state.require()
    selector = refs.resolve(ref)
    await tabs.page.locator(selector).first.click(timeout=5000)
    await asyncio.sleep(0.3)
    await tabs.sync_new_popups()
    return f"Clicked {ref}"


@mcp.tool()
async def fill(ref: str, value: str) -> str:
    """Clear an input and type new text. Use ref (@e3) or selector."""
    tabs, refs = _state.require()
    selector = refs.resolve(ref)
    await tabs.page.locator(selector).first.fill(value, timeout=5000)
    return f"Filled {ref} with: {value}"


@mcp.tool()
async def select(ref: str, value: str) -> str:
    """Select an option from a dropdown by ref or selector."""
    tabs, refs = _state.require()
    selector = refs.resolve(ref)
    loc = tabs.page.locator(selector).first
    try:
        await loc.select_option(value=value, timeout=5000)
    except Exception:
        await loc.select_option(label=value, timeout=5000)
    return f"Selected '{value}' in {ref}"


@mcp.tool()
async def check(ref: str, checked: bool = True) -> str:
    """Check or uncheck a checkbox/radio by ref or selector."""
    tabs, refs = _state.require()
    selector = refs.resolve(ref)
    loc = tabs.page.locator(selector).first
    if checked:
        await loc.check(timeout=5000)
    else:
        await loc.uncheck(timeout=5000)
    return f"{'Checked' if checked else 'Unchecked'} {ref}"


# ---------------------------------------------------------------------------
# Vision tools (coordinates — fallback)
# ---------------------------------------------------------------------------

@mcp.tool()
async def screenshot() -> Image:
    """Take a screenshot. Use ONLY when you need visual info — CAPTCHAs,
    canvas, bot challenges, visual layout. Prefer snapshot for normal pages."""
    tabs, refs = _state.require()
    png_bytes = await tabs.page.screenshot(full_page=False)
    return Image(data=png_bytes, format="png")


@mcp.tool()
async def click_xy(x: int, y: int, description: str = "") -> Image:
    """Click at pixel coordinates. For CAPTCHAs, canvas, bot-protected elements.
    Take a screenshot first to find coordinates. Returns follow-up screenshot."""
    tabs, refs = _state.require()
    await tabs.page.mouse.click(x, y)
    await asyncio.sleep(0.5)
    await tabs.sync_new_popups()
    png_bytes = await tabs.page.screenshot(full_page=False)
    return Image(data=png_bytes, format="png")


@mcp.tool()
async def type_xy(x: int, y: int, text: str, clear_first: bool = True) -> Image:
    """Click at coordinates then type. For non-standard inputs.
    Returns follow-up screenshot."""
    tabs, refs = _state.require()
    page = tabs.page
    await page.mouse.click(x, y)
    await asyncio.sleep(0.15)
    if clear_first:
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.05)
    await page.keyboard.type(text, delay=50)
    await asyncio.sleep(0.3)
    png_bytes = await page.screenshot(full_page=False)
    return Image(data=png_bytes, format="png")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

@mcp.tool()
async def goto(url: str) -> str:
    """Navigate the current tab to a URL."""
    tabs, refs = _state.require()
    await tabs.page.goto(url, wait_until="domcontentloaded")
    title = await tabs.page.title()
    return f"Navigated to {url} — {title}"


@mcp.tool()
async def navback() -> str:
    """Navigate back (browser back button)."""
    tabs, refs = _state.require()
    await tabs.page.go_back(wait_until="domcontentloaded")
    return f"Back — {tabs.page.url}"


# ---------------------------------------------------------------------------
# Keyboard / scroll / wait
# ---------------------------------------------------------------------------

@mcp.tool()
async def keys(method: str, value: str) -> str:
    """Keyboard input. method='press' for Enter, Tab, Escape, Backspace,
    ArrowDown, Control+a. method='type' to type text into focused element."""
    tabs, refs = _state.require()
    if method == "press":
        await tabs.page.keyboard.press(value)
    else:
        await tabs.page.keyboard.type(value, delay=80)
    return f"Key {method}: {value}"


@mcp.tool()
async def scroll(direction: str, percent: int = 80) -> str:
    """Scroll up or down by percentage of viewport."""
    tabs, refs = _state.require()
    viewport = tabs.page.viewport_size or {"height": 720}
    delta = int(viewport["height"] * percent / 100)
    if direction == "up":
        delta = -delta
    await tabs.page.mouse.wheel(0, delta)
    await asyncio.sleep(0.3)
    return f"Scrolled {direction} {percent}%"


@mcp.tool()
async def wait(time_ms: int = 2000) -> str:
    """Wait milliseconds. Use after actions that trigger loading."""
    await asyncio.sleep(time_ms / 1000)
    return f"Waited {time_ms}ms"


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

@mcp.tool()
async def new_tab(url: str = "about:blank") -> str:
    """Open a new tab, optionally navigate to URL."""
    tabs, refs = _state.require()
    idx = await tabs.new_tab(url)
    title = await tabs.page.title()
    return f"Opened tab {idx}: {title or url}"


@mcp.tool()
async def switch_tab(index: int) -> str:
    """Switch to tab by index."""
    tabs, refs = _state.require()
    tabs.switch(index)
    title = await tabs.page.title()
    return f"Tab {index}: {title} ({tabs.page.url})"


@mcp.tool()
async def list_tabs() -> str:
    """List all open tabs."""
    tabs, refs = _state.require()
    await tabs.sync_new_popups()
    return json.dumps(tabs.list_tabs(), indent=2)


@mcp.tool()
async def close_tab(index: int | None = None) -> str:
    """Close tab by index (omit for current tab)."""
    tabs, refs = _state.require()
    await tabs.close_tab(index)
    return f"Closed. Now tab {tabs.active_index}: {tabs.page.url}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = "stdio"
    for arg in sys.argv[1:]:
        if arg.startswith("--transport="):
            transport = arg.split("=", 1)[1]
    mcp.run(transport=transport)
