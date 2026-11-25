from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd


# Columns that are release-specific identifiers and carry no meaningful
# diff signal — excluded from comparison entirely.
IGNORED_COLUMNS = {
    "tab_col_dv_vs_id",
    "datavault",
    "source",
    "src_name",
    "dv_vs_id",
    "tab_dv_vs_id",
    "bv_vs_id",
}

# Fixed key columns used for every comparison.
# Only the subset that is actually present in the CSV will be used.
KEY_COLUMNS = [
    "source_name",
    "src_physical_schema",
    "stg_physical_schema",
    "dv_physical_schema",
    "cdc_physical_schema",
    "ini_physical_schema",
    "src_table_name",
    "stg_table_name",
    "dv_table_name",
    "dv_table_type",
    "src_column_name",
    "stg_column_name",
    "dv_column_name",
    "bv_physical_schema",
    "bv_table_type",
    "bv_table_name",
    "bv_column_name",
]

# Inventory comparison (expected_objects.csv from vaultspeed_inventory.py).
# Objects are matched by schema and name. The database columns hold the optional
# --database label chosen per run, not release content, and release/object version
# IDs change on every release, so both are ignored.
INVENTORY_KEY_COLUMNS = ["schema_name", "object_name"]
INVENTORY_IGNORED_COLUMNS = {
    "database_name",
    "underlying_database",
    "dv_id",
    "dv_vs_id",
    "bv_vs_id",
    "tab_dv_vs_id",
    "tab_dv_business_view_id",
}
INVENTORY_DISPLAY_COLUMNS = [
    "schema_name",
    "object_name",
    "object_type",
    "layer",
    "vaultspeed_type",
    "expected_basis",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DiffResult:
    key_columns: list[str]
    all_columns: list[str]
    added: pd.DataFrame        # rows only in release_b
    removed: pd.DataFrame      # rows only in release_a
    changed: pd.DataFrame      # rows with same key but different values
    unchanged_count: int
    release_a_label: str = ""
    release_b_label: str = ""
    title: str = "Metadata Export Comparison"
    # Columns shown for added/removed rows in the terminal; None shows all columns.
    display_columns: list[str] | None = None

    @property
    def total_a(self) -> int:
        return len(self.removed) + len(self.changed) + self.unchanged_count

    @property
    def total_b(self) -> int:
        return len(self.added) + len(self.changed) + self.unchanged_count

    @property
    def has_differences(self) -> bool:
        return len(self.added) > 0 or len(self.removed) > 0 or len(self.changed) > 0

    def plain_text_summary(self) -> str:
        lines = [
            "=" * 60,
            f"{self.title.upper()} SUMMARY",
            "=" * 60,
            f"Release A : {self.release_a_label}",
            f"Release B : {self.release_b_label}",
            f"Key columns: {', '.join(self.key_columns)}",
            "",
            f"Total rows in A : {self.total_a}",
            f"Total rows in B : {self.total_b}",
            "",
            f"  Unchanged : {self.unchanged_count}",
            f"  Added     : {len(self.added)}",
            f"  Removed   : {len(self.removed)}",
            f"  Changed   : {len(self.changed)}",
        ]

        if not self.has_differences:
            lines += ["", "No differences found."]
            return "\n".join(lines)

        if not self.added.empty:
            lines += ["", f"ADDED ({len(self.added)} rows):"]
            for _, row in self.added.iterrows():
                key_str = " | ".join(f"{c}={row[c]}" for c in self.key_columns if c in row)
                lines.append(f"  + {key_str}")

        if not self.removed.empty:
            lines += ["", f"REMOVED ({len(self.removed)} rows):"]
            for _, row in self.removed.iterrows():
                key_str = " | ".join(f"{c}={row[c]}" for c in self.key_columns if c in row)
                lines.append(f"  - {key_str}")

        if not self.changed.empty:
            lines += ["", f"CHANGED ({len(self.changed)} rows):"]
            value_cols = [c for c in self.all_columns if c not in self.key_columns]
            for _, row in self.changed.iterrows():
                key_str = " | ".join(f"{c}={row[c]}" for c in self.key_columns if c in row)
                lines.append(f"  ~ {key_str}")
                for col in value_cols:
                    col_a = f"{col}_a"
                    col_b = f"{col}_b"
                    if col_a in row and col_b in row:
                        val_a = row[col_a]
                        val_b = row[col_b]
                        if str(val_a) != str(val_b):
                            lines.append(f"      {col}: '{val_a}' → '{val_b}'")

        lines.append("=" * 60)
        return "\n".join(lines)

    def to_json(self) -> str:
        value_cols = [c for c in self.all_columns if c not in self.key_columns]

        def _changed_rows() -> list[dict]:
            rows = []
            for _, row in self.changed.iterrows():
                entry: dict = {c: row[c] for c in self.key_columns if c in row}
                entry["changes"] = {}
                for col in value_cols:
                    a, b = row.get(f"{col}_a"), row.get(f"{col}_b")
                    if a is not None and b is not None and str(a) != str(b):
                        entry["changes"][col] = {"from": str(a), "to": str(b)}
                rows.append(entry)
            return rows

        payload = {
            "generated_at": _utc_now().isoformat().replace("+00:00", "Z"),
            "title": self.title,
            "release_a": self.release_a_label,
            "release_b": self.release_b_label,
            "key_columns": self.key_columns,
            "summary": {
                "total_a": self.total_a,
                "total_b": self.total_b,
                "unchanged": self.unchanged_count,
                "added": len(self.added),
                "removed": len(self.removed),
                "changed": len(self.changed),
            },
            "added": self.added.to_dict(orient="records"),
            "removed": self.removed.to_dict(orient="records"),
            "changed": _changed_rows(),
        }
        return json.dumps(payload, indent=2, default=str)

    def to_markdown(self) -> str:
        value_cols = [c for c in self.all_columns if c not in self.key_columns]
        ts = _utc_now().strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"# {self.title}",
            "",
            f"_Generated: {ts}_",
            "",
            "## Releases",
            "",
            f"| | Release |",
            f"|---|---|",
            f"| **A (baseline)** | {self.release_a_label} |",
            f"| **B (compare)** | {self.release_b_label} |",
            "",
            "## Summary",
            "",
            f"| Metric | Count |",
            f"|---|---|",
            f"| Rows in A | {self.total_a} |",
            f"| Rows in B | {self.total_b} |",
            f"| Unchanged | {self.unchanged_count} |",
            f"| ➕ Added | {len(self.added)} |",
            f"| ➖ Removed | {len(self.removed)} |",
            f"| ✏️ Changed | {len(self.changed)} |",
            "",
        ]

        if not self.has_differences:
            lines.append("_No differences found._")
            return "\n".join(lines)

        def _md_table(df: pd.DataFrame, cols: list[str]) -> list[str]:
            if df.empty:
                return ["_None_", ""]
            present = [c for c in cols if c in df.columns]
            header = "| " + " | ".join(present) + " |"
            sep = "| " + " | ".join(["---"] * len(present)) + " |"
            rows = [header, sep]
            for _, row in df.iterrows():
                rows.append("| " + " | ".join(str(row.get(c, "")) for c in present) + " |")
            return rows + [""]

        if not self.added.empty:
            lines += [f"## ➕ Added ({len(self.added)} rows)", ""]
            lines += _md_table(self.added, self.key_columns + value_cols)

        if not self.removed.empty:
            lines += [f"## ➖ Removed ({len(self.removed)} rows)", ""]
            lines += _md_table(self.removed, self.key_columns + value_cols)

        if not self.changed.empty:
            lines += [f"## ✏️ Changed ({len(self.changed)} rows)", ""]
            # Build a flat table: key cols + alternating from/to per changed value col
            differing = [
                c for c in value_cols
                if f"{c}_a" in self.changed.columns
                and f"{c}_b" in self.changed.columns
                and (self.changed[f"{c}_a"] != self.changed[f"{c}_b"]).any()
            ]
            header_cols = self.key_columns + [h for c in differing for h in (f"{c} (A)", f"{c} (B)")]
            header = "| " + " | ".join(header_cols) + " |"
            sep = "| " + " | ".join(["---"] * len(header_cols)) + " |"
            lines += [header, sep]
            for _, row in self.changed.iterrows():
                cells = [str(row.get(c, "")) for c in self.key_columns]
                for c in differing:
                    cells += [str(row.get(f"{c}_a", "")), str(row.get(f"{c}_b", ""))]
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")

        return "\n".join(lines)


