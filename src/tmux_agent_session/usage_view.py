"""Rich renderables for the picker's Usage tab: summary tiles, a stacked bar per day, and per-model detail."""
from __future__ import annotations

import datetime as dt
from typing import Iterable

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.measure import Measurement
from rich.table import Table
from rich.text import Text

from .formatting import PALETTE, short_model
from .usage import DailyUsage, UsageReport, UsageTotals


ACCENT = PALETTE["accent"]
MUTED = PALETTE["muted"]
COST_STYLE = "bold green"
# Token kinds in stacking order; the colors double as the chart legend.
TOKEN_SEGMENTS = (
    ("Input", "input_tokens", "blue"),
    ("Cache write", "cache_write_tokens", "magenta"),
    ("Cache read", "cache_read_tokens", ACCENT),
    ("Output", "output_tokens", "yellow"),
)
BAR_CHAR = "█"
TRACK_CHAR = "─"


def format_token_count(count: int) -> str:
    for suffix, size in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if count >= size:
            return f"{count / size:.1f}{suffix}"
    return str(count)


def format_cost(cost: float) -> str:
    return f"${cost:,.2f}"


def sum_totals(items: Iterable[UsageTotals]) -> UsageTotals:
    combined = UsageTotals()
    for totals in items:
        combined.input_tokens += totals.input_tokens
        combined.cache_write_tokens += totals.cache_write_tokens
        combined.cache_read_tokens += totals.cache_read_tokens
        combined.output_tokens += totals.output_tokens
        combined.cost += totals.cost
        combined.unpriced_models |= totals.unpriced_models
    return combined


def bar_cells(values: list[int], scale: int, width: int) -> list[int]:
    """Cells per segment, rounding cumulative edges so the bar length tracks the total exactly."""
    if scale <= 0 or width <= 0:
        return [0] * len(values)
    cells: list[int] = []
    running = filled = 0
    for value in values:
        running += value
        edge = min(width, round(running / scale * width))
        cells.append(edge - filled)
        filled = edge
    return cells


class StackedBar:
    """A horizontal bar split into colored segments; `scale` tokens fill the available width."""

    def __init__(self, totals: UsageTotals, scale: int) -> None:
        self.segments = [(getattr(totals, attr), style) for _, attr, style in TOKEN_SEGMENTS]
        self.scale = scale

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(4, options.max_width)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width
        bar = Text(no_wrap=True, overflow="crop")
        cells = bar_cells([value for value, _ in self.segments], self.scale, width)
        for count, (_, style) in zip(cells, self.segments):
            bar.append(BAR_CHAR * count, style=style)
        if not bar and any(value for value, _ in self.segments):
            # Keep light days visible next to a heavy one.
            style = next(style for value, style in reversed(self.segments) if value)
            bar.append("▏", style=style)
        bar.append(TRACK_CHAR * (width - bar.cell_len), style=MUTED)
        yield bar


def usage_tile(label: str, totals: UsageTotals, caption: str = "") -> Table:
    tile = Table.grid()
    tile.add_column(no_wrap=True, overflow="ellipsis")
    tile.add_row(Text(label, style=MUTED))
    tokens = Text(format_token_count(totals.total_tokens), style="bold")
    tokens.append(" tokens", style=MUTED)
    tile.add_row(tokens)
    tile.add_row(Text(format_cost(totals.cost), style=COST_STYLE))
    if caption:
        tile.add_row(Text(caption, style=MUTED))
    return tile


def summary_tiles(days: list[DailyUsage]) -> Table:
    newest_first = days[::-1]
    tiles = [
        usage_tile("Today", newest_first[0].totals),
        usage_tile("Last 7 days", sum_totals(day.totals for day in newest_first[:7])),
        usage_tile(f"Last {len(days)} days", sum_totals(day.totals for day in days)),
    ]
    busiest = max(days, key=lambda day: day.totals.total_tokens)
    if busiest.totals.total_tokens:
        tiles.append(usage_tile("Busiest day", busiest.totals, busiest.day.strftime("%a %b %d")))
    grid = Table.grid(expand=True, padding=(0, 2))
    for _ in tiles:
        grid.add_column(ratio=1)
    grid.add_row(*tiles)
    return grid


