# smongo benchmark harness

This repository wraps YCSB 0.17.0 so the same workload can run against:

- `smongo` through the smongo MongoDB wire server
- `mongodb` through a Docker MongoDB instance
- `ferretdb` through a Docker FerretDB instance
- `documentdb` through a supplied MongoDB-compatible URI
- `postgresql` and `mysql` through Docker JDBC targets
- `sqlite` through the YCSB JDBC binding
- `jdbc` through a supplied JDBC driver, URL, and driver jar

YCSB is downloaded into `.bench/` on first use and is not checked in.

## Quick start

From the parent `mdb-embedded` checkout:

```bash
tools/smongo-bench --target smongo --record-count 1000 --operation-count 1000
tools/smongo-bench --target mongodb --record-count 1000 --operation-count 1000
tools/smongo-bench --target postgresql --record-count 1000 --operation-count 1000
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
python benchmark.py --target documentdb --uri "$DOCUMENTDB_URI"
python benchmark.py --target jdbc --jdbc-driver org.postgresql.Driver \
  --jdbc-url 'jdbc:postgresql://127.0.0.1:5432/ycsb' \
  --jdbc-user ycsb --jdbc-password ycsb --jdbc-jar ./postgresql.jar
```

Docker-backed targets start their service with `docker compose up -d <service>`.
Use `--no-docker` when you already have the target running.
If Docker image pulls are slow, raise `--docker-start-timeout`; the default is
600 seconds.
