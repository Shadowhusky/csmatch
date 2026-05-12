"""HLTV scorebot bridge.

Runs a headless Chromium against an HLTV match page and harvests the
live `.gamelog` + scoreboard DOM that the page's own JavaScript fills
from `scorebot-lb.hltv.org`. This sidesteps the Engine.IO/Socket.IO
protocol entirely — we let HLTV's bundled JS do the talking and just
read the rendered DOM at 1Hz.

Why not connect to the Socket.IO endpoint directly? Two reasons:
1. The endpoint's TLS fingerprint check rejects every non-browser HTTP
   client we tried (aiohttp → 403; curl_cffi gets a session id but
   subscribe payloads are silently ignored).
2. HLTV's match-js is Kotlin-compiled, so the actual subscribe event
   name is minified away — opaque from outside the browser runtime.

The bridge is async-friendly so it shares the TUI's event loop. It
exposes events as an async queue.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Literal

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright
except ImportError:  # pragma: no cover - playwright is an optional dep
    async_playwright = None  # type: ignore[assignment]
    Browser = BrowserContext = Page = Playwright = Any  # type: ignore[misc, assignment]


Side = Literal["CT", "T"]


async def _capture_frontmost_app() -> str | None:
    """Return the name of the macOS app currently in the foreground, or
    None if we couldn't determine it. Used to restore focus after the
    Chromium launch steals it."""
    script = (
        'tell application "System Events"\n'
        '  set frontProc to first process whose frontmost is true\n'
        '  return name of frontProc\n'
        "end tell"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        name = stdout.decode().strip()
        # If our terminal was already calling osascript at launch we may
        # see osascript itself as frontmost — skip that.
        if not name or name.lower() in {"osascript", "chromium"} or "headless" in name.lower():
            return None
        return name
    except Exception:
        return None


_COMMON_TERMINAL_APPS = (
    "Terminal", "iTerm2", "iTerm", "Warp", "WezTerm", "Alacritty",
    "kitty", "Hyper", "Tabby", "Ghostty", "Code", "Cursor", "Windsurf",
)


async def _hide_chromium_and_restore_focus(prev_frontmost: str | None) -> None:
    """Bounce focus back to the previously-active app and hide Chromium
    from the Dock / Cmd-Tab. The bounce uses `set frontmost ... to true`
    via System Events — more reliable than `tell app ... to activate`
    because it talks at the process level and doesn't depend on the
    app bundle's display name matching the process name.

    Order matters: activate the target first (steals focus from
    Chromium) THEN hide Chromium (cleans up the Dock icon)."""
    candidates: list[str] = []
    if prev_frontmost:
        candidates.append(prev_frontmost)
    for fallback in _COMMON_TERMINAL_APPS:
        if fallback not in candidates:
            candidates.append(fallback)

    # Try each candidate in turn — first one that exists as a running
    # process gets focus. AppleScript silently skips ones that aren't.
    activate_block = ""
    for name in candidates:
        safe = name.replace("\\", "\\\\").replace('"', '\\"')
        activate_block += (
            f'  try\n'
            f'    if exists (first process whose name is "{safe}") then\n'
            f'      set frontmost of (first process whose name is "{safe}") to true\n'
            f'      set didActivate to true\n'
            f'    end if\n'
            f'  end try\n'
        )

    script = (
        'tell application "System Events"\n'
        '  set didActivate to false\n'
        + activate_block +
        '  try\n'
        '    repeat with p in (every process whose name is "Chromium")\n'
        '      set visible of p to false\n'
        '    end repeat\n'
        '  end try\n'
        '  try\n'
        '    repeat with p in (every process whose name contains "Headless")\n'
        '      set visible of p to false\n'
        '    end repeat\n'
        '  end try\n'
        "end tell"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        if proc.returncode != 0 and b"not allowed" in (stderr or b"").lower():
            # macOS Automation permission was denied. Hint visibly so
            # the user knows why focus isn't bouncing back.
            print(
                "\ncsmatch: macOS Automation permission needed to "
                "control 'System Events' — grant it under System "
                "Settings → Privacy & Security → Automation, otherwise "
                "Chromium will keep stealing focus on launch.\n",
                flush=True,
            )
    except Exception:
        pass

# Extracts the contents of .gamelog as structured rows. We rely on the
# CSS class names rendered by HLTV's scorebot React component.
_EXTRACT_JS = r"""
() => {
    const out = {scoreboard: null, rows: [], teams: [], series: null};
    const sb = document.querySelector('#scoreboardElement .scorebot');
    if (!sb) return out;

    // ── series header (per-map breakdown from .mapholder) ────────────
    // Each .mapholder has: map name, team_a, score_a, optional half-split
    // (h1;h2), team_b, score_b. Maps not yet played have '-' for scores.
    const mapHolders = document.querySelectorAll('.mapholder');
    if (mapHolders.length > 0) {
        const series = {maps: []};
        mapHolders.forEach((mh, idx) => {
            // The text mixes team names, scores, the literal "STATS", and
            // a half-split block like "(11:1;-:-)". Pulling by class is
            // much safer than text-regex.
            const mapName = mh.querySelector('.mapname, .map-name, .mapHolder-mapname')?.textContent.trim()
                || (mh.querySelector('div')?.firstChild?.textContent || '').trim()
                || null;
            const teamSpans = mh.querySelectorAll('.results-teamname');
            const scoreSpans = mh.querySelectorAll('.results-team-score, .currentMapScore, .map-score, .score');
            const entry = {index: idx + 1};
            entry.map = mapName;
            const num = (el) => {
                if (!el) return null;
                const t = el.textContent.trim();
                if (!t || t === '-' || t === '–') return null;
                const n = parseInt(t, 10);
                return isNaN(n) ? null : n;
            };
            // Fallback: pull two numbers directly from the *trimmed* text
            // sequence, ignoring anything that looks like a half-split.
            if (teamSpans.length >= 2) {
                entry.team_a = teamSpans[0].textContent.trim();
                entry.team_b = teamSpans[1].textContent.trim();
            }
            // Total scores: HLTV renders them as the first and last digit-only
            // children of .mapholder, *outside* the (h1:h2;...) parens.
            const allText = mh.innerText.replace(/\(.*?\)/g, '').replace(/\s+/g, ' ').trim();
            const nums = allText.match(/\b(\d+|-)\b/g) || [];
            const parseN = (s) => (s === '-' || s == null) ? null : parseInt(s, 10);
            if (nums.length >= 2) {
                entry.score_a = parseN(nums[0]);
                entry.score_b = parseN(nums[nums.length - 1]);
            } else {
                entry.score_a = null;
                entry.score_b = null;
            }
            series.maps.push(entry);
        });
        // Compute series wins
        let wins_a = 0, wins_b = 0;
        series.maps.forEach(e => {
            if (e.score_a != null && e.score_b != null) {
                if (e.score_a > e.score_b) wins_a++;
                else if (e.score_b > e.score_a) wins_b++;
            }
        });
        series.wins_a = wins_a;
        series.wins_b = wins_b;
        out.series = series;
    }

    // ── per-team scoreboards ─────────────────────────────────────────
    sb.querySelectorAll('table.team').forEach(t => {
        const thead = t.querySelector('thead');
        // The team name is the first non-empty text inside .teamName;
        // fall back to the thead's first child text.
        let name = t.querySelector('.teamName')?.textContent.trim() || null;
        if (!name) {
            name = (thead?.innerText || '').split(/[\n\t]/).map(s => s.trim()).filter(Boolean)[0] || null;
        }
        const side = thead?.className?.includes('ctTeamHeaderBg') ? 'CT'
                    : thead?.className?.includes('tTeamHeaderBg') ? 'T' : null;
        const players = [];
        t.querySelectorAll('tbody tr.row.player').forEach(r => {
            const cells = Array.from(r.querySelectorAll('td')).map(td => td.innerText.trim());
            // cells: [nick, _, _, hp, _, money, K, A, D, ADR]
            const num = (s) => { const n = parseInt(String(s).replace(/[^\d-]/g, ''), 10); return isNaN(n) ? null : n; };
            const f = (s) => { const v = parseFloat(s); return isNaN(v) ? null : v; };
            players.push({
                nick: cells[0] || null,
                alive: !r.className.includes('playerDeadText'),
                hp: num(cells[3]),
                money: num(cells[5]),
                kills: num(cells[6]) ?? 0,
                assists: num(cells[7]) ?? 0,
                deaths: num(cells[8]) ?? 0,
                adr: f(cells[9]),
            });
        });
        out.teams.push({name, side, players});
    });

    // Header bar: round number, current map, ctScore, tScore, time
    const currentRound = sb.querySelector('.currentRoundText')?.textContent || null;
    const ctScore = sb.querySelector('.ctScore')?.textContent?.trim() || null;
    const tScore  = sb.querySelector('.tScore')?.textContent?.trim() || null;
    const timeText = sb.querySelector('.timeText')?.textContent?.trim() || null;
    // The round text contains "R: 5 - dust2" or similar. Map name lives
    // inside the currentRoundText span — split out.
    let roundNum = null, mapName = null;
    if (currentRound) {
        const m = currentRound.match(/^\s*(\d+)\s*-\s*(.+?)\s*$/);
        if (m) { roundNum = parseInt(m[1], 10); mapName = m[2]; }
    }
    out.scoreboard = {
        round: roundNum,
        map: mapName,
        ctScore: ctScore ? parseInt(ctScore, 10) : null,
        tScore: tScore ? parseInt(tScore, 10) : null,
        time: timeText,
        bombPlanted: !!sb.querySelector('.timeText img[src*=bomb]'),
    };

    // Gamelog rows. The .gamelog is a sibling of .scoreboard, not nested
    // inside .scorebot in all layouts — query from document.
    const boxes = document.querySelectorAll('.gamelog .gamelogBox');
    boxes.forEach(box => {
        const classes = box.className;
        const row = {classes: classes, text: box.innerText.trim()};

        const sideOf = (el) => el && el.classList && el.classList.contains('tplayer') ? 'T' : 'CT';
        if (classes.includes('playerKill')) {
            // The killer span is the first .tplayer/.ctplayer; the victim
            // is the last one (assist spans land in between).
            const playerSpans = box.querySelectorAll('span.tplayer, span.ctplayer');
            row.kind = 'kill';
            row.killer = playerSpans[0]?.textContent.trim() || null;
            row.killerSide = sideOf(playerSpans[0]);
            const v = playerSpans[playerSpans.length - 1];
            row.victim = v?.textContent.trim() || null;
            row.victimSide = sideOf(v);
            const assistEl = box.querySelector('[title*="assist" i] span.tplayer, [title*="assist" i] span.ctplayer');
            row.assist = assistEl?.textContent.trim() || null;
            row.flashAssist = !!box.querySelector('[title*="Flash assist" i]');
            row.headshot = !!box.querySelector('.headshotIcon');
            const weaponImg = box.querySelector('.playerWeapon');
            if (weaponImg) {
                const src = weaponImg.getAttribute('src') || '';
                const m = src.match(/\/weapons\/([^./]+)/);
                row.weapon = m ? m[1] : null;
            }
        } else if (classes.includes('bombPlant')) {
            row.kind = 'bombPlant';
            const ps = box.querySelector('span.tplayer, span.ctplayer');
            row.planter = ps?.textContent.trim() || null;
            const m = (row.text || '').match(/planted the bomb on ([AB])\s*\((\d+)on(\d+)\)/);
            if (m) {
                row.site = m[1];
                row.tAlive = parseInt(m[2], 10);
                row.ctAlive = parseInt(m[3], 10);
            }
        } else if (classes.includes('bombDefuse') || /\bdefused\b/i.test(row.text || '')) {
            row.kind = 'bombDefuse';
            // Defuse rows often have only one .ctplayer (the defuser) at start.
            const ps = box.querySelector('span.ctplayer, span.tplayer');
            const name = ps?.textContent.trim() || null;
            row.defuser = (name && name !== 'CT' && name !== 'T') ? name : null;
        } else if (classes.includes('roundStart')) {
            row.kind = 'roundStart';
        } else if (classes.includes('winnerTERRORIST') || classes.includes('winnerCT')) {
            row.kind = 'roundOver';
            row.winnerSide = classes.includes('winnerTERRORIST') ? 'T' : 'CT';
            const m = (row.text || '').match(/\((\d+)\s*-\s*(\d+)\)\s*-\s*(.+?)\s*$/);
            if (m) {
                row.tScore = parseInt(m[1], 10);  // first number is winner side's running score
                row.ctScore = parseInt(m[2], 10);
                row.reason = m[3].trim();
            }
        } else {
            row.kind = 'other';
        }
        out.rows.push(row);
    });
    return out;
}
"""


# ──────────────────────────────────────────────────────────────────────
# Event types
# ──────────────────────────────────────────────────────────────────────

@dataclass
class ScorebotEvent:
    """Base type for a single live event. Subclasses are concrete kinds."""
    ts: datetime = field(default_factory=datetime.now)
    kind: str = "event"
    # Filled in by the bridge at emit time from the current scoreboard
    # state so the UI can label each line with the round it belongs to.
    round: int | None = None
    map: str | None = None


@dataclass
class OtherEvent(ScorebotEvent):
    """Catch-all for any .gamelogBox class we don't have a typed parser for."""
    kind: str = "other"
    text: str = ""
    classes: str = ""


