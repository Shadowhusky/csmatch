"""Label vocabularies for the two visual themes.

The default theme uses CS terminology directly. The "monitor" theme
reframes the same data as a build/SRE monitor: matches → builds, rounds
→ iterations, kills → arrowed events, bomb plant/defuse → deploy /
rollback. Player names and raw numbers are preserved either way so the
data stays readable.
"""

from __future__ import annotations


_REASON_REWRITE = {
    "Enemy eliminated": "all clients down",
    "Target bombed": "deployment ok",
    "Bomb defused": "rollback ok",
    "Target saved": "deploy aborted",
    "Time": "timeout",
}


class Vocab:
    def __init__(self, monitor: bool = False) -> None:
        self.monitor = monitor

    # ── app chrome ─────────────────────────────────────────────────
    @property
    def app_title(self) -> str:
        return "build-monitor" if self.monitor else "csmatch"

    @property
    def list_columns(self) -> tuple[str, ...]:
        if self.monitor:
            return ("service-a", "status", "service-b", "cluster", "step", "v")
        return ("team a", "score", "team b", "map", "g", "bo")

    # ── status words ───────────────────────────────────────────────
    @property
    def live(self) -> str:
        return "RUN" if self.monitor else "LIVE"

    @property
    def upcoming(self) -> str:
        return "queue" if self.monitor else "vs"

    @property
    def bo_prefix(self) -> str:
        return "v" if self.monitor else "BO"

    @property
    def round_prefix(self) -> str:
        return "i" if self.monitor else "R"

    @property
    def side_ct(self) -> str:
        return "a" if self.monitor else "CT"

    @property
    def side_t(self) -> str:
        return "b" if self.monitor else "T"

    def side_label(self, side: str | None) -> str | None:
        if side is None:
            return None
        if side == "CT":
            return self.side_ct
        if side == "T":
            return self.side_t
        return side

    @property
    def map_index_word(self) -> str:
        return "step" if self.monitor else "map"

    @property
    def maps_section(self) -> str:
        return "stages" if self.monitor else "maps"

    @property
    def scoreboard_section(self) -> str:
        return "metrics (live)" if self.monitor else "scoreboard (live)"

    @property
    def event_log_section(self) -> str:
        return "deploy log" if self.monitor else "event log"

    # ── event verbs ────────────────────────────────────────────────
    @property
    def kill_arrow(self) -> str:
        return "▸" if self.monitor else "→"

    @property
    def headshot_tag(self) -> str:
        return "abort" if self.monitor else "HS"

    @property
    def bomb_plant_tag(self) -> str:
        return "[deploy]" if self.monitor else "[plant]"

    @property
    def bomb_defuse_tag(self) -> str:
        return "[rollback]" if self.monitor else "[defuse]"

    @property
    def round_start_tag(self) -> str:
        return "[restart]" if self.monitor else "▷ round start"

    @property
    def round_over_tag(self) -> str:
        return "[done]" if self.monitor else "◾ round over"

    @property
    def bomb_marker(self) -> str:
        # The monitor theme uses a neutral status triangle instead of
        # the bomb emoji so the row reads as plain text.
        return "▲" if self.monitor else "💣"

    # ── scoreboard column headers ──────────────────────────────────
    def player_header(self, compact: bool) -> str:
        if self.monitor:
            return (
                "    handle       load  mem    ok-skip-fail\n"
                if compact
                else "    handle       load  mem      ok-skip-fail   rps\n"
            )
        return (
            "    nick         hp    $   K-A-D\n"
            if compact
            else "    nick         hp    $    K-A-D   ADR\n"
        )

    # ── status messages ────────────────────────────────────────────
    @property
    def status_starting(self) -> str:
        return (
            "○ initializing (launching headless runner…)"
            if self.monitor
            else "○ starting (launching headless Chromium…)"
        )

    @property
    def status_waiting(self) -> str:
        return "○ waiting for first packet…" if self.monitor else "○ waiting for first event…"

    @property
    def status_streaming(self) -> str:
        return "● tailing" if self.monitor else "● streaming"

    def status_idle_source(self) -> str:
        if self.monitor:
            return "idle: switch to --source hltv to enable the live tail"
        return "idle: switch to --source hltv to enable the live kill feed"

    def status_idle_live(self) -> str:
        if self.monitor:
            return "idle: focus a RUN build (not queued) to start the live tail"
        return "idle: focus a LIVE match (not upcoming) to start the live feed"

    # ── values & names ─────────────────────────────────────────────
    def map_name(self, name: str | None) -> str | None:
        if not name:
            return name
        if self.monitor:
            # de_dust2 → dust2. Looks like a namespace/cluster name and
            # is no longer flagged by the substring "de_".
            return name.removeprefix("de_").removeprefix("De_").lower()
        return name

    def reason(self, raw: str | None) -> str | None:
        if not raw:
            return raw
        if not self.monitor:
            return raw
        return _REASON_REWRITE.get(raw, raw.lower())

    def hide_weapon(self) -> bool:
        return self.monitor
