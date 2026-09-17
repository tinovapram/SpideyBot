"""
/bypass command — resolve link shorteners to their final destination.

Everything runs through Camoufox (stealth Firefox) on a virtual display where
one is available:

* **Sites that gate behind JS counters** → load the page with injected bypass
  JS and click through it (`_resolve_generic`).
* **move2link.co / siendu.com** → clear Cloudflare, then walk the gate JWT
  through the move2link API (`_resolve_move2link`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from telethon import TelegramClient, events

import structlog

logger = structlog.get_logger(__name__)

# ── Hosts that bypass JS targets ──────────────────────────────────────────────

_BUNDLED_HOSTS: dict[str, list[str]] = {
    "ouo":            ["ouo.press", "ouo.io"],
    "adfly":          ["adfoc.us"],
    "gplinks":        ["gplinks.co", "gplinks.com"],
    "cuty":           ["cuttty.com", "cuty.io"],
    "exeio":          ["exe.io", "exeygo.com"],
    "lksfy":          ["lksfy.com", "linkshortify.com"],
    "rinku":          ["rinku.me", "rinku.pro", "7mb.io"],
    "bstshrt":        ["boostellar.com", "bstlar.com", "bstshrt.com"],
    "boostylink":     ["boostylink.com"],
    "keyforge":       ["keyforge.win"],
    "linkunlocker":   ["linkunlocker.com"],
    "cut4money":      ["adurl.io", "cut4money.com", "shr2.link"],
    "droplink":       ["droplink.co"],
    "linkvertise":    ["linkvertise.com"],
    "filecrypt":      ["filecrypt.cc", "filecrypt.to", "filecrypt.co"],
    "sub2unlock":     ["sub2unlock.com"],
    "sub4unlock-com": ["sub4unlock.com", "sub4unlock.pro"],
    "sub4unlock-io":  ["sub4unlock.io", "sub2unlock.io"],
    "sub4unlock-me":  ["sub4unlock.me", "sub2unlock.me"],
    "lootlabs":       ["lootlabs.gg", "loot-link.com", "lootlinks.com",
                       "lootlinks.co", "lootdest.org", "lootdest.com",
                       "lootdest.net", "speedy-links.com", "best-links.org",
                       "rapid-links.com", "direct-links.net"],
    "shrinkme":       ["shrinkme.com", "shrinkme.click", "shrinke.me", "shrinkme.io"],
    "shrinkearn":     ["oii.la", "tpi.li"],
    "shrinkpe":       ["aii.sh", "lnbz.la", "shrink.pe"],
    "1shortlink":     ["1shortlink.com"],
    "arolinks":       ["arolinks.com", "vplink.in"],
    "linksterr":      ["linksterr.com"],
    "linkjust":       ["linkjust.com"],
    "liteshort":      ["liteshort.com", "link.liteshort.com"],
    "shortxlinks":    ["shortxlinks.com", "shortxlinks.in"],
    "genlink":        ["genlink.site", "rplinks.in"],
    "icutlink":       ["icutlink.com"],
    "fclc":           ["fc-lc.xyz", "fc.lc", "oii.io"],
    "bblink":         ["web.bbmkts.com"],
    "linknext":       ["linknext.io", "shorte.io"],
    "tfly":           ["tfly.link"],
    "mitly":          ["mitly.us"],
    "linclik":        ["linclik.com"],
    "cpmlink":        ["cpm.link", "cpmlink.pro"],
    "cpmlink-net":    ["cpmlink.net"],
    "shrtfly":        ["shrtslug.biz"],
    "tech8s":         ["ez4short.com", "game5s.com", "tech8s.net", "carrnissan.com"],
    "workink":        ["work.ink"],
    "link4m":         ["link4m.co"],
    "gaea":           ["lockr.net", "lockr.so", "lockr.to"],
    "freedlink":      ["frdl.by", "frdl.my", "frdl.is"],
    "dlsurf":         ["dlsurf.com", "dl.surf"],
    "sfl":            ["sfl.gl"],
    "earnlinks":      ["earnlinks.in", "linksgo.in"],
    "earn4link":      ["earn4link.in"],
    "ontops":         ["ontops.link"],
    "clipi":          ["clipi.cc"],
    "nitrolink":      ["nitro-link.com"],
    "multiup":        ["multiup.io"],
    "filepress":      ["filepress.baby"],
    "molyn":          ["molyn.top"],
    "move2link":      ["move2link.co"],
}


def _host_matches(hostname: str, roots: list[str]) -> bool:
    h = hostname.lower()
    return any(h == d or h.endswith("." + d) for d in roots)


def _is_bypass_host(hostname: str) -> bool:
    return any(_host_matches(hostname, domains) for domains in _BUNDLED_HOSTS.values())


# ── move2link.co resolution (Camoufox + move2link API) ────────────────────────
#
# move2link.co/<slug> is fronted by Cloudflare and then bounces to
#     https://zoo.siendu.com/<locale>/random?token=<JWT>&step=0
# The landing page never reveals the destination — the JWT has to be walked
# through the move2link API until it reaches the last step:
#     PUT  /views/track    {token}  -> data.token     (advance one step)
#     POST /views/finalize {token}  -> data.redirect_url (the real destination)
# Camoufox is required first, purely to clear the Cloudflare challenge so the
# gate issues a token.

_MOVE2LINK_ROOTS = ("move2link.co", "siendu.com")
_MOVE2LINK_API = "https://api.move2link.com/api/v1"

_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

def _default_headless() -> bool | str:
    """Pick the Camoufox display mode.

    ``headless="virtual"`` runs Firefox against an Xvfb virtual display, which
    is far harder to fingerprint than true headless. Xvfb is Linux-only, so
    other platforms fall back to plain headless.

    Override with ``CAMOUFOX_HEADLESS`` = ``virtual`` | ``1`` | ``0``.
    """
    override = os.environ.get("CAMOUFOX_HEADLESS", "").strip().lower()
    if override in {"0", "false", "no"}:
        return False
    if override in {"1", "true", "yes"}:
        return True
    # Default, and an explicit "virtual" request: Xvfb only exists on Linux, so
    # this also tolerates the Docker default leaking into a Windows/macOS env.
    return "virtual" if sys.platform.startswith("linux") else True


_HEADLESS: bool | str = _default_headless()


def _is_move2link_host(hostname: str) -> bool:
    return _host_matches(hostname, list(_MOVE2LINK_ROOTS))


def _decode_jwt_payload(token: str) -> dict | None:
    """Decode a JWT payload (no signature verification)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None


