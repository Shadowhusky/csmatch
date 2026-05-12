"""CLI entrypoint for csmatch."""

from __future__ import annotations

import asyncio
import sys

import click

from csmatch.sources.base import MatchSource


def _build_source(name: str) -> MatchSource:
    if name == "mock":
        from csmatch.sources.mock import MockSource
        return MockSource(count=4)
    if name == "bo3":
        from csmatch.sources.bo3gg import BO3Source
        return BO3Source()
    if name == "hltv":
        from csmatch.sources.hltv import HLTVSource
        return HLTVSource()
    raise click.BadParameter(f"unknown source: {name!r}")


@click.command()
@click.option(
    "--source",
    type=click.Choice(["bo3", "hltv", "mock"], case_sensitive=False),
    default="bo3",
    help="Data source to use.",
)
@click.option(
    "--once",
    is_flag=True,
    help="Print the current live match list and exit (no TUI).",
)
def main(source: str, once: bool) -> None:
    """csmatch — live CS2 pro match terminal viewer."""
    src = _build_source(source.lower())

    if once:
        async def go() -> int:
            try:
                matches = await src.list_live()
            except Exception as e:
                click.echo(f"error: {e}", err=True)
                return 2
            if not matches:
                click.echo("no live matches")
                return 0
            for m in matches:
                marker = "●" if m.status == "live" else "○"
                # series score
                if m.series_score is not None:
                    series = f"{m.series_score.team_a}-{m.series_score.team_b}"
                elif m.status == "live":
                    series = "LIVE"
                else:
                    series = "vs"
                # current map round score, if any
                live_bit = ""
                if m.score is not None:
                    live_bit = f" · {m.score.team_a:>2}-{m.score.team_b:<2}"
                    if m.score.side_a:
                        live_bit += f" {m.score.side_a}"
                line = (
                    f"{marker} {m.team_a.name[:18]:<18} "
                    f"{series:>5}{live_bit:<12} "
                    f"{m.team_b.name[:18]:<18}  "
                    f"{m.map or '-':<12} BO{m.best_of or '?'}"
                )
                click.echo(line)
            return 0

        sys.exit(asyncio.run(go()))

    from csmatch.tui import CsMatchApp
    app = CsMatchApp(src)
    app.run()


if __name__ == "__main__":
    main()
