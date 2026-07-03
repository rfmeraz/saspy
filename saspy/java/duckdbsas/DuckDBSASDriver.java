package org.saspy.duckdb;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.Driver;
import java.sql.DriverManager;
import java.sql.DriverPropertyInfo;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.SQLFeatureNotSupportedException;
import java.sql.Types;
import java.util.Properties;
import java.util.logging.Logger;

/**
 * Delegating JDBC driver for using DuckDB with SAS/ACCESS Interface to JDBC.
 *
 * DuckDB's driver reports getColumnDisplaySize()=0 and getPrecision()=Integer.MAX_VALUE
 * for VARCHAR columns, which SAS turns into char(1) columns with a broken "$." format.
 * This shim delegates everything to org.duckdb.DuckDBDriver but rewrites
 * ResultSetMetaData display size / precision for character columns to a fixed,
 * URL-configurable length.
 *
 * URL syntax:  jdbc:duckdbsas:<path>[;sas_text_len=N][;jdbc_stream_results=false]
 *   sas_text_len          reported char column length (bytes), default 1024
 *   jdbc_stream_results   forwarded to DuckDB as a connection property, default true
 */
public final class DuckDBSASDriver implements Driver {

    static final String PREFIX = "jdbc:duckdbsas:";
    static final int DEFAULT_TEXT_LEN = 1024;

    static {
        try {
            DriverManager.registerDriver(new DuckDBSASDriver());
        } catch (SQLException e) {
            throw new ExceptionInInitializerError(e);
        }
    }

    @Override
    public boolean acceptsURL(String url) {
        return url != null && url.startsWith(PREFIX);
    }

    @Override
    public Connection connect(String url, Properties info) throws SQLException {
        if (!acceptsURL(url)) {
            return null;
        }
        String rest = url.substring(PREFIX.length());
        int textLen = DEFAULT_TEXT_LEN;
        Properties props = new Properties();
        if (info != null) {
            props.putAll(info);
        }
        props.putIfAbsent("jdbc_stream_results", "true");

        StringBuilder path = new StringBuilder();
        for (String part : rest.split(";", -1)) {
            int eq = part.indexOf('=');
            String key = eq < 0 ? "" : part.substring(0, eq).trim().toLowerCase();
            if (key.equals("sas_text_len")) {
                try {
                    textLen = Math.max(1, Math.min(32767, Integer.parseInt(part.substring(eq + 1).trim())));
                } catch (NumberFormatException e) {
                    throw new SQLException("invalid sas_text_len in URL: " + part);
                }
            } else if (!key.isEmpty() && !key.contains("/") && !key.contains("\\")) {
                props.setProperty(key, part.substring(eq + 1).trim());
            } else {
                if (path.length() > 0) {
                    path.append(';');
                }
                path.append(part);
            }
        }

        Driver inner = new org.duckdb.DuckDBDriver();
        Connection c = inner.connect("jdbc:duckdb:" + path, props);
        if (c == null) {
            throw new SQLException("DuckDB driver refused URL: jdbc:duckdb:" + path);
        }
        return (Connection) wrap(c, Connection.class, textLen);
    }

    /** Recursively proxy JDBC interfaces so every ResultSetMetaData gets fixed. */
    static Object wrap(Object target, Class<?> iface, int textLen) {
        InvocationHandler h = (proxy, method, args) -> {
            Object r;
            try {
                r = method.invoke(target, args);
            } catch (InvocationTargetException e) {
                throw e.getCause();
            }
            if (r == null) {
                return null;
            }
            Class<?> rt = method.getReturnType();
            if (rt == ResultSetMetaData.class) {
                return wrap(r, ResultSetMetaData.class, textLen);
            }
            if (rt.isInterface() && rt.getName().startsWith("java.sql.") && rt != java.sql.Array.class) {
                return wrap(r, rt, textLen);
            }
            return r;
        };
        if (iface == ResultSetMetaData.class) {
            ResultSetMetaData md = (ResultSetMetaData) target;
            h = (proxy, method, args) -> {
                String name = method.getName();
                if ((name.equals("getColumnDisplaySize") || name.equals("getPrecision")) && args != null) {
                    int col = (Integer) args[0];
                    int t = md.getColumnType(col);
                    if (t == Types.VARCHAR || t == Types.CHAR || t == Types.LONGVARCHAR
                            || t == Types.NVARCHAR || t == Types.NCHAR || t == Types.LONGNVARCHAR) {
                        return textLen;
                    }
                }
                try {
                    return method.invoke(md, args);
                } catch (InvocationTargetException e) {
                    throw e.getCause();
                }
            };
        }
        return Proxy.newProxyInstance(DuckDBSASDriver.class.getClassLoader(), new Class<?>[]{iface}, h);
    }

    @Override
    public int getMajorVersion() {
        return 1;
    }

    @Override
    public int getMinorVersion() {
        return 0;
    }

    @Override
    public boolean jdbcCompliant() {
        return false;
    }

    @Override
    public DriverPropertyInfo[] getPropertyInfo(String url, Properties info) {
        return new DriverPropertyInfo[0];
    }

    @Override
    public Logger getParentLogger() throws SQLFeatureNotSupportedException {
        throw new SQLFeatureNotSupportedException();
    }
}
