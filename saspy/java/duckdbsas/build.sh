#!/usr/bin/env bash
# Build the DuckDBSASDriver shim jar and install it (with the DuckDB JDBC
# driver) into the SAS deployment's JDBC DataDrivers directory, which is the
# only classpath location SAS/ACCESS Interface to JDBC will load drivers from.
#
# Prereqs: a JDK (javac), curl, sudo rights for the install step.
# Usage:   ./build.sh [duckdb_jdbc_version]
set -euo pipefail

VER="${1:-1.5.4.0}"
SASJDBC="/usr/local/SASHome/AccessClients/9.4/DataDrivers/jdbc/duckdb"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

JAR="duckdb_jdbc-${VER}.jar"
if [ ! -f "$WORK/$JAR" ]; then
    echo "downloading org.duckdb:duckdb_jdbc:$VER from Maven Central..."
    curl -sfo "$WORK/$JAR" \
      "https://repo1.maven.org/maven2/org/duckdb/duckdb_jdbc/${VER}/${JAR}"
fi

mkdir -p "$WORK/classes"
javac --release 21 -cp "$WORK/$JAR" -d "$WORK/classes" \
      "$HERE/DuckDBSASDriver.java"
jar cf "$WORK/duckdb-sas-shim.jar" -C "$WORK/classes" org

echo "installing to $SASJDBC (sudo)..."
sudo mkdir -p "$SASJDBC"
sudo cp "$WORK/$JAR" "$WORK/duckdb-sas-shim.jar" "$SASJDBC/"
echo "done:"
ls -la "$SASJDBC/"
