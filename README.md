# VaultSpeed Metadata Export Compare

An interactive CLI tool that connects to your VaultSpeed subscription and can:

- **Compare metadata exports.** Generate the metadata exports of two releases and produce a colour-coded diff with a JSON and Markdown report.
- **Build a Snowflake inventory.** List every RDV/BDV table and view one release creates in Snowflake, with the source tables feeding each object (see [Snowflake inventory](#snowflake-inventory)).
- **Compare two inventories.** Show which Snowflake objects a release adds, removes or changes, offline from two saved inventories.

![VaultSpeed Metadata Export Compare](docs/screenshot.png)

---

## Requirements

- Python **3.11** or higher
- Access to a VaultSpeed environment: username + password, or a browser access token for environments that sign in through single sign-on (see [Using `.env` instead](#using-env-instead))
- An internet connection to reach your VaultSpeed API
- For comparing metadata exports: a **metadata export preset that includes all attributes** (see below)
- For the Snowflake inventory command line only: nothing beyond Python. `vaultspeed_inventory.py` needs no packages

### VaultSpeed metadata export preset

This tool compares exports column by column. For the comparison to be meaningful, the preset you select must include **all available attributes** — not a filtered or reduced set.

To verify or create such a preset in VaultSpeed:

1. Go to **Metadata Export** in your VaultSpeed environment.
2. Open or create a preset and make sure **all attribute columns are enabled**.
3. Use that preset when the tool prompts you to select one.

The tool uses the following columns as the **comparison key** (any of these that are present in the export will be used):

| Key column |
|---|
| `source_name` |
| `src_physical_schema` |
| `stg_physical_schema` |
| `dv_physical_schema` |
| `cdc_physical_schema` |
| `ini_physical_schema` |
| `src_table_name` |
| `stg_table_name` |
| `dv_table_name` |
| `dv_table_type` |
| `src_column_name` |
| `stg_column_name` |
| `dv_column_name` |
| `bv_physical_schema` |
| `bv_table_type` |
| `bv_table_name` |
| `bv_column_name` |

All remaining columns in the export are compared as **values** — any difference between the two releases will be flagged.

---

## Installation

### macOS

1. **Clone the repository**
   ```bash
   git clone https://github.com/vaultspeed-labs/metadata-export-compare.git
   cd metadata-export-compare
   ```

2. **Create and activate a virtual environment** *(recommended)*
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

---

### Windows

1. **Clone the repository**
   ```cmd
   git clone https://github.com/vaultspeed-labs/metadata-export-compare.git
   cd metadata-export-compare
   ```

2. **Create and activate a virtual environment** *(recommended)*
   ```cmd
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```cmd
   pip install -r requirements.txt
   ```

> **Windows Terminal tip:** For the best colour rendering, use [Windows Terminal](https://aka.ms/terminal) instead of the classic Command Prompt. PowerShell works well too.

---

## Running the tool

### macOS
```bash
python main.py
```

### Windows
```cmd
python main.py
```

---

## Interactive flow

Once started, the tool shows a menu:

```
? What do you want to do?
  Compare metadata exports of two releases
  Build Snowflake inventory for a release
  Compare two Snowflake inventories (offline)
  ✕ Exit
```

You are asked for credentials the first time you pick a task that calls the API; the login is reused for later tasks in the same session. Comparing two inventories reads local files only and needs no login. After each task the menu is shown again.

**Compare metadata exports** walks you through these steps:

```
1. Credentials      — select a saved profile, use .env, or enter new credentials
2. Project          — pick a VaultSpeed project (sorted alphabetically)
3. Data vault       — pick a data vault within that project
4. Release A        — select a DV release, then a BV release (baseline)
5. Release B        — select a DV release, then a BV release (compare to)
6. Export preset    — auto-selected if only one exists, otherwise prompted
```

**Build Snowflake inventory** asks for the project, data vault, DV release and BV release, then:

```
5. Database label   — optional; only written to the output columns
6. Source lineage   — on by default; adds GET requests for each source's definitions (seconds
                      for a small project, a few minutes for a large one)
7. Output folder    — defaults to exports/inventory_<data vault>_<release>_<timestamp>
```

A spinner shows the API calls as they run, and a summary panel shows the object counts and where the files were written. The result is the same as running `vaultspeed_inventory.py` from the command line (see [Snowflake inventory](#snowflake-inventory)).

**Compare two Snowflake inventories** lists the inventory folders found under `exports/` (newest first, labelled with their DV and BV release), or lets you enter a folder path. Build an inventory for each release first, then compare them to see which Snowflake objects the newer release adds, removes or changes.

At any prompt, select **‹ Back** to return to the previous step. Press **Ctrl+C** to exit at any time.

---

## Credential profiles

On first run you will be prompted for:

| Field | Description |
|---|---|
| **Base URL** | Your VaultSpeed API base URL, e.g. `https://app.vaultspeed.com/api` |
| **Username** | Your VaultSpeed login e-mail |
| **Password** | Your VaultSpeed password |

You can save these as a named profile for future runs. Profiles are stored **encrypted** on disk:

- **macOS / Linux:** `~/.vaultspeed-compare/credentials.enc`
- **Windows:** `C:\Users\<you>\.vaultspeed-compare\credentials.enc`

The encryption key is stored in the same folder, so the encryption keeps passwords out of plain sight but does not protect them from anyone who can read that folder.

### Using `.env` instead

If a `.env` file in the repository root (or environment variables) sets `VAULTSPEED_BASE_URL` plus either `VAULTSPEED_USERNAME`/`VAULTSPEED_PASSWORD` or an access token, the credentials menu offers **Use .env / environment variables**. This is the same configuration the inventory command line uses; copy `.env.example` to `.env` to start.

**Environments with single sign-on.** Some VaultSpeed environments sign users in through an identity provider. There, your password works in the browser but the API's `/login` returns `401 incorrect user name or password`. Use a browser access token instead:

1. Sign in to VaultSpeed in your browser, press **F12** and open **Network**.
2. Select any request to `/api/...` and copy its `Authorization` header value.
3. Save it in a file named `token` in the repository root (the `Bearer ` prefix is fine), or set `VAULTSPEED_ACCESS_TOKEN`.
4. Leave `VAULTSPEED_USERNAME` and `VAULTSPEED_PASSWORD` empty. The token is only used when both are empty.

Browser tokens expire after some hours; replace the file when you get `HTTP 401` errors. `.env` and `token` are gitignored.

---

## Output

After a comparison completes, two report files are written to your current working directory. `<A>` and `<B>` are the release labels, `<DV release> / <BV release>` (for example `seven / init`) with unsafe characters replaced by `_`. Inventory comparisons use the prefix `compare_inventory_`.

| File | Contents |
|---|---|
| `compare_<A>_vs_<B>_<timestamp>.json` | Structured diff — added, removed, and changed rows with field-level from/to values |
| `compare_<A>_vs_<B>_<timestamp>.md` | Human-readable Markdown report with summary table and per-section diff tables |

The terminal output shows:

- A **summary panel** with row counts (unchanged / added / removed / changed)
- **Added rows** in green
- **Removed rows** in red
- **Changed rows** in orange — with side-by-side old and new values per column

---

## Ignored columns

The following columns are excluded from the comparison because they contain release-specific identifiers that differ by definition and carry no meaningful diff signal:

`tab_col_dv_vs_id`, `datavault`, `source`, `src_name`, `dv_vs_id`, `tab_dv_vs_id`, `bv_vs_id`

To change this list, edit the `IGNORED_COLUMNS` set at the top of `compare.py`.

When comparing inventories, objects are matched on `schema_name` + `object_name`. The release and object IDs (`dv_id`, `dv_vs_id`, `bv_vs_id`, `tab_dv_vs_id`, `tab_dv_business_view_id`) and the database label columns (`database_name`, `underlying_database`) are ignored; see `INVENTORY_IGNORED_COLUMNS` in `compare.py`.

---

## Snowflake inventory

`vaultspeed_inventory.py` exports the objects a release is expected to create in Snowflake (RDV tables, BV cross-source tables, generated business views, LAS temp tables and FMC tables) to CSV files, together with the source tables feeding each object. It reads the VaultSpeed API with GET requests only and does not connect to Snowflake.

Run it from the menu, or from the command line for automation:

```bash
python vaultspeed_inventory.py --project "MY_PROJECT" --data-vault "MY_DATAVAULT" --release-number 42 --out exports/inventory-42
```

The command line reads credentials from `.env`, environment variables or the `token` file, the same way as the menu (see [Using `.env` instead](#using-env-instead) and `.env.example`). Run `python vaultspeed_inventory.py --help` for all options, including offline replay of a saved API snapshot with `--snapshot-dir`.

Each run writes a new folder (an existing folder is never overwritten), and only once every API call and check has succeeded:

| File | Contents |
|---|---|
| `expected_objects.csv` | **Main output.** One row per expected table or view: schema, name, type, layer, why it is expected, and the source tables feeding it |
| `object_sources.csv` | One row per object and source table, with the rule that matched them |
| `hub_group_members.csv` | Original source hubs mapped to the grouped hubs that replace them |
| `object_relationships.csv` | Business view → underlying table, and link → hub dependencies |
| `review_objects.csv` | Definitions found in VaultSpeed that are not expected objects, for investigation |
| `source_overview_names.csv` | Source-oriented overview names that differ from generated names (information only) |
| `manifest.json`, `raw/` | Release context, limits, and the API snapshot for reproducing the run offline |

The module uses the Python standard library only, so it can run without `pip install` and can be uploaded on its own as a Snowflake stored procedure import. Keep it that way: it must not import anything from the rest of this repository.

- [docs/vaultspeed-inventory.md](docs/vaultspeed-inventory.md): output files, columns, how each object and source is established, and limits.
- [docs/vaultspeed-inventory-snowflake-guide.md](docs/vaultspeed-inventory-snowflake-guide.md): running the inventory as a Snowflake stored procedure.

Inventory folders and their `raw/` API snapshots contain your project's metadata. They are written under `exports/`, which is gitignored; review them before sharing.

---

## Project structure

```
metadata-export-compare/
├── main.py                  # Interactive menu and flows
├── vaultspeed_client.py     # VaultSpeed API client (metadata export endpoints)
├── vaultspeed_inventory.py  # Snowflake inventory (standard library only; also a CLI)
├── compare.py               # CSV diff logic for exports and inventories
├── display.py               # Rich terminal visualisation
├── auth.py                  # Encrypted credential profile management
├── .env.example             # Template for .env credentials
├── docs/                    # Inventory guide and Snowflake stored procedure guide
├── tests/                   # Unit tests
└── requirements.txt         # Python dependencies (for main.py)
```

Run the tests from the repository root:

```bash
python -m unittest discover -s tests -q
```

The inventory tests need no packages; the comparison tests are skipped unless `requirements.txt` is installed.

---

## Troubleshooting

**`ModuleNotFoundError`**
Run `pip install -r requirements.txt` inside your activated virtual environment.

**`Login failed`**
Double-check your base URL, username, and password. The base URL should end with `/api`, e.g. `https://app.vaultspeed.com/api`. If the password works in the browser but the API still returns `401`, the environment probably uses single sign-on; use a browser token as described in [Using `.env` instead](#using-env-instead).

**Colours not rendering on Windows**
Use Windows Terminal or PowerShell. If colours still look wrong, run:
```cmd
pip install colorama
```

**Export seems stuck on "Waiting for export"**
The tool waits for the server-side export with no time limit. Large releases can take several minutes. Press **Ctrl+C** to stop and re-run later, or contact your VaultSpeed administrator if it never completes.

**Inventory fails with an `ERROR` or `Inventory failed` message**
The inventory stops rather than write partial results; nothing is written to the output folder. The message names the API call or check that failed. See [docs/vaultspeed-inventory.md](docs/vaultspeed-inventory.md) for details.
