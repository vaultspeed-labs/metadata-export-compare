# Run the VaultSpeed inventory in Snowflake

This guide shows how to run the supplied `vaultspeed_inventory.py` inside Snowflake without installing Python on a workstation. An administrator configures network access and credentials once. Users then run a SQL `CALL` from Snowsight.

The procedure imports the supplied Python file and reuses its API collection and transformation functions. A small inline handler replaces the command-line entry point. Results are saved in a Snowflake table, and the raw API snapshot is saved as a zip file in an internal stage, instead of a local CSV directory.

**Deployment status:** this is a deployment example, not an installed procedure. The handler's Python logic has been run outside Snowflake against a VaultSpeed tenant with stand-ins for the Snowflake session and Secret API. The SQL, the Snowflake runtime behaviour (external access, Secret retrieval, stage upload, thread use), and the procedure as a whole have not been executed in a Snowflake account. Validate it in a development environment before operational use.

## 1. Confirm prerequisites

You will need:

- The supplied `vaultspeed_inventory.py` file.
- A Snowflake warehouse and access to Snowsight.
- An administrator able to create a network rule, Secret, and external access integration, and grant access to them.
- A deployment role able to create tables, stages, and Python procedures in a dedicated schema.
- The VaultSpeed HTTPS API base URL and API-enabled username/password.
- The exact project name, Data Vault name, and DV release number. If multiple BV releases belong to the DV release, also obtain the BV release name.

First confirm that Snowflake can reach the VaultSpeed API hostname. A workstation VPN connection does not give Snowflake that same connectivity. A private endpoint requires an appropriate private connectivity design before using this example.

The supplied script uses Python's standard library. The procedure below uses Python 3.12 and adds Snowpark for writing the results to Snowflake.

## 2. Create the deployment objects

The examples use `TOOLS_DB.VAULTSPEED_QA`. Replace this with your approved database and schema **throughout the SQL and inline Python**, and select your deployment role and warehouse before running the statements. The database is assumed to exist.

```sql
CREATE SCHEMA IF NOT EXISTS TOOLS_DB.VAULTSPEED_QA;

-- Holds vaultspeed_inventory.py.
CREATE STAGE IF NOT EXISTS TOOLS_DB.VAULTSPEED_QA.INVENTORY_CODE
    DIRECTORY = (ENABLE = TRUE);

-- Holds one raw API snapshot zip per run (VaultSpeed project metadata).
CREATE STAGE IF NOT EXISTS TOOLS_DB.VAULTSPEED_QA.INVENTORY_SNAPSHOTS
    DIRECTORY = (ENABLE = TRUE);

CREATE TABLE IF NOT EXISTS TOOLS_DB.VAULTSPEED_QA.INVENTORY_RUNS (
    RUN_ID VARCHAR,
    COLLECTED_AT TIMESTAMP_TZ,
    DOCUMENT VARIANT
);
```

Each successful call inserts one row. Its document holds the selected release, the expected objects, review records, relationships, hub-group membership, source lineage (when requested), and API request evidence. Earlier runs remain available.

## 3. Configure VaultSpeed access

Have the administrator run the following using your actual credentials. The example uses the VaultSpeed API endpoint `https://app.vaultspeed.com/api`; replace it if your tenant uses another host. The egress rule allows Snowflake to connect to that API host on port 443. The network rule takes a hostname and port, without `https://` or a URL path.

```sql
CREATE NETWORK RULE TOOLS_DB.VAULTSPEED_QA.VAULTSPEED_API_RULE
    MODE = EGRESS
    TYPE = HOST_PORT
    VALUE_LIST = ('app.vaultspeed.com:443');

CREATE SECRET TOOLS_DB.VAULTSPEED_QA.VAULTSPEED_CREDENTIALS
    TYPE = PASSWORD
    USERNAME = '<VaultSpeed API username>'
    PASSWORD = '<VaultSpeed API password>';

CREATE EXTERNAL ACCESS INTEGRATION VAULTSPEED_API_ACCESS
    ALLOWED_NETWORK_RULES = (
        TOOLS_DB.VAULTSPEED_QA.VAULTSPEED_API_RULE
    )
    ALLOWED_AUTHENTICATION_SECRETS = (
        TOOLS_DB.VAULTSPEED_QA.VAULTSPEED_CREDENTIALS
    )
    ENABLED = TRUE;
```