def token_legend(totals: UsageTotals) -> Text:
    legend = Text()
    for index, (label, attr, style) in enumerate(TOKEN_SEGMENTS):
        if index:
            legend.append("   ")
        legend.append("■ ", style=style)
        legend.append(f"{label} ", style=MUTED)
        legend.append(format_token_count(getattr(totals, attr)), style="bold")
    return legend


def day_label(day: dt.date, today: dt.date) -> str:
    if day == today:
        return "Today"
    if day == today - dt.timedelta(days=1):
        return "Yesterday"
    return day.strftime("%a %b %d")


def day_row(day: DailyUsage, scale: int, today: dt.date, selected: bool = False) -> Table:
    """One chart row; fixed column widths keep rows aligned when each is rendered on its own."""
    totals = day.totals
    label = Text()
    label.append("› " if selected else "  ", style=f"bold {ACCENT}")
    label_style = f"bold {ACCENT}" if selected else ("bold" if day.day == today else "")
    label.append(day_label(day.day, today), style=label_style if totals.total_tokens or selected else MUTED)
    if totals.total_tokens:
        tokens = Text(format_token_count(totals.total_tokens), style="bold")
        cost = Text(format_cost(totals.cost), style=COST_STYLE)
    else:
        tokens, cost = Text("—", style=MUTED), Text("")
    row = Table.grid(expand=True, padding=(0, 1))
    row.add_column(no_wrap=True, width=12)
    row.add_column(ratio=1)
    row.add_column(justify="right", no_wrap=True, width=6)
    row.add_column(justify="right", no_wrap=True, width=10)
    row.add_row(label, StackedBar(totals, scale), tokens, cost)
    return row


def day_scale(days: list[DailyUsage]) -> int:
    return max((day.totals.total_tokens for day in days), default=0)


def token_split(totals: UsageTotals) -> Text:
    split = Text()
    for index, (_, attr, style) in enumerate(TOKEN_SEGMENTS):
        if index:
            split.append("  ")
        split.append("■ ", style=style)
        split.append(format_token_count(getattr(totals, attr)))
    return split


def model_breakdown(day: DailyUsage | None, today: dt.date | None = None) -> Group:
    if day is None:
        return Group(Text("No day selected.", style=MUTED))
    title = Text(day_label(day.day, today) if today else day.day.strftime("%a %b %d"), style="bold")
    if today and day.day >= today - dt.timedelta(days=1):
        title.append(day.day.strftime("  %a %b %d"), style=MUTED)
    if not day.models:
        return Group(title, Text(""), Text("No Claude usage on this day.", style=MUTED))

    day_tokens = day.totals.total_tokens
    summary = Text(f"{format_token_count(day_tokens)} tokens", style="bold")
    summary.append("  ·  ", style=MUTED)
    summary.append(format_cost(day.totals.cost), style=COST_STYLE)
    parts: list = [title, summary]
    for model, totals in sorted(day.models.items(), key=lambda item: item[1].total_tokens, reverse=True):
        heading = Table.grid(expand=True)
        heading.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        heading.add_column(justify="right", no_wrap=True)
        share = totals.total_tokens / day_tokens if day_tokens else 0
        percent = "<1%" if 0 < share < 0.005 else f"{share:.0%}"
        heading.add_row(Text(short_model(model) or model, style="bold"), Text(percent, style=MUTED))
        figures = Table.grid(expand=True)
        figures.add_column(ratio=1, no_wrap=True)
        figures.add_column(justify="right", no_wrap=True)
        tokens = Text(format_token_count(totals.total_tokens), style="bold")
        tokens.append(" tokens", style=MUTED)
        cost = Text("not priced", style=MUTED) if totals.unpriced_models else Text(format_cost(totals.cost), style=COST_STYLE)
        figures.add_row(tokens, cost)
        parts += [Text(""), heading, StackedBar(totals, day_tokens), figures, token_split(totals)]
    return Group(*parts)


def usage_overview(report: UsageReport) -> Group:
    history = sum_totals(day.totals for day in report.days)
    return Group(
        summary_tiles(report.days),
        Text(""),
        Text(f"Claude tokens per day · last {len(report.days)} days", style=MUTED),
        token_legend(history),
    )


def usage_footnote(report: UsageReport) -> Text:
    footnote = Text("Estimated at Anthropic API list prices · days in local time", style=MUTED)
    unpriced = sum_totals(day.totals for day in report.days).unpriced_models
    if unpriced:
        footnote.append("\n* Not priced: " + ", ".join(sorted(unpriced)), style=MUTED)
    return footnote
