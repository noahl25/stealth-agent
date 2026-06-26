"""
Stealth browser MCP server — expose CloakBrowser tools over MCP.

Usage:
    uv run python mcp_server.py              # stdio (for Claude Desktop / agent integration)

Concurrency model — SESSIONS:
    All pages live in ONE persistent browser context (shared cookies/login). Each
    "tab" is a SESSION = its own page + its own element-ref store, addressed by a
    session id ("s1", "s2", ...). Every tool takes an optional `session` arg; pass a
    session id so concurrent agents each drive their OWN page and never clobber a
    shared active-tab pointer. Omit `session` to use the default session (single-agent
    use, unchanged behaviour).
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

from main import RefStore, take_snapshot, _wrap_page_content

load_dotenv()


class BrowserState:
    """One persistent browser context holding many independent SESSIONS.

    sessions: id -> {"page": Page, "refs": RefStore}. Per-session state is what makes
    concurrent multi-agent use safe — no shared active-tab pointer.
    """

    def __init__(self) -> None:
        self.context: Any | None = None
        self.sessions: dict[str, dict] = {}
        self._default: str | None = None
        self._counter: int = 0
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

    def _register(self, page: Any) -> str:
        """Track a page as a new session; return its id."""
        self._counter += 1
        sid = f"s{self._counter}"
        self.sessions[sid] = {"page": page, "refs": RefStore()}
        return sid

    def _adopt_orphans(self) -> list[str]:
        """Register any context pages not yet tracked (e.g. popups). Returns new ids."""
        if not self.context:
            return []
        known = {s["page"] for s in self.sessions.values()}
        new = []
        for p in self.context.pages:
            if p not in known:
                new.append(self._register(p))
        return new

    async def open(self, headed: bool = False, use_xvfb: bool = False) -> str:
        if self.is_open:
            return (f"Browser already open ({len(self.sessions)} session(s)). "
                    f"Use new_tab for an isolated session.")
        from cloakbrowser import launch_persistent_context_async

        profile_dir = os.environ.get(
            "STEALTH_BROWSER_PROFILE", os.path.expanduser("~/stealth-browser")
        )
        os.makedirs(profile_dir, exist_ok=True)

        xvfb_disp: str | None = None
        try:
            if headed and use_xvfb:
                xvfb_disp = await self._start_xvfb()
            self.context = await launch_persistent_context_async(
                profile_dir, headless=not headed, viewport={"width": 1280, "height": 720}
            )
        except Exception:
            await self._stop_xvfb()
            raise

        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await page.goto("about:blank")
        self._default = self._register(page)
        mode = "headed" if headed else "headless"
        extra = f", xvfb={xvfb_disp}" if xvfb_disp else ""
        return f"Browser opened ({mode}, profile={profile_dir}{extra}). Default session: {self._default}."

    async def open_session(self, url: str = "about:blank") -> str:
        if not self.is_open:
            raise RuntimeError("Browser is not open. Call open_browser first.")
        page = await self.context.new_page()
        await page.goto(url or "about:blank", wait_until="domcontentloaded")
        return self._register(page)

    async def close_session(self, sid: str) -> str:
        s = self.sessions.pop(sid, None)
        if not s:
            return f"No such session '{sid}'."
        try:
            await s["page"].close()
        except Exception:
            pass
        if self._default == sid:
            self._default = next(iter(self.sessions), None)
        return f"Closed session {sid}. Remaining: {', '.join(self.sessions) or '(none)'}."

    def resolve(self, session: str | None = None):
        """Return (page, refs, sid) for a session id, or the default session."""
        if not self.is_open:
            raise RuntimeError("Browser is not open. Call open_browser first.")
        sid = session or self._default
        s = self.sessions.get(sid)
        if not s:
            raise RuntimeError(
                f"No such session '{sid}'. Open one with new_tab; see list_tabs."
            )
        return s["page"], s["refs"], sid

    def list_sessions(self) -> list[dict]:
        return [
            {"session": sid, "url": s["page"].url, "default": sid == self._default}
            for sid, s in self.sessions.items()
        ]

    async def close(self) -> str:
        if not self.is_open:
            return "Browser is not open."
        try:
            await self.context.close()
        finally:
            self.context = None
            self.sessions = {}
            self._default = None
            await self._stop_xvfb()
        return "Browser closed."


_state = BrowserState()

mcp = FastMCP("stealth-browser")


# ---------------------------------------------------------------------------
# Browser lifecycle
# ---------------------------------------------------------------------------

@mcp.tool()
async def open_browser(headed: bool = False, use_xvfb: bool = False) -> str:
    """Launch the stealth browser (idempotent — the first caller opens it; others reuse it).
    Then call new_tab to get your OWN session id for concurrent-safe use.

    Set headed=true to show the window; on a display-less server also set use_xvfb=true
    to run headed inside a virtual X display (auto start/stop). use_xvfb is ignored when headed=false."""
    return await _state.open(headed=headed, use_xvfb=use_xvfb)


@mcp.tool()
async def close_browser() -> str:
    """Close the browser and ALL sessions. Call when the whole swarm is done with the browser."""
    return await _state.close()


# ---------------------------------------------------------------------------
# Tabs / sessions (concurrency)
# ---------------------------------------------------------------------------

@mcp.tool()
async def new_tab(url: str = "about:blank") -> str:
    """Open a NEW isolated tab/session and return its session id. Pass that id as the
    `session` arg to every other tool so concurrent agents never share a page. Each
    session has its own page + element refs; all sessions share one browser (cookies/login)."""
    sid = await _state.open_session(url)
    page, _refs, _ = _state.resolve(sid)
    title = await page.title()
    return f'Opened session {sid}: {title or url}. Pass session="{sid}" to other tools.'


@mcp.tool()
async def list_tabs() -> str:
    """List open tabs/sessions (id, url, which is default). Also adopts any popups."""
    _state._adopt_orphans()
    return json.dumps(_state.list_sessions(), indent=2)


@mcp.tool()
async def close_tab(session: str) -> str:
    """Close a tab/session by its id (e.g. "s2")."""
    return await _state.close_session(session)


# ---------------------------------------------------------------------------
# Snapshot & content
# ---------------------------------------------------------------------------

@mcp.tool()
async def snapshot(session: str | None = None) -> str:
    """Compact accessibility snapshot with short refs (@e1, @e2...) for interactive
    elements — your PRIMARY tool for understanding a page (cheaper than a screenshot).
    Refs are scoped to this session and invalidated after the page changes; re-snapshot
    for fresh ones. Pass `session` to target your own tab."""
    page, refs, sid = _state.resolve(session)
    tree = await take_snapshot(page, refs)
    header = f"Session {sid} | Page: {page.url} | {refs.count} interactive refs"
    return f"{header}\n\n{_wrap_page_content(tree)}"


@mcp.tool()
async def get_text(ref: str, session: str | None = None) -> str:
    """Get the text content of an element by ref (@e1) or CSS/XPath selector."""
    page, refs, _ = _state.resolve(session)
    selector = refs.resolve(ref)
    loc = page.locator(selector).first
    text = await loc.text_content(timeout=5000)
    return _wrap_page_content((text or "").strip() or "(empty)")


@mcp.tool()
async def get_page_content(
    selector: str | None = None,
    max_length: int = 10000,
    session: str | None = None,
) -> str:
    """Extract readable text from the page or a section. Use for articles, posts, docs,
    search results, filings. Optionally scope to a CSS selector."""
    page, refs, _ = _state.resolve(session)
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
async def click(ref: str, session: str | None = None) -> str:
    """Click an element by ref (@e1) or selector. Use snapshot first to get refs."""
    page, refs, _ = _state.resolve(session)
    selector = refs.resolve(ref)
    await page.locator(selector).first.click(timeout=5000)
    await asyncio.sleep(0.3)
    popups = _state._adopt_orphans()
    msg = f"Clicked {ref}"
    if popups:
        msg += f" (popup opened as session {', '.join(popups)})"
    return msg


@mcp.tool()
async def fill(ref: str, value: str, session: str | None = None) -> str:
    """Clear an input and type new text. Use ref (@e3) or selector."""
    page, refs, _ = _state.resolve(session)
    selector = refs.resolve(ref)
    await page.locator(selector).first.fill(value, timeout=5000)
    return f"Filled {ref} with: {value}"


@mcp.tool()
async def select(ref: str, value: str, session: str | None = None) -> str:
    """Select an option from a dropdown by ref or selector."""
    page, refs, _ = _state.resolve(session)
    selector = refs.resolve(ref)
    loc = page.locator(selector).first
    try:
        await loc.select_option(value=value, timeout=5000)
    except Exception:
        await loc.select_option(label=value, timeout=5000)
    return f"Selected '{value}' in {ref}"


@mcp.tool()
async def check(ref: str, checked: bool = True, session: str | None = None) -> str:
    """Check or uncheck a checkbox/radio by ref or selector."""
    page, refs, _ = _state.resolve(session)
    selector = refs.resolve(ref)
    loc = page.locator(selector).first
    if checked:
        await loc.check(timeout=5000)
    else:
        await loc.uncheck(timeout=5000)
    return f"{'Checked' if checked else 'Unchecked'} {ref}"


# ---------------------------------------------------------------------------
# Vision tools (coordinates — fallback)
# ---------------------------------------------------------------------------

@mcp.tool()
async def screenshot(session: str | None = None) -> Image:
    """Take a screenshot. Use ONLY when you need visual info — CAPTCHAs, canvas, bot
    challenges, visual layout. Prefer snapshot for normal pages."""
    page, refs, _ = _state.resolve(session)
    png_bytes = await page.screenshot(full_page=False)
    return Image(data=png_bytes, format="png")


@mcp.tool()
async def click_xy(x: int, y: int, description: str = "", session: str | None = None) -> Image:
    """Click at pixel coordinates. For CAPTCHAs, canvas, bot-protected elements. Take a
    screenshot first to find coordinates. Returns a follow-up screenshot."""
    page, refs, _ = _state.resolve(session)
    await page.mouse.click(x, y)
    await asyncio.sleep(0.5)
    _state._adopt_orphans()
    png_bytes = await page.screenshot(full_page=False)
    return Image(data=png_bytes, format="png")


@mcp.tool()
async def type_xy(x: int, y: int, text: str, clear_first: bool = True, session: str | None = None) -> Image:
    """Click at coordinates then type. For non-standard inputs. Returns a follow-up screenshot."""
    page, refs, _ = _state.resolve(session)
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
async def goto(url: str, session: str | None = None) -> str:
    """Navigate this session's tab to a URL."""
    page, refs, sid = _state.resolve(session)
    await page.goto(url, wait_until="domcontentloaded")
    title = await page.title()
    return f"[{sid}] Navigated to {url} — {title}"


