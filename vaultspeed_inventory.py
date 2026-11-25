#!/usr/bin/env python3
"""Export release-scoped modeled Snowflake RDV/BDV objects using the VaultSpeed API.

POST /login for credentials; metadata requests are GET only. Standard library only.
Does not connect to Snowflake or authorize object removal.
See docs/vaultspeed-inventory.md for scope, CSV contracts and examples.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
EXPECTED_FIELDS = ['database_name', 'schema_name', 'object_name', 'object_type', 'layer',
                   'vaultspeed_type', 'dv_id', 'dv_vs_id', 'bv_vs_id', 'tab_dv_vs_id',
                   'tab_dv_business_view_id', 'underlying_database', 'underlying_schema',
                   'underlying_object', 'expected_basis', 'basis_type', 'source_count', 'incr_source_objects',
                   'init_source_objects', 'stg_objects', 'source_resolution']
OBJECT_SOURCE_FIELDS = ['database_name', 'schema_name', 'object_name', 'object_type', 'layer',
                        'source_name', 'src_vs_id', 'source_dv_object', 'tab_src_dv_vs_id',
                        'modeled_source_object', 'physical_source_table', 'incr_source_object',
                        'init_source_object', 'stg_object', 'resolution']
# Some VaultSpeed hosts sit behind Cloudflare, which rejects urllib's default
# 'Python-urllib/x.y' User-Agent with HTTP 403 (error 1010) before the API sees the request.
USER_AGENT = 'vaultspeed-inventory'
LINK_CODES = {'LNK', 'LND', 'NHL'}
SAT_CODES = {'SAT', 'LKS', 'LDS'}
# Parameter-derived objects (not listed by any object endpoint). Each rule was checked
# against the generated DDL of a real release with the configuration in VERIFIED_CONFIG;
# other configurations are reported for review instead of guessed.
FMC_TABLES = [  # (object type code in /metadata/object-types, naming parameter, prefix)
    ('BVLCI', 'LOAD_CYCLE_INFO_TABLE_NAME', ''),
    ('DVLCI', 'LOAD_CYCLE_INFO_TABLE_NAME', 'DV_'),
    ('BVLWT', 'FMC_BV_LOADING_WINDOW_TABLE_NAME', ''),
    ('FMCH', 'FMC_LOADING_HIST_TABLE_NAME', ''),
    ('FMCOH', 'FMC_OBJECT_LOADING_HIST_TABLE_NAME', ''),
]
VERIFIED_CONFIG = {'USE_FMC': 'Y', 'FMC_TYPE': 'Airflow', 'STORE_TEMP_IN_STAGING': 'Y'}
MEMBER_FIELDS = ['dv_vs_id', 'hub_group_id', 'group_tab_dv_id', 'group_hub_name',
                 'group_tab_dv_vs_id', 'source_name', 'src_id', 'src_vs_id',
                 'original_hub_name', 'tab_dv_id_orig', 'tab_src_dv_id_orig',
                 'hub_tab_elements_id', 'status', 'source_hub_version_id',
                 'modeled_source_object', 'resolution']
REVIEW_FIELDS = ['category', 'object_name', 'object_id', 'detail']
DEPENDENCY_FIELDS = ['parent_database', 'parent_schema', 'parent_object',
                     'dependent_database', 'dependent_schema', 'dependent_object', 'relationship']


class InventoryError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_env(path):
    values = {}
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.removeprefix('export ').split('=', 1)
            values[key.strip()] = value.strip().strip('\"\'')
    return values


def credentials(args):
    env = read_env(args.env_file)
    base = args.base_url or os.environ.get('VAULTSPEED_BASE_URL') or env.get('VAULTSPEED_BASE_URL', '')
    parsed = urlparse(base)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise InventoryError('Set a valid HTTPS VAULTSPEED_BASE_URL or --base-url.')
    base = base.rstrip('/')
    username = os.environ.get('VAULTSPEED_USERNAME') or env.get('VAULTSPEED_USERNAME', '')
    password = os.environ.get('VAULTSPEED_PASSWORD') or env.get('VAULTSPEED_PASSWORD', '')
    if username or password:
        if not username or not password:
            raise InventoryError('Set both VAULTSPEED_USERNAME and VAULTSPEED_PASSWORD, or clear both to use a token.')
        return base, login(base, username, password)
    token = os.environ.get('VAULTSPEED_ACCESS_TOKEN') or env.get('VAULTSPEED_ACCESS_TOKEN', '')
    if not token and args.token_file.is_file():
        token = args.token_file.read_text(encoding='utf-8').strip()
    match = re.search(r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token)
    if match:
        token = match.group(0)
    else:
        token = token.removeprefix('Bearer ').strip().strip('\"\'')
    if not token or any(c.isspace() for c in token):
        raise InventoryError('Set VAULTSPEED_USERNAME and VAULTSPEED_PASSWORD, or provide a fallback access token.')
    return base, token


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward bearer credentials to a redirect destination.
        return None


def login(base, username, password):
    """Exchange credentials without logging or saving the login request/response."""
    request = Request(base + '/login',
                      data=json.dumps({'username': username, 'password': password}).encode('utf-8'),
                      headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                               'User-Agent': USER_AGENT},
                      method='POST')
    try:
        with build_opener(NoRedirect()).open(request, timeout=60) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise InventoryError(f'Login failed: HTTP {error.code}. Check credentials and tenant API login support; no inventory published.') from None
    except (URLError, TimeoutError):
        raise InventoryError('Login connection failed; no inventory published.') from None
    except (ValueError, UnicodeError):
        raise InventoryError('Login returned invalid JSON; no inventory published.') from None
    token = payload.get('access_token') if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise InventoryError('Login response missing a valid access_token; no inventory published.')
    return token


class LiveAPI:
    def __init__(self, base, token, raw_dir, page_size=500, workers=4, progress=None):
        self.base, self.token, self.raw_dir = base, token, raw_dir
        self.page_size = page_size
        self.workers = workers
        # Called as progress(path, count) after each endpoint; default prints to stderr.
        self.progress = progress
        self.records = {}
        self.lock = threading.Lock()

    def request(self, path, params=None):
        if not path.startswith('/') or path.startswith('//'):
            raise InventoryError('Expected an API-relative endpoint.')
        url = self.base + path + ('?' + urlencode(params) if params else '')
        for attempt in range(4):
            req = Request(url, headers={'Authorization': 'Bearer ' + self.token,
                                       'Accept': 'application/json', 'User-Agent': USER_AGENT}, method='GET')
            try:
                with build_opener(NoRedirect()).open(req, timeout=60) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code in (429, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise InventoryError(f'GET {path}: HTTP {error.code}; no inventory published.') from None
            except (URLError, TimeoutError, ConnectionError):
                # Transient resets occur under parallel requests; retry before giving up.
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise InventoryError(f'GET {path}: connection failed; no inventory published.') from None
            except (ValueError, UnicodeError):
                raise InventoryError(f'GET {path}: expected JSON; no inventory published.') from None
        raise InventoryError('Request retry limit exceeded.')

    def get(self, path, paginated=True):
        if path in self.records:
            return self.records[path]['data']
        started = utc_now()
        if paginated:
            data, seen_pages, skip = [], set(), 0
            while True:
                page = self.request(path, {'filter': json.dumps({'limit': self.page_size, 'skip': skip})})
                if not isinstance(page, list) or any(not isinstance(r, dict) for r in page):
                    raise InventoryError(f'GET {path}: expected a list of records.')
                if not page:
                    break
                fingerprint = digest(json.dumps(page, sort_keys=True).encode())
                if fingerprint in seen_pages or len(page) > self.page_size:
                    raise InventoryError(f'GET {path}: pagination was ignored or repeated; cannot establish completeness.')
                seen_pages.add(fingerprint)
                data.extend(page)
                skip += len(page)
                if skip > 1_000_000:
                    raise InventoryError(f'GET {path}: pagination limit exceeded.')
        else:
            data = self.request(path)
        filename = digest(path.encode())[:24] + '.json'
        payload = (json.dumps(data, indent=2, sort_keys=True) + '\n').encode()
        with self.lock:
            self.raw_dir.mkdir(exist_ok=True)
            (self.raw_dir / filename).write_bytes(payload)
            self.records[path] = {'data': data, 'file': filename, 'sha256': digest(payload),
                                  'started_at': started, 'finished_at': utc_now(), 'paginated': paginated}
            self.save_manifest()
        count = len(data) if isinstance(data, list) else None
        if self.progress:
            self.progress(path, count)
        else:
            print(f'Fetched {path}: {"record" if count is None else count}', file=sys.stderr)
        return data

    def get_many(self, paths, paginated=True):
        unique_paths = list(dict.fromkeys(paths))
        with ThreadPoolExecutor(max(1, self.workers)) as pool:
            return dict(zip(unique_paths, pool.map(lambda p: self.get(p, paginated), unique_paths)))

    def save_manifest(self):
        manifest = {'base_url': self.base, 'endpoints': {
            path: {k: v for k, v in record.items() if k != 'data'} for path, record in self.records.items()}}
        (self.raw_dir / 'snapshot.json').write_text(json.dumps(manifest, indent=2) + '\n')


class SnapshotAPI:
    """Replay a checksummed snapshot, including the original investigation cache."""
    def __init__(self, directory):
        self.directory = directory.resolve()
        self.records = {}
        self.used = {}
        manifest = directory / 'snapshot.json'
        if manifest.exists():
            data = json.loads(manifest.read_text())
            self.base = data['base_url']
            self.records = data['endpoints']
        else:
            log = directory / 'requests.jsonl'
            if not log.is_file():
                raise InventoryError('Snapshot needs snapshot.json or the investigation requests.jsonl.')
            bases = set()
            for line in log.read_text().splitlines():
                record = json.loads(line)
                bases.add(record['base_url'])
                self.records[record['path']] = record
            if len(bases) != 1:
                raise InventoryError('Snapshot contains multiple API environments.')
            self.base = bases.pop()

    def get(self, path, paginated=True):
        record = self.records.get(path)
        if not record:
            raise InventoryError(f'Snapshot does not contain {path}; collect a complete live snapshot.')
        filename = (self.directory / record['file']).resolve()
        if not filename.is_relative_to(self.directory):
            raise InventoryError('Snapshot path escapes its directory.')
        payload = filename.read_bytes()
        if not record.get('sha256') or digest(payload) != record['sha256']:
            raise InventoryError(f'Snapshot checksum missing or invalid for {path}.')
        data = json.loads(payload)
        if paginated and (not isinstance(data, list) or any(not isinstance(r, dict) for r in data)):
            raise InventoryError(f'Snapshot {path}: expected a list of records.')
        self.used[path] = record
        return data

    def get_many(self, paths, paginated=True):
        return {p: self.get(p, paginated) for p in dict.fromkeys(paths)}


def unique(records, label):
    if len(records) != 1:
        raise InventoryError(f'{label}: expected exactly one match, found {len(records)}.')
    return records[0]


def required_text(value, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise InventoryError(f'{label} must be a nonempty identifier.')
    return value


def by_name(records, field, label):
    result = {}
    for record in records:
        name = required_text(record.get(field), f'{label}.{field}')
        if name in result:
            raise InventoryError(f'{label}: ambiguous duplicate name {name}.')
        result[name] = record
    return result


def select_record(records, field, value, label):
    matches = [r for r in records if str(r.get(field)) == str(value)]
    if len(matches) != 1:
        choices = sorted({str(r[field]) for r in records if r.get(field) is not None})
        raise InventoryError(f'{label}: expected exactly one match for {value!r}, '
                             f'found {len(matches)}. Available values: {", ".join(choices) or "(none)"}.')
    return matches[0]


def collect(api, args):
    project = vault = None
    if args.dv_id is None:
        project = select_record(api.get('/projects'), 'project_name', args.project, 'Project')
        vaults = [r for r in api.get('/data-vaults') if r.get('project_id') == project['project_id']]
        vault = select_record(vaults, 'dv_name', args.data_vault, 'Data Vault in selected project')
        args.dv_id = vault['dv_id']
    releases = api.get(f'/data-vault/{args.dv_id}/releases')
    if args.dv_vs_id is not None:
        field, value = 'dv_vs_id', args.dv_vs_id
    elif getattr(args, 'release_name', None) is not None:
        field, value = 'vs_dv_name', args.release_name
    else:
        field, value = 'vs_dv_number', args.release_number
    release = select_record(releases, field, value, 'Selected DV release')
    if release.get('dv_id') != args.dv_id:
        raise InventoryError('Selected release belongs to a different Data Vault.')
    vid = release['dv_vs_id']
    bvs = api.get(f'/data-vaults/{args.dv_id}/business-vaults/releases')
    candidates = [b for b in bvs if b['dv_vs_id'] == vid and b.get('dv_id') in (None, args.dv_id)]
    if args.bv_vs_id is not None:
        bv = select_record(candidates, 'bv_vs_id', args.bv_vs_id, 'BV release')
    elif getattr(args, 'bv_release_name', None) is not None:
        bv = select_record(candidates, 'bv_vs_name', args.bv_release_name, 'BV release')
    elif len(candidates) == 1:
        bv = candidates[0]
    else:
        names = sorted({str(b.get('bv_vs_name', '(unnamed)')) for b in candidates})
        raise InventoryError('BV release: expected exactly one associated release, '
                             f'found {len(candidates)}. Use --bv-release-name. '
                             f'Available names: {", ".join(names) or "(none)"}.')
    bid = bv['bv_vs_id']
    return {
        'project': project, 'data_vault': vault, 'release': release, 'bv_release': bv,
        'parameters': api.get(f'/parameters/{vid}/data-vault', paginated=False),
        'project_parameters': None if getattr(args, 'no_derived_objects', False)
                              else api.get(f'/parameters/{vid}/project', paginated=False),
        'system_parameters': None if getattr(args, 'no_derived_objects', False)
                             else api.get(f'/parameters/{vid}/system', paginated=False),  # USE_FMC
        'object_types': api.get('/metadata/object-types'),
        'catalog': api.get(f'/data-vaults/release/{vid}/objects'),
        'overview': api.get(f'/data-vault/{vid}/overview/objects'),
        'views': api.get(f'/business-vault/{bid}/business-views'),
        'adjacency': api.get(f'/business-vault/{bid}/adjacent-hubs'),
        'groups': api.get(f'/data-vault/{vid}/hub-groups'),
        'sources': api.get('/sources'),
        'source_releases': api.get(f'/data-vault/{vid}/sources'),
    }


def transform(api, data, args):
    release, bv = data['release'], data['bv_release']
    vid, bid = release['dv_vs_id'], bv['bv_vs_id']
    review, expected, members, dependencies = [], {}, [], set()
    params = defaultdict(list)
    for param in data['parameters']:
        params[param['parameter_code']].append(param['parameter_value'])
    def schema(override, code):
        value = override or unique(params[code], f'Release parameter {code}')
        required_text(value, code)
        if '<' in value or '>' in value:
            raise InventoryError(f'{code} contains a template; pass an explicit schema override.')
        return value
    rdv_schema, bdv_schema = schema(args.rdv_schema, 'SCHEMA_FL'), schema(args.bdv_schema, 'SCHEMA_BV')
    database = args.database if args.database is not None else ''
    bdv_database = args.bdv_database if args.bdv_database is not None else database
    if args.database is not None:
        required_text(database, '--database')
    if args.bdv_database is not None:
        required_text(bdv_database, '--bdv-database')
    catalog = by_name(data['catalog'], 'table_name', 'Object catalog')
    for obj in catalog.values():
        if obj.get('dv_vs_id') != vid:
            raise InventoryError('Object catalog contains a different DV release.')
    types = defaultdict(list)
    for meta in data['object_types']:
        if meta.get('tab_type_desc') and meta.get('tab_type_id', 0) > 0:
            types[meta['tab_type_desc']].append(meta)
    def physical_layer(obj):
        candidates = types[obj['object_type']]
        return unique(candidates, f'Object type {obj["object_type"]}')
    def add(obj, layer, kind, basis, view=None, name=None, schema_name=None, vs_type=None):
        db, sch = (database, rdv_schema) if layer == 'RDV' else (bdv_database, bdv_schema)
        if layer == 'FMC':
            db, sch = database, schema_name
        # The generator creates business views as quoted uppercase identifiers even when the
        # business name has lowercase letters (e.g. view_LAS_... -> "VIEW_LAS_..."; seen in generated ETL).
        name = name or required_text(view['business_name'].upper() if view else obj['table_name'], 'Object name')
        underlying = physical_location.get(obj['table_name']) if view else None
        row = dict(zip(EXPECTED_FIELDS, [''] * len(EXPECTED_FIELDS)))
        row.update(database_name=db, schema_name=sch, object_name=name, object_type=kind,
                   layer=layer, vaultspeed_type=vs_type or (view['tab_type_desc'] if view else obj['object_type']),
                   dv_id=args.dv_id, dv_vs_id=vid, bv_vs_id=bid, tab_dv_vs_id=obj['tab_dv_vs_id'] if obj else '',
                   tab_dv_business_view_id=view['tab_dv_business_view_id'] if view else '',
                   underlying_database=underlying[0] if underlying else '',
                   underlying_schema=underlying[1] if underlying else '',
                   underlying_object=underlying[2] if underlying else '', expected_basis=basis,
                   basis_type='parameter_rule' if basis.startswith('derived_') else 'api_metadata')
        key = (db, sch, name)
        if key in expected and expected[key] != row:
            raise InventoryError(f'Conflicting physical definitions for {sch}.{name}.')
        expected[key] = row
        return key

    overview_names, overview_variants, blocked_names = set(), [], set()
    physical_location = {}
    for current in data['overview']:
        if current.get('dv_vs_id') != vid:
            raise InventoryError('Overview contains a different DV release.')
        name = required_text(current.get('table_name'), 'Overview object name')
        obj = catalog.get(name)
        if obj is None:
            # Overview uses source-oriented satellite names in some releases.
            # Do not guess their generated names from short names or prefixes.
            overview_variants.append(current)
            continue
        if obj['object_type'] != current['object_type']:
            raise InventoryError(f'Overview/catalog type mismatch for {name}.')
        if current.get('status') not in (None, 'E'):
            blocked_names.add(name)
            review.append({'category': 'overview_status', 'object_name': name,
                           'object_id': current.get('tab_src_dv_vs_id'), 'detail': str(current.get('status'))})
            continue
        overview_names.add(name)

    views_by_target = defaultdict(list)
    for view in data['views']:
        if view.get('bv_vs_id') != bid or not isinstance(view.get('generate'), bool):
            raise InventoryError('BV view has inconsistent release or missing generate flag.')
        views_by_target[view['table_name']].append(view)
    cross_source_names, rdv_names = set(), set()
    for name, obj in catalog.items():
        if name in blocked_names and any(v['generate'] for v in views_by_target[name]):
            raise InventoryError(f'Conflicting overview status and generated view for {name}.')
        if name in blocked_names:
            continue
        meta = physical_layer(obj)
        if meta['layer_type_short_name'] == 'FL' and (name in overview_names or views_by_target[name]):
            rdv_names.add(name)
            basis = 'current_dv_overview' if name in overview_names else 'current_business_view_target'
            physical_location[name] = add(obj, 'RDV', 'BASE TABLE', basis)
        # LNA/LAS are BV base structures, with optional BVV aliases. They are not RDV tables.
        elif meta['tab_type'] in ('LNA', 'LAS') and meta['layer_type_short_name'] == 'BV' and views_by_target[name]:
            cross_source_names.add(name)
            physical_location[name] = add(obj, 'BDV', 'BASE TABLE', 'business_vault_cross_source_definition')
        else:
            review.append({'category': 'catalog_only_definition', 'object_name': name,
                           'object_id': obj['tab_dv_vs_id'],
                           'detail': 'Absent from current DV overview; not established as an expected physical object.'})
    for view in data['views']:
        name = view['table_name']
        if not view['generate']:
            review.append({'category': 'disabled_business_view', 'object_name': view['business_name'],
                           'object_id': view['tab_dv_business_view_id'], 'detail': 'generate=false; base object handled separately.'})
            continue
        if name not in physical_location:
            raise InventoryError(f'Generated view {view["business_name"]} has no classified physical target: {name}.')
        alias_key = add(catalog[name], 'BDV', 'VIEW', 'business_view_generate_true', view)
        dependencies.add((*physical_location[name], *alias_key, 'business_view_of'))

    derived = derived_objects(data, types_by_code(data), catalog, cross_source_names,
                              physical_location, add, review, dependencies)

    # Use IDs from adjacency; its display names may refer to either base names or aliases.
    names_by_id = {obj['tab_dv_vs_id']: name for name, obj in catalog.items()}
    for hub in data['adjacency']:
        if hub.get('dv_vs_id') != vid or hub.get('bv_vs_id') != bid:
            raise InventoryError('Adjacency contains a different release.')
        for neighbor in hub.get('droplist', []):
            ids = (hub['tab_dv_vs_id'], neighbor['adjacent_hub_tab_dv_vs_id'], neighbor['adjacent_link_tab_dv_vs_id'])
            if any(i not in names_by_id for i in ids):
                raise InventoryError('Adjacency references an unknown object version ID.')
            current_name, other_name, link_name = (names_by_id[i] for i in ids)
            for parent in (current_name, other_name):
                if parent in physical_location and link_name in physical_location:
                    dependencies.add((*physical_location[parent], *physical_location[link_name], 'link_references_hub'))

    source_defs = {s['src_id']: s for s in data['sources']}
    source_releases = {s['src_id']: s for s in data['source_releases']}
    sources_by_name = defaultdict(list)
    for src_id, sr in source_releases.items():
        if sr.get('dv_vs_id') != vid or src_id not in source_defs:
            raise InventoryError('Source-release dependency has inconsistent identity.')
        sources_by_name[source_defs[src_id]['src_name']].append(sr)
    for group in data['groups']:
        if group.get('dv_vs_id') != vid:
            raise InventoryError('Hub group contains a different release.')
        name = group['hub_table_name']
        obj = catalog.get(name)
        if obj is None:
            raise InventoryError(f'Hub group {name} has no release object definition.')
        for member in group['hubs']:
            sr = unique(sources_by_name[member['src_name']], f'Source for group member {member["src_name"]}')
            sv = sr['src_vs_id']
            hubs = api.get(f'/source/release/{sv}/hubs/2')
            matches = [h for h in hubs if h['tab_src_dv_id'] == member['tab_src_dv_id_orig']]
            match = matches[0] if len(matches) == 1 else {}
            if member['status'] == 'E' and (len(matches) != 1 or match.get('table_name') != member['hub_table_name_orig'] or match.get('src_vs_id') != sv or name not in rdv_names):
                raise InventoryError(f'Cannot resolve E hub member {member["src_name"]}.{member["hub_table_name_orig"]} by ID.')
            if member['status'] not in ('E', 'D'):
                raise InventoryError('Unknown hub membership status; cannot interpret grouping safely.')
            members.append(dict(dv_vs_id=vid, hub_group_id=group['hub_group_id'],
                group_tab_dv_id=group['tab_dv_id'], group_hub_name=name,
                group_tab_dv_vs_id=obj['tab_dv_vs_id'], source_name=member['src_name'],
                src_id=sr['src_id'], src_vs_id=sv, original_hub_name=member['hub_table_name_orig'],
                tab_dv_id_orig=member['tab_dv_id_orig'], tab_src_dv_id_orig=member['tab_src_dv_id_orig'],
                hub_tab_elements_id=member['hub_tab_elements_id'], status=member['status'],
                source_hub_version_id=match.get('tab_src_dv_vs_id', ''),
                modeled_source_object=match.get('src_table_name', ''),
                resolution='exact_id_match' if match else 'no_current_source_hub_match'))
    return {'objects': sorted(expected.values(), key=lambda r: (r['database_name'], r['schema_name'], r['object_name'])),
            'members': members, 'review': review,
            'dependencies': [dict(zip(DEPENDENCY_FIELDS, row)) for row in sorted(dependencies)],
            'overview_variants': overview_variants,
            'schemas': dict({'RDV': rdv_schema, 'BDV': bdv_schema}, **derived['schemas']),
            'counts': dict({'rdv_tables': len(rdv_names), 'bdv_cross_source_tables': len(cross_source_names),
                            'bdv_views': sum(v['generate'] for v in data['views'])}, **derived['counts'])}


def types_by_code(data):
    return {m['tab_type']: m for m in data['object_types'] if m.get('tab_type')}


def derived_objects(data, types, catalog, cross_source_names, physical_location, add, review, dependencies):
    """Objects VaultSpeed generates that no object endpoint lists: LAS temp tables and FMC tables.

    Names come from project parameters; which objects exist is a generator rule checked
    against generated DDL (see VERIFIED_CONFIG and docs/vaultspeed-inventory.md).
    """
    result = {'schemas': {}, 'counts': {}}
    if data.get('project_parameters') is None:
        return result
    values = defaultdict(list)
    for param in data['project_parameters'] + (data.get('system_parameters') or []) + data['parameters']:
        values[param['parameter_code']].append(param['parameter_value'])
    def value(code):
        found = set(values.get(code, []))
        if len(found) > 1:
            raise InventoryError(f'Parameter {code} has conflicting values.')
        return found.pop() if found else None
    def needs_review(code, detail):
        review.append({'category': 'unverified_configuration', 'object_name': code, 'object_id': '',
                       'detail': detail})
    for code, expected in VERIFIED_CONFIG.items():
        if value(code) != expected:
            needs_review(code, f'{code}={value(code)}; derived objects were verified only with {code}={expected}.')
    for code in ('LAS', 'LAS_TEMP', *(t for t, _, _ in FMC_TABLES)):
        if code not in types:
            raise InventoryError(f'Object type {code} missing from /metadata/object-types.')

    # One temp table per LAS satellite, in the BV schema (observed even with STORE_TEMP_IN_STAGING=Y).
    prefix, suffix = value('TEMP_TABLE_PREFIX'), value('TEMP_TABLE_SUFFIX')
    temp_count = 0
    for name in sorted(cross_source_names):
        if catalog[name]['object_type'] != types['LAS']['tab_type_desc']:
            continue
        temp_name = '_'.join(p for p in (prefix, name, suffix) if p)
        key = add(catalog[name], 'BDV', 'BASE TABLE', 'derived_las_temp:TEMP_TABLE_PREFIX', name=temp_name,
                  vs_type='LAS_TEMP')
        dependencies.add((*physical_location[name], *key, 'load_temp_table_of'))
        temp_count += 1
    result['counts']['bdv_las_temp_tables'] = temp_count

    # FMC control tables, only when the FMC is generated.
    fmc_count = 0
    if value('USE_FMC') == 'Y':
        fmc_schema = required_text(value('SCHEMA_FMC'), 'SCHEMA_FMC')
        if '<' in fmc_schema or '>' in fmc_schema:
            raise InventoryError('SCHEMA_FMC contains a template; derived FMC tables cannot be named.')
        for code, param, name_prefix in FMC_TABLES:
            table = required_text(value(param), param)
            basis = f'derived_fmc:{"DV_+" if name_prefix else ""}{param}'
            add(None, 'FMC', 'BASE TABLE', basis, name=name_prefix + table, schema_name=fmc_schema, vs_type=code)
            fmc_count += 1
        result['schemas']['FMC'] = fmc_schema
    result['counts']['fmc_tables'] = fmc_count
    return result


def name_tokens(name, source_name):
    """Prefix plus sorted remaining tokens; overview names reorder the same tokens."""
    suffix = '_' + source_name
    base = name[:-len(suffix)] if source_name and name.endswith(suffix) else name
    head, _, rest = base.partition('_')
    return head, tuple(sorted(rest.split('_')))


def source_lineage(api, data, result):
    """Map each expected object to the source objects feeding it, using GET-only source metadata.

    Every rule is ID-based or requires a unique candidate; unmatched definitions go to review.
    """
    vid = data['release']['dv_vs_id']
    catalog = {o['table_name']: o for o in data['catalog']}
    overview = {o['tab_src_dv_vs_id']: o for o in data['overview'] if o.get('tab_src_dv_vs_id') is not None}
    meta_by_id = {m['tab_type_id']: m for m in data['object_types'] if m.get('tab_type_id', 0) > 0}
    code_by_desc = {m['tab_type_desc']: m['tab_type'] for m in meta_by_id.values() if m.get('tab_type_desc')}
    catalog_types = {o['object_type'] for o in catalog.values()}
    type_ids = sorted(i for i, m in meta_by_id.items()
                      if m.get('layer_type_short_name') == 'FL' and m.get('tab_type_desc') in catalog_types)
    source_defs = {s['src_id']: s for s in data['sources']}
    releases = {sr['src_vs_id']: source_defs[sr['src_id']]['src_name'] for sr in data['source_releases']}
    params = defaultdict(dict)
    source_params = api.get(f'/parameters/{vid}/source', paginated=False)  # endpoint ignores paging
    if not isinstance(source_params, list):
        raise InventoryError('Source parameters: expected a list.')
    for p in source_params:
        params[p.get('src_name')][p.get('parameter_code')] = p.get('parameter_value')

    paths = [f'/source/release/{sv}/objects' for sv in releases]
    paths += [f'/source/release/{sv}/hubs/{tt}' for sv in releases for tt in type_ids]
    fetched = api.get_many(paths)
    physical = {}
    for sv in releases:
        for o in fetched[f'/source/release/{sv}/objects']:
            physical[(sv, o['table_name'])] = o.get('table_name_src') or o['table_name']
    defs = {}
    for sv in releases:
        for tt in type_ids:
            for d in fetched[f'/source/release/{sv}/hubs/{tt}']:
                if d.get('src_vs_id') != sv:
                    raise InventoryError('Source-level definition belongs to another source release.')
                defs[d['tab_src_dv_vs_id']] = dict(d, source_name=releases[sv], tab_code=meta_by_id[tt]['tab_type'],
                                                   object_type=meta_by_id[tt]['tab_type_desc'])

    resolved, rule = {}, {}
    def assign(sid, name, how):
        resolved[sid], rule[sid] = name, how
    # 1. Explicit IDs: hub-group membership, then the overview's source-definition ID.
    members = {(m['source_name'], m['tab_src_dv_id_orig']): m['group_hub_name']
               for m in result['members'] if m['status'] == 'E'}
    for sid, d in defs.items():
        key = (d['source_name'], d['tab_src_dv_id'])
        if d['tab_code'] == 'HUB' and key in members:
            assign(sid, members[key], 'hub_group_member')
        elif sid in overview and overview[sid]['table_name'] in catalog:
            assign(sid, overview[sid]['table_name'], 'overview_id')
    # 2. Overview ID whose source-oriented name reorders the generated name's tokens.
    overview_names = {o['table_name'] for o in overview.values()}
    index = defaultdict(list)
    for name, obj in catalog.items():
        if name not in overview_names:
            for sv in set(obj.get('src_vs_ids') or []):
                index[(obj['object_type'], sv, name_tokens(name, releases.get(sv, '')))].append(name)
    for sid, d in defs.items():
        if sid not in resolved and sid in overview:
            o = overview[sid]
            found = index.get((o['object_type'], d['src_vs_id'], name_tokens(o['table_name'], d['source_name'])), [])
            if len(found) == 1:
                assign(sid, found[0], 'overview_id_name_tokens')
    # 3. Grouped link members: same link type and the same set of resolved DV hubs.
    link_ids = [sid for sid, d in defs.items() if d['tab_code'] in LINK_CODES]
    pending_sats = [sid for sid, d in defs.items() if d['tab_code'] in SAT_CODES and sid not in resolved]
    rels = api.get_many([f'/source/sdvv/relationships/{sid}' for sid in link_ids + pending_sats])
    def parents(sid):
        return {r['r_tab_src_dv_vs_id'] for r in rels[f'/source/sdvv/relationships/{sid}']
                if r.get('constraint_type') == 'R' and r.get('r_tab_src_dv_vs_id') is not None}
    def hub_set(sid):
        return frozenset(resolved.get(p, f'?{p}') for p in parents(sid))
    same_release, any_release = defaultdict(set), defaultdict(set)
    for sid in link_ids:
        if sid in resolved:
            d, hubs = defs[sid], hub_set(sid)
            same_release[(d['tab_code'], d['src_vs_id'], hubs)].add(resolved[sid])
            any_release[(d['tab_code'], hubs)].add(resolved[sid])
    for sid in link_ids:
        found = same_release.get((defs[sid]['tab_code'], defs[sid]['src_vs_id'], hub_set(sid)), set())
        if sid not in resolved and len(found) == 1:
            assign(sid, next(iter(found)), 'link_member_hub_set')
    for sid, d in defs.items():
        target = catalog.get(d['table_name'])
        if sid not in resolved and target and target['object_type'] == d['object_type']:
            assign(sid, d['table_name'], 'source_name')
    for sid in link_ids:
        found = any_release.get((defs[sid]['tab_code'], hub_set(sid)), set())
        if sid not in resolved and len(found) == 1:
            assign(sid, next(iter(found)), 'link_member_hub_set_cross_source')
    # 4. Satellites on grouped parents: unique unassigned DV satellite named after the DV parent.
    used = set(resolved.values())
    for sid in pending_sats:
        d = defs[sid]
        found_parents = parents(sid)
        if sid in resolved or len(found_parents) != 1:
            continue
        parent = resolved.get(next(iter(found_parents)))
        short = catalog.get(parent, {}).get('table_short_name')
        if not short:
            continue
        prefix = f'{d["table_name"].partition("_")[0]}_{short}_'
        found = [n for n, o in catalog.items() if n not in used and n.startswith(prefix)
                 and o['object_type'] == d['object_type'] and d['src_vs_id'] in (o.get('src_vs_ids') or [])]
        if len(found) == 1:
            assign(sid, found[0], 'satellite_parent')
            used.add(found[0])

    feeds = defaultdict(list)
    review = []
    for sid, d in defs.items():
        if sid not in resolved:
            review.append({'category': 'unresolved_source_definition', 'object_name': d['table_name'], 'object_id': sid,
                           'detail': f'{d["source_name"]} {d["object_type"]} not matched to a release object.'})
            continue
        sv, modeled = d['src_vs_id'], d.get('src_table_name') or ''
        table = physical.get((sv, modeled), '')
        if not table:
            review.append({'category': 'source_object_not_found', 'object_name': modeled, 'object_id': sid,
                           'detail': f'{d["source_name"]} source object missing from /source/release/{sv}/objects.'})
        p = params.get(d['source_name'], {})
        qualify = lambda schema, name: f'{schema}.{name}' if schema and name else ''
        feeds[resolved[sid]].append({
            'source_name': d['source_name'], 'src_vs_id': sv, 'source_dv_object': d['table_name'],
            'tab_src_dv_vs_id': sid, 'modeled_source_object': modeled, 'physical_source_table': table,
            'incr_source_object': qualify(p.get('SCHEMA_CDC'), table),
            'init_source_object': qualify(p.get('SCHEMA_INI'), table),
            'stg_object': qualify(p.get('SCHEMA_STG'), modeled), 'resolution': rule[sid]})

    # Links across sources have no source-level definition in the API. Infer from adjacent hubs,
    # limited to the link's own source releases, and label the result as inferred.
    names_by_id = {o['tab_dv_vs_id']: n for n, o in catalog.items()}
    code = lambda name: code_by_desc.get(catalog[name]['object_type'])
    lna_hubs = defaultdict(set)
    for hub in data['adjacency']:
        for neighbor in hub.get('droplist', []):
            link = names_by_id.get(neighbor['adjacent_link_tab_dv_vs_id'])
            if link and code(link) == 'LNA':
                lna_hubs[link] |= {names_by_id.get(hub['tab_dv_vs_id']), names_by_id.get(neighbor['adjacent_hub_tab_dv_vs_id'])} - {None}
    for link, hubs in lna_hubs.items():
        allowed = set(catalog[link].get('src_vs_ids') or [])
        feeds[link] = [dict(r, resolution='inferred_cross_source_adjacent_hubs')
                       for h in sorted(hubs) for r in feeds.get(h, []) if r['src_vs_id'] in allowed]
    for name in catalog:
        if code(name) == 'LAS':
            rest = name.partition('_')[2]
            found = [link for link in lna_hubs if rest.startswith(link.partition('_')[2] + '_')]
            if len(found) == 1:
                feeds[name] = [dict(r, resolution='inferred_cross_source_parent_link') for r in feeds[found[0]]]

    object_rows, objects_without = [], 0
    for row in result['objects']:
        if row['basis_type'] != 'api_metadata':
            row['source_resolution'] = 'not_applicable'
            continue
        base = row['underlying_object'] if row['object_type'] == 'VIEW' else row['object_name']
        sources = list({(r['source_name'], r['modeled_source_object'], r['source_dv_object']): r
                        for r in feeds.get(base, [])}.values())
        sources.sort(key=lambda r: (r['source_name'], r['modeled_source_object'], r['source_dv_object']))
        join = lambda field: '; '.join(sorted({r[field] for r in sources if r[field]}))
        row.update(source_count=len({(r['source_name'], r['modeled_source_object']) for r in sources}),
                   incr_source_objects=join('incr_source_object'), init_source_objects=join('init_source_object'),
                   stg_objects=join('stg_object'),
                   source_resolution='+'.join(sorted({r['resolution'] for r in sources})) or 'unresolved')
        if not sources:
            objects_without += 1
            if row['object_type'] != 'VIEW':
                review.append({'category': 'no_source_lineage', 'object_name': row['object_name'],
                               'object_id': row['tab_dv_vs_id'], 'detail': 'No source definition resolved to this object.'})
        for r in sources:
            object_rows.append(dict({k: row[k] for k in ('database_name', 'schema_name', 'object_name', 'object_type', 'layer')}, **r))
    result['review'].extend(review)
    return {'rows': object_rows, 'counts': {
        'source_definitions': len(defs), 'resolved_definitions': len(resolved),
        'resolution_rules': dict(Counter(rule.values())), 'objects_without_sources': objects_without}}


def csv_file(path, rows, fields):
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_inventory(args, out, make_api):
    """Collect, transform and publish one inventory into the new directory `out`.

    `make_api(raw_dir)` returns a LiveAPI writing its snapshot to raw_dir, or a SnapshotAPI.
    `args` needs the attributes main() defines (dv_id, dv_vs_id, bv_vs_id, database, ...).
    Files are written to a staging directory and only renamed to `out` once every step
    succeeds. Returns the summary also written to manifest.json. Raises InventoryError.
    """
    output = Path(out).resolve()
    if output.exists():
        raise InventoryError('Output directory already exists; choose a new directory to avoid mixing runs.')
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot_dir = getattr(args, 'snapshot_dir', None)
    with tempfile.TemporaryDirectory(prefix='.vs-inventory-', dir=output.parent) as staging:
        stage = Path(staging)
        api = make_api(stage / 'raw')
        started = utc_now()
        data = collect(api, args)
        result = transform(api, data, args)
        lineage = None if getattr(args, 'no_source_lineage', False) else source_lineage(api, data, result)
        if lineage:
            csv_file(stage / 'object_sources.csv', lineage['rows'], OBJECT_SOURCE_FIELDS)
        csv_file(stage / 'expected_objects.csv', result['objects'], EXPECTED_FIELDS)
        csv_file(stage / 'hub_group_members.csv', result['members'], MEMBER_FIELDS)
        csv_file(stage / 'review_objects.csv', result['review'], REVIEW_FIELDS)
        csv_file(stage / 'object_relationships.csv', result['dependencies'], DEPENDENCY_FIELDS)
        variant_fields = ['dv_vs_id', 'tab_src_dv_vs_id', 'table_name', 'object_type']
        csv_file(stage / 'source_overview_names.csv',
                 [{k: r[k] for k in variant_fields} for r in result['overview_variants']], variant_fields)
        files = {p.name: digest(p.read_bytes()) for p in stage.iterdir() if p.is_file()}
        manifest = {'started_at': started, 'finished_at': utc_now(), 'api_base_url': api.base,
            'mode': 'offline_snapshot' if snapshot_dir else 'live_get_only',
            'snapshot_source': str(Path(snapshot_dir).resolve()) if snapshot_dir else 'raw/snapshot.json',
            'project': data['project'], 'data_vault': data['data_vault'],
            'release': data['release'], 'bv_release': data['bv_release'],
            'database': args.database, 'bdv_database': args.bdv_database or args.database,
            'schemas': result['schemas'], 'counts': result['counts'],
            'source_lineage': lineage['counts'] if lineage else None,
            'expected_objects': len(result['objects']), 'review_objects': len(result['review']),
            'membership_status_counts': dict(Counter(m['status'] for m in result['members'])),
            'scope': 'Modeled RDV core tables, BV cross-source core tables, generated business view aliases; '
                     'unless --no-derived-objects, parameter-derived LAS temp tables (BV schema) and FMC tables '
                     '(basis_type=parameter_rule).',
            'derived_objects_verified_config': VERIFIED_CONFIG,
            'excluded_scope': ['STG/EXT/INI/MTD', 'temp tables in staging schemas and other runtime helpers',
                'PIT/bridge/Studio custom outputs', 'procedures/sequences/tasks',
                'satellite parent lineage and column transformation expressions'],
            'deployment_verified': False,
            'object_type_basis': 'Standard SQL Snowflake core objects: base tables plus generated BV views; custom DDL not parsed.',
            'complete_for_all_snowflake_objects': False, 'file_sha256': files,
            'endpoint_evidence': api.used if isinstance(api, SnapshotAPI) else
                {p: {k: v for k, v in r.items() if k != 'data'} for p, r in api.records.items()}}
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        # Only publish after every required API call and mapping check succeeds.
        if output.exists():
            raise InventoryError('Output directory appeared during collection; choose another destination.')
        os.rename(stage, output)
    return {'output': output, 'expected_objects': len(result['objects']), 'counts': result['counts'],
            'source_lineage': lineage['counts'] if lineage else None, 'review_objects': len(result['review'])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', help='Exact VaultSpeed project name')
    parser.add_argument('--data-vault', help='Exact Data Vault name within the project')
    parser.add_argument('--dv-id', type=int, help='Advanced alternative to --project and --data-vault')
    release = parser.add_mutually_exclusive_group(required=True)
    release.add_argument('--release-number', help='Visible DV release number')
    release.add_argument('--release-name', help='Exact visible DV release name')
    release.add_argument('--dv-vs-id', type=int)
    bv_release = parser.add_mutually_exclusive_group()
    bv_release.add_argument('--bv-release-name', help='Exact BV release name, if multiple versions exist')
    bv_release.add_argument('--bv-vs-id', type=int, help='Advanced alternative to --bv-release-name')
    parser.add_argument('--database', help='Optional deployment database label for output; not used for API requests')
    parser.add_argument('--bdv-database', help='Defaults to --database')
    parser.add_argument('--rdv-schema', help='Override release SCHEMA_FL parameter')
    parser.add_argument('--bdv-schema', help='Override release SCHEMA_BV parameter')
    parser.add_argument('--out', type=Path, required=True, help='New output directory; existing directories are rejected')
    parser.add_argument('--snapshot-dir', type=Path, help='Offline replay of a captured API snapshot')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--token-file', type=Path, default=ROOT / 'token')
    parser.add_argument('--base-url')
    parser.add_argument('--page-size', type=int, default=500)
    parser.add_argument('--workers', type=int, default=4, help='Parallel GET requests for source metadata (default 4)')
    parser.add_argument('--no-derived-objects', action='store_true',
                        help='Skip parameter-derived objects (LAS temp and FMC tables); '
                             'needed to replay snapshots taken without project parameters')
    parser.add_argument('--no-source-lineage', action='store_true',
                        help='Skip source-object lineage (needed to replay snapshots taken without it)')
    args = parser.parse_args(argv)
    if args.dv_id is None and (not args.project or not args.data_vault):
        parser.error('Supply --project and --data-vault (or advanced --dv-id).')
    if args.dv_id is not None and (args.project is not None or args.data_vault is not None):
        parser.error('--dv-id cannot be combined with --project or --data-vault.')
    if args.page_size < 1:
        parser.error('--page-size must be positive')
    if not 1 <= args.workers <= 16:
        parser.error('--workers must be between 1 and 16')
    try:
        def make_api(raw_dir):
            if args.snapshot_dir:
                return SnapshotAPI(args.snapshot_dir)
            base, token = credentials(args)
            return LiveAPI(base, token, raw_dir, args.page_size, args.workers)
        summary = run_inventory(args, args.out, make_api)
        print(f'Wrote {summary["expected_objects"]} expected modeled objects to {summary["output"]}')
        print(json.dumps(summary['counts'], sort_keys=True))
        if summary['source_lineage']:
            print('Source lineage: ' + json.dumps(summary['source_lineage'], sort_keys=True))
        print(f'{summary["review_objects"]} definitions need review; Snowflake deployment has not been checked.')
        return 0
    except (InventoryError, OSError, ValueError, KeyError, TypeError) as error:
        # Never include raw HTTP errors/headers or credentials in error output.
        message = str(error) if isinstance(error, InventoryError) else f'Invalid input or response ({type(error).__name__}).'
        print('ERROR: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
