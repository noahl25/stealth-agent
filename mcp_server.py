"""
Stealth browser MCP server — expose CloakBrowser tools over MCP.

Usage:
    uv run python mcp_server.py              # stdio (for Claude Desktop / agent integration)
"""

from __future__ import annotations

import asyncio
import json
import os
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
        self.context: Any | None = None
        self.tabs: TabManager | None = None
        self.refs: RefStore | None = None
        self._xvfb_proc: Any | None = None
        self._prev_display: str | None = None

    @property
    def is_open(self) -> bool:
        return self.context is not None

    async def _start_xvfb(self) -> str:
        """Start an Xvfb virtual display, set $DISPLAY, and return the display id."""
        import shutil
        import subprocess

        if shutil.which("Xvfb") is None:
            raise RuntimeError(
                "Xvfb is not installed. Install it (e.g. `apt-get install -y xvfb`) "
                "or call open_browser without use_xvfb."
            )
        disp = next(
            (
                n
                for n in range(99, 110)
                if not os.path.exists(f"/tmp/.X{n}-lock")
                and not os.path.exists(f"/tmp/.X11-unix/X{n}")
            ),
            None,
        )
        if disp is None:
            raise RuntimeError("No free X display number available (:99–:109).")

        proc = subprocess.Popen(
            ["Xvfb", f":{disp}", "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sock = f"/tmp/.X11-unix/X{disp}"
        for _ in range(100):  # wait up to ~10s for Xvfb to be ready
            if proc.poll() is not None:
                raise RuntimeError("Xvfb exited during startup.")
            if os.path.exists(sock):
                break
            await asyncio.sleep(0.1)
        else:
            proc.terminate()
            raise RuntimeError("Xvfb did not become ready in time.")

        self._xvfb_proc = proc
        self._prev_display = os.environ.get("DISPLAY")
        os.environ["DISPLAY"] = f":{disp}"
        return f":{disp}"

    async def _stop_xvfb(self) -> None:
        """Terminate Xvfb (if running) and restore the prior $DISPLAY."""
        if self._xvfb_proc is None:
            return
        self._xvfb_proc.terminate()
        try:
            self._xvfb_proc.wait(timeout=5)
        except Exception:
            self._xvfb_proc.kill()
        self._xvfb_proc = None
        if self._prev_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = self._prev_display
        self._prev_display = None

    async def open(self, headed: bool = False, use_xvfb: bool = False) -> str:
        if self.is_open:
            return "Browser is already open."
        from cloakbrowser import launch_persistent_context_async

        # Persistent profile: cookies, logins, and localStorage survive across
        # sessions (also avoids incognito detection). Default location is
        # ~/stealth-browser so it resolves consistently on any machine;
        # override with the STEALTH_BROWSER_PROFILE env var.
        profile_dir = os.environ.get(
            "STEALTH_BROWSER_PROFILE", os.path.expanduser("~/stealth-browser")
        )
        os.makedirs(profile_dir, exist_ok=True)

        xvfb_disp: str | None = None
        try:
            # On a headless server with no real X display, headed mode needs a
            # virtual display. Only relevant when headed=True.
            if headed and use_xvfb:
                xvfb_disp = await self._start_xvfb()

            # Returns a BrowserContext directly (no separate Browser object).
            self.context = await launch_persistent_context_async(
                profile_dir,
                headless=not headed,
                viewport={"width": 1280, "height": 720},
            )
        except Exception:
            await self._stop_xvfb()
            raise

        # A persistent context starts with one blank page; reuse it instead of
        # opening a second tab.
        first_page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )
        await first_page.goto("about:blank")
        self.tabs = TabManager(self.context)
        self.tabs.add(first_page)
        self.refs = RefStore()
        mode = "headed" if headed else "headless"
        extra = f", xvfb={xvfb_disp}" if xvfb_disp else ""
        return f"Browser opened ({mode}, profile={profile_dir}{extra})."

    async def close(self) -> str:
        if not self.is_open:
            return "Browser is not open."
        # Closing the persistent context shuts down the browser and flushes the
        # profile (cookies/storage) to disk.
        await self.context.close()
        self.context = None
        self.tabs = None
        self.refs = None
        await self._stop_xvfb()
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
async def open_browser(headed: bool = False, use_xvfb: bool = False) -> str:
    """Launch the stealth browser. Must be called before any other browser tool.

    Set headed=true to show the browser window. On a headless server with no
    real display, also set use_xvfb=true to run the headed browser inside a
    virtual X display (Xvfb), which is started and torn down automatically.
    use_xvfb is ignored when headed=false (headless needs no display)."""
    return await _state.open(headed=headed, use_xvfb=use_xvfb)


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
