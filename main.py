#!/usr/bin/env python3
"""
metadata-export-compare

Interactive CLI for VaultSpeed metadata:
  - compare the metadata exports of two releases
  - build the Snowflake inventory of one release (vaultspeed_inventory.py)
  - compare two inventories
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich import box

import auth
import vaultspeed_inventory as inv
from compare import compare, compare_inventories, DiffResult
from display import render_diff
from vaultspeed_client import VaultSpeedClient, VaultSpeedApiError

console = Console()

ROOT = Path(__file__).resolve().parent
EXPORTS_DIR = Path("exports")

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


class _Abort(Exception):
    """Stop the current task and return to the main menu."""


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


def _ask(question) -> Any:
    """Run a questionary prompt; Ctrl-C exits."""
    answer = question.ask()
    if answer is None:
        sys.exit(0)
    return answer


def _spinner() -> Progress:
    return Progress(SpinnerColumn(style="#6424eb"), TextColumn("{task.description}", style="#808090"), transient=True)


def _safe_name(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

_ENV_KEYS = ("VAULTSPEED_BASE_URL", "VAULTSPEED_USERNAME", "VAULTSPEED_PASSWORD", "VAULTSPEED_ACCESS_TOKEN")


def _env_credentials_available() -> bool:
    """True when .env, the environment or a token file holds inventory-style credentials."""
    env = inv.read_env(ROOT / ".env")
    has_base = bool(os.environ.get("VAULTSPEED_BASE_URL") or env.get("VAULTSPEED_BASE_URL"))
    has_secret = any(os.environ.get(k) or env.get(k) for k in _ENV_KEYS[1:]) or (ROOT / "token").is_file()
    return has_base and has_secret


def _login_from_env() -> VaultSpeedClient:
    """Log in with .env / environment variables / token file, as vaultspeed_inventory.py does."""
    args = argparse.Namespace(base_url=None, env_file=ROOT / ".env", token_file=ROOT / "token")
    try:
        base, token = inv.credentials(args)
    except inv.InventoryError as exc:
        console.print(f"[#fa584c]Login failed:[/#fa584c] {exc}")
        sys.exit(1)
    return VaultSpeedClient(base_url=base, access_token=token)


def _authenticate() -> VaultSpeedClient:
    profiles = auth.list_profiles()
    env_available = _env_credentials_available()

    if profiles or env_available:
        choices = [{"name": f"Profile: {p}", "value": ("profile", p)} for p in profiles]
        if env_available:
            choices.append({"name": "Use .env / environment variables", "value": ("env", None)})
        choices.append({"name": "Enter new credentials", "value": ("new", None)})
        kind, selected = _ask(questionary.select("Select credentials:", choices=choices))
    else:
        kind, selected = "new", None

    if kind == "env":
        with _spinner() as progress:
            progress.add_task("Logging in…")
            return _login_from_env()

    if kind == "profile":
        profile = auth.get_profile(selected)
        base_url = profile["base_url"]
        username = profile["username"]
        password = profile["password"]
        profile_name_to_save = None
    else:
        base_url, username, password, profile_name_to_save = auth.prompt_credentials(profiles)

    with _spinner() as progress:
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
    Let the user pick a DV release and then a BV release.
    Returns (dv_release, bv_release) or (_BACK, _BACK) if the user goes back.
    """
    def _dv_label(r: dict) -> str:
        return f"{r.get('vs_dv_name', '')} (id={r.get('dv_vs_id', '')})"

    def _bv_label(r: dict) -> str:
        return f"{r.get('bv_vs_name', '')} (id={r.get('bv_vs_id', '')})"

    prefix = f"[{label}] " if label else ""
    while True:
        sorted_dv = sorted(dv_releases, key=lambda r: int(r.get("dv_vs_id", 0)), reverse=True)
        dv_release = _choose(f"{prefix}Select data vault release:", sorted_dv, _dv_label)
        if dv_release is _BACK:
            return _BACK, _BACK
        dvvsid = int(dv_release["dv_vs_id"])

        with _spinner() as p:
            p.add_task("Fetching business vault releases…")
            bv_releases = client.list_business_vault_releases(dvid=dvid, dvvsid=dvvsid)

        if not bv_releases:
            console.print("[#fbab40]No business vault releases for this DV release — pick another.[/#fbab40]")
            continue

        sorted_bv = sorted(bv_releases, key=lambda r: int(r.get("bv_vs_id", 0)), reverse=True)
        bv_release = _choose(f"{prefix}Select business vault release:", sorted_bv, _bv_label)
        if bv_release is _BACK:
            continue  # back to DV release selection

        return dv_release, bv_release


