import unittest

from saspy.sasduckdb import (segment_code, _iter_statements,
                             classify_sql_statement, rewrite_sql,
                             parse_proc_options)
from saspy.sasexceptions import (SASDuckDBNotSupportedError, SASDuckDBMacroError,
                                 SASDuckDBExecutionError)

try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False


class TestSegmentationAndParsing(unittest.TestCase):
    """Pure-Python parsing tests: no SAS session, no duckdb connection."""

    def test_pure_sas_one_segment(self):
        code = "data work.a; x=1; run;\nproc print data=work.a; run;\n"
        segs = segment_code(code)
        self.assertEqual([s.kind for s in segs], ['sas'])
        self.assertEqual(segs[0].text, code)

    def test_sql_block_basic_ordering(self):
        code = ("data w.a; x=1; run;\n"
                "proc sql;\n create table w.b as select * from w.a;\nquit;\n"
                "proc print data=w.b; run;\n")
        segs = segment_code(code)
        self.assertEqual([s.kind for s in segs], ['sas', 'sql', 'sas'])
        self.assertTrue(segs[1].text.lstrip().lower().startswith('proc sql'))
        self.assertTrue(segs[1].text.rstrip().lower().endswith('quit;'))
        self.assertEqual(segs[1].dialect, 'sql')
        # one body statement (the create table)
        self.assertEqual(len(segs[1].stmts), 1)

    def test_quoted_quit_not_terminator(self):
        code = ("proc sql;\n"
                "  create table w.x as select * from t where note = 'quit;';\n"
                "quit;\n")
        segs = segment_code(code)
        self.assertEqual([s.kind for s in segs], ['sql'])
        self.assertEqual(len(segs[0].stmts), 1)
        self.assertIn("'quit;'", segs[0].stmts[0].text)

    def test_comments_containing_quit(self):
        code = ("proc sql;\n"
                "  /* quit; not real */\n"
                "  * quit; \n"
                "  select a from t;\n"
                "quit;\n")
        segs = segment_code(code)
        self.assertEqual([s.kind for s in segs], ['sql'])
        # star comment is one statement, select is another
        kinds = [s.words[:1] for s in segs[0].stmts]
        self.assertIn(('*',), kinds)
        self.assertIn(('select',), kinds)

    def test_implicit_quit_at_step_boundary(self):
        code = ("proc sql;\n  create table w.x as select 1 as a;\n"
                "data w.y; set w.x; run;\n")
        segs = segment_code(code)
        self.assertEqual([s.kind for s in segs], ['sql', 'sas'])
        self.assertNotIn('data', segs[0].text.lower())
        self.assertTrue(segs[1].text.lstrip().lower().startswith('data'))

    def test_fedsql_detected(self):
        code = "proc fedsql;\n select a from t;\nquit;\n"
        segs = segment_code(code)
        self.assertEqual(segs[0].kind, 'sql')
        self.assertEqual(segs[0].dialect, 'fedsql')

    def test_datalines_not_scanned(self):
        code = ("data w.raw;\n input s $char20.;\n datalines;\n"
                "proc sql; quit;\n"
                "another line\n"
                ";\n"
                "run;\n"
                "proc sql; select a from w.raw; quit;\n")
        segs = segment_code(code)
        self.assertEqual([s.kind for s in segs], ['sas', 'sql'])
        self.assertIn('proc sql; quit;', segs[0].text)  # data, untouched

    def test_datalines4_variant(self):
        code = ("data w.raw;\n input s $char10.;\n cards4;\n"
                "x;y;z\n"
                ";;;;\n"
                "run;\n")
        segs = segment_code(code)
        self.assertEqual([s.kind for s in segs], ['sas'])

    def test_sql_in_macro_def_raises(self):
        code = ("%macro m;\n proc sql; select 1; quit;\n%mend;\n")
        with self.assertRaises(SASDuckDBNotSupportedError):
            segment_code(code)

    def test_sql_after_macro_def_ok(self):
        code = ("%macro m; data w.a; run; %mend;\n"
                "proc sql; select 1 as a; quit;\n")
        segs = segment_code(code)
        self.assertEqual([s.kind for s in segs], ['sas', 'sql'])

    def test_proc_options_captured(self):
        code = "proc sql noprint stimer;\n select a into :x from t;\nquit;\n"
        segs = segment_code(code)
        self.assertEqual(segs[0].proc_options.lower().split(), ['noprint', 'stimer'])

    def test_missing_quit_at_eof(self):
        code = "data w.a; run;\nproc sql;\n select a from w.a;\n"
        segs = segment_code(code)
        self.assertEqual([s.kind for s in segs], ['sas', 'sql'])
        self.assertEqual(len(segs[1].stmts), 1)

    def test_consecutive_sql_blocks(self):
        code = ("proc sql; create table w.a as select 1 as x; quit;\n"
                "proc sql; create table w.b as select 2 as y; quit;\n")
        segs = segment_code(code)
        self.assertEqual([s.kind for s in segs], ['sql', 'sql'])

    def test_double_quoted_string_with_semicolon(self):
        code = 'data w.a; s = "a;b"; run;\nproc sql; select 1; quit;\n'
        segs = segment_code(code)
        self.assertEqual([s.kind for s in segs], ['sas', 'sql'])
        self.assertIn('"a;b"', segs[0].text)


