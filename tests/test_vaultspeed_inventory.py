import argparse
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import vaultspeed_inventory as inv


def fixture():
    def obj(name, oid, kind):
        return {'table_name': name, 'tab_dv_vs_id': oid, 'dv_vs_id': 10, 'object_type': kind}
    def view(name, alias, oid):
        return {'table_name': name, 'business_name': alias, 'tab_dv_business_view_id': oid,
                'bv_vs_id': 30, 'generate': True, 'tab_type_desc': 'Business View'}
    def member(status, original_id):
        return {'src_name': 'SOURCE', 'status': status, 'hub_table_name_orig': 'H_CHILD',
                'tab_src_dv_id_orig': original_id, 'tab_dv_id_orig': original_id + 1000,
                'hub_tab_elements_id': original_id + 2000}
    data = {
        'release': {'dv_id': 1, 'dv_vs_id': 10, 'vs_dv_number': '7', 'vs_dv_name': 'Release Seven'},
        'bv_release': {'dv_id': 1, 'dv_vs_id': 10, 'bv_vs_id': 30, 'bv_vs_name': 'init'},
        'parameters': [{'parameter_code': 'SCHEMA_FL', 'parameter_value': 'RDV'},
                       {'parameter_code': 'SCHEMA_BV', 'parameter_value': 'BDV'}],
        'catalog': [obj('H_GROUP', 100, 'Hubs'), obj('SH_CURRENT', 101, 'Satellites'),
                    obj('LAS_CROSS', 102, 'Links across sources'), obj('L_OLD', 103, 'Links'),
                    obj('R_NO_VIEW', 104, 'References')],
        'overview': [obj('H_GROUP', 100, 'Hubs'), obj('SH_SOURCE_NAME', 101, 'Satellites'),
                     obj('R_NO_VIEW', 104, 'References')],
        'views': [view('H_GROUP', 'H_ALIAS', 200), view('SH_CURRENT', 'SH_ALIAS', 201),
                  view('LAS_CROSS', 'view_LAS_CROSS', 202)],
        'groups': [{'dv_vs_id': 10, 'hub_group_id': 300, 'tab_dv_id': 400,
                    'hub_table_name': 'H_GROUP', 'hubs': [member('E', 501), member('D', 500)]}],
        'sources': [{'src_id': 2, 'src_name': 'SOURCE'}],
        'source_releases': [{'dv_vs_id': 10, 'src_id': 2, 'src_vs_id': 20}],
        'adjacency': [{'dv_vs_id': 10, 'bv_vs_id': 30, 'tab_dv_vs_id': 100,
                       'droplist': [{'adjacent_hub_tab_dv_vs_id': 100, 'adjacent_link_tab_dv_vs_id': 102}]}],
        'object_types': [{'tab_type_id': i, 'tab_type': code, 'tab_type_desc': kind,
                          'layer_type_short_name': layer} for i, code, kind, layer in
                         [(2, 'HUB', 'Hubs', 'FL'), (3, 'SAT', 'Satellites', 'FL'),
                          (4, 'LNK', 'Links', 'FL'), (42, 'REF', 'References', 'FL'),
                          (53, 'LNA', 'Links across sources', 'BV'),
                          (54, 'LAS', 'Satellites on Links across sources', 'BV'), (55, 'LAS_TEMP', None, 'BV'),
                          (45, 'BVLWT', None, 'FMC'), (57, 'BVLCI', None, 'FMC'), (58, 'DVLCI', None, 'FMC'),
                          (61, 'FMCH', None, 'FMC'), (76, 'FMCOH', None, 'FMC')]],
    }
    hubs = [{'tab_src_dv_id': 501, 'table_name': 'H_CHILD', 'tab_src_dv_vs_id': 600,
             'src_table_name': 'SPLIT_CHILD', 'src_vs_id': 20}]
    for row in data['overview']:
        row['tab_src_dv_vs_id'] = row.pop('tab_dv_vs_id') + 1000
    return data, hubs


class FakeAPI:
    def __init__(self, hubs):
        self.hubs = hubs

    def get(self, path, paginated=True):
        if path != '/source/release/20/hubs/2':
            raise AssertionError(path)
        return self.hubs


def arguments():
    return argparse.Namespace(dv_id=1, database='TargetDb', bdv_database=None,
                              rdv_schema=None, bdv_schema=None)


