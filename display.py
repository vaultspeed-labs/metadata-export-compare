from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich import box
from rich.text import Text

from compare import DiffResult

console = Console()

_GREEN = "green"
_RED = "red"
_YELLOW = "yellow"
_DIM = "dim"


def _make_row_table(df, columns: list[str], style: str, max_rows: int = 50) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold")
    for col in columns:
        table.add_column(col, overflow="fold")

    shown = df.head(max_rows)
    for _, row in shown.iterrows():
        table.add_row(*[str(row.get(c, "")) for c in columns], style=style)

    if len(df) > max_rows:
        table.add_row(*["…"] * len(columns), style=_DIM)

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

    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold")
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
        table.add_row(*["…"] * table.column_count, style=_DIM)

    return table


def render_diff(result: DiffResult) -> None:
    console.print()
    console.print(
        Panel.fit(
            f"[bold]Metadata Export Comparison[/bold]\n"
            f"[{_RED}]A:[/{_RED}] {result.release_a_label}\n"
            f"[{_GREEN}]B:[/{_GREEN}] {result.release_b_label}",
            border_style="blue",
        )
    )

    # Summary panel
    summary_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    summary_table.add_column("Metric", style="bold")
    summary_table.add_column("Value", justify="right")
    summary_table.add_row("Rows in A", str(result.total_a))
    summary_table.add_row("Rows in B", str(result.total_b))
    summary_table.add_row("Unchanged", str(result.unchanged_count), )
    summary_table.add_row(
        Text("Added", style=_GREEN),
        Text(str(len(result.added)), style=_GREEN),
    )
    summary_table.add_row(
        Text("Removed", style=_RED),
        Text(str(len(result.removed)), style=_RED),
    )
    summary_table.add_row(
        Text("Changed", style=_YELLOW),
        Text(str(len(result.changed)), style=_YELLOW),
    )

    console.print(Panel(summary_table, title="Summary", border_style="blue"))

    if not result.has_differences:
        console.print(
            Panel(
                "[bold green]No differences found — the two exports are identical.[/bold green]",
                border_style="green",
            )
        )
        return

    display_cols = result.key_columns + [c for c in result.all_columns if c not in result.key_columns]

    # Added rows
    if not result.added.empty:
        console.print(Rule(f"[{_GREEN}]Added ({len(result.added)} rows)[/{_GREEN}]"))
        table = _make_row_table(result.added, display_cols, style=_GREEN)
        console.print(table)

    # Removed rows
    if not result.removed.empty:
        console.print(Rule(f"[{_RED}]Removed ({len(result.removed)} rows)[/{_RED}]"))
        table = _make_row_table(result.removed, display_cols, style=_RED)
        console.print(table)

    # Changed rows
    if not result.changed.empty:
        value_cols = [c for c in result.all_columns if c not in result.key_columns]
        console.print(Rule(f"[{_YELLOW}]Changed ({len(result.changed)} rows)[/{_YELLOW}]"))
        table = _make_changed_table(result.changed, result.key_columns, value_cols)
        console.print(table)

    console.print()
    console.print(Rule("Plain Text Summary"))
    console.print(result.plain_text_summary())
