# Live log redesign — design spec

Date: 2026-06-10

## Goal

Make the HLTV live event log clearer, more comprehensive, and more
immersive, and add a natural-language ("narrative") display mode. The
live stream is already realtime + lossless (socket-tap bridge); this is
purely a presentation + light-analysis layer on top of the existing
event stream.

## Decisions (from brainstorming)

- **Narrative is a third display mode**, cycled by the existing `w` key:
  `default → narrative → monitor → default`. Narrative narrates the
  **event log** as prose; the scoreboard/maps stay structured (a tabular
  scoreboard has no sensible prose form).
- **Four context features**, all derived live from the kill + scoreboard
  stream (no extra network): round-result summaries, opening-kill marker,
  multi-kills/aces, clutch detection.
- **Kill location**: approximate bombsite-relative zone (A side / B side /
  mid / …) for popular maps, best-effort, graceful fallback (unknown map
  or out-of-range → omit, never error). Only **kills** get locations
  (the socket `BombPlanted` payload has no site/coords).
- **Visual style**: refined-clean ASCII (no required emoji/glyphs).
- **Narrative density**: concise — one sentence per kill/bomb; round
  start is a header only; round-over folds result + clutch into one line.

## Rendering — the three modes

Events are grouped into round blocks keyed by (map, round, epoch) — round
numbers repeat across maps, and the epoch increments on each round start
so warmup kills (which HLTV reports as round 1) don't merge into the real
round 1. Blocks render newest-first; entries inside a block render
chronologically (the round reads top-to-bottom: opening kill first,
round-over/clutch as the closing beat). Each block header carries the
round result once known, preferring the winning team's name over the bare
side label.

### Default (refined clean)
```
event log
 ── R14 · inferno ──────────────  CT win · defused · 8-6
  0:38  rikko + KRIMZ  →  torzsi   [ak47] HS  ·opening   A site
  0:29  rikko          →  Spinx    [ak47]                ramp
  0:12  Jackinho       →  rikko    [awp]                 apps
  0:04  Jackinho       →  Brollan  [deagle] HS  ·3K      A site
                                        ⤷ Jackinho 1v2 clutch
 ── R13 · inferno ──────────────  T win · bombed · 7-6
  ...
```
- side colors unchanged (T=yellow, CT=blue); killer/victim coloured by side.
- `·opening`, `·3K`/`·4K`/`·ACE`, `1vN clutch` are dim markers.
- location right-aligned/trailing, dim; omitted when unknown.

### Narrative (new)
```
event log
 ── round 14 · inferno · CT win 8-6 ──
  0:38  rikko opened the round, killing torzsi with an AK-47
        (headshot, assisted by KRIMZ) at A site
  0:29  rikko killed Spinx with an AK-47 near ramp
  0:12  Jackinho killed rikko with an AWP at apps
  0:04  Jackinho killed Brollan with a Desert Eagle (headshot) — 3K
        CT won the round — bomb defused (8-6). Jackinho clutched the 1v2.
```

### Monitor (existing theme, new markers folded in)
- Wording unchanged (`▸`, `abort`, `[deploy]`, …). New markers get monitor
  words: `·opening → entry`, `·3K → x3`, clutch → `solo-recover 1v2`.

## Module breakdown (interfaces are the contract)

All new modules are pure (no Playwright/network) and independently unit-tested.

### `scorebot.py` — capture kill coordinates (small change)
Add to `KillEvent`:
```python
killer_x: float | None = None
killer_y: float | None = None
victim_x: float | None = None
victim_y: float | None = None
```
In `_kill_event(d, assist_nick)`, extract `d.get("killerX")`, `"killerY"`,
`"victimX"`, `"victimY"` (already present in the socket Kill payload).
No other bridge behaviour changes.

### `locations.py` (new, pure)
```python
def zone_for(map_name: str | None, x: float | None, y: float | None) -> str | None:
    """Coarse bombsite-relative zone for a world coordinate on a known map.
    Returns e.g. 'A site', 'B site', 'mid', or None when the map is unknown
    or coords are missing."""
```
- Per-map landmark table in **world coordinates**:
  `_LANDMARKS = {"de_inferno": [("A site", x, y), ("B site", x, y), ("mid", x, y), ...], ...}`
  for: de_mirage, de_inferno, de_dust2, de_nuke, de_ancient, de_anubis,
  de_overpass, de_vertigo, de_train.
- Classify by **nearest landmark** (Euclidean in world units).
- Map not in table, x/y None or non-finite, or nearest landmark farther
  than ~3000 units (bogus data) → return None.
- Landmark values are approximate and **validated against live captures**
  during implementation; clearly commented as approximate.
- Tests: known coords near a site classify correctly; unknown map → None;
  None coords → None.

### `analysis.py` (new, pure)
```python
@dataclass
class RoundSummary:
    round: int | None
    map: str | None
    winner_side: str          # "CT" | "T"
    t_score: int | None
    ct_score: int | None
    reason: str

@dataclass
class Annotation:
    opening: bool = False         # first kill of the round
    multikill: int = 0            # killer's running kill count this round
    clutch: str | None = None     # e.g. "1v2" — set on the round-over of a WON clutch
    clutcher: str | None = None   # nick of the clutcher
    summary: RoundSummary | None = None  # set on RoundOverEvent

class RoundTracker:
    def feed_players(self, players: PlayerScoreboard) -> None: ...
    def feed_event(self, event: ScorebotEvent) -> Annotation: ...
```
Behaviour:
- Maintain latest rosters (nicks per side) from `feed_players`.
- **Round reset** on RoundStartEvent OR when an event's `.round` differs
  from the tracked round (robust to a missed round-start): alive sets =
  full rosters per side (fallback: empty → clutch detection simply
  disabled until rosters known), `kills_this_round = {}`, `opening_done =
  False`, clutch state cleared.