def project_parameters(**overrides):
    values = {'SCHEMA_FMC': 'FMC_S', 'TEMP_TABLE_PREFIX': 'TMP', 'TEMP_TABLE_SUFFIX': None,
              'LOAD_CYCLE_INFO_TABLE_NAME': 'LCI_T', 'FMC_BV_LOADING_WINDOW_TABLE_NAME': 'BV_LW_T',
              'FMC_LOADING_HIST_TABLE_NAME': 'HIST_T', 'FMC_OBJECT_LOADING_HIST_TABLE_NAME': 'OBJ_HIST_T',
              'FMC_LOADING_WINDOW_TABLE_NAME': 'LW_T'}
    values.update(overrides)
    return [{'parameter_code': k, 'parameter_value': v} for k, v in values.items()]


def system_parameters(use_fmc='Y'):
    return [{'parameter_code': 'USE_FMC', 'parameter_value': use_fmc}]


DV_HELPER_PARAMETERS = [{'parameter_code': 'FMC_TYPE', 'parameter_value': 'Airflow'},
                        {'parameter_code': 'STORE_TEMP_IN_STAGING', 'parameter_value': 'Y'}]


def save_snapshot(path, data, hubs):
    routes = {
        '/projects': [{'project_id': 5, 'project_name': 'Project A'}],
        '/data-vaults': [{'project_id': 99, 'dv_name': 'Vault A', 'dv_id': 2},
                         {'project_id': 5, 'dv_name': 'Vault A', 'dv_id': 1}],
        '/data-vault/1/releases': [data['release']],
        '/data-vaults/1/business-vaults/releases': [data['bv_release']],
        '/parameters/10/data-vault': data['parameters'] + [dict(r) for r in DV_HELPER_PARAMETERS],
        '/parameters/10/project': project_parameters(),
        '/parameters/10/system': system_parameters(),
        '/metadata/object-types': data['object_types'],
        '/data-vaults/release/10/objects': data['catalog'],
        '/data-vault/10/overview/objects': data['overview'],
        '/business-vault/30/business-views': data['views'],
        '/business-vault/30/adjacent-hubs': data['adjacency'],
        '/data-vault/10/hub-groups': data['groups'],
        '/sources': data['sources'],
        '/data-vault/10/sources': data['source_releases'],
        '/source/release/20/hubs/2': hubs,
        '/parameters/10/source': [
            {'src_name': 'SOURCE', 'parameter_code': 'SCHEMA_CDC', 'parameter_value': 'LAND.SOURCE'},
            {'src_name': 'SOURCE', 'parameter_code': 'SCHEMA_INI', 'parameter_value': 'SOURCE_INI'},
            {'src_name': 'SOURCE', 'parameter_code': 'SCHEMA_STG', 'parameter_value': 'SOURCE_STG'}],
        '/source/release/20/objects': [{'table_name': 'SPLIT_CHILD', 'table_name_src': 'CHILD_TABLE'}],
        '/source/release/20/hubs/3': [],
        '/source/release/20/hubs/4': [],
        '/source/release/20/hubs/42': [],
    }
    path.mkdir()
    entries = {}
    for i, (endpoint, value) in enumerate(routes.items()):
        payload = json.dumps(value).encode()
        filename = f'{i}.json'
        (path / filename).write_bytes(payload)
        entries[endpoint] = {'file': filename, 'sha256': inv.digest(payload)}
    (path / 'snapshot.json').write_text(json.dumps({'base_url': 'https://example.invalid/api', 'endpoints': entries}))


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.data, self.hubs = fixture()
        self.args = arguments()

    def build(self):
        return inv.transform(FakeAPI(self.hubs), self.data, self.args)

    def test_physical_layer_and_alias_identity(self):
        result = self.build()
        objects = {(r['schema_name'], r['object_name']): r for r in result['objects']}
        self.assertEqual(len(objects), 7)
        self.assertNotIn(('RDV', 'H_CHILD'), objects)
        self.assertNotIn(('RDV', 'LAS_CROSS'), objects)
        self.assertEqual(objects['BDV', 'LAS_CROSS']['object_type'], 'BASE TABLE')
        alias = objects['BDV', 'VIEW_LAS_CROSS']  # business name view_LAS_CROSS, uppercased like the ETL
        self.assertEqual((alias['object_type'], alias['underlying_schema']), ('VIEW', 'BDV'))
        self.assertEqual(objects['RDV', 'H_GROUP']['database_name'], 'TargetDb')

    def test_overview_source_name_is_not_a_physical_expectation(self):
        result = self.build()
        names = {r['object_name'] for r in result['objects']}
        self.assertNotIn('SH_SOURCE_NAME', names)
        self.assertIn('SH_CURRENT', names)
        self.assertEqual(result['overview_variants'][0]['table_name'], 'SH_SOURCE_NAME')

    def test_disabled_alias_does_not_remove_underlying_table(self):
        self.data['views'][1]['generate'] = False
        names = {r['object_name'] for r in self.build()['objects']}
        self.assertIn('SH_CURRENT', names)
        self.assertNotIn('SH_ALIAS', names)

    def test_current_table_without_business_view_is_retained(self):
        self.assertIn('R_NO_VIEW', {r['object_name'] for r in self.build()['objects']})

    def test_historical_catalog_record_is_review_only(self):
        result = self.build()
        self.assertNotIn('L_OLD', {r['object_name'] for r in result['objects']})
        self.assertEqual(result['review'][0]['object_name'], 'L_OLD')

    def test_group_members_with_same_name_keep_status_and_identity(self):
        members = self.build()['members']
        self.assertEqual(len(members), 2)
        self.assertEqual(members[0]['resolution'], 'exact_id_match')
        self.assertEqual(members[1]['resolution'], 'no_current_source_hub_match')
        self.assertNotEqual(members[0]['tab_src_dv_id_orig'], members[1]['tab_src_dv_id_orig'])

    def test_unresolved_E_member_aborts(self):
        self.hubs.clear()
        with self.assertRaises(inv.InventoryError):
            self.build()

    def test_wrong_source_release_aborts(self):
        self.hubs[0]['src_vs_id'] = 999
        with self.assertRaises(inv.InventoryError):
            self.build()

    def test_alias_collision_aborts(self):
        self.data['views'][1]['business_name'] = 'H_ALIAS'
        with self.assertRaises(inv.InventoryError):
            self.build()

    def test_unknown_current_type_aborts(self):
        self.data['object_types'] = self.data['object_types'][1:]
        with self.assertRaises(inv.InventoryError):
            self.build()

    def test_separate_bdv_database_and_schema(self):
        self.args.bdv_database, self.args.bdv_schema = 'BusinessDb', 'MixedCase'
        for row in self.build()['objects']:
            if row['layer'] == 'BDV':
                self.assertEqual((row['database_name'], row['schema_name']), ('BusinessDb', 'MixedCase'))

    def test_cross_source_dependency_uses_bdv_schema(self):
        edges = self.build()['dependencies']
        edge = next(e for e in edges if e['relationship'] == 'link_references_hub')
        self.assertEqual((edge['parent_schema'], edge['dependent_schema']), ('RDV', 'BDV'))

    def test_optional_database_leaves_names_blank(self):
        self.args.database = None
        result = self.build()
        self.assertTrue(all(r['database_name'] == '' for r in result['objects']))
        self.assertTrue(all(r['underlying_database'] == '' for r in result['objects']))
        self.assertTrue(all(r['parent_database'] == r['dependent_database'] == ''
                            for r in result['dependencies']))