def _parse_csv(csv_content: str, sep: str = ";") -> pd.DataFrame:
    """Parse a CSV as strings. Metadata exports are semicolon-delimited."""
    df = pd.read_csv(io.StringIO(csv_content), sep=sep, dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower() for c in df.columns]
    return df


def _pick_key_columns(columns: list[str], candidates: list[str]) -> list[str]:
    """Return the candidate key columns present in this CSV, preserving order."""
    return [c for c in candidates if c in columns]


def compare(
    csv_a: str,
    csv_b: str,
    release_a_label: str = "Release A",
    release_b_label: str = "Release B",
    *,
    sep: str = ";",
    key_columns: list[str] | None = None,
    ignored_columns: set[str] | None = None,
    title: str = "Metadata Export Comparison",
    display_columns: list[str] | None = None,
) -> DiffResult:
    """Compare two CSVs row by row on key columns.

    Defaults suit VaultSpeed metadata exports; pass sep/key_columns/ignored_columns
    for other files (see compare_inventories).
    """
    df_a = _parse_csv(csv_a, sep)
    df_b = _parse_csv(csv_b, sep)

    # Align columns: work with the union of both sets
    all_cols = list(dict.fromkeys(list(df_a.columns) + list(df_b.columns)))
    for col in all_cols:
        if col not in df_a.columns:
            df_a[col] = ""
        if col not in df_b.columns:
            df_b[col] = ""

    # Drop columns that carry no meaningful diff signal
    ignored = IGNORED_COLUMNS if ignored_columns is None else ignored_columns
    all_cols = [c for c in all_cols if c not in ignored]

    df_a = df_a[all_cols].fillna("").astype(str)
    df_b = df_b[all_cols].fillna("").astype(str)

    key_cols = _pick_key_columns(all_cols, KEY_COLUMNS if key_columns is None else key_columns)
    if not key_cols:
        raise ValueError("None of the key columns are present in the CSV files.")
    value_cols = [c for c in all_cols if c not in key_cols]

    # Use merge-based comparison so duplicate keys and row-order differences
    # are handled correctly — no index alignment assumptions needed.
    merged = df_a.merge(df_b, on=key_cols, how="outer", suffixes=("_a", "_b"), indicator=True)

    removed = (
        merged[merged["_merge"] == "left_only"][key_cols + [f"{c}_a" for c in value_cols]]
        .rename(columns={f"{c}_a": c for c in value_cols})
        .reset_index(drop=True)
    )
    added = (
        merged[merged["_merge"] == "right_only"][key_cols + [f"{c}_b" for c in value_cols]]
        .rename(columns={f"{c}_b": c for c in value_cols})
        .reset_index(drop=True)
    )
    both = merged[merged["_merge"] == "both"].copy()

    # Detect changed rows among rows present in both exports
    if not both.empty and value_cols:
        diff_flags = pd.concat(
            [both[f"{c}_a"].fillna("") != both[f"{c}_b"].fillna("") for c in value_cols],
            axis=1,
        ).any(axis=1)
        changed = both[diff_flags][key_cols + [f"{c}_a" for c in value_cols] + [f"{c}_b" for c in value_cols]]
        unchanged_count = int((~diff_flags).sum())
    else:
        changed = pd.DataFrame()
        unchanged_count = len(both)

    return DiffResult(
        key_columns=key_cols,
        all_columns=all_cols,
        added=added,
        removed=removed,
        changed=changed,
        unchanged_count=unchanged_count,
        release_a_label=release_a_label,
        release_b_label=release_b_label,
        title=title,
        display_columns=display_columns,
    )


def compare_inventories(
    csv_a: str,
    csv_b: str,
    release_a_label: str = "Inventory A",
    release_b_label: str = "Inventory B",
) -> DiffResult:
    """Compare two expected_objects.csv files written by vaultspeed_inventory.py."""
    return compare(
        csv_a,
        csv_b,
        release_a_label,
        release_b_label,
        sep=",",
        key_columns=INVENTORY_KEY_COLUMNS,
        ignored_columns=INVENTORY_IGNORED_COLUMNS,
        title="Snowflake Inventory Comparison",
        display_columns=INVENTORY_DISPLAY_COLUMNS,
    )