def _extract_move2link_token(text: str) -> str | None:
    """Pull the gate JWT out of a URL, a `?token=` query, or a 'Loading <url>' title."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("Loading "):
        candidate = candidate[len("Loading "):].strip()
    match = re.search(r"[?&]token=([A-Za-z0-9_.\-]+)", candidate)
    if match and _JWT_RE.match(match.group(1)):
        return match.group(1)
    return candidate if _JWT_RE.match(candidate) else None


async def _move2link_api(path: str, method: str, token: str) -> dict:
    """Call one move2link API step; returns the response `data` object."""
    import requests

    def _call() -> dict:
        resp = requests.request(
            method,
            f"{_MOVE2LINK_API}{path}",
            headers={"Content-Type": "application/json"},
            json={"token": token, "csrf_token": "x", "imps": []},
            timeout=15,
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("data") or {}

    return await asyncio.to_thread(_call)


async def _unlock_move2link(token: str) -> str | None:
    """Walk the gate JWT to its final step and return the destination URL."""
    current = token
    for _ in range(10):
        payload = _decode_jwt_payload(current)
        if not payload:
            return None
        try:
            step = int(payload.get("step", 0))
            max_step = int(payload.get("max_step", 0))
        except (TypeError, ValueError):
            return None
        if max_step < 1 or step >= max_step - 1:
            break
        data = await _move2link_api("/views/track", "PUT", current)
        nxt = str(data.get("token") or "").strip()
        if not _JWT_RE.match(nxt):
            return None
        current = nxt

    data = await _move2link_api("/views/finalize", "POST", current)
    dest = str(data.get("redirect_url") or "").strip()
    return dest if _URL_RE.match(dest) else None


async def _capture_move2link_token(url: str, *, timeout_ms: int) -> str | None:
    """Load the gate in Camoufox and wait for it to hand us the JWT."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_ms, 15_000) / 1000

    async with _camoufox_page() as page:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            logger.debug("move2link_goto", url=url, error=str(exc))

        while loop.time() < deadline:
            # The token shows up in the title ("Loading <gate-url>") while the
            # gate is still loading, then in page.url once the redirect commits.
            try:
                title = await page.title()
            except Exception:
                title = ""
            token = _extract_move2link_token(title) or _extract_move2link_token(page.url)
            if token:
                return token
            await page.wait_for_timeout(1000)

    return None


async def _resolve_move2link(url: str, *, timeout_ms: int) -> str | None:
    """Clear Cloudflare with Camoufox, capture the gate token, unlock via API."""
    try:
        token = await _capture_move2link_token(url, timeout_ms=timeout_ms)
    except ImportError:
        logger.warning("camoufox_missing", url=url)
        return None
    except Exception as exc:
        logger.warning("move2link_browser_failed", url=url, error=str(exc))
        return None

    if not token:
        logger.warning("move2link_no_token", url=url)
        return None

    try:
        return await _unlock_move2link(token)
    except Exception as exc:
        logger.warning("move2link_unlock_failed", url=url, error=str(exc))
        return None


# ── Bypass JS (injected before page scripts) ──────────────────────────────────