@mcp.tool()
async def navback(session: str | None = None) -> str:
    """Navigate back (browser back button)."""
    page, refs, sid = _state.resolve(session)
    await page.go_back(wait_until="domcontentloaded")
    return f"[{sid}] Back — {page.url}"


# ---------------------------------------------------------------------------
# Keyboard / scroll / wait
# ---------------------------------------------------------------------------

@mcp.tool()
async def keys(method: str, value: str, session: str | None = None) -> str:
    """Keyboard input. method='press' for Enter, Tab, Escape, Backspace, ArrowDown,
    Control+a. method='type' to type text into the focused element."""
    page, refs, _ = _state.resolve(session)
    if method == "press":
        await page.keyboard.press(value)
    else:
        await page.keyboard.type(value, delay=80)
    return f"Key {method}: {value}"


@mcp.tool()
async def scroll(direction: str, percent: int = 80, session: str | None = None) -> str:
    """Scroll up or down by percentage of viewport."""
    page, refs, _ = _state.resolve(session)
    viewport = page.viewport_size or {"height": 720}
    delta = int(viewport["height"] * percent / 100)
    if direction == "up":
        delta = -delta
    await page.mouse.wheel(0, delta)
    await asyncio.sleep(0.3)
    return f"Scrolled {direction} {percent}%"


@mcp.tool()
async def wait(time_ms: int = 2000) -> str:
    """Wait milliseconds. Use after actions that trigger loading."""
    await asyncio.sleep(time_ms / 1000)
    return f"Waited {time_ms}ms"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = "stdio"
    for arg in sys.argv[1:]:
        if arg.startswith("--transport="):
            transport = arg.split("=", 1)[1]
    mcp.run(transport=transport)
