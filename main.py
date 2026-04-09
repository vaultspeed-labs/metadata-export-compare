#!/usr/bin/env python3
"""
metadata-export-compare

Interactive CLI to compare two VaultSpeed metadata exports.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import questionary
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.text import Text

import auth
from compare import compare
from display import render_diff
from vaultspeed_client import VaultSpeedClient, VaultSpeedApiError

console = Console()

# ---------------------------------------------------------------------------
# ANSI logo
# ---------------------------------------------------------------------------

_LOGO_LINES = [
    r" __     __          _ _                           _      __  ___  __   __   ",
    r" \ \   / /_ _ _   _| | |_ ___ ____   ___  ___  __| |     \ \/ _ \/ /  /_/   ",
    r"  \ \ / / _` | | | | | __/ __|  _ \ / _ \/ _ \/ _` |      \/ / \ \/ /\      ",
    r"   \ V / (_| | |_| | | |_\__ \ |_) |  __/  __/ (_| |      / / \/\ \/ /      ",
    r"    \_/ \__,_|\__,_|_|\__|___/ .__/ \___|\___|\__,_|     /_/ \/_/\__/       ",
    r"                             |_|                                              ",
]

_TAGLINE = (
    "[#808090]»»»[/#808090] "
    "[bold #9c72f2]Metadata Export Compare[/bold #9c72f2] "
    "[#808090]«««[/#808090]"
)


def _print_logo() -> None:
    console.print()
    for text in _LOGO_LINES:
        console.print(f"  [bold #9c72f2]{text}[/bold #9c72f2]")
    console.print(f"\n  {_TAGLINE}\n")


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------

_BACK = object()  # sentinel returned when the user cancels / goes back


def _choose(prompt: str, choices: list[dict], label_fn, back_label: str = "‹ Back") -> Any:
    """Present a list of items. Returns the selected item or _BACK."""
    options = [{"name": label_fn(item), "value": item} for item in choices]
    options.append({"name": back_label, "value": _BACK})
    selected = questionary.select(prompt, choices=options).ask()
    if selected is None:  # Ctrl-C
        sys.exit(0)
    return selected


def _choose_two(prompt_a: str, prompt_b: str, choices: list[dict], label_fn) -> tuple[Any, Any]:
    """Ask the user to pick two distinct items. Returns (_BACK, _BACK) if user goes back."""
    a = _choose(prompt_a, choices, label_fn)
    if a is _BACK:
        return _BACK, _BACK
    remaining = [item for item in choices if label_fn(item) != label_fn(a)]
    if not remaining:
        console.print("[red]Only one release available — cannot compare.[/red]")
        return _BACK, _BACK
    b = _choose(prompt_b, remaining, label_fn)
    if b is _BACK:
        return _BACK, _BACK
    return a, b


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _authenticate() -> VaultSpeedClient:
    profiles = auth.list_profiles()

    if profiles:
        choices = [{"name": f"Profile: {p}", "value": p} for p in profiles]
        choices.append({"name": "Enter new credentials", "value": None})
        selected = questionary.select("Select credentials:", choices=choices).ask()
        if selected is None:
            sys.exit(0)

        if selected is not None and selected in profiles:
            profile = auth.get_profile(selected)
            base_url = profile["base_url"]
            username = profile["username"]
            password = profile["password"]
            profile_name_to_save = None
        else:
            base_url, username, password, profile_name_to_save = auth.prompt_credentials(profiles)
    else:
        base_url, username, password, profile_name_to_save = auth.prompt_credentials([])

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as progress:
        progress.add_task("Logging in…")
        try:
            token = VaultSpeedClient.login(base_url, username, password)
        except Exception as exc:
            console.print(f"[#fa584c]Login failed:[/#fa584c] {exc}")
            sys.exit(1)

    if profile_name_to_save:
        auth.save_profile(profile_name_to_save, base_url, username, password)
        console.print(f"[#00946a]Credentials saved as profile '{profile_name_to_save}'.[/#00946a]")

    return VaultSpeedClient(base_url=base_url, access_token=token)


# ---------------------------------------------------------------------------
# Release selection
# ---------------------------------------------------------------------------

def _pick_dv_and_bv_release(
    client: VaultSpeedClient,
    dvid: int,
    dv_releases: list[dict],
    label: str,
) -> tuple[Any, Any]:
    """
    Let the user pick a DV release and then a BV release for one side (A or B).
    Returns (dv_release, bv_release) or (_BACK, _BACK) if the user goes back.
    """
    def _dv_label(r: dict) -> str:
        return f"{r.get('vs_dv_name', '')} (id={r.get('dv_vs_id', '')})"

    def _bv_label(r: dict) -> str:
        return f"{r.get('bv_vs_name', '')} (id={r.get('bv_vs_id', '')})"

    while True:
        dv_release = _choose(f"[{label}] Select data vault release:", dv_releases, _dv_label)
        if dv_release is _BACK:
            return _BACK, _BACK
        dvvsid = int(dv_release["dv_vs_id"])

        with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
            p.add_task(f"Fetching business vault releases for {label}…")
            bv_releases = client.list_business_vault_releases(dvid=dvid, dvvsid=dvvsid)

        if not bv_releases:
            console.print(f"[#fbab40]No business vault releases for this DV release — pick another.[/#fbab40]")
            continue

        bv_release = _choose(f"[{label}] Select business vault release:", bv_releases, _bv_label)
        if bv_release is _BACK:
            continue  # back to DV release selection for this side

        return dv_release, bv_release


def _select_releases(client: VaultSpeedClient) -> tuple[dict[str, Any], dict[str, Any], int]:
    """
    Walk the user through:
      project → data vault
        → Release A: DV release → BV release
        → Release B: DV release → BV release
        → preset

    Any "‹ Back" selection returns to the previous step.
    Returns (bv_release_a, bv_release_b, preset_id).
    """
    # Fetch projects & presets once — they don't change during a session
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
        p.add_task("Fetching projects & presets…")
        projects = client.list_projects()
        presets = client.list_metadata_export_presets()

    if not projects:
        console.print("[#fa584c]No projects found.[/#fa584c]")
        sys.exit(1)
    if not presets:
        console.print("[#fa584c]No metadata export presets found.[/#fa584c]")
        sys.exit(1)

    # Resolve preset up-front (auto-select when only one exists)
    if len(presets) == 1:
        preset = presets[0]
        console.print(f"[#808090]Using preset:[/#808090] [bold #9c72f2]{preset.get('name', preset)}[/bold #9c72f2]")
        preset_id = int(preset["preset_id"])
        fixed_preset = True
    else:
        preset_id = -1
        fixed_preset = False

    while True:
        # ── Project ──────────────────────────────────────────────────
        project = _choose(
            "Select project:",
            sorted(projects, key=lambda x: x.get("project_name", "").lower()),
            lambda x: x.get("project_name", str(x)),
            back_label="✕ Exit",
        )
        if project is _BACK:
            sys.exit(0)

        # ── Data vault ───────────────────────────────────────────────
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
            p.add_task("Fetching data vaults…")
            dvs = client.list_data_vaults(project["project_name"])

        if not dvs:
            console.print("[#fbab40]No data vaults found for this project — pick another.[/#fbab40]")
            continue

        dv = _choose("Select data vault:", dvs, lambda x: x.get("dv_name", str(x)))
        if dv is _BACK:
            continue
        dvid = int(dv["dv_id"])

        # ── DV releases (fetched once, shared for A and B) ───────────
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
            p.add_task("Fetching data vault releases…")
            dv_releases = client.list_data_vault_releases(dvid)

        if not dv_releases:
            console.print("[#fbab40]No data vault releases found — pick another data vault.[/#fbab40]")
            continue

        # ── Release A: DV release → BV release ───────────────────────
        console.rule("[bold #ae2695]Release A[/bold #ae2695]", style="#ae2695")
        _, release_a = _pick_dv_and_bv_release(client, dvid, dv_releases, "A")
        if release_a is _BACK:
            continue  # back to data vault

        # ── Release B: DV release → BV release ───────────────────────
        console.rule("[bold #2ee0cb]Release B[/bold #2ee0cb]", style="#2ee0cb")
        _, release_b = _pick_dv_and_bv_release(client, dvid, dv_releases, "B")
        if release_b is _BACK:
            continue  # back to data vault

        # ── Preset (when multiple exist) ─────────────────────────────
        if not fixed_preset:
            chosen_preset = _choose("Select export preset:", presets, lambda x: x.get("name", str(x)))
            if chosen_preset is _BACK:
                continue
            preset_id = int(chosen_preset["preset_id"])

        return release_a, release_b, preset_id


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _export(client: VaultSpeedClient, release: dict, preset_id: int, label: str) -> str:
    bv_vs_id = int(release["bv_vs_id"])
    console.print(f"[#808090]Exporting[/#808090] [bold #9c72f2]{label}[/bold #9c72f2][#808090]…[/#808090]")
    with Progress(SpinnerColumn(style="#6424eb"), TextColumn("{task.description}", style="#808090"), transient=True) as p:
        p.add_task(f"Waiting for export ({label})…")
        try:
            return client.export_metadata(preset_id=preset_id, bv_vs_id=bv_vs_id)
        except VaultSpeedApiError as exc:
            console.print(f"[#fa584c]Export failed for {label}:[/#fa584c] {exc}")
            sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _print_logo()

    client = _authenticate()
    release_a, release_b, preset_id = _select_releases(client)

    label_a = release_a.get("bv_vs_name", f"bv_vs_id={release_a.get('bv_vs_id')}")
    label_b = release_b.get("bv_vs_name", f"bv_vs_id={release_b.get('bv_vs_id')}")

    csv_a = _export(client, release_a, preset_id, label_a)
    csv_b = _export(client, release_b, preset_id, label_b)

    console.print(f"[#808090]Comparing exports…[/#808090]")
    result = compare(csv_a, csv_b, release_a_label=label_a, release_b_label=label_b)

    render_diff(result)

    # Write JSON + Markdown output files
    def _safe_name(s: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", s)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    stem = f"compare_{_safe_name(label_a)}_vs_{_safe_name(label_b)}_{ts}"

    json_path = Path(f"{stem}.json")
    md_path = Path(f"{stem}.md")

    json_path.write_text(result.to_json(), encoding="utf-8")
    md_path.write_text(result.to_markdown(), encoding="utf-8")

    console.print(f"\n[bold #00946a]✓[/bold #00946a] [#808090]JSON report :[/#808090] [#9c72f2]{json_path}[/#9c72f2]")
    console.print(f"[bold #00946a]✓[/bold #00946a] [#808090]MD report   :[/#808090] [#9c72f2]{md_path}[/#9c72f2]")


if __name__ == "__main__":
    main()
