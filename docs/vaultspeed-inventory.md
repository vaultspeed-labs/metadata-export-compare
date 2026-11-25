# Expected Snowflake objects from VaultSpeed

[`vaultspeed_inventory.py`](../vaultspeed_inventory.py) exports the RDV and BDV objects a selected release is expected to create once its DDL/ETL has run, as CSV files. It uses the VaultSpeed API only; it does not connect to Snowflake, and Excel is not required. Python 3.11+ is required, with no additional packages.

As an alternative to the command line, the interactive menu (`python main.py`) has a **Build Snowflake inventory** option that runs the same inventory.

The intended workflow is to compare modeled objects with Snowflake, identify environment mismatches and unexplained objects, and reduce manual investigation before cleanup. This script supplies only the VaultSpeed side: the modeled list. Comparing it with deployed objects is a separate step, using your own Snowflake listing.

## Export the modeled inventory

Configure `VAULTSPEED_BASE_URL`, `VAULTSPEED_USERNAME`, and `VAULTSPEED_PASSWORD` in `.env` or the environment. Use an API user provisioned for the tenant, or your own credentials if API login is supported. The script exchanges credentials through `POST /login`, keeps the returned access token in memory, and reads metadata using GET requests. Login credentials and responses are not saved to the snapshot.

For tenants where username/password API login does not work, clear both username/password settings and use a bearer token as a backup. Sign in through the browser, open **F12 → Network**, refresh or trigger an API request, and copy its request `Authorization` header token. Save the raw token (or `Bearer ...`) in `token` in the repository root, beside `.env`, or set `VAULTSPEED_ACCESS_TOKEN`. Browser tokens expire and require manual replacement, making this a workaround rather than an ideal automation method.

Environment variables override matching `.env` values. Username/password take priority; supplying only one is an error, and failed login does not silently switch to a token. With both blank, `VAULTSPEED_ACCESS_TOKEN` takes priority over the token file. Offline snapshot replay needs no authentication.

From the repository root:

```bash
python vaultspeed_inventory.py \
  --project "MY_PROJECT" \
  --data-vault "MY_DATAVAULT" \
  --release-number 42 \
  --out exports/inventory-42
```

`--database` is optional and is not used for any API request. It only adds a
caller-supplied deployment database label to output names. With this release's
schema-only parameter settings, the database prefix is determined when DDL/ETL
runs during deployment; the VaultSpeed model does not establish it.

Without `--database`, exported database fields are blank. To label the export for
a specific deployment, add `--database MY_DATAVAULT_DB`; preserve identifier
case by passing its exact spelling without SQL quotes. `--bdv-database` optionally
labels a separate BV database and otherwise defaults to `--database`.
The API cannot verify where a release was deployed. This script does not infer a
database from schema parameter strings.

Supply the project and Data Vault names visible in VaultSpeed. Names match exactly,
including case; the vault is resolved only within the selected project. Choose
`--release-number 42` or `--release-name "Release 42"`. No backend IDs are needed.
If a name is missing or ambiguous, the script stops and reports available values.

The script selects the Business Vault release associated with the selected DV
release automatically when there is exactly one. If multiple exist, it lists their
names; supply `--bv-release-name "init"` to select explicitly. It never silently
chooses the newest release. Advanced ID alternatives remain available through
`--dv-id`, `--dv-vs-id`, and `--bv-vs-id` for older snapshots and troubleshooting.
Do not combine `--dv-id` with project/vault names.

Live snapshots include the project/vault lookup responses, so the same name-based
command works for offline replay. Older snapshots without these responses require
`--dv-id` (the recorded ID is in `manifest.json` or `expected_objects.csv`).
The manifest records the selected project, vault, DV release, and BV release.