- **KillEvent**: `opening = not opening_done` (then set True);
  `kills_this_round[killer] += 1`; `multikill = that count`; remove victim
  from `alive[victim_side]`. After removal, if one side has exactly 1
  alive and the other ≥ 2 and no clutch recorded yet, record
  `(clutcher=lone nick, n=opponents alive)`.
- **RoundOverEvent**: set `summary`; if a clutch was recorded and the
  clutcher's side == winner, set `clutch="1v{n}"`, `clutcher`.
- Side keys use KillEvent `killer_side`/`victim_side` ("CT"/"T").

Edge cases (must be covered / reviewed):
- 1v1 is **not** a clutch (only record when opponents ≥ 2 at the moment
  the player becomes the lone survivor).
- Clutcher dies before round end → no clutch (their side has 0 → loses).
- Both sides reach 1 simultaneously → no clutch.
- Use opponent count **at clutch start** (not later trades).
- Rosters unknown (no scoreboard yet) → degrade gracefully: no clutch,
  but opening/multikill still work (kill-count based).
- Multi-kill counts only kills in the current round; resets each round.

### `narrate.py` (new, pure)
```python
def weapon_name(w: str | None) -> str:  # "ak47"->"AK-47", "m4a1_silencer"->"M4A1-S", ...
def narrate(event: ScorebotEvent, ann: Annotation, location: str | None) -> str:
    """One concise natural-language sentence for an enriched event.
    RoundStartEvent returns "" (the round header carries it)."""
```
- Kill: `"{killer} killed {victim} with {a/an} {weapon}"`; if opening →
  `"{killer} opened the round, killing {victim} …"`. The article is
  pronunciation-aware for initialisms ("an M4A1-S", "a USP-S"); a missing
  weapon omits the phrase entirely. Append in parens: headshot
  ("headshot") and assist ("assisted by {assist}", or "flashed by
  {assist}" when `flash_assist`). Append ` — {n}K`/` — ace` for
  multikill ≥ 3. Append ` at {location}` when present.
- BombPlant: `"{planter} planted the bomb"` (+ ` ({t}v{ct})` if alives known).
- BombDefuse: `"{defuser} defused the bomb"`.
- RoundOver: `"{winner} won the round — {reason, lowercased} ({t}-{ct})."`
  where winner prefers the team name (`RoundSummary.winner_team`, tracked
  from the scoreboard's side→name mapping) over the side; if clutch →
  append ` {clutcher} clutched the {1vN}.`
- Weapon map covers the full CS2 weapon set; unknown weapon → raw name.
- Tests: each event kind narrates correctly; opening/headshot/assist/
  multikill/location/clutch/article/no-weapon variants.

### `vocab.py` — 3-way mode + monitor words for new markers
- Add wording for the markers: opening, multikill, clutch — default vs
  monitor. Keep the `monitor` bool semantics; narrative uses default
  wording for names (it's a separate render path).

### `tui.py` — integration
- Replace `_monitor_mode: bool` with `_view_mode: str` ∈
  {"default","narrative","monitor"}; `action_toggle_view` cycles all three;
  `vocab` = `Vocab(monitor=(_view_mode=="monitor"))`.
- Own a `RoundTracker`. In `push_scorebot`:
  - PlayerScoreboard → `tracker.feed_players(...)` (plus existing storage).
  - event → `ann = tracker.feed_event(evt)`; compute `location =
    locations.zone_for(evt.map, evt.victim_x, evt.victim_y)` for kills;
    store a small `LogEntry(event, ann, location)` in the log list.
  - Reset the tracker + log on match focus change / `reset_scorebot`.
- Event-log render branches on `_view_mode`:
  - narrative → for each LogEntry, prose via `narrate(...)`, grouped under
    round headers that show the round result (from the round's `summary`).
  - default/monitor → structured render (current style, refined) with the
    new markers (`·opening`, `·3K`, clutch line) + trailing location, and
    round headers carrying the result.
- Round header result is taken from the round's RoundOverEvent `summary`
  annotation (found within that round's entries).
- The TUI's existing `_is_duplicate` guard can be simplified (the bridge
  is now authoritative on dedup) but a light guard may remain.

## Testing
- Unit tests (pure, no network) for `locations`, `analysis`, `narrate`
  added under `tests/`.
- `analysis` tests must cover the clutch edge cases above + opening +
  multikill/ace + round reset on round change.
- Live validation: run against a live HLTV match, eyeball all three modes,
  confirm locations are plausible and round summaries/clutches are correct.
- Existing suite stays green.

## Non-goals / known limitations
- Bomb-plant site (A/B) is **not** shown — not in the socket payload.
- Locations are approximate (coarse zones), popular maps only.
- No new network traffic; no change to the bridge's transport.