def _pick_data_vault(client: VaultSpeedClient) -> tuple[Any, list[dict]]:
    """
    project → data vault. Returns (data_vault, dv_releases), or (_BACK, []) when the
    user backs out of the project list.
    """
    with _spinner() as p:
        p.add_task("Fetching projects…")
        projects = client.list_projects()
    if not projects:
        raise _Abort("No projects found.")

    while True:
        project = _choose(
            "Select project:",
            sorted(projects, key=lambda x: x.get("project_name", "").lower()),
            lambda x: x.get("project_name", str(x)),
            back_label="‹ Main menu",
        )
        if project is _BACK:
            return _BACK, []

        with _spinner() as p:
            p.add_task("Fetching data vaults…")
            dvs = client.list_data_vaults(project["project_name"])
        if not dvs:
            console.print("[#fbab40]No data vaults found for this project — pick another.[/#fbab40]")
            continue

        dv = _choose("Select data vault:", dvs, lambda x: x.get("dv_name", str(x)))
        if dv is _BACK:
            continue

        with _spinner() as p:
            p.add_task("Fetching data vault releases…")
            dv_releases = client.list_data_vault_releases(int(dv["dv_id"]))
        if not dv_releases:
            console.print("[#fbab40]No data vault releases found — pick another data vault.[/#fbab40]")
            continue

        return dv, dv_releases


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _write_reports(result: DiffResult, prefix: str) -> None:
    stem = f"{prefix}_{_safe_name(result.release_a_label)}_vs_{_safe_name(result.release_b_label)}_{_timestamp()}"
    json_path = Path(f"{stem}.json")
    md_path = Path(f"{stem}.md")
    json_path.write_text(result.to_json(), encoding="utf-8")
    md_path.write_text(result.to_markdown(), encoding="utf-8")
    console.print(f"\n[bold #00946a]✓[/bold #00946a] [#808090]JSON report :[/#808090] [#9c72f2]{json_path}[/#9c72f2]")
    console.print(f"[bold #00946a]✓[/bold #00946a] [#808090]MD report   :[/#808090] [#9c72f2]{md_path}[/#9c72f2]")


# ---------------------------------------------------------------------------
# Task 1: compare metadata exports
# ---------------------------------------------------------------------------

def _export(client: VaultSpeedClient, release: dict, preset_id: int, label: str) -> str:
    bv_vs_id = int(release["bv_vs_id"])
    console.print(f"[#808090]Exporting[/#808090] [bold #9c72f2]{label}[/bold #9c72f2][#808090]…[/#808090]")
    with _spinner() as p:
        p.add_task(f"Waiting for export ({label})…")
        try:
            return client.export_metadata(preset_id=preset_id, bv_vs_id=bv_vs_id)
        except VaultSpeedApiError as exc:
            raise _Abort(f"Export failed for {label}: {exc}") from None


def _release_label(dv_release: dict, bv_release: dict) -> str:
    """'<DV release> / <BV release>', e.g. 'seven / init' (BV names alone repeat across releases)."""
    dv_name = dv_release.get("vs_dv_name") or f"dv_vs_id={dv_release.get('dv_vs_id')}"
    bv_name = bv_release.get("bv_vs_name") or f"bv_vs_id={bv_release.get('bv_vs_id')}"
    return f"{dv_name} / {bv_name}"