The default schema names come from the release parameters `SCHEMA_FL` and `SCHEMA_BV`. Override them using `--rdv-schema` / `--bdv-schema` when the target environment differs. Use `--bdv-database` if RDV and BDV reside in different databases. Schema and table names are used as returned by the API. Business-view names are uppercased, because the generated ETL creates them as quoted uppercase identifiers (for example business name `view_LAS_EVENT_CUSTOMER` becomes `"BDV"."VIEW_LAS_EVENT_CUSTOMER"`).

An output directory must be new. Failed API calls, ignored/repeating pagination, inconsistent identities, unresolved `E` grouping members, ambiguous releases, and conflicting physical names stop the run before any inventory is published. Each live run collects a fresh snapshot rather than reusing previous requests.

## Output files

| File | Contents |
| --- | --- |
| `expected_objects.csv` | One row per schema/object (with optional database label), with expected Snowflake type, VaultSpeed type, layer, release/object IDs, underlying object for aliases, the evidence basis, and the source objects feeding it (see [Source lineage](#source-lineage)). This is the main output: the expected RDV/BDV object list. |
| `object_sources.csv` | One row per expected object and contributing source object: source name and release, source-level definition, modeled (split) object, physical source table, incremental/init/staging qualified names, and the matching rule. |
| `hub_group_members.csv` | Original source hub → effective grouped hub mapping, source release and member IDs, status, and resolution. Includes `D` records without promoting them to physical expectations. |
| `object_relationships.csv` | Qualified base-object → business-view relationships, and hub → link dependencies obtained from API adjacency IDs. Not complete satellite or column lineage. |
| `review_objects.csv` | Catalog-only/status-sensitive definitions, disabled business views, and `unverified_configuration` settings, kept separate from expected objects. |
| `source_overview_names.csv` | Source-oriented overview names absent from the physical object catalog. Informational records; these names are not added as physical expectations. |
| `manifest.json` | Release identity, schemas, counts, scope limitations, API evidence, and export checksums. |
| `raw/` | Checksummed API responses and `snapshot.json` for live runs. Allows offline reproduction. |

The inventory identifies each object by **`schema_name`, `object_name`**, with an optional caller-supplied `database_name`. `object_type` distinguishes expected tables and views. IDs remain in the export for traceability and are not interchangeable: `tab_dv_id`, `tab_dv_vs_id`, and `tab_src_dv_id` belong to different domains.

## What is expected to exist

The inventory covers standard SQL Snowflake core objects:

1. **RDV base tables:** current definitions corroborated by an exact name in the DV overview or a current BV mapping to the catalog's physical object name. This retains base tables even if their BV alias has `generate = false`, and retains current tables that have no BV alias.
2. **BDV cross-source base tables:** the catalog's `LNA` / `LAS` definitions, classified using `/metadata/object-types`, with a current BV mapping. These belong in the BV schema. Their names must not be added to the RDV schema merely because they appear in the object-version catalog.
3. **BDV views:** business view definitions with `generate = true`, using `business_name` for the physical view name and the catalog target for the underlying object.

These three groups have `basis_type = api_metadata`. Two further groups are generated by VaultSpeed but not listed by any object endpoint; the script derives them from project and system parameters (`basis_type = parameter_rule`):

4. **BDV temp tables for links-across-sources satellites:** `TEMP_TABLE_PREFIX` + `_` + each `LAS` satellite's name, in `SCHEMA_BV` (object type `LAS_TEMP`).
5. **FMC load-control tables** (when `USE_FMC = Y`): in `SCHEMA_FMC`, named by `LOAD_CYCLE_INFO_TABLE_NAME`, `DV_` + that name, `FMC_BV_LOADING_WINDOW_TABLE_NAME`, `FMC_LOADING_HIST_TABLE_NAME`, and `FMC_OBJECT_LOADING_HIST_TABLE_NAME` (object types `BVLCI`, `DVLCI`, `BVLWT`, `FMCH`, `FMCOH`).

The rules for groups 4 and 5 come from the generated DDL, not from an API contract. Caveats:

- They were checked against the generated code of a real release (VaultSpeed 8.0.3.2; see the check below) with `USE_FMC = Y`, `FMC_TYPE = Airflow`, and `STORE_TEMP_IN_STAGING = Y`. With other values the script still runs but lists the setting in `review_objects.csv` (`unverified_configuration`). For example, with `STORE_TEMP_IN_STAGING = N` VaultSpeed puts the other temp tables in the vault schemas, and those are not listed.
- `DV_LOAD_CYCLE_INFO` has no naming parameter of its own; its name is `DV_` + `LOAD_CYCLE_INFO_TABLE_NAME`, as observed in the DDL.
- `FMC_LOADING_WINDOW_TABLE_NAME` (`FMC_LW_TBL`) is set as a parameter, but VaultSpeed did not create that table, so it is not listed.
- The `TMP_SLAS_` tables are in the BV schema even though `STORE_TEMP_IN_STAGING = Y` moves all other temp tables to staging.
- A new VaultSpeed version can change these rules; re-check against its DDL.

Use `--no-derived-objects` to list only `api_metadata` rows, or to replay a snapshot taken before these rows were added.

**Check against generated code.** For a real release, the DDL creates the tables in the `RDV`, `BDV`, and `FMC` schemas, and the ETL creates the business views in `BDV`. Both were generated with VaultSpeed 8.0.3.2 and downloaded with `GET /generations/business-vault/release/{bv_vs_id}/download/name/{file_name}`. The export matched them exactly, with no differences in schema, name, or type. The review definitions appear in neither package, and neither contains PITs or bridges. Outside these schemas, the ETL also creates load procedures in a procedure schema and delta-firewall views in the source `*_DFV` schemas; both are out of scope.

Grouping-member records include `E` members resolved by ID and `D` records retained as context. The remaining object-version catalog definitions are review records. Their presence in an API response is insufficient to classify them as current physical objects.

Some source-oriented overview names differ from the generated names. For example, the overview contains `SH_CLIENT_CUSTOMER_CUSTOMER_SOURCE_A`, while the catalog and BV mapping identify `SH_CUSTOMER_SOURCE_A`. The script uses explicit BV target mappings to establish the latter's physical name; the expected inventory never infers modeled object names from prefixes or short names. (The parameter-derived temp tables apply `TEMP_TABLE_PREFIX` to names already established from the catalog.) (Source lineage uses constrained name rules only to attach sources to objects already established here; see below.)

For example, for a grouped hub:

```text
Original source hub: SOURCE_A / H_CLIENT_CUSTOMER
    membership → grouped hub H_CUSTOMER
Expected table: MY_DATAVAULT_DB.RDV.H_CUSTOMER
Expected view:  MY_DATAVAULT_DB.BDV.H_CLIENT_CUSTOMER
```

The logical source hub is not an additional expected RDV table. An observed older `RDV.H_CLIENT_CUSTOMER` requires migration/dependency review.

## Source lineage

An object can be fed by several sources and source objects: a grouped hub by every
member hub's source object, a grouped link by every member link's. The source
columns therefore hold semicolon-separated lists:

| Column | Contents |
| --- | --- |
| `source_count` | Distinct (source, modeled source object) pairs feeding the object. |
| `incr_source_objects` | `SCHEMA_CDC.<physical source table>`: what incremental (INCR) code reads. |
| `init_source_objects` | `SCHEMA_INI.<physical source table>`: what initial-load (INIT) code reads. |
| `stg_objects` | `SCHEMA_STG.<modeled source object>`: the VaultSpeed staging table. |
| `source_resolution` | Rule(s) that matched the source definitions to this object, joined with `+`. |

Schemas come from each source's parameters (`/parameters/{dv_vs_id}/source`). INIT
and INCR schemas are sometimes the same value, and may include a database prefix.
Split objects stage under their modeled name but read the original physical table,
so two splits can share one `incr_source_objects` entry while listing two `stg_objects`.
For example, a grouped hub `RDV.H_CUSTOMER` can list several source objects across
SOURCE_A, SOURCE_B, and SOURCE_C; the view `BDV.H_CLIENT_CUSTOMER`
shows the same list.

The API has no single DV-object-to-source endpoint, so the script reads each source
release's definitions (`/source/release/{src_vs_id}/hubs/{tab_type_id}` for hubs,
links, satellites, and references, each naming its source object) and matches them
to release objects. Each rule is ID-based or requires exactly one candidate:

| Rule | Meaning |
| --- | --- |
| `overview_id` | The DV overview row carries the definition's ID and the generated name. |
| `hub_group_member` | Explicit hub-group membership (`E` status), resolved by ID. |
| `overview_id_name_tokens` | Overview ID whose source-oriented name reorders the generated name's tokens; unique within object type and source release. |
| `link_member_hub_set` | A grouped link member: same link type and set of DV hubs as a resolved link in its source release (`_cross_source` when the group spans releases). Hubs come from `/source/sdvv/relationships/{id}`. |
| `satellite_parent` | A satellite on a grouped parent: the only unassigned DV satellite of that type and source release named after the DV parent. |
| `source_name` | Source-level name equals the release object's name and type. |
| `inferred_cross_source_*` | Links across sources have no source-level definition in the API. Their sources are inferred from the adjacent hubs' sources within the link's own source release; their satellites take the parent link's sources. |

Unmatched definitions are listed in `review_objects.csv` (`unresolved_source_definition`)
rather than guessed; base objects without sources are listed as `no_source_lineage`.

For a real release, every source-level definition resolved and every expected
object had sources. Compared with a metadata export of the same release, all
three columns matched exactly for every RDV table. For the `SLAS_` satellites
the INCR/INIT sources matched, but `stg_objects` listed every `EVENT` split where
the export showed one each. The `LAS_` links had no source rows in the export
to compare against. Treat `inferred_cross_source_*` rows accordingly.

## Using the export

Use `expected_objects.csv` as the modeled inventory for your own Snowflake
analysis. The script exports VaultSpeed metadata only; it does not generate SQL,
connect to Snowflake, or compare warehouse objects. Deployment and actual object
existence must be established separately.

## Scope and interpretation

This implementation covers core RDV/BDV objects, the BV temp tables for links-across-sources satellites, and FMC load-control tables. STG/EXT/INI/MTD objects (including temp tables placed in staging schemas), other runtime helpers, PITs, bridges, Studio/custom outputs, procedures, sequences, and tasks are outside its inventory. Objects from those families may exist legitimately in the queried schemas. Custom DDL that changes a core object's physical type is also outside the model assumptions. The manifest records these limitations explicitly.

`/data-vaults/release/{id}/objects` returns business-vault definitions as well as RDV objects, so the script classifies each entry using the API's layer metadata rather than treating every entry as RDV.

The script does not assert that the selected release was deployed, parse migration/DDL/ETL artifacts (the DDL comparison above was a one-off check), prove data movement, or establish safe object deletion. Choose the release appropriate to the environment being inspected and review that deployment context before interpreting differences.

Grouping membership and business-view mappings are explicit API relationships. `object_relationships.csv` additionally supplies verified hub/link adjacency, including links across sources. Source lineage is object-level; full column transformations remain a separate extension.

## Reproduce and test

Offline replay requires no credentials or network access. Snapshots are verified by checksum and are never silently mixed with live data. Keep the snapshot directory when sharing a reproducible result; it contains VaultSpeed project metadata but does not contain the bearer token.

From the repository root:

```bash
python -m unittest discover -s tests -q
```

Tests cover grouping IDs/statuses, layer classification, source/generated naming differences, disabled views, tables without aliases, historical definitions, collisions, optional database labels, pagination, snapshot integrity, failure before publishing partial results, parameter-derived temp and FMC tables (including disabled FMC and unverified settings), and each source-lineage rule (including ambiguous candidates left unresolved).
