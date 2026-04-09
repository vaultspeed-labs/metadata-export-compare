from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich import box
from rich.text import Text

from compare import DiffResult

console = Console()

# ---------------------------------------------------------------------------
# VaultSpeed brand palette (mapped to Rich hex color syntax)
# ---------------------------------------------------------------------------
_PURPLE     = "#9c72f2"   # purple-300  — primary accent, rules, borders
_PURPLE_DIM = "#6424eb"   # purple      — panel borders
_GREEN      = "#3eb797"   # green-dim   — added rows
_GREEN_HI   = "#00946a"   # green       — added header
_RED        = "#fa584c"   # red         — removed rows
_ORANGE     = "#fbab40"   # orange-dim  — changed rows
_ORANGE_HI  = "#c6700c"   # orange      — changed header
_PINK       = "#ae2695"   # pink        — release A label
_TEAL       = "#2ee0cb"   # teal        — release B label
_MUTED      = "#808090"   # text-muted  — dim / truncation


def _make_row_table(df, columns: list[str], style: str, max_rows: int = 50) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style=f"bold {_PURPLE}")
    for col in columns:
        table.add_column(col, overflow="fold")

    shown = df.head(max_rows)
    for _, row in shown.iterrows():
        table.add_row(*[str(row.get(c, "")) for c in columns], style=style)

    if len(df) > max_rows:
        table.add_row(*["…"] * len(columns), style=_MUTED)

    return table


def _make_changed_table(changed_df, key_cols: list[str], value_cols: list[str], max_rows: int = 50) -> Table:
    # Only include value columns that actually differ in at least one row
    differing_value_cols: list[str] = []
    for col in value_cols:
        col_a = f"{col}_a"
        col_b = f"{col}_b"
        if col_a in changed_df.columns and col_b in changed_df.columns:
            if (changed_df[col_a] != changed_df[col_b]).any():
                differing_value_cols.append(col)

    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style=f"bold {_PURPLE}")
    for col in key_cols:
        table.add_column(col, overflow="fold")
    for col in differing_value_cols:
        table.add_column(f"{col} (A)", overflow="fold", style=_RED)
        table.add_column(f"{col} (B)", overflow="fold", style=_GREEN)

    shown = changed_df.head(max_rows)
    for _, row in shown.iterrows():
        cells: list[str] = []
        for col in key_cols:
            cells.append(str(row.get(col, "")))
        for col in differing_value_cols:
            cells.append(str(row.get(f"{col}_a", "")))
            cells.append(str(row.get(f"{col}_b", "")))
        table.add_row(*cells)

    if len(changed_df) > max_rows:
        table.add_row(*["…"] * len(table.columns), style=_MUTED)

    return table


def render_diff(result: DiffResult) -> None:
    console.print()
    console.print(
        Panel.fit(
            f"[bold {_PURPLE}]Metadata Export Comparison[/bold {_PURPLE}]\n"
            f"[{_PINK}]A:[/{_PINK}] {result.release_a_label}\n"
            f"[{_TEAL}]B:[/{_TEAL}] {result.release_b_label}",
            border_style=_PURPLE_DIM,
        )
    )

    # Summary panel
    summary_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    summary_table.add_column("Metric", style=f"bold {_PURPLE}")
    summary_table.add_column("Value", justify="right")
    summary_table.add_row("Rows in A", str(result.total_a))
    summary_table.add_row("Rows in B", str(result.total_b))
    summary_table.add_row("Unchanged", str(result.unchanged_count))
    summary_table.add_row(
        Text("Added", style=_GREEN_HI),
        Text(str(len(result.added)), style=_GREEN_HI),
    )
    summary_table.add_row(
        Text("Removed", style=_RED),
        Text(str(len(result.removed)), style=_RED),
    )
    summary_table.add_row(
        Text("Changed", style=_ORANGE),
        Text(str(len(result.changed)), style=_ORANGE),
    )

    console.print(Panel(summary_table, title=f"[bold {_PURPLE}]Summary[/bold {_PURPLE}]", border_style=_PURPLE_DIM))

    if not result.has_differences:
        console.print(
            Panel(
                f"[bold {_GREEN_HI}]No differences found — the two exports are identical.[/bold {_GREEN_HI}]",
                border_style=_GREEN_HI,
            )
        )
        return

    display_cols = result.key_columns + [c for c in result.all_columns if c not in result.key_columns]

    # Added rows
    if not result.added.empty:
        console.print(Rule(f"[bold {_GREEN_HI}]Added ({len(result.added)} rows)[/bold {_GREEN_HI}]", style=_GREEN_HI))
        table = _make_row_table(result.added, display_cols, style=_GREEN)
        console.print(table)

    # Removed rows
    if not result.removed.empty:
        console.print(Rule(f"[bold {_RED}]Removed ({len(result.removed)} rows)[/bold {_RED}]", style=_RED))
        table = _make_row_table(result.removed, display_cols, style=_RED)
        console.print(table)

    # Changed rows
    if not result.changed.empty:
        value_cols = [c for c in result.all_columns if c not in result.key_columns]
        console.print(Rule(f"[bold {_ORANGE}]Changed ({len(result.changed)} rows)[/bold {_ORANGE}]", style=_ORANGE))
        table = _make_changed_table(result.changed, result.key_columns, value_cols)
        console.print(table)

    console.print()
    console.print(Rule(f"[{_PURPLE}]Plain Text Summary[/{_PURPLE}]", style=_PURPLE_DIM))
    console.print(result.plain_text_summary())