Create the Secret through your organization's approved credential-management process. Do not put actual credentials in shared deployment files. The handler retrieves the Secret at runtime and keeps the VaultSpeed access token in memory; neither is written to the table or the snapshot.

Grant the procedure deployment role access, replacing `VAULTSPEED_DEPLOYER` with the actual role:

```sql
GRANT USAGE ON DATABASE TOOLS_DB TO ROLE VAULTSPEED_DEPLOYER;
GRANT USAGE ON SCHEMA TOOLS_DB.VAULTSPEED_QA
    TO ROLE VAULTSPEED_DEPLOYER;
GRANT READ ON SECRET TOOLS_DB.VAULTSPEED_QA.VAULTSPEED_CREDENTIALS
    TO ROLE VAULTSPEED_DEPLOYER;
GRANT USAGE ON INTEGRATION VAULTSPEED_API_ACCESS
    TO ROLE VAULTSPEED_DEPLOYER;
```

That role also needs the schema creation privileges and object access needed for steps 2 and 5. If another role created the stages or table, grant the deployment role `READ` on `INVENTORY_CODE`, `READ`/`WRITE` on `INVENTORY_SNAPSHOTS`, and `INSERT` on the table. Account-specific role provisioning remains with the administrator.

Snowflake documents the network, Secret, and integration setup in [Creating and using an external access integration](https://docs.snowflake.com/en/developer-guide/external-network-access/creating-using-external-network-access).

## 4. Upload the supplied Python file

Use Snowsight's **Load files into a Stage** flow to upload `vaultspeed_inventory.py` (from the repository root) to `TOOLS_DB.VAULTSPEED_QA.INVENTORY_CODE`, at the root of the stage. Uploading a file through the browser does not require Python on the workstation.

Verify the upload:

```sql
LIST @TOOLS_DB.VAULTSPEED_QA.INVENTORY_CODE;
```

The procedure's `IMPORTS` path must match the uploaded filename and any folder path. See [Staging files using Snowsight](https://docs.snowflake.com/en/user-guide/data-load-local-file-system-stage-ui).

When you receive a new version of the script, upload it over the old file. The procedure picks it up on the next call; re-create the procedure only if this guide's handler changes.

## 5. Create the Python procedure

The handler below uses the configured VaultSpeed API base URL, including its API path prefix if present. Its hostname must match the egress rule in step 3. Do not append `/login`; the supplied script does that itself.

The handler imports the supplied module without calling `main()`. It passes parameters directly to `collect()`, `transform()`, and optionally `source_lineage()`, avoiding command-line parsing, `.env` files, token files, and process-wide environment changes.

```sql
CREATE OR REPLACE PROCEDURE TOOLS_DB.VAULTSPEED_QA.VAULTSPEED_INVENTORY(
    PROJECT_NAME VARCHAR,
    DATA_VAULT_NAME VARCHAR,
    RELEASE_NUMBER VARCHAR,
    DEPLOYMENT_DATABASE VARCHAR,
    BV_RELEASE_NAME VARCHAR DEFAULT NULL,
    INCLUDE_SOURCE_LINEAGE BOOLEAN DEFAULT FALSE
)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.12'
PACKAGES = ('snowflake-snowpark-python')
IMPORTS = ('@TOOLS_DB.VAULTSPEED_QA.INVENTORY_CODE/vaultspeed_inventory.py')
HANDLER = 'run'
EXTERNAL_ACCESS_INTEGRATIONS = (VAULTSPEED_API_ACCESS)
SECRETS = (
    'vaultspeed_credentials' =
        TOOLS_DB.VAULTSPEED_QA.VAULTSPEED_CREDENTIALS
)
EXECUTE AS OWNER
AS
$$
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import uuid

import _snowflake
import vaultspeed_inventory as inventory

BASE_URL = 'https://app.vaultspeed.com/api'
RUNS_TABLE = 'TOOLS_DB.VAULTSPEED_QA.INVENTORY_RUNS'
SNAPSHOT_STAGE = '@TOOLS_DB.VAULTSPEED_QA.INVENTORY_SNAPSHOTS'

def run(session, project_name, data_vault_name, release_number,
        deployment_database, bv_release_name, include_source_lineage):
    for label, value in (
        ('Project name', project_name),
        ('Data Vault name', data_vault_name),
        ('Release number', release_number),
    ):
        inventory.required_text(value, label)

    # NULL leaves the optional deployment database label blank.
    args = SimpleNamespace(
        project=project_name, data_vault=data_vault_name, dv_id=None,
        release_number=release_number, release_name=None, dv_vs_id=None,
        bv_release_name=bv_release_name, bv_vs_id=None,
        database=deployment_database, bdv_database=None,
        rdv_schema=None, bdv_schema=None,
        no_derived_objects=False,
    )

    run_id = str(uuid.uuid4())
    started_at = inventory.utc_now()
    credential = _snowflake.get_username_password('vaultspeed_credentials')
    token = inventory.login(BASE_URL, credential.username, credential.password)

    with tempfile.TemporaryDirectory(dir='/tmp') as work_dir:
        raw_dir = Path(work_dir) / 'raw'
        api = inventory.LiveAPI(BASE_URL, token, raw_dir, page_size=500, workers=4)
        data = inventory.collect(api, args)
        result = inventory.transform(api, data, args)
        lineage = inventory.source_lineage(api, data, result) if include_source_lineage else None

        document = {
            'run_id': run_id,
            'started_at': started_at,
            'finished_at': inventory.utc_now(),
            'api_base_url': BASE_URL,
            'mode': 'live_get_only',
            'project': data['project'],
            'data_vault': data['data_vault'],
            'release': data['release'],
            'bv_release': data['bv_release'],
            'database': deployment_database,
            'bdv_database': deployment_database,
            'deployment_verified': False,
            'complete_for_all_snowflake_objects': False,
            'derived_objects_verified_config': inventory.VERIFIED_CONFIG,
            'result': result,
            'object_sources': lineage['rows'] if lineage else None,
            'source_lineage': lineage['counts'] if lineage else None,
            # Request evidence without response bodies; the bodies are in the snapshot zip.
            'endpoint_evidence': {
                path: {k: v for k, v in record.items() if k != 'data'}
                for path, record in api.records.items()
            },
            'snapshot_location': f'{SNAPSHOT_STAGE}/{run_id}/raw.zip',
        }
        payload = json.dumps(document)

        # Zip the checksummed responses; unzipped, the raw/ folder replays offline
        # with: python vaultspeed_inventory.py ... --snapshot-dir raw
        archive = shutil.make_archive(str(Path(work_dir) / 'raw'), 'zip', work_dir, 'raw')
        session.file.put(archive, f'{SNAPSHOT_STAGE}/{run_id}',
                         auto_compress=False, overwrite=False)

    # Insert only after every API call, mapping check, and snapshot upload succeeds.
    session.sql(
        f'''INSERT INTO {RUNS_TABLE} (RUN_ID, COLLECTED_AT, DOCUMENT)
            SELECT ?, CURRENT_TIMESTAMP(), PARSE_JSON(?)''',
        params=[run_id, payload],
    ).collect()

    return {
        'run_id': run_id,
        'expected_objects': len(result['objects']),
        'review_objects': len(result['review']),
        'counts': result['counts'],
        'source_lineage': lineage['counts'] if lineage else None,
        'snapshot_location': document['snapshot_location'],
        'deployment_verified': False,
    }
$$;
```

The handler writes to fixed destinations using the procedure owner's privileges. Callers do not choose a target table or stage, or supply credentials. Grant execution only to the intended operators.

Notes on the handler:

- **Derived objects** (the BV temp tables for links-across-sources satellites and the FMC tables) are always included, as in the command-line default. They need two extra GETs (`/parameters/{release}/project` and `/system`). See "What is expected to exist" in [vaultspeed-inventory.md](vaultspeed-inventory.md#what-is-expected-to-exist) for their caveats.
- **Source lineage** is off by default. With `INCLUDE_SOURCE_LINEAGE = TRUE`, the run adds roughly 1,000 GET requests, four at a time; a release with ~1,700 objects took a few minutes from a workstation. Without it, a run makes about 100 requests.
- **Size:** for a release with ~1,700 objects and lineage, the run document was about 6 MB of JSON and the snapshot zip under 1 MB. The document must stay within Snowflake's maximum `VARIANT` size for your account.
- **Uploading a snapshot:** if `session.file.put` is not permitted in your environment, remove that call and the `snapshot_location` entries; the table row still holds the results and request checksums, but not the response bodies.

This example uses the release's `SCHEMA_FL` and `SCHEMA_BV` values and one optional deployment database label for both layers. To expose the script's schema overrides, separate BDV database, release-name selection, or `--no-derived-objects`, extend the procedure parameters and handler deliberately.

The table stores structured results; the stage stores the raw snapshot. The procedure does **not** create the command-line export's CSV files or `manifest.json`. Its run document carries release identity and key scope flags, but is a separate output contract.

See Snowflake's [CREATE PROCEDURE reference](https://docs.snowflake.com/en/sql-reference/sql/create-procedure) for runtime, import, and handler configuration, and the [Secret API reference](https://docs.snowflake.com/en/developer-guide/external-network-access/secret-api-reference) for credential retrieval.

## 6. Grant user access

Replace `VAULTSPEED_OPERATOR` and `QA_WH` with your operator role and warehouse. Granting `SELECT` on the run table also permits reading the VaultSpeed metadata retained in each document; access to the snapshot stage is not needed to run the procedure and can be granted separately.

```sql
GRANT USAGE ON DATABASE TOOLS_DB TO ROLE VAULTSPEED_OPERATOR;
GRANT USAGE ON SCHEMA TOOLS_DB.VAULTSPEED_QA
    TO ROLE VAULTSPEED_OPERATOR;
GRANT USAGE ON WAREHOUSE QA_WH TO ROLE VAULTSPEED_OPERATOR;
GRANT USAGE ON PROCEDURE
    TOOLS_DB.VAULTSPEED_QA.VAULTSPEED_INVENTORY(
        VARCHAR, VARCHAR, VARCHAR, VARCHAR, VARCHAR, BOOLEAN
    ) TO ROLE VAULTSPEED_OPERATOR;
GRANT SELECT ON TABLE TOOLS_DB.VAULTSPEED_QA.INVENTORY_RUNS
    TO ROLE VAULTSPEED_OPERATOR;
```

Operators do not need direct access to the Secret to invoke this owner-rights procedure.

## 7. Run the inventory

From a Snowsight SQL worksheet, select the operator role and warehouse, then run:

```sql
CALL TOOLS_DB.VAULTSPEED_QA.VAULTSPEED_INVENTORY(
    'MY_PROJECT',
    'MY_DATAVAULT',
    '42',
    'MY_DATAVAULT_DB'
);
```

Replace these illustrative values with the selected VaultSpeed project, Data Vault, release number, and deployment database. Names match exactly, including case. The database argument is a caller-supplied output label; it does not select a VaultSpeed release or verify deployment. Pass SQL `NULL` to leave that label blank.

If the script reports multiple associated BV releases, pass the exact BV release name as the fifth argument. To include source lineage, pass `TRUE` as the sixth:

```sql
CALL TOOLS_DB.VAULTSPEED_QA.VAULTSPEED_INVENTORY(
    'MY_PROJECT', 'MY_DATAVAULT', '42', 'MY_DATAVAULT_DB', NULL, TRUE
);
```

A successful call returns the run ID, counts, and snapshot location. A failed collection or transformation raises an error and inserts no run row. Each call fetches a fresh snapshot.

## 8. Query the results

List successful runs:

```sql
SELECT RUN_ID, COLLECTED_AT,
       DOCUMENT:release:vs_dv_number::VARCHAR AS DV_RELEASE,
       DOCUMENT:bv_release:bv_vs_name::VARCHAR AS BV_RELEASE,
       DOCUMENT:result:counts AS COUNTS,
       DOCUMENT:source_lineage AS SOURCE_LINEAGE
FROM TOOLS_DB.VAULTSPEED_QA.INVENTORY_RUNS
ORDER BY COLLECTED_AT DESC;
```

Use the run ID returned by the call to list the expected objects. These columns match `expected_objects.csv` from the command-line export:

```sql
SELECT
    o.value:database_name::VARCHAR AS DATABASE_NAME,
    o.value:schema_name::VARCHAR AS SCHEMA_NAME,
    o.value:object_name::VARCHAR AS OBJECT_NAME,
    o.value:object_type::VARCHAR AS OBJECT_TYPE,
    o.value:layer::VARCHAR AS LAYER,
    o.value:vaultspeed_type::VARCHAR AS VAULTSPEED_TYPE,
    o.value:underlying_object::VARCHAR AS UNDERLYING_OBJECT,
    o.value:expected_basis::VARCHAR AS EXPECTED_BASIS,
    o.value:basis_type::VARCHAR AS BASIS_TYPE,
    o.value:incr_source_objects::VARCHAR AS INCR_SOURCE_OBJECTS,
    o.value:init_source_objects::VARCHAR AS INIT_SOURCE_OBJECTS,
    o.value:stg_objects::VARCHAR AS STG_OBJECTS,
    o.value:source_resolution::VARCHAR AS SOURCE_RESOLUTION
FROM TOOLS_DB.VAULTSPEED_QA.INVENTORY_RUNS r,
     LATERAL FLATTEN(INPUT => r.DOCUMENT:result:objects) o
WHERE r.RUN_ID = '<run ID returned by CALL>'
ORDER BY SCHEMA_NAME, OBJECT_NAME;
```

The source columns are blank unless the run included source lineage. `BASIS_TYPE` separates objects the API lists (`api_metadata`) from tables derived from parameters (`parameter_rule`); see the readme for the difference.

Other arrays in the document can be expanded in the same way:

| Path | Contents |
| --- | --- |
| `result:objects` | Expected objects (`expected_objects.csv`) |
| `object_sources` | One row per object and source object (`object_sources.csv`); null without lineage |
| `result:members` | Original source hub to grouped hub membership (`hub_group_members.csv`) |
| `result:dependencies` | Business-view, hub/link, and temp-table relationships (`object_relationships.csv`) |
| `result:review` | Definitions and settings that require review (`review_objects.csv`) |
| `result:overview_variants` | Source-oriented names not added as physical expectations |

Object names are uppercase as VaultSpeed creates them, including business views whose VaultSpeed business name has lowercase letters.

To use a run's raw snapshot outside Snowflake, download `raw.zip` from the stage (`GET` in SnowSQL or the Snowsight stage browser), unzip it, and replay it with the command-line script: `--snapshot-dir raw` (add `--no-source-lineage` if the run did not include lineage).

## 9. Validate and operate

For the first deployment, run against a known release and compare the result with a command-line export of the same release: object counts, `basis_type` counts, review records, and the selected DV/BV identities. Check that invalid credentials or an invalid release fail without adding a run. Confirm that an operator can invoke the procedure without direct Secret access, and that the snapshot zip appears in the stage.

## Inventory scope

Running in Snowflake does not change the script's interpretation of VaultSpeed metadata. The inventory lists RDV tables, BDV tables for links across sources, generated business views, the BV temp tables for links-across-sources satellites, and the FMC load-control tables. It does not establish that the selected release was deployed or authorize object deletion.

STG/EXT/INI/MTD source layers (including temp tables placed in staging schemas), PIT/bridge/Studio custom outputs, procedures, sequences, tasks, and column-level lineage are outside this inventory.
