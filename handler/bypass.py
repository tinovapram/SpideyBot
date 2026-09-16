"""
/bypass command — resolve link shorteners to their final destination.

Uses Playwright headless Chromium with injected bypass JS.
"""

from __future__ import annotations

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
}


def _host_matches(hostname: str, roots: list[str]) -> bool:
    h = hostname.lower()
    return any(h == d or h.endswith("." + d) for d in roots)


def _is_bypass_host(hostname: str) -> bool:
    return any(_host_matches(hostname, domains) for domains in _BUNDLED_HOSTS.values())


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
]


async def resolve_url(url: str, *, timeout_ms: int = 30_000) -> str:
    """Resolve *url* through headless Chromium with bypass JS. Returns final URL."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.add_init_script(_INJECT_JS)
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception:
                pass

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

            return page.url
        finally:
            await browser.close()


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
            "⚠️ **Playwright not installed.**\n"
            "Run: `pip install playwright && playwright install chromium`"
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