@dataclass
class KillEvent(ScorebotEvent):
    kind: str = "kill"
    killer: str = ""
    killer_side: Side = "CT"
    victim: str = ""
    victim_side: Side = "T"
    assist: str | None = None
    flash_assist: bool = False
    headshot: bool = False
    weapon: str | None = None


@dataclass
class BombPlantEvent(ScorebotEvent):
    kind: str = "bombPlant"
    planter: str = ""
    site: str | None = None
    t_alive: int | None = None
    ct_alive: int | None = None


@dataclass
class BombDefuseEvent(ScorebotEvent):
    kind: str = "bombDefuse"
    defuser: str = ""


@dataclass
class RoundStartEvent(ScorebotEvent):
    kind: str = "roundStart"


@dataclass
class RoundOverEvent(ScorebotEvent):
    kind: str = "roundOver"
    winner_side: Side = "T"
    t_score: int | None = None
    ct_score: int | None = None
    reason: str = ""


@dataclass
class ScoreboardState:
    """Snapshot of the top-bar state. Pushed on every tick."""
    round: int | None = None
    map: str | None = None
    ct_score: int | None = None
    t_score: int | None = None
    time: str | None = None
    bomb_planted: bool = False


@dataclass
class LivePlayer:
    nick: str
    alive: bool = True
    hp: int | None = None
    money: int | None = None
    kills: int = 0
    assists: int = 0
    deaths: int = 0
    adr: float | None = None

    @property
    def kd(self) -> str:
        return f"{self.kills}-{self.deaths}"