def _compare_exports(client: VaultSpeedClient) -> None:
    with _spinner() as p:
        p.add_task("Fetching metadata export presets…")
        presets = client.list_metadata_export_presets()
    if not presets:
        raise _Abort("No metadata export presets found.")

    if len(presets) == 1:
        preset_id = int(presets[0]["preset_id"])
        console.print(f"[#808090]Using preset:[/#808090] [bold #9c72f2]{presets[0].get('name', preset_id)}[/bold #9c72f2]")
    else:
        preset_id = None

    while True:
        dv, dv_releases = _pick_data_vault(client)
        if dv is _BACK:
            return
        dvid = int(dv["dv_id"])

        console.rule("[bold #ae2695]Release A[/bold #ae2695]", style="#ae2695")
        dv_release_a, release_a = _pick_dv_and_bv_release(client, dvid, dv_releases, "A")
        if release_a is _BACK:
            continue

        console.rule("[bold #2ee0cb]Release B[/bold #2ee0cb]", style="#2ee0cb")
        dv_release_b, release_b = _pick_dv_and_bv_release(client, dvid, dv_releases, "B")
        if release_b is _BACK:
            continue

        chosen_preset_id = preset_id
        if chosen_preset_id is None:
            chosen = _choose("Select export preset:", presets, lambda x: x.get("name", str(x)))
            if chosen is _BACK:
                continue
            chosen_preset_id = int(chosen["preset_id"])
        break

    label_a = _release_label(dv_release_a, release_a)
    label_b = _release_label(dv_release_b, release_b)

    csv_a = _export(client, release_a, chosen_preset_id, label_a)
    csv_b = _export(client, release_b, chosen_preset_id, label_b)

    console.print("[#808090]Comparing exports…[/#808090]")
    result = compare(csv_a, csv_b, release_a_label=label_a, release_b_label=label_b)
    render_diff(result)
    _write_reports(result, "compare")


# ---------------------------------------------------------------------------
# Task 2: build a Snowflake inventory
# ---------------------------------------------------------------------------

def _inventory_args(dv: dict, dv_release: dict, bv_release: dict, database: str, lineage: bool) -> argparse.Namespace:
    """The attributes vaultspeed_inventory.collect()/transform() read, with IDs from the picker."""
    return argparse.Namespace(
        project=None, data_vault=None,
        dv_id=dv["dv_id"],
        dv_vs_id=dv_release["dv_vs_id"], release_name=None, release_number=None,
        bv_vs_id=bv_release["bv_vs_id"], bv_release_name=None,
        database=database or None, bdv_database=None, rdv_schema=None, bdv_schema=None,
        snapshot_dir=None, no_derived_objects=False, no_source_lineage=not lineage,
    )


def _build_inventory(client: VaultSpeedClient) -> None:
    while True:
        dv, dv_releases = _pick_data_vault(client)
        if dv is _BACK:
            return
        dv_release, bv_release = _pick_dv_and_bv_release(client, int(dv["dv_id"]), dv_releases, "")
        if dv_release is _BACK:
            continue
        break

    database = _ask(questionary.text(
        "Deployment database label (optional, only written to the output):", default=""
    )).strip()
    lineage = _ask(questionary.confirm(
        "Include source lineage? (several hundred extra API calls, a few minutes)", default=True
    ))
    release_name = dv_release.get("vs_dv_name") or f"dv_vs_id_{dv_release['dv_vs_id']}"
    default_out = EXPORTS_DIR / f"inventory_{_safe_name(dv.get('dv_name', 'dv'))}_{_safe_name(str(release_name))}_{_timestamp()}"
    out = Path(_ask(questionary.text("Output folder (must not exist yet):", default=str(default_out))).strip())

    args = _inventory_args(dv, dv_release, bv_release, database, lineage)
    fetched = 0

    with _spinner() as p:
        task = p.add_task("Collecting release metadata…")

        def on_fetch(path: str, count: int | None) -> None:
            nonlocal fetched
            fetched += 1
            p.update(task, description=f"Fetched {fetched} endpoints · {path}")

        def make_api(raw_dir: Path) -> inv.LiveAPI:
            return inv.LiveAPI(client.base_url.rstrip("/"), client.access_token, raw_dir, progress=on_fetch)

        try:
            summary = inv.run_inventory(args, out, make_api)
        except (inv.InventoryError, OSError, ValueError, KeyError, TypeError) as error:
            # Same redaction as the command line: never echo raw responses or headers.
            message = str(error) if isinstance(error, inv.InventoryError) else f"Invalid input or response ({type(error).__name__})."
            raise _Abort(f"Inventory failed: {message}") from None

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("Metric", style="bold #9c72f2")
    table.add_column("Value", justify="right")
    table.add_row("Expected objects", str(summary["expected_objects"]))
    for key, value in sorted(summary["counts"].items()):
        table.add_row(f"  {key}", str(value))
    if summary["source_lineage"]:
        table.add_row("Objects without sources", str(summary["source_lineage"]["objects_without_sources"]))
    table.add_row("Definitions to review", str(summary["review_objects"]))
    console.print(Panel(table, title=f"[bold #9c72f2]Inventory · {release_name}[/bold #9c72f2]", border_style="#6424eb"))
    console.print(f"[bold #00946a]✓[/bold #00946a] [#808090]Written to:[/#808090] [#9c72f2]{summary['output']}[/#9c72f2]")
    console.print("[#808090]Snowflake itself has not been checked; see docs/vaultspeed-inventory.md.[/#808090]")