def _one_stmt(sql_body):
    """Helper: classify the single body statement of a proc sql block."""
    segs = segment_code('proc sql;\n' + sql_body + '\nquit;\n')
    assert segs[0].kind == 'sql' and len(segs[0].stmts) == 1
    return classify_sql_statement(segs[0].stmts[0])


class TestClassifierAndRewriter(unittest.TestCase):

    # --- classification ---

    def test_ctas_qualified(self):
        c = _one_stmt('create table mylib.out as select a, b from t;')
        self.assertEqual(c.kind, 'ctas')
        self.assertEqual((c.libref, c.table), ('mylib', 'out'))
        self.assertTrue(c.select_sql.lower().startswith('select'))

    def test_ctas_unqualified_and_cte(self):
        c = _one_stmt('create table out as with x as (select 1 as a) '
                      'select * from x;')
        self.assertEqual(c.kind, 'ctas')
        self.assertEqual((c.libref, c.table), ('', 'out'))
        self.assertTrue(c.select_sql.lower().startswith('with'))

    def test_ctas_dsopts_raises(self):
        with self.assertRaises(SASDuckDBNotSupportedError):
            _one_stmt('create table out(compress=yes) as select 1 as a;')

    def test_ctas_like_raises(self):
        with self.assertRaises(SASDuckDBNotSupportedError):
            _one_stmt('create table out like other;')

    def test_create_view_raises(self):
        with self.assertRaises(SASDuckDBNotSupportedError):
            _one_stmt('create view v as select 1;')

    def test_into_single(self):
        c = _one_stmt('select count(*) into :n from t;')
        self.assertEqual(c.kind, 'select_into')
        self.assertEqual(c.into_specs, [('n', None, False)])
        self.assertNotIn('into', c.select_sql.lower())
        self.assertIn('from t', c.select_sql)

    def test_into_multi(self):
        c = _one_stmt('select a, b into :x, :y from t;')
        self.assertEqual([s[0] for s in c.into_specs], ['x', 'y'])

    def test_into_separated_and_trimmed(self):
        c = _one_stmt("select name into :names separated by ',' trimmed from t;")
        self.assertEqual(c.into_specs, [('names', ',', True)])

    def test_into_range_raises(self):
        with self.assertRaises(SASDuckDBNotSupportedError):
            _one_stmt('select a into :v1-:v9 from t;')

    def test_into_notrim_raises(self):
        with self.assertRaises(SASDuckDBNotSupportedError):
            _one_stmt('select a into :v notrim from t;')

    def test_into_inside_subquery_not_into(self):
        # 'into' at depth>0 must not be treated as an INTO clause
        c = _one_stmt('select a from (select 1 as a) q;')
        self.assertEqual(c.kind, 'select')

    def test_bare_select(self):
        c = _one_stmt('select a, calculated b from t;')
        self.assertEqual(c.kind, 'select')

    def test_let_passthrough(self):
        c = _one_stmt('%let cut = 10;')
        self.assertEqual(c.kind, 'sas_passthrough')

    def test_unsupported_statements_raise(self):
        for bad in ('insert into t values (1);', 'update t set a=1;',
                    'delete from t;', 'drop table t;', 'alter table t add b int;',
                    'describe t;', 'validate select 1;', 'reset print;'):
            with self.assertRaises(SASDuckDBNotSupportedError, msg=bad):
                _one_stmt(bad)

    # --- proc options ---

    def test_proc_options(self):
        o = parse_proc_options('noprint stimer')
        self.assertTrue(o['noprint'])
        self.assertEqual(o['ignored'], ['stimer'])
        with self.assertRaises(SASDuckDBNotSupportedError):
            parse_proc_options('outobs=5')
        with self.assertRaises(SASDuckDBNotSupportedError):
            parse_proc_options('bogusopt')

    # --- rewriting ---

    def test_rewrite_macro_resolution(self):
        vals = {'cut': '42', 'nm': "O'Hare"}
        out = rewrite_sql('select * from t where x > &cut. and y = "&nm"',
                          resolver=lambda n: vals[n])
        self.assertIn('x > 42', out)
        self.assertIn("'O''Hare'", out)

    def test_rewrite_single_quotes_untouched(self):
        out = rewrite_sql("select '&notamacro' as s from t",
                          resolver=lambda n: (_ for _ in ()).throw(AssertionError))
        self.assertIn("'&notamacro'", out)

    def test_rewrite_indirect_macro_raises(self):
        with self.assertRaises(SASDuckDBMacroError):
            rewrite_sql('select * from t where x > &&ind', resolver=lambda n: '1')

    def test_rewrite_comment_not_resolved(self):
        out = rewrite_sql('select a /* &junk */ from t', resolver=lambda n: 'X')
        self.assertIn('&junk', out)

    def test_rewrite_dquote_to_squote(self):
        out = rewrite_sql('select "it''s" as s, "plain" as p from t')
        self.assertIn("'plain'", out)

    def test_rewrite_name_literals(self):
        out = rewrite_sql("select 'my col'n, \"other col\"n from t")
        self.assertIn('"my col"', out)
        self.assertIn('"other col"', out)

    def test_rewrite_date_constants(self):
        out = rewrite_sql("select * from t where d >= '01jan2020'd "
                          "and ts < '15Mar2021:13:45:00'dt and tm = '12:30:00't")
        self.assertIn("DATE '2020-01-01'", out)
        self.assertIn("TIMESTAMP '2021-03-15 13:45:00.000000'", out)
        self.assertIn("TIME '12:30:00.000000'", out)

    def test_rewrite_two_digit_year_raises(self):
        with self.assertRaises(SASDuckDBNotSupportedError):
            rewrite_sql("select * from t where d = '01jan20'd")

    def test_rewrite_calculated_stripped(self):
        out = rewrite_sql('select a+1 as b, calculated b * 2 as c from t')
        self.assertNotIn('calculated', out.lower())
        self.assertIn('b * 2', out)


