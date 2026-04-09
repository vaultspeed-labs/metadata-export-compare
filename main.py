#!/usr/bin/env python3
"""
metadata-export-compare

Interactive CLI to compare two VaultSpeed metadata exports.
"""
from __future__ import annotations

import sys
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

_VAULT_DOOR = """\
   [bold blue]╔══════════╗[/bold blue]
   [bold blue]║[/bold blue][dim] ·  ·  · [/dim][bold blue]║[/bold blue]
   [bold blue]║[/bold blue][bold white]  ╔════╗  [/bold white][bold blue]║[/bold blue]
   [bold blue]║[/bold blue][bold white]  ║ [bold cyan]◉[/bold cyan][bold white] ║  [/bold white][bold blue]║[/bold blue]
   [bold blue]║[/bold blue][bold white]  ╚════╝  [/bold white][bold blue]║[/bold blue]
   [bold blue]║[/bold blue][dim] ·  ·  · [/dim][bold blue]║[/bold blue]
   [bold blue]╚══════════╝[/bold blue]"""

_WORDMARK_LINES = [
    (" __   __          _ _   ", "[bold cyan]"),
    (r" \ \ / /__ _  _| | |_  ", "[bold cyan]"),
    (r"  \ V / _` || | | |_|  ", "[bold cyan]"),
    (r"   \_/\__,_|\__,_|\__|  ", "[bold cyan]"),
    ("                         ", "[bold cyan]"),
    (" ___                   _ ", "[bold yellow]"),
    ("/ __|_ __  ___ ___ __| |", "[bold yellow]"),
    (r"\__ \ '_ \/ -_) -_) _` |", "[bold yellow]"),
    ("|___/ .__/\___\___\__,_|", "[bold yellow]"),
    ("    |_|                  ", "[bold yellow]"),
]

_SPEED_TAGLINE = (
    "[dim]»»»[/dim] [bold]Metadata Export Compare[/bold] [dim]«««[/dim]"
)


def _print_logo() -> None:
    vault_lines = _VAULT_DOOR.split("\n")
    word_lines = _WORDMARK_LINES

    # Pad vault lines to match word-mark height
    max_h = max(len(vault_lines), len(word_lines))
    vault_lines += [""] * (max_h - len(vault_lines))
    word_lines_padded = [("", "")] * ((max_h - len(word_lines)) // 2) + list(word_lines)
    word_lines_padded += [("", "")] * (max_h - len(word_lines_padded))

    console.print()
    for vault_ln, (word_text, word_style) in zip(vault_lines, word_lines_padded):
        console.print(f"  {vault_ln}  {word_style}{word_text}[/]")
    console.print()
    console.print(f"  {'':>13}{_SPEED_TAGLINE}")
    console.print()


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------

def _choose(prompt: str, choices: list[dict], label_fn) -> dict:
    """Present a list of items and return the selected one."""
    options = [{"name": label_fn(item), "value": item} for item in choices]
    selected = questionary.select(prompt, choices=options).ask()
    if selected is None:
        console.print("[yellow]Cancelled.[/yellow]")
        sys.exit(0)
    return selected


def _choose_two(prompt_a: str, prompt_b: str, choices: list[dict], label_fn) -> tuple[dict, dict]:
    """Ask the user to pick two distinct items from the same list."""
    a = _choose(prompt_a, choices, label_fn)
    remaining = [item for item in choices if label_fn(item) != label_fn(a)]
    if not remaining:
        console.print("[red]Only one release available — cannot compare.[/red]")
        sys.exit(1)
    b = _choose(prompt_b, remaining, label_fn)
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
            console.print(f"[red]Login failed:[/red] {exc}")
            sys.exit(1)

    if profile_name_to_save:
        auth.save_profile(profile_name_to_save, base_url, username, password)
        console.print(f"[green]Credentials saved as profile '{profile_name_to_save}'.[/green]")

    return VaultSpeedClient(base_url=base_url, access_token=token)


# ---------------------------------------------------------------------------
# Release selection
# ---------------------------------------------------------------------------

def _select_releases(client: VaultSpeedClient) -> tuple[dict[str, Any], dict[str, Any], int]:
    """
    Walk the user through project → data vault → two BV releases.
    Returns (release_a, release_b, preset_id).
    """
    # Projects
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
        p.add_task("Fetching projects…")
        projects = client.list_projects()

    if not projects:
        console.print("[red]No projects found.[/red]")
        sys.exit(1)

    project = _choose("Select project:", projects, lambda x: x.get("project_name", str(x)))

    # Data vaults
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
        p.add_task("Fetching data vaults…")
        dvs = client.list_data_vaults(project["project_name"])

    if not dvs:
        console.print("[red]No data vaults found for this project.[/red]")
        sys.exit(1)

    dv = _choose("Select data vault:", dvs, lambda x: x.get("dv_name", str(x)))
    dvid = int(dv["dv_id"])

    # BV releases
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
        p.add_task("Fetching releases…")
        releases = client.list_business_vault_releases(dvid=dvid)

    if len(releases) < 2:
        console.print("[red]Need at least two business vault releases to compare.[/red]")
        sys.exit(1)

    def _release_label(r: dict) -> str:
        name = r.get("bv_vs_name", "")
        rid = r.get("bv_vs_id", "")
        dv_rel = r.get("dv_vs_name", r.get("dv_vs_id", ""))
        return f"{name} (id={rid}, dv_release={dv_rel})"

    release_a, release_b = _choose_two(
        "Select Release A (baseline):",
        "Select Release B (compare to):",
        releases,
        _release_label,
    )

    # Preset
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
        p.add_task("Fetching export presets…")
        presets = client.list_metadata_export_presets()

    if not presets:
        console.print("[red]No metadata export presets found.[/red]")
        sys.exit(1)

    if len(presets) == 1:
        preset = presets[0]
        console.print(f"Using preset: [bold]{preset.get('name', preset)}[/bold]")
    else:
        preset = _choose("Select export preset:", presets, lambda x: x.get("name", str(x)))

    preset_id = int(preset["preset_id"])
    return release_a, release_b, preset_id


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _export(client: VaultSpeedClient, release: dict, preset_id: int, label: str) -> str:
    bv_vs_id = int(release["bv_vs_id"])
    console.print(f"Exporting [bold]{label}[/bold]…")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as p:
        p.add_task(f"Waiting for export ({label})…")
        try:
            return client.export_metadata(preset_id=preset_id, bv_vs_id=bv_vs_id)
        except VaultSpeedApiError as exc:
            console.print(f"[red]Export failed for {label}:[/red] {exc}")
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

    console.print("Comparing exports…")
    result = compare(csv_a, csv_b, release_a_label=label_a, release_b_label=label_b)

    render_diff(result)


if __name__ == "__main__":
    main()