# ---------------------------------------------------------------------------
# Task 3: compare two inventories (offline)
# ---------------------------------------------------------------------------

def _inventory_label(folder: Path) -> str:
    """'<DV release> / <BV release>' from the manifest, or the folder name."""
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        dv_name = manifest["release"].get("vs_dv_name") or manifest["release"].get("vs_dv_number")
        bv_name = manifest["bv_release"].get("bv_vs_name")
        if dv_name:
            return f"{dv_name} / {bv_name}" if bv_name else str(dv_name)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return folder.name


def _find_inventories() -> list[Path]:
    if not EXPORTS_DIR.is_dir():
        return []
    found = {p.parent for p in EXPORTS_DIR.glob("*/expected_objects.csv")}
    found |= {p.parent for p in EXPORTS_DIR.glob("*/*/expected_objects.csv")}
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _pick_inventory(prompt: str, exclude: Path | None) -> Any:
    options = [p for p in _find_inventories() if p != exclude]
    enter = object()
    choices = [{"name": f"{_inventory_label(p)}  [{p}]", "value": p} for p in options]
    choices.append({"name": "Enter a folder path…", "value": enter})
    choices.append({"name": "‹ Back", "value": _BACK})
    selected = _ask(questionary.select(prompt, choices=choices))
    if selected is enter:
        selected = Path(_ask(questionary.path("Inventory folder:", only_directories=True)).strip())
        if not (selected / "expected_objects.csv").is_file():
            console.print(f"[#fbab40]{selected} has no expected_objects.csv.[/#fbab40]")
            return _pick_inventory(prompt, exclude)
    return selected


def _compare_inventories() -> None:
    while True:
        console.rule("[bold #ae2695]Inventory A (baseline)[/bold #ae2695]", style="#ae2695")
        folder_a = _pick_inventory("Select inventory A:", None)
        if folder_a is _BACK:
            return
        console.rule("[bold #2ee0cb]Inventory B (compare to)[/bold #2ee0cb]", style="#2ee0cb")
        folder_b = _pick_inventory("Select inventory B:", folder_a)
        if folder_b is _BACK:
            continue
        break

    label_a, label_b = _inventory_label(folder_a), _inventory_label(folder_b)
    if label_a == label_b:
        label_a, label_b = f"{label_a} ({folder_a.name})", f"{label_b} ({folder_b.name})"
    csv_a = (folder_a / "expected_objects.csv").read_text(encoding="utf-8")
    csv_b = (folder_b / "expected_objects.csv").read_text(encoding="utf-8")
    result = compare_inventories(csv_a, csv_b, release_a_label=label_a, release_b_label=label_b)
    render_diff(result)
    _write_reports(result, "compare_inventory")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_TASKS = [
    ("Compare metadata exports of two releases", "exports"),
    ("Build Snowflake inventory for a release", "inventory"),
    ("Compare two Snowflake inventories (offline)", "compare_inventories"),
]


def main() -> None:
    _print_logo()
    client: VaultSpeedClient | None = None

    while True:
        choices = [{"name": name, "value": key} for name, key in _TASKS]
        choices.append({"name": "✕ Exit", "value": "exit"})
        task = _ask(questionary.select("What do you want to do?", choices=choices))
        if task == "exit":
            return
        try:
            if task == "compare_inventories":
                _compare_inventories()
                continue
            if client is None:
                client = _authenticate()
            if task == "exports":
                _compare_exports(client)
            else:
                _build_inventory(client)
        except _Abort as exc:
            console.print(f"[#fa584c]{exc}[/#fa584c]")
        console.print()


if __name__ == "__main__":
    main()