class DerivedObjectTests(unittest.TestCase):
    def setUp(self):
        self.data, self.hubs = fixture()
        self.data['catalog'].append({'table_name': 'SLAS_CROSS_SRC', 'tab_dv_vs_id': 105, 'dv_vs_id': 10,
                                     'object_type': 'Satellites on Links across sources'})
        self.data['views'].append({'table_name': 'SLAS_CROSS_SRC', 'business_name': 'V_SLAS', 'bv_vs_id': 30,
                                   'tab_dv_business_view_id': 203, 'generate': True, 'tab_type_desc': 'Business View'})
        self.data['parameters'] += [dict(r) for r in DV_HELPER_PARAMETERS]
        self.data['project_parameters'] = project_parameters()
        self.data['system_parameters'] = system_parameters()
        self.args = arguments()

    def build(self):
        result = inv.transform(FakeAPI(self.hubs), self.data, self.args)
        return result, {(r['schema_name'], r['object_name']): r for r in result['objects']}

    def test_las_temp_table_in_bv_schema(self):
        result, rows = self.build()
        temp = rows['BDV', 'TMP_SLAS_CROSS_SRC']
        self.assertEqual((temp['vaultspeed_type'], temp['basis_type']), ('LAS_TEMP', 'parameter_rule'))
        self.assertNotIn(('BDV', 'TMP_LAS_CROSS'), rows)  # links across sources get no temp table
        self.assertEqual(rows['RDV', 'H_GROUP']['basis_type'], 'api_metadata')
        self.assertIn('load_temp_table_of', {e['relationship'] for e in result['dependencies']})
        self.assertEqual(result['counts']['bdv_las_temp_tables'], 1)

    def test_fmc_tables_named_from_parameters(self):
        result, rows = self.build()
        fmc = sorted(name for schema, name in rows if schema == 'FMC_S')
        self.assertEqual(fmc, ['BV_LW_T', 'DV_LCI_T', 'HIST_T', 'LCI_T', 'OBJ_HIST_T'])  # no LW_T
        self.assertTrue(all(rows['FMC_S', n]['layer'] == 'FMC' for n in fmc))
        self.assertEqual(result['schemas']['FMC'], 'FMC_S')
        self.assertFalse([r for r in result['review'] if r['category'] == 'unverified_configuration'])

    def test_fmc_disabled_and_unverified_settings_are_reported(self):
        self.data['system_parameters'] = system_parameters('N')
        self.data['parameters'][-1]['parameter_value'] = 'N'  # STORE_TEMP_IN_STAGING
        result, rows = self.build()
        self.assertFalse([k for k in rows if k[0] == 'FMC_S'])
        flagged = {r['object_name'] for r in result['review'] if r['category'] == 'unverified_configuration'}
        self.assertEqual(flagged, {'USE_FMC', 'STORE_TEMP_IN_STAGING'})

    def test_skipped_without_project_parameters(self):
        self.data['project_parameters'] = None
        result, rows = self.build()
        self.assertTrue(all(r['basis_type'] == 'api_metadata' for r in result['objects']))


