"""Pure live-analysis layer over the scorebot event stream.

Consumes events and player scoreboards in arrival (chronological) order
and emits a per-event `Annotation`: opening-kill marker, the killer's
running multi-kill count, clutch detection (a *won* 1vN), and a round
summary on round-over. No Playwright, no network.
"""

from __future__ import annotations

from dataclasses import dataclass

from csmatch.scorebot import (
    KillEvent,
    PlayerScoreboard,
    RoundOverEvent,
    RoundStartEvent,
    ScorebotEvent,
    Side,
)


@dataclass
class RoundSummary:
    round: int | None
    map: str | None
    winner_side: str  # "CT" | "T"
    t_score: int | None
    ct_score: int | None
    reason: str
    winner_team: str | None = None  # team name on the winning side, if known


@dataclass
class Annotation:
    opening: bool = False         # first kill of the round
    multikill: int = 0            # killer's running kill count this round
    clutch: str | None = None     # e.g. "1v2" — set on the round-over of a WON clutch
    clutcher: str | None = None   # nick of the clutcher
    summary: RoundSummary | None = None  # set on RoundOverEvent


class RoundTracker:
    """Stateful, single-match annotator. Feed players + events in order."""

    def __init__(self) -> None:
        self._rosters: dict[Side, set[str]] = {"CT": set(), "T": set()}
        self._team_names: dict[Side, str] = {}
        self._round: int | None = None
        self._alive: dict[Side, set[str]] = {"CT": set(), "T": set()}
        self._kills_this_round: dict[str, int] = {}
        self._opening_done = False
        # Clutch recorded *at the moment a player becomes the lone survivor*,
        # using the opponent count then — not later trades.
        self._clutch_nick: str | None = None
        self._clutch_side: Side | None = None
        self._clutch_n: int = 0

    def feed_players(self, players: PlayerScoreboard) -> None:
        for team in players.teams:
            if team.side not in ("CT", "T"):
                continue
            self._rosters[team.side] = {p.nick for p in team.players if p.nick}
            if team.name:
                self._team_names[team.side] = team.name
        # Rosters arriving before the round's first kill: seed the alive
        # sets so clutch detection works from the very first round, even
        # when no round-start has been fed yet. Never after a kill — that
        # would resurrect dead players mid-round.
        if not self._opening_done and not any(self._alive.values()):
            self._alive = {"CT": set(self._rosters["CT"]), "T": set(self._rosters["T"])}

    def feed_event(self, event: ScorebotEvent) -> Annotation:
        self._maybe_reset(event)

        if isinstance(event, KillEvent):
            return self._on_kill(event)
        if isinstance(event, RoundOverEvent):
            return self._on_round_over(event)
        return Annotation()

    # ── round lifecycle ────────────────────────────────────────────────

    def _maybe_reset(self, event: ScorebotEvent) -> None:
        # Reset on an explicit round start, or whenever an event reports a
        # round different from the one we're tracking (robust to a missed
        # round-start). A RoundOverEvent carries the round it ends, so it
        # must not trigger a reset before we summarise it.
        if isinstance(event, RoundStartEvent):
            self._reset_round(event.round)
            return
        if isinstance(event, RoundOverEvent):
            if self._round is None:
                self._round = event.round
            elif event.round is not None and event.round != self._round:
                # Round-over for a round we never saw start (reconnect
                # score-jump): reset first so the previous round's clutch
                # and alive state can't leak into this summary.
                self._reset_round(event.round)
            return
        if event.round is not None and event.round != self._round:
            self._reset_round(event.round)

    def _reset_round(self, round_: int | None) -> None:
        self._round = round_
        self._alive = {"CT": set(self._rosters["CT"]), "T": set(self._rosters["T"])}
        self._kills_this_round = {}
        self._opening_done = False
        self._clutch_nick = None
        self._clutch_side = None
        self._clutch_n = 0

    # ── per-event handlers ─────────────────────────────────────────────

    def _on_kill(self, event: KillEvent) -> Annotation:
        ann = Annotation()

        ann.opening = not self._opening_done
        self._opening_done = True

        if event.killer:
            count = self._kills_this_round.get(event.killer, 0) + 1
            self._kills_this_round[event.killer] = count
            ann.multikill = count

        self._alive[event.victim_side].discard(event.victim)
        self._detect_clutch()
        return ann

    def _detect_clutch(self) -> None:
        if self._clutch_nick is not None:
            return
        # Rosters unknown → both alive sets empty → no clutch (degrade).
        for side in ("CT", "T"):
            other: Side = "T" if side == "CT" else "CT"
            mine = self._alive[side]
            theirs = self._alive[other]
            # Only a genuine 1vN: lone survivor with >= 2 opponents alive.
            # Both sides reaching 1 simultaneously is excluded by len>=2.
            if len(mine) == 1 and len(theirs) >= 2:
                self._clutch_nick = next(iter(mine))
                self._clutch_side = side  # type: ignore[assignment]
                self._clutch_n = len(theirs)
                return

    def _on_round_over(self, event: RoundOverEvent) -> Annotation:
        summary = RoundSummary(
            round=event.round if event.round is not None else self._round,
            map=event.map,
            winner_side=event.winner_side,
            t_score=event.t_score,
            ct_score=event.ct_score,
            reason=event.reason,
            winner_team=self._team_names.get(event.winner_side),
        )
        ann = Annotation(summary=summary)
        # A clutch only counts if it was won: the clutcher's side took the
        # round. If the clutcher died, their alive set is empty and their
        # side cannot be the (live) winner of a 1vN, so this also covers
        # "clutcher dies before round end".
        if (
            self._clutch_nick is not None
            and self._clutch_side == event.winner_side
            and self._clutch_nick in self._alive[event.winner_side]
        ):
            ann.clutch = f"1v{self._clutch_n}"
            ann.clutcher = self._clutch_nick
        return ann