@dataclass
class TeamScoreboard:
    """One team's currently-live scoreboard, pushed every tick."""
    name: str = ""
    side: Literal["CT", "T"] | None = None
    players: list[LivePlayer] = field(default_factory=list)


@dataclass
class PlayerScoreboard:
    """Both teams' live rosters. Pushed once per tick."""
    teams: list[TeamScoreboard] = field(default_factory=list)


@dataclass
class SeriesMap:
    index: int
    map: str | None = None
    team_a: str | None = None
    team_b: str | None = None
    score_a: int | None = None
    score_b: int | None = None


@dataclass
class SeriesSnapshot:
    """Per-map breakdown of the series, plus computed map wins."""
    maps: list[SeriesMap] = field(default_factory=list)
    wins_a: int = 0
    wins_b: int = 0

    @property
    def current_map_index(self) -> int | None:
        """1-based index of the currently-active map — the first map
        where neither side has hit the CS2 MR12 regulation threshold of
        13 rounds yet. Maps that ended at 13+ (or in overtime) are
        skipped; the first map with no scores at all (a fresh series)
        falls through and is returned."""
        for m in self.maps:
            a = m.score_a or 0
            b = m.score_b or 0
            if a < 13 and b < 13:
                return m.index
        return None

    @property
    def current_map(self) -> str | None:
        idx = self.current_map_index
        if idx is None or idx > len(self.maps):
            return None
        return self.maps[idx - 1].map