class RouteAPI:
    def __init__(self, routes):
        self.routes = routes

    def get(self, path, paginated=True):
        if path not in self.routes:
            raise AssertionError(path)
        return self.routes[path]

    def get_many(self, paths, paginated=True):
        return {p: self.get(p) for p in paths}


def lineage_fixture():
    """One source release exercising every lineage rule."""
    def obj(name, oid, kind, short, releases):
        return {'table_name': name, 'tab_dv_vs_id': oid, 'dv_vs_id': 10, 'object_type': kind,
                'table_short_name': short, 'src_vs_ids': releases}
    def ov(name, kind, sid):
        return {'table_name': name, 'object_type': kind, 'dv_vs_id': 10, 'status': None, 'tab_src_dv_vs_id': sid}
    def view(name, oid):
        return {'table_name': name, 'business_name': 'V_' + name, 'tab_dv_business_view_id': oid,
                'bv_vs_id': 30, 'generate': True, 'tab_type_desc': 'Business View'}
    def d(sid, dv_id, name, source_object):
        return {'tab_src_dv_vs_id': sid, 'tab_src_dv_id': dv_id, 'table_name': name,
                'src_table_name': source_object, 'src_vs_id': 20}
    def fk(*parent_ids):
        return [{'constraint_type': 'R', 'r_tab_src_dv_vs_id': p} for p in parent_ids]
    sat, hub, lnd = 'Satellites on Hubs', 'Hubs', 'Object Based Links'
    lna, las = 'Links across sources', 'Satellites on Links across sources'
    catalog = [obj('H_GROUP', 100, hub, 'GROUP', [20, 20]), obj('H_OTHER', 101, hub, 'OTHER', [20]),
               obj('SH_GROUP_SRC', 102, sat, 'GROUP', [20]), obj('SH_B_A_SRC', 103, sat, 'B', [20]),
               obj('LOBJ_X', 104, lnd, 'X', [20, 20]), obj('LAS_OTHER_GROUP', 105, lna, 'OG', [20]),
               obj('SLAS_OTHER_GROUP_SRC', 106, las, 'OG', [20])]
    data = {
        'release': {'dv_id': 1, 'dv_vs_id': 10}, 'bv_release': {'bv_vs_id': 30},
        'parameters': [{'parameter_code': 'SCHEMA_FL', 'parameter_value': 'RDV'},
                       {'parameter_code': 'SCHEMA_BV', 'parameter_value': 'BDV'}],
        'catalog': catalog,
        'overview': [ov('H_GROUP', hub, 700), ov('H_OTHER', hub, 701), ov('SH_CHILD_SRC', sat, 702),
                     ov('SH_A_B_SRC', sat, 703), ov('LOBJ_X', lnd, 704)],
        'views': [view(o['table_name'], 200 + i) for i, o in enumerate(catalog)],
        'groups': [{'dv_vs_id': 10, 'hub_group_id': 1, 'tab_dv_id': 2, 'hub_table_name': 'H_GROUP', 'hubs': [
            {'src_name': 'SRC', 'status': 'E', 'hub_table_name_orig': n, 'tab_src_dv_id_orig': i,
             'tab_dv_id_orig': i, 'hub_tab_elements_id': i} for n, i in (('H_GROUP_A', 501), ('H_CHILD', 502))]}],
        'sources': [{'src_id': 2, 'src_name': 'SRC'}],
        'source_releases': [{'dv_vs_id': 10, 'src_id': 2, 'src_vs_id': 20}],
        'adjacency': [{'dv_vs_id': 10, 'bv_vs_id': 30, 'tab_dv_vs_id': 101,
                       'droplist': [{'adjacent_hub_tab_dv_vs_id': 100, 'adjacent_link_tab_dv_vs_id': 105}]}],
        'object_types': [{'tab_type_id': i, 'tab_type': code, 'tab_type_desc': kind, 'layer_type_short_name': layer}
                         for i, code, kind, layer in [(2, 'HUB', hub, 'FL'), (3, 'SAT', sat, 'FL'),
                                                      (6, 'LND', lnd, 'FL'), (53, 'LNA', lna, 'BV'),
                                                      (54, 'LAS', las, 'BV')]],
    }
    hubs = [d(700, 501, 'H_GROUP_A', 'OBJ_A'), d(707, 502, 'H_CHILD', 'OBJ_B'), d(701, 503, 'H_OTHER', 'OBJ_C')]
    routes = {
        '/source/release/20/hubs/2': hubs,
        '/source/release/20/hubs/3': [d(702, 504, 'SH_CHILD_SRC', 'OBJ_B'), d(703, 505, 'SH_A_B_SRC', 'OBJ_C')],
        '/source/release/20/hubs/6': [d(704, 506, 'LOBJ_X', 'OBJ_D'), d(705, 507, 'LOBJ_Y', 'OBJ_E')],
        '/source/release/20/objects': [{'table_name': n, 'table_name_src': t} for n, t in
                                       (('OBJ_A', 'T_A'), ('OBJ_B', 'T_A'), ('OBJ_C', 'T_C'),
                                        ('OBJ_D', 'T_D'), ('OBJ_E', 'T_E'))],
        '/parameters/10/source': [{'src_name': 'SRC', 'parameter_code': c, 'parameter_value': v} for c, v in
                                  (('SCHEMA_CDC', 'LAND.SRC'), ('SCHEMA_INI', 'SRC_INI'), ('SCHEMA_STG', 'SRC_STG'))],
        '/source/sdvv/relationships/704': fk(700, 701),
        '/source/sdvv/relationships/705': fk(707, 701),
        '/source/sdvv/relationships/702': fk(707),
    }
    return data, routes