_FIXTURE = r"""
data work.claims;
  length pt $8 state $2 note $40;
  do i = 1 to 12;
    pt = cats('P', put(i, z3.));
    state = choosec(mod(i, 3) + 1, 'CA', 'OR', 'WA');
    note = choosec(mod(i, 4) + 1, 'plain', 'has,comma', 'has"quote', ' ');
    amt = i * 10.5;
    if mod(i, 5) = 0 then amt = .;
    svc_date = '01jan2024'd + i;
    svc_dt = '01jan2024:08:00:00'dt + i * 3600 + 0.25;
    if mod(i, 6) = 0 then svc_date = .;
    output;
  end;
  drop i;
  format svc_date date9. svc_dt datetime26.6;
run;
"""


@unittest.skipIf(not DUCKDB_AVAILABLE, "duckdb is not installed")
class TestSasToDuckDBCsv(unittest.TestCase):
    transport = 'csv'
    kwargs = {}

    @classmethod
    def setUpClass(cls):
        import saspy
        cls.sas = saspy.SASsession(results='text')
        cls.sas.set_batch(True)
        ll = cls.sas.submit(_FIXTURE, results='text')
        assert 'ERROR' not in ll['LOG'], ll['LOG']

    @classmethod
    def tearDownClass(cls):
        cls.sas._endsas()

    def setUp(self):
        self.con = duckdb.connect() if self.transport == 'csv' else None

    def tearDown(self):
        if self.con is not None:
            self.con.close()

    def _run(self, code, **kw):
        args = dict(con=self.con, transport=self.transport)
        args.update(self.kwargs)
        args.update(kw)
        return self.sas.sas_to_duckdb(code, **args)

    def _rowcount(self, table, libref='work'):
        ll = self.sas.submit(
            'proc sql noprint; select count(*) into :_tc from {}.{}; quit;'
            .format(libref, table), results='text')
        return int(self.sas.symget('_tc'))

    def test_pure_sas_passthrough(self):
        ll = self._run('data work._p1; x = 40 + 2; run;')
        self.assertIn('_P1', ll['LOG'].upper())
        self.assertEqual(self._rowcount('_p1'), 1)

    def test_ctas_from_shipped_sas_table(self):
        ll = self._run("""
        proc sql;
          create table work.agg1 as
            select state, count(*) as n, sum(amt) as amt_sum
            from work.claims group by state order by state;
        quit;
        """)
        self.assertEqual(self._rowcount('agg1'), 3)
        df = self.sas.sd2df('agg1')
        self.assertEqual(list(df['state']), ['CA', 'OR', 'WA'])
        self.assertEqual(int(df['n'].sum()), 12)

    def test_mixed_ordering_and_macros(self):
        ll = self._run("""
        %let cut = 60;
        proc sql;
          create table work.overcut as
            select pt, amt from work.claims where amt > &cut;
        quit;
        data work.over2; set work.overcut; amt2 = amt * 2; run;
        """)
        n = self._rowcount('over2')
        self.assertGreater(n, 0)
        df = self.sas.sd2df('overcut')
        self.assertTrue((df['amt'] > 60).all())

    def test_select_into_variants(self):
        self._run("""
        proc sql noprint;
          select count(*), max(amt) into :ntot, :maxamt from work.claims;
          select distinct state into :states separated by '|'
            from work.claims order by state;
        quit;
        """)
        self.assertEqual(int(self.sas.symget('ntot')), 12)
        self.assertEqual(float(self.sas.symget('maxamt')), 126.0)
        self.assertEqual(self.sas.symget('states'), 'CA|OR|WA')

    def test_bare_select_render_and_noprint(self):
        ll = self._run("""
        proc sql;
          select state, count(*) as n from work.claims group by state;
        quit;
        """)
        self.assertIn('CA', ll['LST'])
        ll2 = self._run("""
        proc sql noprint;
          select state, count(*) as n from work.claims group by state;
        quit;
        """)
        self.assertEqual(ll2['LST'].strip(), '')

    def test_noprint_select_still_executes(self):
        # NOPRINT suppresses output only: the query must run, surface errors,
        # and set SQLOBS. Native SAS sets SQLOBS=1 for a destination-less
        # NOPRINT select; both transports match that.
        self._run("""
        proc sql noprint;
          select state from work.claims;
        quit;
        """)
        self.assertEqual(int(self.sas.symget('SQLOBS')), 1)
        with self.assertRaises(SASDuckDBExecutionError):
            self._run('proc sql noprint; '
                      'select no_such_col from work.claims; quit;')

    def test_timestamp_and_time_variants(self):
        self._run("""
        proc sql;
          create table work.tsv as select
            '2021-06-01 08:09:10.123456789'::TIMESTAMP_NS as ts_ns,
            '2021-06-01 08:09:10.123'::TIMESTAMP_MS       as ts_ms,
            '2021-06-01 08:09:10'::TIMESTAMP_S            as ts_s,
            '2021-06-01 08:09:10.5+00'::TIMESTAMPTZ       as ts_tz,
            '14:30:00.25+02'::TIMETZ                      as tm_tz;
        quit;
        """)
        df = self.sas.sd2df('tsv')
        self.assertEqual(str(df['ts_ns'][0]), '2021-06-01 08:09:10.123456')
        self.assertEqual(str(df['ts_ms'][0]), '2021-06-01 08:09:10.123000')
        self.assertEqual(str(df['ts_s'][0]), '2021-06-01 08:09:10')
        # tz-naive conversion happens in DuckDB's session timezone
        chk = duckdb.connect()
        want_tz = str(chk.execute(
            "select ('2021-06-01 08:09:10.5+00'::TIMESTAMPTZ)::timestamp"
        ).fetchone()[0])
        want_tm = str(chk.execute(
            "select ('14:30:00.25+02'::TIMETZ)::time").fetchone()[0])
        chk.close()
        self.assertEqual(str(df['ts_tz'][0]), want_tz)
        self.assertIn(want_tm.split('.')[0], str(df['tm_tz'][0]))

    def test_fedsql_block(self):
        ll = self._run("""
        proc fedsql;
          create table work.fed1 as select state from work.claims where amt > 100;
        quit;
        """)
        self.assertGreater(self._rowcount('fed1'), 0)

    def test_external_parquet_passthrough(self):
        import tempfile, os
        d = tempfile.mkdtemp(dir='/dev/shm')
        pq = os.path.join(d, 'ext.parquet')
        ddb = duckdb.connect()
        ddb.execute("copy (select range as k, 'x' || range as v from range(50)) "
                    "to '{}' (format parquet)".format(pq))
        ddb.close()
        try:
            ll = self._run("""
            proc sql;
              create table work.frompq as
                select * from read_parquet('{}') where k < 10;
            quit;
            """.format(pq))
            self.assertEqual(self._rowcount('frompq'), 10)
            self.assertNotIn('shipped SAS table', ll['LOG'])
        finally:
            os.remove(pq)
            os.rmdir(d)

    def test_date_datetime_roundtrip(self):
        self._run("""
        proc sql;
          create table work.dts as
            select pt, svc_date, svc_dt from work.claims
            where svc_date is not null order by pt;
        quit;
        """)
        df = self.sas.sd2df('dts')
        orig = self.sas.sd2df('claims')
        orig = orig[orig['svc_date'].notna()].sort_values('pt').reset_index()
        self.assertEqual(list(df['svc_date']), list(orig['svc_date']))
        self.assertEqual(list(df['svc_dt']), list(orig['svc_dt']))

    def test_null_and_special_chars_roundtrip(self):
        self._run("""
        proc sql;
          create table work.rt as select pt, note, amt from work.claims order by pt;
        quit;
        """)
        df = self.sas.sd2df('rt')
        orig = self.sas.sd2df('claims').sort_values('pt').reset_index()
        self.assertEqual(list(df['note'].fillna('')), list(orig['note'].fillna('')))
        self.assertEqual(df['amt'].isna().sum(), orig['amt'].isna().sum())

    def test_unsupported_statement_raises(self):
        from saspy.sasexceptions import SASDuckDBNotSupportedError
        with self.assertRaises(SASDuckDBNotSupportedError):
            self._run('proc sql; insert into work.claims (pt) values (\'x\'); quit;')

    def test_undefined_macro_raises(self):
        from saspy.sasexceptions import SASDuckDBMacroError
        with self.assertRaises(SASDuckDBMacroError):
            self._run('proc sql; select * from work.claims '
                      'where amt > &no_such_macro_xyz; quit;')

    def test_duckdb_error_surfaced(self):
        try:
            self._run('proc sql; create table work.bad1 as '
                      'select no_such_col from work.claims; quit;')
        except SASDuckDBExecutionError as e:
            self.assertIn('no_such_col', str(e))
        else:
            self.fail('expected SASDuckDBExecutionError')

    def test_sas_error_stops_by_default(self):
        from saspy.sasexceptions import SASDuckDBSASCodeError
        with self.assertRaises(SASDuckDBSASCodeError):
            self._run('data work.oops; set work.does_not_exist_xyz; run;')

    def test_nosub_returns_plan(self):
        self.sas.teach_me_SAS(True)
        try:
            ll = self._run("""
            data work.x1; x=1; run;
            proc sql; create table work.y1 as select * from work.x1; quit;
            """)
        finally:
            self.sas.teach_me_SAS(False)
        self.assertIn('SAS segment', ll['LOG'])
        self.assertIn('DuckDB', ll['LOG'])
        self.assertFalse(self.sas.exist('y1', 'work'))

    def test_sqlobs_set(self):
        self._run("""
        proc sql;
          create table work.sq as select * from work.claims where state = 'CA';
        quit;
        """)
        self.assertEqual(int(self.sas.symget('SQLOBS')), 4)

    def test_large_smoke_100k(self):
        self.sas.submit("""
        data work.big;
          do id = 1 to 100000;
            grp = mod(id, 100); val = ranuni(42) * 1000; output;
          end;
        run;
        """, results='text')
        self._run("""
        proc sql;
          create table work.bigagg as
            select grp, count(*) as n, avg(val) as m
            from work.big group by grp order by grp;
        quit;
        """)
        self.assertEqual(self._rowcount('bigagg'), 100)
        df = self.sas.sd2df('bigagg')
        self.assertEqual(int(df['n'].sum()), 100000)


