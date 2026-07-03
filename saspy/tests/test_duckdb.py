import glob
import os
import tempfile
import unittest

import saspy
from saspy.sasexceptions import (SASDuckDBError, SASDuckDBExecutionError,
                                 SASDuckDBTransferError)

try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False


@unittest.skipIf(not DUCKDB_AVAILABLE, "duckdb is not installed")
class TestDuckdbToSas(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sas = saspy.SASsession(results='text')
        cls.sas.set_batch(True)
        cls.ddb = duckdb.connect()
        cls.ddb.execute("""
            create table test_mixed as select * from (values
              (1, 'Alice', DATE '1985-03-15', 91.5, true,
               TIMESTAMP '2024-01-15 10:30:00.123456'),
              (2, 'Bob',   DATE '1990-07-04', 78.25, false,
               TIMESTAMP '2024-02-20 14:45:30'),
              (3, 'Carol', NULL, 88.0, true, NULL)
            ) t(id, name, birth_dt, score, active, created_ts)
        """)
        cls.ddb.execute("""
            create table test_chars as select * from (values
              ('00093721305', 'CA', 'active'),
              ('00006094885', 'OR', NULL),
              ('58160082111', 'WA', 'expired')
            ) t(ndc, state_cd, status)
        """)
        cls.ddb.execute("""
            create table test_large as
              select range as id, 'name_' || (range % 100) as name,
                     DATE '2020-01-01' + interval (range % 1000) day as svc_date,
                     (range % 10000) / 100.0 as amount
              from range(10000)
        """)

    @classmethod
    def tearDownClass(cls):
        cls.ddb.close()
        cls.sas._endsas()

    def _contents_lst(self, table):
        ll = self.sas.submit(
            'proc contents data=work.{}; run;'.format(table), results='text')
        return ll['LST']

    def test_returns_sasdata(self):
        sd = self.sas.duckdb_to_sas(self.ddb, 'test_mixed', table='ddb_inst')
        self.assertIsInstance(sd, saspy.SASdata)

    def test_row_count(self):
        self.sas.duckdb_to_sas(self.ddb, 'test_mixed', table='ddb_rows')
        ll = self.sas.submit('proc sql noprint; select count(*) into :n '
                             'from work.ddb_rows; quit;', results='text')
        self.assertEqual(int(self.sas.symget('n')), 3)

    def test_types_and_nulls_roundtrip(self):
        self.sas.duckdb_to_sas(self.ddb, 'test_mixed', table='ddb_types')
        df = self.sas.sd2df('ddb_types')
        self.assertEqual(list(df['name']), ['Alice', 'Bob', 'Carol'])
        self.assertEqual(str(df['birth_dt'][0])[:10], '1985-03-15')
        self.assertTrue(df['birth_dt'].isna()[2])       # Carol NULL date
        self.assertTrue(df['created_ts'].isna()[2])
        self.assertEqual(str(df['created_ts'][0]), '2024-01-15 10:30:00.123456')
        self.assertEqual(list(df['active']), [1, 0, 1])  # bool -> 1/0
        self.assertEqual(float(df['score'][1]), 78.25)

    def test_query_with_where(self):
        self.sas.duckdb_to_sas(
            self.ddb, 'select id, amount from test_large where id < 500',
            table='ddb_sub')
        df = self.sas.sd2df('ddb_sub')
        self.assertEqual(len(df), 500)

    def test_labels(self):
        self.sas.duckdb_to_sas(self.ddb, 'test_chars', table='ddb_lab',
                               labels={'ndc': 'National Drug Code',
                                       'STATE_CD': 'State'})
        lst = self._contents_lst('ddb_lab')
        self.assertIn('National Drug Code', lst)
        self.assertIn('State', lst)

    def test_outfmts(self):
        self.sas.duckdb_to_sas(self.ddb, 'test_chars', table='ddb_fmt',
                               outfmts={'ndc': '$11.', 'state_cd': '$2.'})
        lst = self._contents_lst('ddb_fmt')
        self.assertIn('$11.', lst)
        self.assertIn('$2.', lst)

    def test_outdsopts(self):
        self.sas.duckdb_to_sas(self.ddb, 'test_chars', table='ddb_opts',
                               outdsopts={'compress': 'yes'})
        lst = self._contents_lst('ddb_opts')
        self.assertIn('CHAR', lst.upper())   # Compressed CHAR in contents

    def test_explicit_char_lengths(self):
        self.sas.duckdb_to_sas(self.ddb, 'test_chars', table='ddb_cl',
                               char_lengths={'ndc': 11, 'state_cd': 2,
                                             'status': 10})
        df = self.sas.sd2df('ddb_cl')
        self.assertEqual(list(df['ndc'])[:2], ['00093721305', '00006094885'])

    def test_computed_char_lengths(self):
        self.sas.duckdb_to_sas(self.ddb, 'test_chars', table='ddb_ccl')
        df = self.sas.sd2df('ddb_ccl')
        # leading zeros preserved -> loaded as char, full 11 bytes
        self.assertEqual(list(df['ndc'])[:2], ['00093721305', '00006094885'])
        lst = self._contents_lst('ddb_ccl')
        self.assertIn('11', lst)

    def test_datetimes_coercion(self):
        self.sas.duckdb_to_sas(self.ddb, 'test_mixed', table='ddb_dt',
                               datetimes={'created_ts': 'date'})
        df = self.sas.sd2df('ddb_dt')
        self.assertEqual(str(df['created_ts'][0])[:10], '2024-01-15')
        lst = self._contents_lst('ddb_dt')
        self.assertIn('E8601DA', lst)

    def test_libref_target(self):
        self.sas.duckdb_to_sas(self.ddb, 'test_chars', table='ddb_lib',
                               libref='work')
        self.assertTrue(self.sas.exist('ddb_lib', 'work'))

    def test_unassigned_libref_raises(self):
        with self.assertRaises(SASDuckDBError):
            self.sas.duckdb_to_sas(self.ddb, 'test_chars', table='x',
                                   libref='nolib_xyz')

    def test_tempkeep(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm') as d:
            self.sas.duckdb_to_sas(self.ddb, 'test_chars', table='ddb_tk',
                                   tempdir=d, tempkeep=True)
            self.assertTrue(glob.glob(os.path.join(d, '*.csv')))
            self.sas.duckdb_to_sas(self.ddb, 'test_chars', table='ddb_tk2',
                                   tempdir=d, tempkeep=False)
            kept = glob.glob(os.path.join(d, '*.csv'))
            self.assertEqual(len(kept), 1)   # only the tempkeep=True one

    def test_empty_query_raises(self):
        with self.assertRaises(SASDuckDBError):
            self.sas.duckdb_to_sas(self.ddb, '')

    def test_bad_sql_raises(self):
        with self.assertRaises(SASDuckDBExecutionError):
            self.sas.duckdb_to_sas(self.ddb, 'select nope from test_mixed',
                                   table='never')

    def test_large_query(self):
        self.sas.duckdb_to_sas(self.ddb, 'test_large', table='ddb_big')
        ll = self.sas.submit('proc sql noprint; select count(*) into :n '
                             'from work.ddb_big; quit;', results='text')
        self.assertEqual(int(self.sas.symget('n')), 10000)

    def test_teach_me_sas_returns_code(self):
        self.sas.teach_me_SAS(True)
        try:
            code = self.sas.duckdb_to_sas(self.ddb, 'test_mixed',
                                          table='ddb_nosub')
        finally:
            self.sas.teach_me_SAS(False)
        self.assertIsInstance(code, str)
        self.assertIn('infile', code)
        self.assertIn('length', code)
        self.assertFalse(self.sas.exist('ddb_nosub', 'work'))


if __name__ == '__main__':
    unittest.main()