class LineageTests(unittest.TestCase):
    def build(self):
        data, routes = lineage_fixture()
        api = RouteAPI(routes)
        result = inv.transform(api, data, argparse.Namespace(dv_id=1, database=None, bdv_database=None,
                                                            rdv_schema=None, bdv_schema=None))
        lineage = inv.source_lineage(api, data, result)
        return {r['object_name']: r for r in result['objects']}, lineage, result

    def test_every_rule_resolves_without_review(self):
        rows, lineage, result = self.build()
        self.assertEqual(lineage['counts']['resolved_definitions'], lineage['counts']['source_definitions'])
        self.assertEqual(lineage['counts']['resolution_rules'], {
            'hub_group_member': 2, 'overview_id': 2, 'overview_id_name_tokens': 1,
            'link_member_hub_set': 1, 'satellite_parent': 1})
        self.assertFalse([r for r in result['review'] if r['category'] != 'catalog_only_definition'])

    def test_grouped_hub_lists_every_member_source(self):
        rows, _, _ = self.build()
        group = rows['H_GROUP']
        self.assertEqual(group['source_count'], 2)
        self.assertEqual(group['incr_source_objects'], 'LAND.SRC.T_A')  # two splits of one physical table
        self.assertEqual(group['init_source_objects'], 'SRC_INI.T_A')
        self.assertEqual(group['stg_objects'], 'SRC_STG.OBJ_A; SRC_STG.OBJ_B')
        self.assertEqual(rows['V_H_GROUP']['incr_source_objects'], group['incr_source_objects'])

    def test_satellites_links_and_cross_source_objects(self):
        rows, _, _ = self.build()
        self.assertEqual(rows['SH_GROUP_SRC']['stg_objects'], 'SRC_STG.OBJ_B')
        self.assertEqual(rows['SH_GROUP_SRC']['source_resolution'], 'satellite_parent')
        self.assertEqual(rows['SH_B_A_SRC']['incr_source_objects'], 'LAND.SRC.T_C')
        self.assertEqual(rows['LOBJ_X']['incr_source_objects'], 'LAND.SRC.T_D; LAND.SRC.T_E')
        self.assertEqual(rows['LAS_OTHER_GROUP']['incr_source_objects'], 'LAND.SRC.T_A; LAND.SRC.T_C')
        self.assertEqual(rows['LAS_OTHER_GROUP']['source_resolution'], 'inferred_cross_source_adjacent_hubs')
        self.assertEqual(rows['SLAS_OTHER_GROUP_SRC']['incr_source_objects'], rows['LAS_OTHER_GROUP']['incr_source_objects'])

    def test_ambiguous_candidates_stay_unresolved(self):
        data, routes = lineage_fixture()
        data['catalog'].append({'table_name': 'SH_GROUP_OTHER_SRC', 'tab_dv_vs_id': 107, 'dv_vs_id': 10,
                                'object_type': 'Satellites on Hubs', 'table_short_name': 'G', 'src_vs_ids': [20]})
        data['views'].append({'table_name': 'SH_GROUP_OTHER_SRC', 'business_name': 'V_X', 'tab_dv_business_view_id': 299,
                              'bv_vs_id': 30, 'generate': True, 'tab_type_desc': 'Business View'})
        api = RouteAPI(routes)
        result = inv.transform(api, data, argparse.Namespace(dv_id=1, database=None, bdv_database=None,
                                                            rdv_schema=None, bdv_schema=None))
        inv.source_lineage(api, data, result)
        categories = Counter(r['category'] for r in result['review'])
        self.assertEqual(categories['unresolved_source_definition'], 1)
        rows = {r['object_name']: r for r in result['objects']}
        self.assertEqual(rows['SH_GROUP_SRC']['source_resolution'], 'unresolved')


