# DuckDB JDBC shim for SAS/ACCESS Interface to JDBC

Used by `SASsession.sas_to_duckdb(..., transport='jdbc')`.

DuckDB's JDBC driver reports `getColumnDisplaySize()=0` and
`getPrecision()=Integer.MAX_VALUE` for VARCHAR columns. SAS/ACCESS to JDBC
turns that into `char(1)` columns with a broken `$.` format and a fetch
exception. `DuckDBSASDriver` is a delegating driver that wraps
`org.duckdb.DuckDBDriver` and reports a fixed, URL-configurable length for
character columns. It also enables DuckDB's chunk-streamed result sets
(`jdbc_stream_results=true`) by default so large pass-through reads do not
materialize entirely in the SAS JVM.

URL syntax:

    jdbc:duckdbsas:/path/to/db.duckdb;sas_text_len=1024

Build and install (downloads the DuckDB JDBC driver, compiles the shim, and
copies both jars into the SAS deployment's sanctioned JDBC driver directory —
SAS refuses `CLASSPATH=` values outside it):

    ./build.sh            # uses duckdb_jdbc 1.5.4.0
    ./build.sh 1.6.0.0    # or another driver version

Known jdbc-transport limitations (documented in sasduckdb.py): character
columns all get `sas_text_len` bytes in SAS (no per-column lengths exist in
DuckDB metadata); TIME values lose sub-second precision (java.sql.Time).
