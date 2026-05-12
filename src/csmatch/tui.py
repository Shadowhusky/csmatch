"""Textual TUI for csmatch.

Two panes: live match list (left) and focused detail (right). Light by
default; press `e` to expand the focused match into a full scoreboard.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

from csmatch.models import Match, MatchDetail
from csmatch.scorebot import (
    BombDefuseEvent,
    BombPlantEvent,
    KillEvent,
    OtherEvent,
    PlayerScoreboard,
    RoundOverEvent,
    RoundStartEvent,
    ScoreboardState,
    ScorebotBridge,
    ScorebotEvent,
    SeriesSnapshot,
)
from csmatch.sources.base import MatchSource, SourceError
from csmatch.sources.hltv import HLTVSource
from csmatch.vocab import Vocab


# Poll cadences (seconds). Conservative defaults so we don't trip
# upstream rate limits like HLTV's Cloudflare 1015. Each sleep is
# additionally jittered by ±JITTER_FRAC so requests don't land on exact
# intervals across IPs.
LIGHT_INTERVAL = 45.0    # list refresh, no match expanded
EXPANDED_INTERVAL = 25.0  # list refresh while a match is expanded
DETAIL_INTERVAL = 15.0    # per-match detail fetch
BACKOFF_INTERVAL = 90.0   # after an upstream rate-limit error
JITTER_FRAC = 0.30


def _jitter(base: float, frac: float = JITTER_FRAC) -> float:
    return max(1.0, base * (1.0 + random.uniform(-frac, frac)))


def _looks_like_rate_limit(err: BaseException) -> bool:
    s = str(err).lower()
    return any(token in s for token in ("429", "1015", "rate", "too many"))


def _age_str(then: datetime | None) -> str:
    if not then:
        return "—"
    # Match aware/naive: use now() in the same tz mode as `then`.
    now = datetime.now(tz=then.tzinfo) if then.tzinfo is not None else datetime.now()
    delta = (now - then).total_seconds()
    if delta < 0:
        return "0s"
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    return f"{int(delta // 3600)}h"


def _format_score(m: Match, vocab: Vocab) -> Text:
    """Unified score column. Lead with series wins; append the current
    map's running score when a map is mid-play."""
    if m.status == "upcoming":
        return Text(vocab.upcoming, style="dim")

    ss = m.series_score
    if ss is None:
        return Text(vocab.live, style="bold red")

    # Series part
    if ss.team_a > ss.team_b:
        out = Text.assemble((f"{ss.team_a}", "bold green"), "-", f"{ss.team_b}")
    elif ss.team_b > ss.team_a:
        out = Text.assemble(f"{ss.team_a}", "-", (f"{ss.team_b}", "bold green"))
    else:
        out = Text(f"{ss.team_a}-{ss.team_b}")

    # Live current-map part
    s = m.score
    if s is not None:
        out.append("  ", style="dim")
        if s.team_a > s.team_b:
            out.append_text(Text.assemble((f"{s.team_a:>2}", "bold green"), "-", f"{s.team_b:<2}"))
        elif s.team_b > s.team_a:
            out.append_text(Text.assemble(f"{s.team_a:>2}", "-", (f"{s.team_b:<2}", "bold green")))
        else:
            out.append(f"{s.team_a:>2}-{s.team_b:<2}")
        side = vocab.side_label(s.side_a)
        if side:
            out.append(f" {side}", style="dim")
    return out