class CollectionTests(unittest.TestCase):
    def test_pagination_handles_server_cap_and_continues_until_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = inv.LiveAPI('https://example.invalid/api', 'not-a-secret', Path(tmp), page_size=2)
            calls = []
            def request(path, params):
                offset = json.loads(params['filter'])['skip']
                calls.append(offset)
                return [{'id': offset}] if offset < 3 else []
            api.request = request
            with redirect_stderr(io.StringIO()):
                self.assertEqual(len(api.get('/objects')), 3)
            self.assertEqual(calls, [0, 1, 2, 3])
            replay = inv.SnapshotAPI(Path(tmp))
            self.assertEqual(replay.get('/objects'), [{'id': 0}, {'id': 1}, {'id': 2}])

    def test_repeated_page_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = inv.LiveAPI('https://example.invalid/api', 'not-a-secret', Path(tmp), page_size=2)
            api.request = lambda path, params: [{'id': 1}]
            with self.assertRaises(inv.InventoryError):
                api.get('/objects')

    def test_modified_snapshot_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'snapshot'
            data, hubs = fixture()
            save_snapshot(root, data, hubs)
            index = json.loads((root / 'snapshot.json').read_text())
            filename = index['endpoints']['/data-vault/1/releases']['file']
            (root / filename).write_text('[]')
            with self.assertRaises(inv.InventoryError):
                inv.SnapshotAPI(root).get('/data-vault/1/releases')

    def test_cli_offline_publishes_complete_outputs_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, hubs = fixture()
            save_snapshot(root / 'snapshot', data, hubs)
            args = ['--dv-id', '1', '--release-number', '7', '--database', 'DB',
                    '--snapshot-dir', str(root / 'snapshot'), '--out', str(root / 'out')]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(inv.main(args), 0)
                # Existing output cannot be silently replaced.
                self.assertEqual(inv.main(args), 1)
            manifest = json.loads((root / 'out/manifest.json').read_text())
            self.assertEqual(manifest['expected_objects'], 12)  # 7 modeled + 5 FMC tables
            self.assertEqual(manifest['counts']['fmc_tables'], 5)
            self.assertFalse(manifest['deployment_verified'])
            with (root / 'out/expected_objects.csv').open() as handle:
                rows = {r['object_name']: r for r in csv.DictReader(handle)}
            for name in ('H_GROUP', 'H_ALIAS'):
                self.assertEqual(rows[name]['incr_source_objects'], 'LAND.SOURCE.CHILD_TABLE')
                self.assertEqual(rows[name]['init_source_objects'], 'SOURCE_INI.CHILD_TABLE')
                self.assertEqual(rows[name]['stg_objects'], 'SOURCE_STG.SPLIT_CHILD')
            self.assertTrue((root / 'out/object_sources.csv').exists())
            # Older snapshots without lineage endpoints replay with --no-source-lineage only.
            older = [a for a in args if a != str(root / 'out')] + [str(root / 'older')]
            index = json.loads((root / 'snapshot/snapshot.json').read_text())
            del index['endpoints']['/parameters/10/source']
            del index['endpoints']['/parameters/10/project']
            del index['endpoints']['/parameters/10/system']
            (root / 'snapshot/snapshot.json').write_text(json.dumps(index))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(inv.main(older), 1)
                self.assertEqual(inv.main(older + ['--no-source-lineage']), 1)
                self.assertEqual(inv.main(older + ['--no-source-lineage', '--no-derived-objects']), 0)
            with (root / 'older/expected_objects.csv').open() as handle:
                self.assertTrue(all(r['incr_source_objects'] == '' for r in csv.DictReader(handle)))
            self.assertFalse((root / 'older/object_sources.csv').exists())
            for filename, checksum in manifest['file_sha256'].items():
                self.assertEqual(inv.digest((root / 'out' / filename).read_bytes()), checksum)

    def test_cli_failed_mapping_does_not_publish_partial_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, hubs = fixture()
            save_snapshot(root / 'snapshot', data, [])
            args = ['--dv-id', '1', '--dv-vs-id', '10', '--database', 'DB',
                    '--snapshot-dir', str(root / 'snapshot'), '--out', str(root / 'out')]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(inv.main(args), 1)
            self.assertFalse((root / 'out').exists())

    def test_multiple_bv_versions_require_explicit_selection(self):
        data, hubs = fixture()
        class API:
            def get(self, path, paginated=True):
                return [data['release']] if path.endswith('/1/releases') else [data['bv_release']] * 2
        args = argparse.Namespace(dv_id=1, dv_vs_id=10, release_number=None, bv_vs_id=None)
        with self.assertRaises(inv.InventoryError):
            inv.collect(API(), args)


