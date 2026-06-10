"""HLTV scorebot bridge.

Runs an off-screen Chromium against an HLTV match page and taps the live
scorebot stream by running *our own* Engine.IO v3 long-polling client
inside the page, against `scorebot-lb.hltv.org`. We parse the raw
`log` + `scoreboard` events directly instead of scraping the rendered
DOM.

Why this design (learned the hard way — see scripts/probe_*.py):
1. The DOM is a lossy projection of the stream: HLTV virtualises the
   `.gamelog` to ~15 rows, so bursts scroll out between polls, and rows
   carry no stable id so identical kills across rounds get deduped away.
   That's the "lost a lot of events" symptom. The socket carries every
   event with a stable `eventId`.
2. We can't connect from plain Python — `scorebot-lb.hltv.org` is behind
   Cloudflare. But a `fetch` issued *from the page context* inherits the
   page's cookies / TLS / origin and clears Cloudflare exactly like
   HLTV's own client.
3. We can't patch the page's WebSocket/XHR to observe HLTV's own socket:
   doing so breaks socket.io's long-poll transport and the live feed
   dies. So we open a *separate* connection we fully control and read.
4. In an automated browser the WebSocket upgrade is always killed by
   anti-bot, so the stream runs over binary XHR long-polling. Our client
   stays on polling (`b64=1`, text framing) and auto-reconnects; each
   connect replays the full match backlog, so eventId dedup makes kill
   capture lossless even across reconnects.

Round transitions are derived from the authoritative `scoreboard`
(currentRound / score deltas) so they're never missed across reconnects;
the per-map series header + round clock come from a light read-only DOM
poll (reading the DOM is safe; only patching transports is not).

The bridge is async-friendly so it shares the TUI's event loop. It
exposes events as an async queue.
"""

from __future__ import annotations

