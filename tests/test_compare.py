import unittest

try:
    import pandas  # noqa: F401
except ImportError:  # compare.py needs the TUI requirements
    pandas = None

if pandas is not None:
    from compare import compare, compare_inventories


EXPORT_A = """source_name;dv_table_name;dv_column_name;data_type;dv_vs_id
SRC;H_CUSTOMER;CUSTOMER_BK;VARCHAR(10);1
SRC;H_CUSTOMER;LOAD_DATE;TIMESTAMP;1
SRC;S_CUSTOMER;NAME;VARCHAR(50);1
"""
EXPORT_B = """source_name;dv_table_name;dv_column_name;data_type;dv_vs_id
SRC;H_CUSTOMER;CUSTOMER_BK;VARCHAR(20);2
SRC;H_CUSTOMER;LOAD_DATE;TIMESTAMP;2
SRC;S_CUSTOMER;EMAIL;VARCHAR(100);2
"""

INV_HEADER = ("database_name,schema_name,object_name,object_type,layer,vaultspeed_type,dv_id,dv_vs_id,"
              "bv_vs_id,tab_dv_vs_id,tab_dv_business_view_id,expected_basis,incr_source_objects\n")
INV_A = INV_HEADER + (
    "DB,RDV,H_CUSTOMER,BASE TABLE,RDV,Hubs,1,10,30,100,,current_dv_overview,CDC.CUSTOMER\n"
    "DB,RDV,S_CUSTOMER,BASE TABLE,RDV,Satellites,1,10,30,101,,current_dv_overview,CDC.CUSTOMER\n"
    "DB,BDV,H_CUSTOMER_VIEW,VIEW,BDV,Business View,1,10,30,100,200,business_view_generate_true,\"CDC.A; CDC.B\"\n"
)
INV_B = INV_HEADER + (
    "DB,RDV,H_CUSTOMER,BASE TABLE,RDV,Hubs,1,11,31,110,,current_dv_overview,CDC.CUSTOMER\n"
    "DB,RDV,S_CUSTOMER,BASE TABLE,RDV,Satellites,1,11,31,111,,current_dv_overview,CDC.CUSTOMER_V2\n"
    "DB,RDV,L_ORDER,BASE TABLE,RDV,Links,1,11,31,112,,current_dv_overview,CDC.ORDER\n"
)


@unittest.skipIf(pandas is None, "pandas not installed (pip install -r requirements.txt)")
class CompareExportsTest(unittest.TestCase):
    def test_metadata_export_defaults(self):
        result = compare(EXPORT_A, EXPORT_B, "A", "B")
        self.assertEqual(result.key_columns, ["source_name", "dv_table_name", "dv_column_name"])
        self.assertNotIn("dv_vs_id", result.all_columns)  # release ID is ignored
        self.assertEqual((len(result.added), len(result.removed), len(result.changed), result.unchanged_count),
                         (1, 1, 1, 1))
        self.assertEqual(result.changed.iloc[0]["data_type_a"], "VARCHAR(10)")
        self.assertEqual(result.changed.iloc[0]["data_type_b"], "VARCHAR(20)")
        self.assertIn("Metadata Export Comparison", result.to_markdown())
        self.assertIn('"generated_at"', result.to_json())

    def test_missing_key_columns_is_an_error(self):
        with self.assertRaises(ValueError):
            compare("a;b\n1;2\n", "a;b\n1;3\n", key_columns=["missing"])


@unittest.skipIf(pandas is None, "pandas not installed (pip install -r requirements.txt)")
class CompareInventoriesTest(unittest.TestCase):
    def test_release_ids_ignored_and_objects_matched_by_schema_and_name(self):
        result = compare_inventories(INV_A, INV_B, "Release 1", "Release 2")
        self.assertEqual(result.key_columns, ["schema_name", "object_name"])
        for column in ("dv_vs_id", "bv_vs_id", "tab_dv_vs_id", "tab_dv_business_view_id", "dv_id", "database_name"):
            self.assertNotIn(column, result.all_columns)
        self.assertEqual(list(result.added["object_name"]), ["L_ORDER"])
        self.assertEqual(list(result.removed["object_name"]), ["H_CUSTOMER_VIEW"])
        self.assertEqual(list(result.changed["object_name"]), ["S_CUSTOMER"])
        self.assertEqual(result.unchanged_count, 1)  # H_CUSTOMER differs only in IDs
        self.assertEqual(result.title, "Snowflake Inventory Comparison")
        self.assertIn("incr_source_objects", result.to_json())


if __name__ == "__main__":
    unittest.main()