class SelectionTests(unittest.TestCase):
    def test_name_based_cli_and_snapshot_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, hubs = fixture()
            save_snapshot(root / 'snapshot', data, hubs)
            common = ['--project', 'Project A', '--data-vault', 'Vault A',
                      '--database', 'DB', '--snapshot-dir', str(root / 'snapshot')]
            for i, selector in enumerate([['--release-name', 'Release Seven'],
                                           ['--release-number', '7', '--bv-release-name', 'init']]):
                output = root / str(i)
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(inv.main(common + selector + ['--out', str(output)]), 0)
                manifest = json.loads((output / 'manifest.json').read_text())
                self.assertEqual(manifest['data_vault']['dv_id'], 1)
                self.assertEqual(manifest['project']['project_name'], 'Project A')
            self.assertEqual((root / '0' / 'expected_objects.csv').read_bytes(),
                             (root / '1' / 'expected_objects.csv').read_bytes())

    def test_cli_metadata_export_without_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, hubs = fixture()
            save_snapshot(root / 'snapshot', data, hubs)
            args = ['--project', 'Project A', '--data-vault', 'Vault A',
                    '--release-number', '7', '--snapshot-dir', str(root / 'snapshot'),
                    '--out', str(root / 'output')]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(inv.main(args), 0)
            with (root / 'output/expected_objects.csv').open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 12)  # includes 5 FMC tables
            self.assertTrue(all(r['database_name'] == '' for r in rows))
            manifest = json.loads((root / 'output/manifest.json').read_text())
            self.assertIsNone(manifest['database'])
            self.assertFalse(list((root / 'output').glob('*.sql')))
            self.assertFalse((root / 'output/reconciliation.csv').exists())

    def test_missing_or_duplicate_names_fail_with_choices(self):
        for rows, name in [([{'name': 'Available'}], 'Typo'),
                           ([{'name': 'Same'}, {'name': 'Same'}], 'Same')]:
            with self.assertRaisesRegex(inv.InventoryError, 'Available values:'):
                inv.select_record(rows, 'name', name, 'Selection')

    def test_bv_name_scoped_to_selected_dv_release(self):
        data, _ = fixture()
        selected = dict(data['bv_release'], bv_vs_id=31, bv_vs_name='Reviewed')
        other = dict(selected, bv_vs_id=99, dv_vs_id=99)
        class API:
            def get(self, path, paginated=True):
                if path == '/data-vault/1/releases':
                    return [data['release']]
                if path == '/data-vaults/1/business-vaults/releases':
                    return [data['bv_release'], selected, other]
                return []
        args = argparse.Namespace(dv_id=1, dv_vs_id=None, release_number=None,
                                  release_name='Release Seven', bv_vs_id=None,
                                  bv_release_name='Reviewed')
        self.assertEqual(inv.collect(API(), args)['bv_release']['bv_vs_id'], 31)
        args.bv_release_name = None
        with self.assertRaisesRegex(inv.InventoryError, '--bv-release-name.*Reviewed'):
            inv.collect(API(), args)


