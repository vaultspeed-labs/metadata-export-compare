from __future__ import annotations

import io
from dataclasses import dataclass, field

import pandas as pd


# Columns that together form a logical primary key for a metadata row.
# If none of these are present the comparison falls back to all columns.
_CANDIDATE_KEY_COLUMNS = [
    ["src_table_name", "src_column_name", "dv_table_name", "dv_column_name"],
    ["src_table_name", "src_column_name", "dv_table_name"],
    ["src_table_name", "src_column_name"],
    ["dv_table_name", "dv_column_name"],
]


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
            "METADATA EXPORT COMPARISON SUMMARY",
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
            lines += ["", "No differences found — the two exports are identical."]
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


def _parse_csv(csv_content: str) -> pd.DataFrame:
    """Parse VaultSpeed semicolon-delimited metadata export CSV."""
    df = pd.read_csv(io.StringIO(csv_content), sep=";", dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower() for c in df.columns]
    return df


def _pick_key_columns(columns: list[str]) -> list[str]:
    for candidate in _CANDIDATE_KEY_COLUMNS:
        if all(c in columns for c in candidate):
            return candidate
    # Fall back to all columns as the key (pure row comparison)
    return columns


def compare(
    csv_a: str,
    csv_b: str,
    release_a_label: str = "Release A",
    release_b_label: str = "Release B",
) -> DiffResult:
    df_a = _parse_csv(csv_a)
    df_b = _parse_csv(csv_b)

    # Align columns: work with the union of both sets
    all_cols = list(dict.fromkeys(list(df_a.columns) + list(df_b.columns)))
    for col in all_cols:
        if col not in df_a.columns:
            df_a[col] = ""
        if col not in df_b.columns:
            df_b[col] = ""

    df_a = df_a[all_cols].fillna("").astype(str)
    df_b = df_b[all_cols].fillna("").astype(str)

    key_cols = _pick_key_columns(all_cols)
    value_cols = [c for c in all_cols if c not in key_cols]

    df_a = df_a.set_index(key_cols)
    df_b = df_b.set_index(key_cols)

    idx_a = set(df_a.index)
    idx_b = set(df_b.index)

    removed_idx = idx_a - idx_b
    added_idx = idx_b - idx_a
    common_idx = idx_a & idx_b

    removed = df_a.loc[list(removed_idx)].reset_index() if removed_idx else pd.DataFrame(columns=all_cols)
    added = df_b.loc[list(added_idx)].reset_index() if added_idx else pd.DataFrame(columns=all_cols)

    # Detect changed rows among common keys
    if common_idx and value_cols:
        common_a = df_a.loc[list(common_idx), value_cols]
        common_b = df_b.loc[list(common_idx), value_cols]
        diff_mask = (common_a != common_b).any(axis=1)
        changed_keys = diff_mask[diff_mask].index

        if len(changed_keys) > 0:
            changed_a = df_a.loc[changed_keys, value_cols].add_suffix("_a")
            changed_b = df_b.loc[changed_keys, value_cols].add_suffix("_b")
            changed = pd.concat([changed_a, changed_b], axis=1).reset_index()
        else:
            changed = pd.DataFrame()

        unchanged_count = int((~diff_mask).sum())
    else:
        changed = pd.DataFrame()
        unchanged_count = len(common_idx)

    return DiffResult(
        key_columns=key_cols,
        all_columns=all_cols,
        added=added,
        removed=removed,
        changed=changed,
        unchanged_count=unchanged_count,
        release_a_label=release_a_label,
        release_b_label=release_b_label,
    )