# ──────────────────────────────────────────────────────────────────────
# The bridge
# ──────────────────────────────────────────────────────────────────────

class ScorebotBridge:
    """One bridge instance corresponds to one open match page."""

    # Bound the dedup set so a multi-hour session can't slowly grow it
    # without limit. The virtualised gamelog only ever shows ~15 rows, so
    # 2k is plenty of recency.
    _MAX_SEEN_KEYS = 2000
    # Stop the polling loop after this many consecutive evaluate errors
    # (page closed, browser crash, network drop) so we don't spam the
    # queue with error events forever.
    _MAX_CONSECUTIVE_ERRORS = 8

    def __init__(self, *, poll_interval: float = 1.0) -> None:
        if async_playwright is None:
            raise RuntimeError("playwright is not installed; pip install playwright")
        self._poll_interval = poll_interval
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue[ScorebotEvent | ScoreboardState | PlayerScoreboard | SeriesSnapshot] = asyncio.Queue()
        self._seen_keys: list[str] = []  # ordered; bounded
        self._seen_keys_set: set[str] = set()
        self._started_at: float = 0.0
        self._latest_state: ScoreboardState | None = None

    async def start(self, match_url: str) -> None:
        """Launch the browser and start polling the gamelog.

        Safe to call repeatedly; each call tears down any prior session.
        """
        await self.stop()
        # On macOS, capture which app currently has focus so we can hand
        # it back after Chromium launches and inevitably grabs focus.
        prev_frontmost: str | None = None
        if sys.platform == "darwin":
            prev_frontmost = await _capture_frontmost_app()
        self._pw = await async_playwright().start()
        # We must launch *visible* — HLTV's Cloudflare/anti-bot kills the
        # scorebot WebSocket within a second when it detects headless
        # Chromium, falling back to slow XHR polling that doesn't carry
        # live events. Pushing the window off-screen keeps the UX clean
        # from the user's perspective.
        self._browser = await self._pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-position=-2400,-2400",
                "--window-size=1280,720",
                "--no-default-browser-check",
                "--no-first-run",
                "--mute-audio",
            ],
        )
        # On macOS Chromium grabs focus and briefly shows in the Dock
        # on launch. Hide it and bounce focus back to whatever app was
        # frontmost before us. Wait ~250ms for macOS to finish
        # activating Chromium first, otherwise our `set frontmost`
        # fires before Chromium's activation is in the system event
        # queue and gets overwritten by it.
        if sys.platform == "darwin":
            await asyncio.sleep(0.25)
            await _hide_chromium_and_restore_focus(prev_frontmost)
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )
        self._page = await self._context.new_page()
        await self._page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
        self._started_at = asyncio.get_running_loop().time()
        # Give the scorebot a beat to populate before we start emitting,
        # otherwise we'll flood the kill feed with "historic" events.
        await asyncio.sleep(2.0)
        await self._prime()
        self._task = asyncio.create_task(self._loop())

    async def _prime(self) -> None:
        """Seed the seen-set with whatever's already in the log so we
        don't emit historical entries as 'new'."""
        assert self._page is not None
        try:
            data = await self._page.evaluate(_EXTRACT_JS)
        except Exception:
            return
        for row in data.get("rows", []):
            self._add_seen_key(_row_key(row))

    async def navigate(self, match_url: str) -> None:
        """Point the existing browser at a different match without
        re-launching it. Keeps the focus-steal cost to once-per-app."""
        if self._page is None:
            # Bridge wasn't started; this is a programmer error but be
            # forgiving — fall back to a full start.
            await self.start(match_url)
            return
        # Stop the poll loop while we navigate so it doesn't read a
        # transitional DOM and emit junk events.
        self.pause()
        try:
            await self._page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            await self._queue.put(_error_event(f"navigate: {e}"))
            return
        # New match → reset dedup + state; reprime fresh.
        self._seen_keys.clear()
        self._seen_keys_set.clear()
        self._latest_state = None
        await asyncio.sleep(2.0)
        await self._prime()
        self.resume()

    def pause(self) -> None:
        """Stop the poll loop but keep the browser alive."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def resume(self) -> None:
        """Restart the poll loop after a pause. No-op if already running
        or if start() was never called."""
        if self._task and not self._task.done():
            return
        if self._page is None:
            return
        self._task = asyncio.create_task(self._loop())

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = self._browser = self._pw = self._page = None
        self._seen_keys.clear()
        self._seen_keys_set.clear()

    def _add_seen_key(self, key: str) -> None:
        if key in self._seen_keys_set:
            return
        self._seen_keys.append(key)
        self._seen_keys_set.add(key)
        if len(self._seen_keys) > self._MAX_SEEN_KEYS:
            # Evict oldest. The visible gamelog has ~15 rows, so anything
            # this old has long since scrolled off and won't recur.
            drop = self._seen_keys[: len(self._seen_keys) - self._MAX_SEEN_KEYS]
            self._seen_keys = self._seen_keys[-self._MAX_SEEN_KEYS:]
            for k in drop:
                self._seen_keys_set.discard(k)

    async def _loop(self) -> None:
        assert self._page is not None
        errors = 0
        while True:
            try:
                data = await self._page.evaluate(_EXTRACT_JS)
                errors = 0
            except Exception as e:
                errors += 1
                await self._queue.put(_error_event(str(e)))
                if errors >= self._MAX_CONSECUTIVE_ERRORS:
                    # The page is gone or the browser died. Stop spamming
                    # the queue; let the parent observe via status.
                    return
                await asyncio.sleep(self._poll_interval)
                continue

            sb = data.get("scoreboard")
            if sb:
                state = _to_state(sb)
                self._latest_state = state
                await self._queue.put(state)

            teams_raw = data.get("teams") or []
            if teams_raw:
                await self._queue.put(_to_player_scoreboard(teams_raw))

            series_raw = data.get("series")
            if series_raw:
                await self._queue.put(_to_series(series_raw))

            current_round = self._latest_state.round if self._latest_state else None
            current_map = self._latest_state.map if self._latest_state else None
            for row in data.get("rows", []) or []:
                key = _row_key(row)
                if key in self._seen_keys_set:
                    continue
                self._add_seen_key(key)
                evt = _to_event(row)
                if evt is not None:
                    evt.round = current_round
                    evt.map = current_map
                    await self._queue.put(evt)

            await asyncio.sleep(self._poll_interval)

    async def events(self) -> AsyncIterator[ScorebotEvent | ScoreboardState | PlayerScoreboard | SeriesSnapshot]:
        while True:
            item = await self._queue.get()
            yield item

    def get_nowait(self) -> ScorebotEvent | ScoreboardState | PlayerScoreboard | SeriesSnapshot | None:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None


# ──────────────────────────────────────────────────────────────────────
# Row → event mapping (pure functions; no Playwright in unit tests)
# ──────────────────────────────────────────────────────────────────────

def _row_key(row: dict[str, Any]) -> str:
    """A stable key for a gamelog row so we can dedup across polls."""
    kind = row.get("kind", "?")
    text = row.get("text") or ""
    # For round events the text repeats every round; combine with the
    # currently-displayed round score embedded in the text.
    return f"{kind}|{text}"


def _to_series(raw: dict[str, Any]) -> SeriesSnapshot:
    maps: list[SeriesMap] = []
    for entry in raw.get("maps") or []:
        maps.append(
            SeriesMap(
                index=int(entry.get("index") or 0),
                map=entry.get("map"),
                team_a=entry.get("team_a"),
                team_b=entry.get("team_b"),
                score_a=entry.get("score_a"),
                score_b=entry.get("score_b"),
            )
        )
    return SeriesSnapshot(
        maps=maps,
        wins_a=int(raw.get("wins_a") or 0),
        wins_b=int(raw.get("wins_b") or 0),
    )


def _to_player_scoreboard(teams_raw: list[dict[str, Any]]) -> PlayerScoreboard:
    teams: list[TeamScoreboard] = []
    for t in teams_raw:
        players = [
            LivePlayer(
                nick=str(p.get("nick") or "?"),
                alive=bool(p.get("alive", True)),
                hp=p.get("hp"),
                money=p.get("money"),
                kills=int(p.get("kills") or 0),
                assists=int(p.get("assists") or 0),
                deaths=int(p.get("deaths") or 0),
                adr=p.get("adr"),
            )
            for p in (t.get("players") or [])
        ]
        teams.append(
            TeamScoreboard(
                name=str(t.get("name") or ""),
                side=t.get("side"),
                players=players,
            )
        )
    return PlayerScoreboard(teams=teams)


def _to_state(sb: dict[str, Any]) -> ScoreboardState:
    return ScoreboardState(
        round=sb.get("round"),
        map=sb.get("map"),
        ct_score=sb.get("ctScore"),
        t_score=sb.get("tScore"),
        time=sb.get("time"),
        bomb_planted=bool(sb.get("bombPlanted")),
    )


def _to_event(row: dict[str, Any]) -> ScorebotEvent | None:
    kind = row.get("kind")
    if kind == "kill":
        return KillEvent(
            killer=row.get("killer") or "",
            killer_side=row.get("killerSide") or "CT",
            victim=row.get("victim") or "",
            victim_side=row.get("victimSide") or "T",
            assist=row.get("assist"),
            flash_assist=bool(row.get("flashAssist")),
            headshot=bool(row.get("headshot")),
            weapon=row.get("weapon"),
        )
    if kind == "bombPlant":
        return BombPlantEvent(
            planter=row.get("planter") or "",
            site=row.get("site"),
            t_alive=row.get("tAlive"),
            ct_alive=row.get("ctAlive"),
        )
    if kind == "bombDefuse":
        return BombDefuseEvent(defuser=row.get("defuser") or "")
    if kind == "roundStart":
        return RoundStartEvent()
    if kind == "roundOver":
        return RoundOverEvent(
            winner_side=row.get("winnerSide") or "T",
            t_score=row.get("tScore"),
            ct_score=row.get("ctScore"),
            reason=row.get("reason") or "",
        )
    # Anything else — surface as an OtherEvent so the user sees the raw
    # text instead of silently dropping a gamelog line.
    text = (row.get("text") or "").strip()
    classes = (row.get("classes") or "").strip()
    if not text:
        return None
    return OtherEvent(text=text, classes=classes)


def _error_event(msg: str) -> ScorebotEvent:
    e = ScorebotEvent(kind="error")
    setattr(e, "msg", msg)
    return e