class AuthenticationTests(unittest.TestCase):
    def test_login_posts_json_and_returns_token(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"access_token":"session-token"}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(inv, 'build_opener', return_value=opener) as factory:
            self.assertEqual(inv.login('https://example.invalid/api', 'api-user', 'secret'), 'session-token')
        self.assertIsInstance(factory.call_args.args[0], inv.NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://example.invalid/api/login')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(json.loads(request.data), {'username': 'api-user', 'password': 'secret'})
        # urllib's default User-Agent is blocked by Cloudflare on some hosts.
        self.assertEqual(request.get_header('User-agent'), inv.USER_AGENT)

    def test_login_http_failure_redacts_details(self):
        opener = MagicMock()
        opener.open.side_effect = HTTPError('https://example.invalid', 401, 'secret', {}, None)
        with patch.object(inv, 'build_opener', return_value=opener):
            with self.assertRaises(inv.InventoryError) as error:
                inv.login('https://example.invalid', 'api-user', 'secret')
        self.assertIn('HTTP 401', str(error.exception))
        self.assertNotIn('secret', str(error.exception))

    def test_login_rejects_missing_token(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"message":"secret"}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(inv, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(inv.InventoryError, 'missing a valid access_token'):
                inv.login('https://example.invalid', 'api-user', 'secret')

    def test_credential_precedence_and_token_backup(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(inv.os.environ, {}, clear=True):
            folder = Path(temp)
            args = argparse.Namespace(env_file=folder / '.env', token_file=folder / 'token', base_url=None)
            args.env_file.write_text('VAULTSPEED_BASE_URL=https://example.invalid/api\nVAULTSPEED_USERNAME=user\nVAULTSPEED_PASSWORD=secret\n')
            args.token_file.write_text('Bearer backup-token')
            with patch.object(inv, 'login', return_value='login-token') as login:
                self.assertEqual(inv.credentials(args)[1], 'login-token')
                login.assert_called_once_with('https://example.invalid/api', 'user', 'secret')
            with patch.object(inv, 'login', side_effect=inv.InventoryError('Login failed')):
                with self.assertRaisesRegex(inv.InventoryError, 'Login failed'):
                    inv.credentials(args)
            args.env_file.write_text('VAULTSPEED_BASE_URL=https://example.invalid/api\nVAULTSPEED_USERNAME=user\n')
            with self.assertRaisesRegex(inv.InventoryError, 'Set both'):
                inv.credentials(args)
            args.env_file.write_text('VAULTSPEED_BASE_URL=https://example.invalid/api\n')
            self.assertEqual(inv.credentials(args)[1], 'backup-token')
            with patch.dict(inv.os.environ, {'VAULTSPEED_ACCESS_TOKEN': 'env-token'}):
                self.assertEqual(inv.credentials(args)[1], 'env-token')


if __name__ == '__main__':
    unittest.main()