def _format_when(m: Match) -> str:
    """For upcoming matches: 'in 1h 23m' or 'at 18:30'. Otherwise: map name."""
    if m.status == "upcoming" and m.started_at:
        delta = (m.started_at - datetime.now(m.started_at.tzinfo)).total_seconds()
        if delta < 0:
            return "soon"
        if delta < 3600:
            return f"in {int(delta // 60)}m"
        h = int(delta // 3600)
        mins = int((delta % 3600) // 60)
        return f"in {h}h{mins:02d}m"
    return m.map or "—"


class MatchList(DataTable):
    """The left-pane list of live matches."""

    BINDINGS = []

    def __init__(self) -> None:
        super().__init__(zebra_stripes=False, cursor_type="row", show_header=True)
        # Columns are added after vocab is known (apply_vocab).
        self._row_to_match: dict[int, Match] = {}
        self._vocab: Vocab = Vocab(monitor=False)
        self._columns_added = False

    def apply_vocab(self, vocab: Vocab) -> None:
        """Set the active vocab; rebuilds the column headers if needed."""
        self._vocab = vocab
        # DataTable doesn't let us rename existing columns, so we rebuild.
        self.clear(columns=True)
        for label in vocab.list_columns:
            self.add_column(label)
        self._columns_added = True
        self._row_to_match.clear()

    def render_matches(self, matches: list[Match]) -> str | None:
        """Repaint the list, preserving cursor position."""
        if not self._columns_added:
            self.apply_vocab(self._vocab)
        prev_id = None
        if self.row_count and self.cursor_row < len(self._row_to_match):
            prev_id = self._row_to_match.get(self.cursor_row)
            prev_id = prev_id.id if prev_id else None

        self.clear()
        self._row_to_match.clear()
        new_cursor = 0
        for i, m in enumerate(matches):
            is_upcoming = m.status == "upcoming"
            name_style = "dim" if is_upcoming else "bold"
            map_or_when = _format_when(m)
            if not is_upcoming:
                map_or_when = self._vocab.map_name(map_or_when) or map_or_when
            map_style = "dim" if is_upcoming else ""
            self.add_row(
                Text(m.team_a.name[:16], style=name_style),
                _format_score(m, self._vocab),
                Text(m.team_b.name[:16], style=name_style),
                Text(map_or_when, style=map_style),
                Text(f"{m.map_index or '?'}", style=map_style),
                Text(f"{m.best_of or '?'}", style=map_style),
                key=m.id,
            )
            self._row_to_match[i] = m
            if m.id == prev_id:
                new_cursor = i
        if matches:
            try:
                self.move_cursor(row=new_cursor)
            except Exception:
                pass
            return matches[new_cursor].id
        return None

    def focused_match(self) -> Match | None:
        if not self.row_count:
            return None
        return self._row_to_match.get(self.cursor_row)


class DetailPane(Static):
    """Right pane showing the focused match. Light or expanded."""

    KILL_FEED_MAX = 40

    def __init__(self) -> None:
        super().__init__("", expand=True)
        self._match: Match | None = None
        self._detail: MatchDetail | None = None
        self._error: str | None = None
        self.expanded: bool = False
        # Kill-delta tracking: nick → last-known total kills
        self._prev_kills: dict[str, int] = {}
        # Rolling event log (polling-derived deltas)
        self._kill_feed: list[tuple[datetime, str, str, int]] = []
        # Live scorebot feed (Playwright bridge). Each entry is one of the
        # ScorebotEvent subclasses or a ScoreboardState snapshot.
        self._scorebot_state: ScoreboardState | None = None
        self._scorebot_events: list[ScorebotEvent] = []
        self._scorebot_players: PlayerScoreboard | None = None
        self._scorebot_series: SeriesSnapshot | None = None
        self._scorebot_status: str | None = None
        self._vocab: Vocab = Vocab(monitor=False)

    def apply_vocab(self, vocab: Vocab) -> None:
        self._vocab = vocab
        self._repaint()

    def on_mount(self) -> None:
        self._repaint()

    def set_focus(self, m: Match | None) -> None:
        self._match = m
        if m is None:
            self._detail = None
            self._error = None
        elif self._detail and self._detail.id != m.id:
            self._detail = None
            self._prev_kills.clear()
            self._kill_feed.clear()
            self._scorebot_state = None
            self._scorebot_events.clear()
        self._repaint()

    def push_scorebot(
        self,
        item: ScorebotEvent | ScoreboardState | PlayerScoreboard | SeriesSnapshot,
    ) -> None:
        """Receive an event/state from the Playwright scorebot bridge."""
        # First payload that lands → we're streaming.
        if self._scorebot_status in (None, "starting", "waiting"):
            self._scorebot_status = "streaming"
        if isinstance(item, ScoreboardState):
            self._scorebot_state = item
        elif isinstance(item, PlayerScoreboard):
            self._scorebot_players = item
        elif isinstance(item, SeriesSnapshot):
            self._scorebot_series = item
        else:
            if item.kind in {"heartbeat", "error"}:
                return  # internal-only; never shown in the UI
            # Defensive dedup: identical events arriving back-to-back
            # (same kind, same round, same identifying fields) get
            # suppressed so the log doesn't render duplicate lines if a
            # row temporarily reappears in the virtualised gamelog.
            if self._is_duplicate(item):
                return
            self._scorebot_events.append(item)
            if len(self._scorebot_events) > self.KILL_FEED_MAX:
                self._scorebot_events = self._scorebot_events[-self.KILL_FEED_MAX:]
        self._repaint()

    @staticmethod
    def _event_fingerprint(e: ScorebotEvent) -> tuple:
        """A stable identity for de-duplication.

        Includes round number so the same kill across two rounds (e.g.
        s1mple killing Senzu twice) still counts as two distinct events.
        """
        if isinstance(e, KillEvent):
            return (e.kind, e.round, e.killer, e.victim, e.weapon, e.assist)
        if isinstance(e, BombPlantEvent):
            return (e.kind, e.round, e.planter, e.site)
        if isinstance(e, BombDefuseEvent):
            return (e.kind, e.round, e.defuser)
        if isinstance(e, RoundStartEvent):
            return (e.kind, e.round)
        if isinstance(e, RoundOverEvent):
            return (e.kind, e.round, e.winner_side, e.t_score, e.ct_score, e.reason)
        if isinstance(e, OtherEvent):
            return (e.kind, e.round, e.text)
        return (e.kind, e.round)

    def _is_duplicate(self, incoming: ScorebotEvent) -> bool:
        if not self._scorebot_events:
            return False
        fp = self._event_fingerprint(incoming)
        # Scan back up to N recent events for the same fingerprint. Most
        # dupes arrive within a couple of ticks of each other.
        for prev in reversed(self._scorebot_events[-10:]):
            if self._event_fingerprint(prev) == fp:
                return True
        return False

    def set_scorebot_status(self, status: str | None) -> None:
        self._scorebot_status = status
        self._repaint()

    def reset_scorebot(self) -> None:
        self._scorebot_state = None
        self._scorebot_events.clear()
        self._scorebot_players = None
        self._scorebot_series = None
        self._scorebot_status = None
        self._repaint()

    def set_detail(self, d: MatchDetail | None, err: str | None = None) -> None:
        if d is not None and self._detail and self._detail.id == d.id:
            # Same match — diff player kills to derive a live event log.
            self._update_kill_feed(d)
        elif d is not None:
            # New match focus — seed the baseline without firing events.
            self._prev_kills = {p.nick: p.kills for p in d.players_a + d.players_b}
            self._kill_feed.clear()
        self._detail = d
        self._error = err
        self._repaint()

    def _update_kill_feed(self, d: MatchDetail) -> None:
        now = datetime.now()
        for team_letter, roster in (("A", d.players_a), ("B", d.players_b)):
            for p in roster:
                prev = self._prev_kills.get(p.nick)
                if prev is not None and p.kills > prev:
                    self._kill_feed.append((now, team_letter, p.nick, p.kills - prev))
                self._prev_kills[p.nick] = p.kills
        # Trim feed
        if len(self._kill_feed) > self.KILL_FEED_MAX:
            self._kill_feed = self._kill_feed[-self.KILL_FEED_MAX :]

    def refresh_render(self) -> None:
        self._repaint()

    def _repaint(self) -> None:
        if self.is_mounted:
            self.update(self._build_view())

    def _build_view(self) -> Text:
        if self._match is None:
            return Text("no live match focused", style="dim")

        m = self._match
        sb = self._scorebot_state
        ss = self._scorebot_series
        v = self._vocab
        text = Text()

        # Row 1: team names with series score between them.
        text.append(f"{m.team_a.name}", style="bold")
        text.append("   ", style="dim")
        if ss is not None:
            a_won = ss.wins_a > ss.wins_b
            b_won = ss.wins_b > ss.wins_a
            text.append(f"{ss.wins_a}", style="bold green" if a_won else "")
            text.append(" : ", style="dim")
            text.append(f"{ss.wins_b}", style="bold green" if b_won else "")
        elif m.series_score is not None:
            text.append(f"{m.series_score.team_a} : {m.series_score.team_b}", style="dim")
        elif m.status == "upcoming":
            text.append(v.upcoming, style="dim")
        else:
            text.append(v.live, style="bold red")
        text.append("   ", style="dim")
        text.append(f"{m.team_b.name}\n", style="bold")

        # Row 2: event · BO · current map · map index
        parts: list[str] = []
        if m.event:
            parts.append(m.event)
        parts.append(f"{v.bo_prefix}{m.best_of or '?'}")
        current_map = (sb.map if sb else None) or (ss.current_map if ss else None) or m.map
        if current_map:
            parts.append(v.map_name(current_map) or current_map)
        map_idx = (ss.current_map_index if ss else None) or m.map_index
        if map_idx and m.best_of:
            parts.append(f"{v.map_index_word} {map_idx}/{m.best_of}")
        text.append("  ·  ".join(parts) + "\n", style="dim")

        # Row 3: live round score (only when scorebot is streaming)
        if sb is not None and (sb.ct_score is not None or sb.t_score is not None):
            text.append(v.round_prefix, style="dim")
            text.append(f"{sb.round or '?'}", style="bold cyan")
            text.append(f"   {v.side_ct} ", style="dim")
            text.append(f"{sb.ct_score or 0}", style="bold blue")
            text.append(" : ", style="dim")
            text.append(f"{sb.t_score or 0}", style="bold yellow")
            text.append(f" {v.side_t}", style="dim")
            if sb.time:
                text.append(f"   ⏱ {sb.time}", style="dim")
            if sb.bomb_planted:
                text.append(f"   {v.bomb_marker}", style="bold red")
            text.append("\n")

        d = self._detail
        if not self.expanded:
            text.append("\npress  ", style="dim")
            text.append("e", style="bold yellow")
            text.append("  to expand\n", style="dim")
            return text

        if self._error:
            text.append(f"\nerror loading detail: {self._error}\n", style="red")
            return text

        if d is None:
            text.append("\nloading detail…\n", style="dim")
            return text

        # Map-by-map breakdown. Prefer the live scorebot snapshot
        # (includes upcoming maps as "—") over the slower detail fetch.
        if self._scorebot_series is not None and self._scorebot_series.maps:
            text.append(f"\n{v.maps_section}:\n", style="bold")
            for sm in self._scorebot_series.maps:
                text.append(f"  {v.map_index_word[0]}{sm.index}  ")
                map_disp = v.map_name(sm.map) or sm.map or "?"
                text.append(f"{map_disp:<10}", style="dim")
                if sm.score_a is not None and sm.score_b is not None:
                    lead_a = sm.score_a > sm.score_b
                    lead_b = sm.score_b > sm.score_a
                    text.append(f"{sm.score_a:>2}", style="bold green" if lead_a else "")
                    text.append(" - ", style="dim")
                    text.append(f"{sm.score_b:<2}", style="bold green" if lead_b else "")
                else:
                    text.append(" - ", style="dim")
                text.append("\n")
        elif d.map_scores:
            text.append(f"\n{v.maps_section}:\n", style="bold")
            for i, s in enumerate(d.map_scores, 1):
                lead_a = s.team_a > s.team_b
                lead_b = s.team_b > s.team_a
                a_style = "bold green" if lead_a else ""
                b_style = "bold green" if lead_b else ""
                text.append(f"  m{i}  ")
                text.append(f"{s.team_a:>2}", style=a_style)
                text.append("-")
                text.append(f"{s.team_b:<2}", style=b_style)
                text.append("\n")

        # Per-player scoreboard. Prefer the live scorebot version when
        # available — it carries HP, money, and refreshes every second.
        if self._scorebot_players is not None and self._scorebot_players.teams:
            # Decide compact vs wide based on the pane's actual content
            # width, not the screen — narrow-layout stacks fully, but the
            # detail pane may still be wide enough for ADR.
            try:
                avail = max(self.container_size.width, self.size.width)
            except Exception:
                avail = 80
            compact = avail < 64
            text.append(f"\n{v.scoreboard_section}:\n", style="bold")
            for team in self._scorebot_players.teams:
                side_color = "blue" if team.side == "CT" else "yellow"
                text.append(f"  {team.name or '(team)'}", style=f"bold {side_color}")
                side_disp = v.side_label(team.side)
                if side_disp:
                    text.append(f"  {side_disp}", style=f"dim {side_color}")
                text.append("\n")
                # column header — match the data layout below
                text.append(v.player_header(compact), style="dim")
                for p in team.players:
                    name_style = "" if p.alive else "dim strike"
                    nick_w = 10 if compact else 12
                    nick = p.nick[:nick_w]
                    text.append(f"    {nick:<{nick_w}}  ", style=name_style)
                    # HP
                    if p.hp is not None:
                        hp_style = (
                            "dim" if not p.alive
                            else ("green" if p.hp > 60 else ("yellow" if p.hp > 25 else "red"))
                        )
                        text.append(f"{p.hp:>3}", style=hp_style)
                    else:
                        text.append("  -")
                    # Money / "mem" cell. Cap absurd values (CS2 max is
                    # $16000); drop the leading '$' in work mode so the
                    # column doesn't read as in-game currency.
                    text.append("  ")
                    money = p.money
                    if money is not None and money > 16500:
                        money = None
                    if money is not None:
                        prefix = "" if v.monitor else "$"
                        if money >= 1000:
                            text.append(f"{prefix}{money / 1000:.1f}k", style="dim")
                        else:
                            text.append(f"{prefix}{money}".ljust(5), style="dim")
                    else:
                        text.append("  -  ", style="dim")
                    # K-A-D as a single compact triple
                    text.append(f"  {p.kills:>2}-{p.assists:<2}-{p.deaths:<2}")
                    if not compact and p.adr is not None:
                        text.append(f"   {p.adr:>5.1f}", style="dim")
                    text.append("\n")
        elif d.players_a or d.players_b:
            section_label = "metrics" if v.monitor else "scoreboard"
            text.append(f"\n{section_label}:\n", style="bold")
            text.append(f"  {d.team_a.name}\n", style="bold")
            for p in d.players_a:
                rating = f" {p.rating:.2f}" if p.rating else ""
                text.append(f"    {p.nick:<14} {p.kd:>7}{rating}\n")
            text.append(f"  {d.team_b.name}\n", style="bold")
            for p in d.players_b:
                rating = f" {p.rating:.2f}" if p.rating else ""
                text.append(f"    {p.nick:<14} {p.kd:>7}{rating}\n")
        elif not d.map_scores and self._scorebot_status is None:
            if v.monitor:
                msg = "(no per-stage data yet; focus a RUN build on the upstream source for realtime tail)"
            else:
                msg = "(no per-map or per-player data yet; expand on a live HLTV match for realtime)"
            text.append(f"\n{msg}\n", style="dim")

        # Scorebot status line (only meaningful for HLTV source).
        if self._scorebot_status is not None:
            label_prefix = "tail: " if v.monitor else "scorebot: "
            text.append(f"\n{label_prefix}", style="dim")
            status = self._scorebot_status
            if status == "streaming":
                style = "bold green"
                label = v.status_streaming
            elif status == "starting":
                style = "yellow"
                label = v.status_starting
            elif status == "waiting":
                style = "yellow"
                label = v.status_waiting
            elif status.startswith("failed:"):
                style = "red"
                label = "× " + status
            elif status.startswith("idle"):
                style = "dim"
                label = status
            else:
                style = "dim"
                label = status
            text.append(label + "\n", style=style)

        if self._scorebot_events:
            text.append(f"\n{v.event_log_section}:\n", style="bold")
            last_round: int | None = None
            for evt in reversed(self._scorebot_events):
                age = _age_str(evt.ts)
                # Round-change separator so the user can anchor when
                # scrolling back through a long log.
                if evt.round is not None and evt.round != last_round:
                    text.append(f"  ── {v.round_prefix}{evt.round}", style="dim cyan")
                    if evt.map:
                        text.append(f"  {v.map_name(evt.map) or evt.map}", style="dim")
                    text.append(" ──\n", style="dim cyan")
                    last_round = evt.round
                text.append(f"  {age:>4}  ")
                if isinstance(evt, KillEvent):
                    killer_style = "yellow" if evt.killer_side == "T" else "blue"
                    victim_style = "yellow" if evt.victim_side == "T" else "blue"
                    text.append(evt.killer, style=f"bold {killer_style}")
                    if evt.assist:
                        text.append(" + ", style="dim")
                        text.append(evt.assist, style="dim cyan")
                    text.append(f" {v.kill_arrow} ", style="dim")
                    text.append(evt.victim, style=f"bold {victim_style}")
                    if evt.weapon and not v.hide_weapon():
                        text.append(f"  [{evt.weapon}]", style="dim")
                    if evt.headshot:
                        text.append(f"  {v.headshot_tag}", style="bold red")
                    text.append("\n")
                elif isinstance(evt, BombPlantEvent):
                    text.append(f"{v.bomb_plant_tag}  ", style="bold red")
                    text.append(evt.planter, style="bold yellow")
                    if evt.site:
                        zone_word = "zone" if v.monitor else "site"
                        text.append(f"  {zone_word} {evt.site}", style="dim")
                    if evt.t_alive is not None and evt.ct_alive is not None:
                        if v.monitor:
                            text.append(f"  ({evt.t_alive}:{evt.ct_alive})", style="dim")
                        else:
                            text.append(f"  ({evt.t_alive}T vs {evt.ct_alive}CT)", style="dim")
                    text.append("\n")
                elif isinstance(evt, BombDefuseEvent):
                    text.append(f"{v.bomb_defuse_tag}  ", style="bold green")
                    if evt.defuser:
                        text.append(evt.defuser, style="bold blue")
                    text.append("\n")
                elif isinstance(evt, RoundStartEvent):
                    text.append(f"{v.round_start_tag}\n", style="dim")
                elif isinstance(evt, RoundOverEvent):
                    text.append(f"{v.round_over_tag}  ", style="bold")
                    side_style = "yellow" if evt.winner_side == "T" else "blue"
                    text.append(v.side_label(evt.winner_side) or evt.winner_side, style=f"bold {side_style}")
                    text.append(f"  ({evt.t_score or 0}-{evt.ct_score or 0})  ", style="dim")
                    text.append(v.reason(evt.reason) or "", style="dim italic")
                    text.append("\n")
                elif isinstance(evt, OtherEvent):
                    text.append(evt.text, style="dim italic")
                    text.append("\n")
                else:
                    text.append(f"{evt.kind}\n", style="dim")
        elif self._kill_feed:
            # Fallback: poll-derived deltas (used for non-HLTV sources)
            text.append(f"\n{v.event_log_section}:\n", style="bold")
            for ts, side, nick, n in reversed(self._kill_feed):
                color = "green" if side == "A" else "red"
                age = _age_str(ts)
                text.append(f"  {age:>4}  ")
                text.append(nick, style=color)
                if v.monitor:
                    text.append(f"  +{n}  ok\n", style="dim")
                elif n == 1:
                    text.append(" got a kill\n", style="dim")
                else:
                    text.append(f"  +{n} kills\n", style="dim")

        if d.fetched_at:
            text.append(f"\nupdated {_age_str(d.fetched_at)} ago\n", style="dim")
        return text


class CsMatchApp(App):
    CSS = """
    Screen { background: $surface; }
    #body { height: 1fr; }
    MatchList { width: 60%; border-right: solid $panel; }
    #detail-scroll { width: 1fr; padding: 1 2; }
    DetailPane { width: 100%; height: auto; }

    /* Narrow layout: stack list on top, detail below */
    Screen.-narrow #body { layout: vertical; }
    Screen.-narrow MatchList { width: 100%; height: 45%; border-right: none; border-bottom: solid $panel; }
    Screen.-narrow #detail-scroll { width: 100%; height: 1fr; padding: 1 1; }

    /* Fullscreen detail: hide the list entirely, give detail all the space */
    Screen.-zoom MatchList { display: none; }
    Screen.-zoom #detail-scroll { width: 100%; padding: 1 2; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("e", "expand", "expand"),
        Binding("f", "zoom", "fullscreen"),
        # Kept out of the footer to keep the chrome minimal.
        Binding("w", "toggle_view", "toggle", show=False),
        Binding("escape", "zoom_off", "exit fullscreen", show=False),
        Binding("r", "refresh", "refresh"),
        Binding("pageup", "scroll_detail_up", "scroll up", show=False),
        Binding("pagedown", "scroll_detail_down", "scroll down", show=False),
        Binding("home", "scroll_detail_home", "top", show=False),
        Binding("end", "scroll_detail_end", "bottom", show=False),
        Binding("j", "scroll_detail_down", "scroll down", show=False),
        Binding("k", "scroll_detail_up", "scroll up", show=False),
    ]

    def __init__(self, source: MatchSource) -> None:
        super().__init__()
        self._source = source
        self._list: MatchList | None = None
        self._detail: DetailPane | None = None
        self._detail_scroll: VerticalScroll | None = None
        self._status: Static | None = None
        self._last_fetch: datetime | None = None
        self._last_err: str | None = None
        self._poller_task: asyncio.Task | None = None
        self._detail_task: asyncio.Task | None = None
        # Scorebot bridge — only spun up for HLTV-source matches when
        # the user expands the detail pane.
        self._scorebot: ScorebotBridge | None = None
        self._scorebot_task: asyncio.Task | None = None
        self._scorebot_match_id: str | None = None
        self._monitor_mode: bool = False

    @property
    def vocab(self) -> Vocab:
        return Vocab(monitor=self._monitor_mode)

    def action_toggle_view(self) -> None:
        self._monitor_mode = not self._monitor_mode
        v = self.vocab
        self.title = f"{v.app_title} · {self._source.name}"
        if self._list is not None:
            self._list.apply_vocab(v)
        if self._detail is not None:
            self._detail.apply_vocab(v)
        asyncio.create_task(self._fetch_list_once())

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self._list = MatchList()
        self._detail = DetailPane()
        self._detail_scroll = VerticalScroll(id="detail-scroll")
        with Horizontal(id="body"):
            yield self._list
            with self._detail_scroll:
                yield self._detail
        self._status = Static("starting…", id="status")
        yield self._status
        yield Footer()

    NARROW_THRESHOLD = 100

    def on_resize(self, event) -> None:
        is_narrow = self.size.width < self.NARROW_THRESHOLD
        if is_narrow:
            self.screen.add_class("-narrow")
        else:
            self.screen.remove_class("-narrow")

    async def on_mount(self) -> None:
        list_ = cast(MatchList, self._list)
        list_.focus()
        v = self.vocab
        list_.apply_vocab(v)
        if self._detail is not None:
            self._detail.apply_vocab(v)
        self.title = f"{v.app_title} · {self._source.name}"
        self.sub_title = "loading…"
        # Initial layout based on starting terminal size.
        if self.size.width < self.NARROW_THRESHOLD:
            self.screen.add_class("-narrow")
        self._poller_task = asyncio.create_task(self._poll_loop())
        self._detail_task = asyncio.create_task(self._detail_loop())

    async def _poll_loop(self) -> None:
        while True:
            backoff = await self._fetch_list_once()
            if backoff:
                interval = BACKOFF_INTERVAL
            else:
                interval = EXPANDED_INTERVAL if (self._detail and self._detail.expanded) else LIGHT_INTERVAL
            try:
                await asyncio.sleep(_jitter(interval))
            except asyncio.CancelledError:
                return

    async def _fetch_list_once(self) -> bool:
        """Refresh the match list. Returns True if we should back off
        further (upstream rate-limit detected)."""
        list_ = cast(MatchList, self._list)
        detail = cast(DetailPane, self._detail)
        status = cast(Static, self._status)
        try:
            matches = await self._source.list_live()
        except SourceError as e:
            self._last_err = str(e)
            rate_limited = _looks_like_rate_limit(e)
            msg = f"× rate-limited; backing off" if rate_limited else f"× {e}"
            status.update(Text(msg, style="red"))
            return rate_limited
        except Exception as e:
            self._last_err = f"{type(e).__name__}: {e}"
            rate_limited = _looks_like_rate_limit(e)
            msg = "× rate-limited; backing off" if rate_limited else f"× {self._last_err}"
            status.update(Text(msg, style="red"))
            return rate_limited
        self._last_err = None
        self._last_fetch = datetime.now()
        list_.render_matches(matches)
        focused = list_.focused_match()
        detail.set_focus(focused)
        self.sub_title = f"{len(matches)} live · updated {_age_str(self._last_fetch)} ago"
        status.update(Text(f"✓ {len(matches)} live · {self._source.name} · last {_age_str(self._last_fetch)}", style="green"))
        return False

    async def _detail_loop(self) -> None:
        while True:
            detail = cast(DetailPane, self._detail)
            list_ = cast(MatchList, self._list)
            interval = DETAIL_INTERVAL
            if detail and detail.expanded:
                m = list_.focused_match()
                # Skip the HTTP detail fetch when the scorebot bridge is
                # already streaming the same match — bridge data is
                # richer AND avoids hammering /matches/<id> a second time.
                bridge_owns_detail = (
                    self._scorebot is not None
                    and self._scorebot_match_id == (m.id if m else None)
                    and detail._scorebot_status == "streaming"
                )
                if m and not bridge_owns_detail:
                    try:
                        d = await self._source.get_detail(m.id)
                        detail.set_detail(d)
                    except Exception as e:
                        detail.set_detail(None, err=f"{type(e).__name__}: {e}")
                        if _looks_like_rate_limit(e):
                            interval = BACKOFF_INTERVAL
            try:
                await asyncio.sleep(_jitter(interval))
            except asyncio.CancelledError:
                return

    # ── actions ──────────────────────────────────────────────────────

    def action_expand(self) -> None:
        detail = cast(DetailPane, self._detail)
        detail.expanded = not detail.expanded
        detail.refresh_render()
        # kick the detail loop immediately
        if detail.expanded and self._detail_task:
            asyncio.create_task(self._kick_detail())
        # Manage the scorebot bridge — only meaningful for HLTV.
        if detail.expanded:
            m = self._list.focused_match() if self._list else None
            if not isinstance(self._source, HLTVSource):
                detail.set_scorebot_status(
                    "idle: switch to --source hltv to enable the live kill feed"
                )
            elif m is None or m.status != "live":
                detail.set_scorebot_status(
                    "idle: focus a LIVE match (not upcoming) to start the live feed"
                )
            else:
                asyncio.create_task(self._start_scorebot(m))
        else:
            asyncio.create_task(self._stop_scorebot())
            detail.set_scorebot_status(None)

    async def _kick_detail(self) -> None:
        list_ = cast(MatchList, self._list)
        detail = cast(DetailPane, self._detail)
        m = list_.focused_match()
        if not m:
            return
        try:
            d = await self._source.get_detail(m.id)
            detail.set_detail(d)
        except Exception as e:
            detail.set_detail(None, err=f"{type(e).__name__}: {e}")

    async def action_refresh(self) -> None:
        await self._fetch_list_once()

    def action_zoom(self) -> None:
        """Toggle detail-pane fullscreen."""
        if self.screen.has_class("-zoom"):
            self.screen.remove_class("-zoom")
        else:
            self.screen.add_class("-zoom")
            # Auto-expand the detail when entering fullscreen so it's
            # actually showing useful content.
            detail = cast(DetailPane, self._detail)
            if not detail.expanded:
                self.action_expand()

    def action_zoom_off(self) -> None:
        if self.screen.has_class("-zoom"):
            self.screen.remove_class("-zoom")

    def action_scroll_detail_up(self) -> None:
        if self._detail_scroll is not None:
            self._detail_scroll.scroll_up(animate=False)

    def action_scroll_detail_down(self) -> None:
        if self._detail_scroll is not None:
            self._detail_scroll.scroll_down(animate=False)

    def action_scroll_detail_home(self) -> None:
        if self._detail_scroll is not None:
            self._detail_scroll.scroll_home(animate=False)

    def action_scroll_detail_end(self) -> None:
        if self._detail_scroll is not None:
            self._detail_scroll.scroll_end(animate=False)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Fired whenever the DataTable cursor moves to a new row."""
        list_ = cast(MatchList, self._list)
        detail = cast(DetailPane, self._detail)
        m = list_.focused_match()
        detail.set_focus(m)
        if detail.expanded:
            asyncio.create_task(self._kick_detail())
        # Re-target the scorebot if the focused match changed.
        if isinstance(self._source, HLTVSource) and detail.expanded and m and m.status == "live":
            if m.id != self._scorebot_match_id:
                asyncio.create_task(self._start_scorebot(m))

    async def _start_scorebot(self, m: Match) -> None:
        """Bring the scorebot bridge online for match `m`.

        Reuses the existing browser session whenever possible — only the
        very first call launches Chromium (and pays the focus-steal
        cost). Subsequent calls navigate the same page to the new
        match's URL and resume the poll loop."""
        detail = cast(DetailPane, self._detail)
        url = f"https://www.hltv.org/matches/{m.id}/x"

        # Fast path: bridge already running on this match — nothing to do.
        if (
            self._scorebot is not None
            and self._scorebot_match_id == m.id
            and self._scorebot.is_running
        ):
            return

        # Path 1 — bridge not yet created: full launch (one-time focus cost).
        if self._scorebot is None:
            detail.reset_scorebot()
            detail.set_scorebot_status("starting")
            try:
                self._scorebot = ScorebotBridge(poll_interval=1.0)
                await self._scorebot.start(url)
            except Exception as e:
                self._scorebot = None
                self._scorebot_match_id = None
                detail.set_scorebot_status(f"failed: {type(e).__name__}: {e}"[:80])
                return
            self._scorebot_match_id = m.id
            detail.set_scorebot_status("waiting")
            self._scorebot_task = asyncio.create_task(self._scorebot_pump())
            return

        # Path 2 — bridge exists: navigate to new match OR resume on same.
        detail.reset_scorebot()
        detail.set_scorebot_status("waiting")
        if self._scorebot_match_id != m.id:
            try:
                await self._scorebot.navigate(url)
            except Exception as e:
                detail.set_scorebot_status(f"failed: {type(e).__name__}: {e}"[:80])
                return
            self._scorebot_match_id = m.id
        else:
            # Same match — just unpause.
            self._scorebot.resume()
        # The pump task may have ended when we paused; restart if so.
        if self._scorebot_task is None or self._scorebot_task.done():
            self._scorebot_task = asyncio.create_task(self._scorebot_pump())

    async def _scorebot_pump(self) -> None:
        if self._scorebot is None:
            return
        detail = cast(DetailPane, self._detail)
        try:
            async for item in self._scorebot.events():
                detail.push_scorebot(item)
        except asyncio.CancelledError:
            return
        except Exception as e:
            detail.set_scorebot_status(f"failed: {type(e).__name__}: {e}"[:100])

    async def _stop_scorebot(self) -> None:
        """Pause the bridge without closing the browser. The Chromium
        process stays alive so a later expand doesn't re-trigger a
        focus-stealing launch."""
        if self._scorebot is not None:
            try:
                self._scorebot.pause()
            except Exception:
                pass

    async def _destroy_scorebot(self) -> None:
        """Fully tear down the bridge — only on app exit."""
        if self._scorebot_task and not self._scorebot_task.done():
            self._scorebot_task.cancel()
            try:
                await self._scorebot_task
            except (asyncio.CancelledError, Exception):
                pass
            self._scorebot_task = None
        if self._scorebot is not None:
            try:
                await self._scorebot.stop()
            except Exception:
                pass
            self._scorebot = None
        self._scorebot_match_id = None

    async def on_unmount(self) -> None:
        for task in (self._poller_task, self._detail_task):
            if task:
                task.cancel()
        await self._destroy_scorebot()
        await self._source.close()