_INJECT_JS = r"""
(() => {
  'use strict';
  try {
    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
    document.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);
  } catch (_) {}
  try {
    const hi = window.setTimeout(() => {}, 0);
    for (let i = 0; i <= hi; i++) { window.clearTimeout(i); window.clearInterval(i); }
  } catch (_) {}
  setInterval(() => {
    try { const h = window.setTimeout(() => {}, 0); for (let i = 0; i <= h; i++) { window.clearTimeout(i); window.clearInterval(i); } } catch (_) {}
  }, 500);
  const KILL = ['#timer','#countdown','.timer','.countdown','.counter','#wait','#waitTime',
    '.wait-time','.download-timer','#pleasewait','#please-wait','.please-wait',
    '#gt-link','#link1s','a.get-link'];
  const killDOM = () => {
    KILL.forEach(s => document.querySelectorAll(s).forEach(el => { el.textContent='0'; el.style.setProperty('display','none','important'); }));
    document.querySelectorAll('button[disabled],a[disabled],input[disabled]').forEach(el => { el.disabled=false; el.style.pointerEvents=''; });
    document.querySelectorAll('a[href]:not([href^="javascript"]):not([href="#"]),#btn-unlock,#btn-wait').forEach(el => {
      el.style.setProperty('display','','important'); el.style.setProperty('visibility','visible','important');
      el.style.setProperty('opacity','1','important'); el.removeAttribute('hidden');
    });
  };
  killDOM();
  new MutationObserver(killDOM).observe(document.documentElement, {childList:true,subtree:true});
})();
"""

# ── Click selectors (in order) ────────────────────────────────────────────────

_CLICK_SELS = [
    "a.get-link:visible",
    "#btn-unlock:visible",
    "#btn-wait:visible",
    "a[href]:visible:text('Get Link')",
    "a[href]:visible:text('Continue')",
    "a[href]:visible:text('Skip')",
    "a[href]:visible:text('Unlock')",
    "a[href]:visible:text('Proceed')",
    "#cf-accept-button:visible",
    "a:visible:text('Verify you are human')",
    "input[type=submit]:visible",
    "button:visible:text('Continue')",
    "button:visible:text('Proceed')",
    "button:visible:text('Verify')",
    "button:visible:text('Verify human')",
    "button:visible:text('I am human')",
    "#challenge-form:visible",
    "#js-challenge:visible",
    "form[name='challenge-form']:visible",
    "form:visible:has(\"input[name='cf-verified']\")",
    "#cf-clearance:visible",
    "#challenge-running:visible",
]


# ── Browser engine (Camoufox) ─────────────────────────────────────────────────

@asynccontextmanager
async def _camoufox_page(*, inject_bypass: bool = False):
    """Yield a Camoufox page, tearing down the browser (and Xvfb) on exit.

    `inject_bypass` installs `_INJECT_JS` before any page script runs. The
    move2link gate clears Cloudflare on its own, so it opts out.
    """
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(headless=_HEADLESS) as browser:
        page = await browser.new_page()
        if inject_bypass:
            await page.add_init_script(_INJECT_JS)
        yield page


async def resolve_url(url: str, *, timeout_ms: int = 45_000) -> str:
    """Resolve *url* to its final destination.

    move2link.co-family links take a dedicated gate + API path; everything else
    falls back to the generic Camoufox + bypass-JS flow.
    """
    host = urlparse(url).hostname or url
    if _is_move2link_host(host):
        destination = await _resolve_move2link(url, timeout_ms=timeout_ms)
        if destination:
            return destination
        logger.debug("move2link_fallback", url=url)
    return await _resolve_generic(url, timeout_ms=timeout_ms)


async def _resolve_generic(url: str, *, timeout_ms: int = 45_000) -> str:
    """Resolve *url* through Camoufox with bypass JS. Returns the final URL."""
    async with _camoufox_page(inject_bypass=True) as page:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            logger.debug("bypass_goto", url=url, error=str(exc))

        # Let bypass JS kick in
        await page.wait_for_timeout(3000)

        # Try clicking through "get link" buttons
        for sel in _CLICK_SELS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                pass

        # Cloudflare challenge auto-solve wait
        for _ in range(5):
            title = await page.title()
            if "Just a moment" in title or "Attention Required" in title:
                await page.wait_for_timeout(3000)
            else:
                break

        return page.url


# ── /bypass command ───────────────────────────────────────────────────────────

async def bypass_handler(event):
    """Handle /bypass <url> — resolve a link shortener to its final destination."""
    args = event.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await event.respond(
            "**Usage:** /bypass *URL*\n\n"
            "Resolves link shorteners and wait pages to the final destination."
        )
        raise events.StopPropagation

    url = args[1].strip()

    status_msg = await event.respond("⏳ **Resolving link…**")

    try:
        final_url = await resolve_url(url)
    except ImportError:
        await status_msg.edit(
            "⚠️ **Camoufox not installed.**\n"
            "Run: `pip install camoufox && camoufox fetch`"
        )
        raise events.StopPropagation
    except Exception as exc:
        logger.error("bypass_failed", url=url, error=str(exc))
        await status_msg.edit(f"❌ **Failed to resolve:** {exc!s:.200}")
        raise events.StopPropagation

    await status_msg.edit(
        f"✅ **Resolved!**\n\n"
        f"**Original:** {url}\n"
        f"**Final:** {final_url}"
    )
    raise events.StopPropagation


# ── Registration ──────────────────────────────────────────────────────────────

def register_bypass_handler(client: TelegramClient) -> None:
    client.add_event_handler(bypass_handler, events.NewMessage(pattern=r"/bypass"))
