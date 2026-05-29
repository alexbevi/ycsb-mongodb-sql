# smongo benchmark harness

This repository wraps YCSB 0.17.0, the latest upstream YCSB release artifact,
so the same workload can run against:

- `smongo` through the smongo MongoDB wire server
- `mongodb` through a Docker MongoDB instance
- `ferretdb` through a Docker FerretDB instance
- `documentdb` through the local DocumentDB Docker image from documentdb.io
- `postgresql` and `mysql` through Docker JDBC targets
- `sqlite` through the YCSB JDBC binding
- `jdbc` through a supplied JDBC driver, URL, and driver jar

YCSB is downloaded into `.bench/` on first use and is not checked in. Docker
targets use `docker compose`; `ferretdb` and `documentdb` also compile a small
modern MongoDB binding, so they require `javac` on `PATH`.

## Quick start

From the parent `mdb-embedded` checkout:

```bash
tools/smongo-bench --target smongo --record-count 1000 --operation-count 1000
tools/smongo-bench --target mongodb --record-count 1000 --operation-count 1000
tools/smongo-bench --target ferretdb --record-count 1000 --operation-count 1000
tools/smongo-bench --target postgresql --record-count 1000 --operation-count 1000
tools/smongo-bench --target mysql --record-count 1000 --operation-count 1000
tools/smongo-bench --target sqlite --record-count 1000 --operation-count 1000
```

From this submodule directly:

```bash
python benchmark.py --target sqlite --record-count 1000 --operation-count 1000
```

The default action is `all`, which runs YCSB `load` followed by `run`.
Results are written under `results/<target>/<timestamp>/`.

## Common options

```bash
python benchmark.py --target smongo --workload workloada --threads 4
python benchmark.py --target mongodb --uri 'mongodb://127.0.0.1:27017/ycsb?w=1'
python benchmark.py --target ferretdb --uri 'mongodb://username:password@127.0.0.1:27019/ycsb?w=1'
python benchmark.py --target documentdb
python benchmark.py --target documentdb --uri "$DOCUMENTDB_URI"
python benchmark.py --target jdbc --jdbc-driver org.postgresql.Driver \
  --jdbc-url 'jdbc:postgresql://127.0.0.1:5432/ycsb' \
  --jdbc-user ycsb --jdbc-password ycsb --jdbc-jar ./postgresql.jar
```

`documentdb` starts `ghcr.io/documentdb/documentdb/documentdb-local:latest`,
publishes port `10260`, and connects with the local image's self-signed TLS
certificate accepted via `tlsAllowInvalidCertificates=true`. Use `--uri` or
`DOCUMENTDB_URI` to point at an already running DocumentDB endpoint instead.
The target uses the modern MongoDB binding rather than the stock YCSB MongoDB
binding so newer MongoDB-compatible servers do not have to support legacy
`OP_QUERY` handshakes.
Use repeated `--java-opt` values if your endpoint needs JVM TLS settings such
as a Java truststore.

The generic `jdbc` target is for SQL engines beyond the built-in `sqlite`,
`postgresql`, and `mysql` targets. Supply the JDBC driver class, URL, user,
password, and one or more comma-separated local paths or URLs in `--jdbc-jar`.
The built-in SQL targets create/reset the YCSB table automatically; generic
JDBC targets write `schema.sql` next to `jdbc.properties` in the result
directory so you can initialize the table with the target database's own SQL
client before running `load`/`run`.

Docker-backed targets start their service with `docker compose up -d <service>`.
Use `--no-docker` when you already have the target running.
`--dry-run` prints the resolved YCSB command without starting services, resetting
SQL tables, or running YCSB.
If Docker image pulls are slow, raise `--docker-start-timeout`; the default is
600 seconds.
When `DOCKER_CONFIG` is unset, the harness uses `.bench/docker-config` with
empty auths and a symlink to the existing Docker CLI plugins so public Docker
Hub pulls do not depend on a local credential helper.