import asyncio
import json
import re
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

    // ── round-attribution walk ──────────────────────────────────────
    // The gamelog accumulates across rounds AND across maps, but
    // scoreboard.round resets at every new map. So stamping each event
    // with the live scoreboard's round mis-labels both (a) events from
    // earlier maps and (b) the last few kills of round N when the poll
    // landed just after round N+1 began.
    //
    // We walk newest-to-oldest and rebuild round numbers from the
    // ground truth in the log itself:
    //   - A roundOver row carries (tScore - ctScore); their sum is the
    //     round that just ended.
    //   - A roundStart row advances the round counter by 1 going from
    //     newer to older (the round started here; older events were in
    //     the previous round).
    //   - A roundOver with score >= 13 marks the END of a map: older
    //     events belong to a previous map (mapsAgo += 1).
    // Map boundaries (from this walk's perspective) sit at "R1 started"
    // markers — anything OLDER than the R1 start of map M is from map
    // M-1. We bump mapsAgo there, not at the map-end round_over (which
    // is *inside* the older map's stream).
    const stateRound = (out.scoreboard && out.scoreboard.round) || null;
    let current = stateRound;
    let mapsAgo = 0;
    out.rows.forEach(row => {
        if (row.kind === 'roundOver') {
            const a = row.tScore || 0;
            const b = row.ctScore || 0;
            const justEnded = a + b;
            row.round = justEnded > 0 ? justEnded : current;
            row.mapsAgo = mapsAgo;
            // The round-over for round N is itself inside round N's
            // territory: older events here are still in round N until
            // we hit round N's start. Keep current = justEnded.
            current = justEnded > 0 ? justEnded : current;
        } else if (row.kind === 'roundStart') {
            row.round = current;
            row.mapsAgo = mapsAgo;
            if (current != null && current > 1) {
                current -= 1;
            } else {
                // We just walked past R1 of the current visible map —
                // older events are from the previous map.
                mapsAgo += 1;
                current = null;
            }
        } else {
            row.round = current;
            row.mapsAgo = mapsAgo;
        }
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
    # World coordinates from the socket Kill payload — used to derive an
    # approximate map zone for the kill. None when absent.
    killer_x: float | None = None
    killer_y: float | None = None
    victim_x: float | None = None
    victim_y: float | None = None


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
# In-page Engine.IO v3 polling client
# ──────────────────────────────────────────────────────────────────────
#
# Injected into the match page and invoked with {listId, gen}. It runs a
# self-reconnecting Engine.IO v3 long-poll against scorebot-lb.hltv.org
# and forwards every Socket.IO event ({ev, arg}) to Python via the
# `__csmatch_pkt` binding, tagged with its generation `g`. A newer
# invocation (higher window.__csmatchGen) supersedes older loops, so
# pause()/navigate() can cleanly stop the in-page client.
_CLIENT_JS = r"""
(cfg) => {
  const listId = String(cfg.listId), gen = cfg.gen;
  window.__csmatchGen = gen;
  const base = "https://scorebot-lb.hltv.org/socket.io/";
  const rnd = () => Math.random().toString(36).slice(2, 10);
  const q = (extra) => `?EIO=3&transport=polling&b64=1&t=${rnd()}${extra || ""}`;
  const alive = () => window.__csmatchGen === gen;
  const emit = (o) => { try { window.__csmatch_pkt(Object.assign({g: gen}, o)); } catch (e) {} };
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  // EIO3 text-mode payload: "<len>:<packet><len>:<packet>..."
  const parsePayload = (txt) => {
    const out = []; let i = 0;
    while (i < txt.length) {
      const j = txt.indexOf(":", i); if (j < 0) break;
      const n = parseInt(txt.slice(i, j), 10); if (isNaN(n)) break;
      out.push(txt.slice(j + 1, j + 1 + n)); i = j + 1 + n;
    }
    return out;
  };
  async function connect() {
    const r = await fetch(base + q(""));
    const openPkt = parsePayload(await r.text()).find(p => p[0] === "0");
    if (!openPkt) throw new Error("no open packet");
    const info = JSON.parse(openPkt.slice(1));
    const sid = info.sid;
    const post = (body) => fetch(base + q(`&sid=${sid}`), {
      method: "POST", body, headers: {"Content-Type": "text/plain;charset=UTF-8"},
    });
    await post("40");
    await post("42" + JSON.stringify(["readyForMatch", JSON.stringify({token: "", listId})]));
    return {sid, post, pingInterval: info.pingInterval || 25000};
  }
  function dispatch(pkts) {
    for (const pkt of pkts) {
      if (pkt.startsWith("42")) {
        try { const arr = JSON.parse(pkt.slice(2)); if (Array.isArray(arr) && arr.length) emit({ev: arr[0], arg: arr[1]}); } catch (e) {}
      } else if (pkt[0] === "1") {
        throw new Error("engine close");
      }
    }
  }
  (async () => {
    let backoff = 400;
    while (alive()) {
      let c;
      try { c = await connect(); emit({ev: "_status", status: "connected"}); backoff = 400; }
      catch (e) { emit({ev: "_status", status: "connect_err", msg: String(e && e.message || e)}); await sleep(backoff); backoff = Math.min(backoff * 2, 8000); continue; }
      // Inner poll loop. A long-poll that resets mid-flight is NORMAL
      // (Cloudflare/LB drop idle connections); we retry the SAME sid
      // rather than re-handshaking, since the Engine.IO session lives
      // ~pingTimeout server-side. Re-handshaking on every reset floods
      // scorebot-lb with new-session requests, which trips Cloudflare and
      // then blocks page navigation. Only a 400/explicit close drops us
      // back to a fresh handshake.
      let lastPing = Date.now(), fails = 0;
      while (alive()) {
        if (Date.now() - lastPing > c.pingInterval - 5000) {
          try { await c.post("2"); lastPing = Date.now(); } catch (e) {}
        }
        let pr;
        try { pr = await fetch(base + q(`&sid=${c.sid}`)); }
        catch (e) { if (++fails >= 8) break; await sleep(Math.min(300 * fails, 2000)); continue; }
        if (pr.status === 400 || pr.status === 403 || pr.status === 404) break;
        if (pr.status !== 200) { if (++fails >= 8) break; await sleep(500); continue; }
        fails = 0;
        try { dispatch(parsePayload(await pr.text())); }
        catch (e) { break; }   // engine-level close -> fresh handshake
      }
    }
  })();
}
"""


# ──────────────────────────────────────────────────────────────────────
# Socket payload → event mapping (pure functions; no Playwright)
# ──────────────────────────────────────────────────────────────────────

# HLTV log/scoreboard sides are "TERRORIST"/"CT"; our model uses "T"/"CT".
def _side(s: str | None) -> Side | None:
    if s == "TERRORIST":
        return "T"
    if s == "CT":
        return "CT"
    return None


# winType → the human reason string the kill feed shows.
_WINTYPE_REASON = {
    "Target_Bombed": "Target bombed",
    "Bomb_Defused": "Bomb defused",
    "Target_Saved": "Target saved",
    "CTs_Win": "Enemy eliminated",
    "Terrorists_Win": "Enemy eliminated",
    "Round_Draw": "Round draw",
}


def _wintype_reason(wt: str | None) -> str:
    return _WINTYPE_REASON.get(wt or "", wt or "")


def _kill_event(d: dict[str, Any], assist_nick: str | None) -> KillEvent:
    """Build a KillEvent from a socket `Kill` entry. A regular assist
    (separate `Assist` entry, linked by killEventId) takes the assist
    slot; otherwise a flash-assist (`flasherNick` on the kill) does."""
    flasher = d.get("flasherNick")
    if assist_nick:
        assist, flash = assist_nick, False
    elif flasher:
        assist, flash = flasher, True
    else:
        assist, flash = None, False
    return KillEvent(
        killer=d.get("killerNick") or d.get("killerName") or "",
        killer_side=_side(d.get("killerSide")) or "CT",
        victim=d.get("victimNick") or d.get("victimName") or "",
        victim_side=_side(d.get("victimSide")) or "T",
        assist=assist,
        flash_assist=flash,
        headshot=bool(d.get("headShot")),
        weapon=d.get("weapon"),
        killer_x=d.get("killerX"),
        killer_y=d.get("killerY"),
        victim_x=d.get("victimX"),
        victim_y=d.get("victimY"),
    )


def _players_from_scoreboard(sb: dict[str, Any]) -> PlayerScoreboard:
    teams: list[TeamScoreboard] = []
    for key, side, name_key in (
        ("CT", "CT", "ctTeamName"),
        ("TERRORIST", "T", "terroristTeamName"),
    ):
        players = [
            LivePlayer(
                nick=str(p.get("nick") or p.get("name") or "?"),
                alive=bool(p.get("alive", True)),
                hp=p.get("hp"),
                money=p.get("money"),
                kills=int(p.get("score") or 0),
                assists=int(p.get("assists") or 0),
                deaths=int(p.get("deaths") or 0),
                adr=p.get("damagePrRound"),
            )
            for p in (sb.get(key) or [])
        ]
        teams.append(TeamScoreboard(name=str(sb.get(name_key) or ""), side=side, players=players))
    return PlayerScoreboard(teams=teams)


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


# ──────────────────────────────────────────────────────────────────────
# The bridge
# ──────────────────────────────────────────────────────────────────────

class ScorebotBridge:
    """One bridge instance corresponds to one open match page."""

    # Bound the dedup sets so a multi-hour session can't grow them without
    # limit. A map has ~24-30 rounds × ~10 kills, so a few thousand keys
    # covers many maps of recency.
    _MAX_SEEN = 6000
    # A log frame with at most this many entries is treated as a live
    # increment (current scoreboard context is authoritative); larger
    # frames are full-backlog replays (connect / reconnect).
    _INCREMENT_MAX = 8

    def __init__(self, *, poll_interval: float = 1.0) -> None:
        if async_playwright is None:
            raise RuntimeError("playwright is not installed; pip install playwright")
        self._poll_interval = poll_interval
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._dom_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[ScorebotEvent | ScoreboardState | PlayerScoreboard | SeriesSnapshot] = asyncio.Queue()
        self._match_id: str | None = None
        self._gen = 0
        self._running = False
        self._exposed = False
        self._status: str | None = None
        self._reset_match_state()

    # ── match-scoped state ──────────────────────────────────────────
    def _reset_match_state(self) -> None:
        self._primed = False                 # has the connect backlog been seeded?
        self._seen_kills: set[int] = set()
        self._seen_kills_order: list[int] = []
        self._roundend_reason: dict[tuple[int, int], str] = {}
        self._recent_planter: str | None = None
        self._recent_plant_alive: tuple[int | None, int | None] = (None, None)
        # scoreboard-derived round/bomb tracking
        self._sb_init = False
        self._prev_round: int | None = None
        self._prev_t: int | None = None
        self._prev_ct: int | None = None
        self._bomb_planted = False
        self._bomb_this_round = False        # bomb planted at any point this round
        self._cur_round: int | None = None
        self._cur_map: str | None = None
        self._clock: str | None = None
        # latest scoreboard scores (for the 1Hz clock-refreshed state push)
        self._disp_t: int | None = None
        self._disp_ct: int | None = None
        self._have_sb = False
        # coalescing
        self._last_state_key: tuple | None = None
        self._last_players_key: tuple | None = None
        self._last_series_key: tuple | None = None

    # ── lifecycle ───────────────────────────────────────────────────
    async def start(self, match_url: str) -> None:
        """Launch the browser and start streaming the scorebot.

        Safe to call repeatedly; each call tears down any prior session.
        """
        await self.stop()
        prev_frontmost: str | None = None
        if sys.platform == "darwin":
            prev_frontmost = await _capture_frontmost_app()
        self._pw = await async_playwright().start()
        # Launch visible-but-off-screen: a real (non-headless) browser is
        # what clears Cloudflare so our in-page fetch to the scorebot
        # succeeds. Off-screen keeps the UX clean.
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
        # Expose the packet binding once; it survives navigations.
        await self._page.expose_function("__csmatch_pkt", self._on_packet)
        self._exposed = True
        await self._page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
        self._match_id = _match_id_from(match_url)
        await self._spin_up()

    async def _spin_up(self) -> None:
        """(Re)inject the in-page client and start the DOM poll. Bumps the
        generation so any prior in-page loop supersedes itself."""
        assert self._page is not None
        self._reset_match_state()
        self._gen += 1
        self._running = True
        await self._page.evaluate(_CLIENT_JS, {"listId": self._match_id, "gen": self._gen})
        self._dom_task = asyncio.create_task(self._dom_loop())

    async def navigate(self, match_url: str) -> None:
        """Point the existing browser at a different match without
        re-launching it (so we keep the focus-steal cost to once-per-app).

        Our extra scorebot-lb polling flags Cloudflare for the session, and
        that flag sticks until `cf_clearance` is reset — so a plain goto to
        the next match lands on Cloudflare's "Just a moment…" wall and the
        scorebot never connects. Clearing cookies first drops the flag; the
        fresh load re-triggers (and auto-solves) the challenge. This needs
        no Chromium relaunch, so focus stays put."""
        if self._page is None or self._context is None:
            await self.start(match_url)
            return
        self.pause()
        try:
            await self._context.clear_cookies()
        except Exception:
            pass
        try:
            await self._page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            # Leave paused; the caller surfaces status. A later expand
            # retries via start()/navigate().
            return
        self._match_id = _match_id_from(match_url)
        await self._spin_up()

    def pause(self) -> None:
        """Stop streaming but keep the browser alive. Bumping the
        generation makes the in-page loop exit on its next tick."""
        self._running = False
        self._gen += 1
        if self._dom_task and not self._dom_task.done():
            self._dom_task.cancel()
        self._dom_task = None

    def resume(self) -> None:
        """Restart streaming after a pause. No-op if already running or if
        start() was never called."""
        if self._running or self._page is None:
            return
        asyncio.create_task(self._spin_up())

    @property
    def is_running(self) -> bool:
        return self._running

    async def stop(self) -> None:
        self._running = False
        self._gen += 1
        if self._dom_task and not self._dom_task.done():
            self._dom_task.cancel()
            try:
                await self._dom_task
            except (asyncio.CancelledError, Exception):
                pass
        self._dom_task = None
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
        self._exposed = False
        self._reset_match_state()

    # ── packet handling ─────────────────────────────────────────────
    def _on_packet(self, o: dict[str, Any]) -> None:
        """Binding callback for every Socket.IO event from the in-page
        client. Runs in the event loop, so put_nowait is safe."""
        if not self._running or o.get("g") != self._gen:
            return
        ev = o.get("ev")
        if ev == "log":
            self._handle_log(o.get("arg"))
        elif ev == "scoreboard":
            self._handle_scoreboard(o.get("arg"))
        elif ev == "_status":
            self._status = o.get("status")

    def _put(self, item: ScorebotEvent | ScoreboardState | PlayerScoreboard | SeriesSnapshot) -> None:
        self._queue.put_nowait(item)

    def _add_kill_id(self, eid: int) -> None:
        self._seen_kills.add(eid)
        self._seen_kills_order.append(eid)
        if len(self._seen_kills_order) > self._MAX_SEEN:
            drop = self._seen_kills_order[: len(self._seen_kills_order) - self._MAX_SEEN]
            self._seen_kills_order = self._seen_kills_order[-self._MAX_SEEN:]
            for k in drop:
                self._seen_kills.discard(k)

    def _handle_log(self, arg: Any) -> None:
        if isinstance(arg, str):
            try:
                arg = json.loads(arg)
            except Exception:
                return
        if not isinstance(arg, dict):
            return
        entries = arg.get("log") or []
        if not entries:
            return
        # Small frames are live increments (current scoreboard context is
        # authoritative); large frames are full-backlog replays.
        is_increment = len(entries) <= self._INCREMENT_MAX
        # The backlog is newest-first; walk chronologically.
        chrono = list(reversed(entries))
        # Index assists by the kill they belong to (same frame, adjacent).
        assists: dict[Any, str] = {}
        for e in chrono:
            if isinstance(e, dict) and "Assist" in e:
                ad = e.get("Assist") or {}
                if ad.get("killEventId") is not None and ad.get("assisterNick"):
                    assists[ad["killEventId"]] = ad["assisterNick"]
        # Don't emit the first connect backlog — it's history. Seed dedup
        # only. Every later frame (live increment OR reconnect replay) is
        # processed normally, so gap events still surface exactly once.
        emit = self._primed
        for e in chrono:
            if not isinstance(e, dict) or not e:
                continue
            kind = next(iter(e))
            d = e.get(kind) or {}
            if kind == "Kill":
                eid = d.get("eventId")
                if eid is None or eid in self._seen_kills:
                    continue
                self._add_kill_id(eid)
                # Skip warmup deathmatch kills (round 0): they're a flood
                # of respawns that aren't match events. `!= 0` never drops
                # a real round (>=1) kill, and emits when the round is still
                # unknown (None) so we never lose genuine events.
                if emit and self._cur_round != 0:
                    evt = _kill_event(d, assists.get(eid))
                    evt.round = self._cur_round
                    evt.map = self._cur_map
                    self._put(evt)
            elif kind == "RoundEnd" and is_increment:
                # Only trust RoundEnd reasons from live increments. A
                # reconnect backlog replays *every map's* rounds, which
                # would otherwise repopulate this map's (t,ct) keys with
                # other maps' reasons (round scores collide across maps).
                t, ct = d.get("terroristScore"), d.get("counterTerroristScore")
                if t is not None and ct is not None:
                    self._roundend_reason[(t, ct)] = _wintype_reason(d.get("winType"))
                    if len(self._roundend_reason) > 200:
                        self._roundend_reason.pop(next(iter(self._roundend_reason)))
            elif kind == "BombPlanted":
                # Buffer the planter; the plant event itself is emitted off
                # the scoreboard's bombPlanted transition (never missed).
                self._recent_planter = d.get("playerNick") or d.get("playerName") or ""
                self._recent_plant_alive = (d.get("tPlayers"), d.get("ctPlayers"))
        if not self._primed:
            self._primed = True

    def _round_reason(self, winner: Side, t: int, ct: int) -> str:
        """Reason string for a round-over. Prefer the authoritative
        winType from a live RoundEnd; fall back to a heuristic from the
        winner and whether the bomb went down this round (covers
        round-overs whose RoundEnd only arrived via a reconnect backlog)."""
        buffered = self._roundend_reason.get((t, ct))
        if buffered:
            return buffered
        if self._bomb_this_round:
            return "Target bombed" if winner == "T" else "Bomb defused"
        return "Enemy eliminated"

    def _emit_state(self) -> None:
        """Push a ScoreboardState built from the latest socket scoreboard
        plus the freshest DOM clock. Called both on scoreboard events and
        on each DOM tick, so the round clock ticks at ~1Hz even when the
        socket is quiet."""
        if not self._have_sb:
            return
        key = (self._cur_round, self._cur_map, self._disp_ct, self._disp_t, self._clock, self._bomb_planted)
        if key == self._last_state_key:
            return
        self._last_state_key = key
        self._put(ScoreboardState(
            round=self._cur_round, map=self._cur_map,
            ct_score=self._disp_ct, t_score=self._disp_t,
            time=self._clock, bomb_planted=self._bomb_planted,
        ))

    def _handle_scoreboard(self, arg: Any) -> None:
        if isinstance(arg, str):
            try:
                arg = json.loads(arg)
            except Exception:
                return
        if not isinstance(arg, dict):
            return
        cur_round = arg.get("currentRound")
        t = arg.get("terroristScore")
        ct = arg.get("counterTerroristScore")
        mp = arg.get("mapName")
        bomb = bool(arg.get("bombPlanted"))
        map_changed = mp != self._cur_map and self._cur_map is not None

        if map_changed:
            # New map → round-over reasons and bomb-planter are keyed by
            # per-map round scores, so wipe them to avoid stale carry-over
            # (e.g. map 1's round that ended 2-1 mislabelling map 2's).
            self._roundend_reason.clear()
            self._recent_planter = None
            self._recent_plant_alive = (None, None)
        if mp is not None:
            self._cur_map = mp
        if cur_round is not None:
            self._cur_round = cur_round

        # Derive round/bomb events from the authoritative scoreboard. We
        # skip the tick where the map changed (scores reset) to avoid
        # spurious transitions.
        if self._sb_init and not map_changed:
            if (
                t is not None and ct is not None
                and self._prev_t is not None and self._prev_ct is not None
                and (t + ct) > (self._prev_t + self._prev_ct)
            ):
                winner: Side = "T" if t > self._prev_t else "CT"
                evt = RoundOverEvent(
                    winner_side=winner, t_score=t, ct_score=ct,
                    reason=self._round_reason(winner, t, ct),
                )
                evt.round = t + ct
                evt.map = self._cur_map
                self._put(evt)
                self._bomb_this_round = False
            if (
                cur_round is not None and self._prev_round is not None
                and cur_round > self._prev_round
            ):
                rs = RoundStartEvent()
                rs.round = cur_round
                rs.map = self._cur_map
                self._put(rs)
                self._bomb_this_round = False
            if bomb and not self._bomb_planted:
                bp = BombPlantEvent(
                    planter=self._recent_planter or "",
                    t_alive=self._recent_plant_alive[0],
                    ct_alive=self._recent_plant_alive[1],
                )
                bp.round = self._cur_round
                bp.map = self._cur_map
                self._put(bp)
        if bomb:
            self._bomb_this_round = True

        self._bomb_planted = bomb
        self._prev_round, self._prev_t, self._prev_ct = cur_round, t, ct
        self._sb_init = True
        self._disp_t, self._disp_ct = t, ct
        self._have_sb = True
        self._emit_state()

        players = _players_from_scoreboard(arg)
        players_key = tuple(
            (tm.name, tm.side, tuple((p.nick, p.alive, p.hp, p.money, p.kills, p.assists, p.deaths, p.adr) for p in tm.players))
            for tm in players.teams
        )
        if players_key != self._last_players_key:
            self._last_players_key = players_key
            self._put(players)

    # ── DOM poll (series header + round clock; read-only, non-invasive) ─
    async def _dom_loop(self) -> None:
        assert self._page is not None
        my_gen = self._gen
        while self._running and self._gen == my_gen:
            try:
                data = await self._page.evaluate(_EXTRACT_JS)
            except Exception:
                await asyncio.sleep(self._poll_interval)
                continue
            sb = data.get("scoreboard") or {}
            self._clock = sb.get("time")
            self._emit_state()
            series_raw = data.get("series")
            if series_raw:
                series = _to_series(series_raw)
                series_key = (
                    series.wins_a, series.wins_b,
                    tuple((m.index, m.map, m.score_a, m.score_b) for m in series.maps),
                )
                if series_key != self._last_series_key:
                    self._last_series_key = series_key
                    self._put(series)
            await asyncio.sleep(self._poll_interval)

    # ── consumer API ────────────────────────────────────────────────
    async def events(self) -> AsyncIterator[ScorebotEvent | ScoreboardState | PlayerScoreboard | SeriesSnapshot]:
        while True:
            item = await self._queue.get()
            yield item

    def get_nowait(self) -> ScorebotEvent | ScoreboardState | PlayerScoreboard | SeriesSnapshot | None:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None


def _match_id_from(url: str) -> str | None:
    m = re.search(r"/matches/(\d+)", url)
    return m.group(1) if m else None