_JDBC_CP = '/usr/local/SASHome/AccessClients/9.4/DataDrivers/jdbc/duckdb'
_JDBC_READY = (DUCKDB_AVAILABLE and
               __import__('os').path.exists(_JDBC_CP + '/duckdb-sas-shim.jar'))


@unittest.skipIf(not _JDBC_READY, 'duckdb JDBC shim not installed')
class TestSasToDuckDBJdbc(TestSasToDuckDBCsv):
    """Same scenarios through the jdbc transport (SAS/ACCESS pass-through)."""
    transport = 'jdbc'
    kwargs = {'jdbc_text_len': 64}

    # jdbc mode renders via SAS ODS; the DUCKDB annotation stream and duckdb
    # exceptions surface differently
    def test_duckdb_error_surfaced(self):
        try:
            self._run('proc sql; create table work.bad1 as '
                      'select no_such_col from work.claims; quit;')
        except SASDuckDBExecutionError as e:
            self.assertIn('no_such_col', str(e).lower())
        else:
            self.fail('expected SASDuckDBExecutionError')

    def test_null_and_special_chars_roundtrip(self):
        self._run("""
        proc sql;
          create table work.rt as select pt, note, amt from work.claims order by pt;
        quit;
        """)
        df = self.sas.sd2df('rt')
        orig = self.sas.sd2df('claims').sort_values('pt').reset_index()
        # jdbc mode: char columns come back at jdbc_text_len; compare stripped
        got = [str(x).strip() if x is not None else '' for x in df['note'].fillna('')]
        want = [str(x).strip() for x in orig['note'].fillna('')]
        self.assertEqual(got, want)
        self.assertEqual(df['amt'].isna().sum(), orig['amt'].isna().sum())

    def test_timestamp_and_time_variants(self):
        # timestamp variants fetch fine through the JDBC driver; TIMETZ
        # triggers a driver NPE - documented: cast it (::time) in your SQL
        self._run("""
        proc sql;
          create table work.tsv as select
            '2021-06-01 08:09:10.123456789'::TIMESTAMP_NS as ts_ns,
            '2021-06-01 08:09:10.123'::TIMESTAMP_MS       as ts_ms,
            '2021-06-01 08:09:10'::TIMESTAMP_S            as ts_s;
        quit;
        """)
        df = self.sas.sd2df('tsv')
        self.assertEqual(str(df['ts_ns'][0]), '2021-06-01 08:09:10.123456')
        self.assertEqual(str(df['ts_ms'][0]), '2021-06-01 08:09:10.123000')
        self.assertEqual(str(df['ts_s'][0]), '2021-06-01 08:09:10')
        with self.assertRaises(SASDuckDBExecutionError):
            self._run("proc sql; create table work.tsx as select "
                      "'14:30:00.25+02'::TIMETZ as tm; quit;")
        self._run("""
        proc sql;
          create table work.tsx as select '14:30:00.25+02'::TIMETZ::time as tm;
        quit;
        """)
        df = self.sas.sd2df('tsx')
        self.assertIn(':30:00', str(df['tm'][0]))


if __name__ == '__main__':
    unittest.main()
