# YCSB MongoDB and SQL benchmark harness

This repository wraps YCSB so the same workload can be run against
MongoDB-compatible and SQL targets, then compared side by side.

Supported targets:

- `mongodb` through a Docker MongoDB instance
- `ferretdb` through a Docker FerretDB instance
- `documentdb` through the local DocumentDB image from
  [documentdb.io](https://documentdb.io/)
- `postgresql` and `mysql` through Docker JDBC targets
- `sqlite` through the YCSB JDBC binding
- `jdbc` through a supplied JDBC driver, URL, and driver jar
- `smongo` when this repository is used from an `mdb-embedded` checkout

YCSB is downloaded into `.bench/` on first use and is not checked in. The
version comes from `YCSB_VERSION` in `benchmark.py`; it is currently `0.17.0`
because that is the latest GitHub release published by upstream YCSB. When
upstream publishes a newer release artifact, update `YCSB_VERSION`, clear
`.bench/`, and rerun the target smoke tests.

## Requirements

- Python 3.11+
- `pymongo` for MongoDB-compatible server version detection in comparison
  tables
- Java runtime for YCSB
- `javac` for `ferretdb` and `documentdb`, which compile a small modern MongoDB
  binding at runtime
- Docker with Compose for `mongodb`, `ferretdb`, `documentdb`, `postgresql`,
  and `mysql`

## Run one target

From this repository:

```bash
python benchmark.py --source sqlite --record-count 1000 --operation-count 1000
python benchmark.py --source mongodb --record-count 1000 --operation-count 1000
python benchmark.py --source postgresql --record-count 1000 --operation-count 1000
python benchmark.py --source mysql --record-count 1000 --operation-count 1000
python benchmark.py --source ferretdb --record-count 1000 --operation-count 1000
python benchmark.py --source documentdb --record-count 1000 --operation-count 1000
```

The default action is `all`, which runs YCSB `load` followed by `run`.
Results are written under `results/<target>/<timestamp>/`.

## Compare two targets

Use `--source` and `--target` to run two targets with the same YCSB workload
settings. The harness writes each raw YCSB output under the same result
directory and creates `comparison.md` with a Markdown table.

```bash
python benchmark.py --source mongodb --target postgresql \
  --record-count 1000 --operation-count 1000

python benchmark.py --source documentdb --target mysql \
  --record-count 1000 --operation-count 1000

python benchmark.py --source ferretdb --target mongodb \
  --record-count 1000 --operation-count 1000

python benchmark.py --source mongodb --target mongodb \
  --record-count 1000 --operation-count 1000
```

Comparison results are written under:

```text
results/compare-<left>-<right>/<timestamp>/comparison.md
```

The table header includes the actual database version detected at runtime when
the target exposes one. For example, the MongoDB column uses the server version
returned by `buildInfo`, not the Docker image tag. The table includes load/run
throughput plus key read, insert, and update latency metrics. The `Difference`
column shows a signed percentage: positive when the source is better than the
target and negative when it is worse; higher throughput is better, lower
latency is better.

Raw outputs remain available as:

```text
results/compare-<source>-<target>/<timestamp>/<source>/load.txt
results/compare-<source>-<target>/<timestamp>/<source>/run.txt
results/compare-<source>-<target>/<timestamp>/<target>/load.txt
results/compare-<source>-<target>/<timestamp>/<target>/run.txt
```

When both sides use the same target, the raw output directories are suffixed
with `-source` and `-target`, for example `mongodb-source/` and
`mongodb-target/`.

Use the `postgresql` target name for Postgres.

## Target notes

Docker-backed targets start their service with:

```bash
docker compose -f docker-compose.yml up -d <service>
```

Use `--no-docker` when you already have the target running.

The `mongodb` target uses `mongo:latest` and asks Docker Compose to pull the
current image before starting the service. The comparison table reports the
server's actual version number.

`documentdb` means the DocumentDB project at
[documentdb.io](https://documentdb.io/). The built-in target starts
`ghcr.io/documentdb/documentdb/documentdb-local:latest`, publishes port `10260`,
and connects with the local image's self-signed TLS certificate accepted via
`tlsAllowInvalidCertificates=true`. Use `--uri` or `DOCUMENTDB_URI` to point at
an already running DocumentDB endpoint instead.

`ferretdb` and `documentdb` use the `modern-mongodb` binding in
`modern_mongo/` rather than the stock YCSB MongoDB binding so newer
MongoDB-compatible servers do not have to support legacy `OP_QUERY` handshakes.

The generic `jdbc` target is for SQL engines beyond the built-in `sqlite`,
`postgresql`, and `mysql` targets:

```bash
python benchmark.py --source jdbc \
  --jdbc-driver org.postgresql.Driver \
  --jdbc-url 'jdbc:postgresql://127.0.0.1:5432/ycsb' \
  --jdbc-user ycsb \
  --jdbc-password ycsb \
  --jdbc-jar ./postgresql.jar
```

Generic JDBC runs write `schema.sql` next to `jdbc.properties` in the result
directory so you can initialize the table with the target database's own SQL
client before running `load`/`run`.

## Useful options

```bash
python benchmark.py --source sqlite --workload workloada --threads 4
python benchmark.py --source mongodb --uri 'mongodb://127.0.0.1:27017/ycsb?w=1'
python benchmark.py --source ferretdb --uri 'mongodb://username:password@127.0.0.1:27019/ycsb?w=1'
python benchmark.py --source documentdb --uri "$DOCUMENTDB_URI"
python benchmark.py --source sqlite --dry-run
```

`--dry-run` prints the resolved YCSB command without starting services,
resetting SQL tables, or running YCSB.

`--java-opt` can be repeated for JVM settings, such as TLS truststore settings
for a custom DocumentDB endpoint.

If Docker image pulls are slow, raise `--docker-start-timeout`; the default is
600 seconds.

When `DOCKER_CONFIG` is unset, the harness uses `.bench/docker-config` with
empty auths and a symlink to the existing Docker CLI plugins so public Docker
Hub pulls do not depend on a local credential helper.

## Using from mdb-embedded

When this repository is checked out as `tools/benchmark` inside
`mdb-embedded`, the parent repo provides `tools/smongo-bench` as a thin wrapper:

```bash
tools/smongo-bench --source smongo --target sqlite \
  --record-count 1000 --operation-count 1000
```

The `smongo` target starts the local `smongo.wire` server automatically.
