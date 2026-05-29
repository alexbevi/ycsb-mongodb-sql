#!/usr/bin/env python3
"""Run YCSB benchmarks against smongo and comparable database targets."""

from __future__ import annotations

import argparse
import copy
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

YCSB_VERSION = "0.17.0"
YCSB_RELEASE = f"https://github.com/brianfrankcooper/YCSB/releases/download/{YCSB_VERSION}"

DEFAULT_RECORD_COUNT = 1000
DEFAULT_OPERATION_COUNT = 1000
DEFAULT_WORKLOAD = "workloada"
DEFAULT_TABLE = "usertable"

REPO_ROOT = Path(__file__).resolve().parent
CACHE_DIR = REPO_ROOT / ".bench"
RESULTS_DIR = REPO_ROOT / "results"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
MODERN_MONGO_DRIVER_JARS = (
    "https://repo1.maven.org/maven2/org/mongodb/mongodb-driver-sync/4.11.4/mongodb-driver-sync-4.11.4.jar,"
    "https://repo1.maven.org/maven2/org/mongodb/mongodb-driver-core/4.11.4/mongodb-driver-core-4.11.4.jar,"
    "https://repo1.maven.org/maven2/org/mongodb/bson/4.11.4/bson-4.11.4.jar"
)


@dataclass(frozen=True)
class Target:
    binding: str
    ycsb_db: str
    docker_service: str | None = None
    wait_host: str | None = None
    wait_port: int | None = None
    default_uri: str | None = None
    jdbc_driver: str | None = None
    jdbc_url: str | None = None
    jdbc_user: str = ""
    jdbc_password: str = ""
    jdbc_jar: str | None = None
    starts_smongo: bool = False
    requires_uri: bool = False


@dataclass(frozen=True)
class BenchmarkRun:
    phases: list[str]
    version: str | None


@dataclass(frozen=True)
class ComparisonRow:
    label: str
    keys: Sequence[str]
    integer: bool = False
    higher_is_better: bool | None = True


TARGETS: dict[str, Target] = {
    "smongo": Target(
        binding="mongodb",
        ycsb_db="mongodb",
        default_uri="mongodb://127.0.0.1:27018/ycsb?w=1",
        starts_smongo=True,
    ),
    "mongodb": Target(
        binding="mongodb",
        ycsb_db="mongodb",
        docker_service="mongodb",
        wait_host="127.0.0.1",
        wait_port=27017,
        default_uri="mongodb://127.0.0.1:27017/ycsb?w=1",
    ),
    "ferretdb": Target(
        binding="mongodb",
        ycsb_db="modern-mongodb",
        docker_service="ferretdb",
        wait_host="127.0.0.1",
        wait_port=27019,
        default_uri="mongodb://username:password@127.0.0.1:27019/ycsb?w=1",
    ),
    "documentdb": Target(
        binding="mongodb",
        ycsb_db="modern-mongodb",
        docker_service="documentdb",
        wait_host="127.0.0.1",
        wait_port=10260,
        default_uri=os.environ.get(
            "DOCUMENTDB_URI",
            "mongodb://username:password@127.0.0.1:10260/ycsb"
            "?authSource=admin&tls=true&tlsAllowInvalidCertificates=true&directConnection=true&w=1",
        ),
    ),
    "postgresql": Target(
        binding="jdbc",
        ycsb_db="jdbc",
        docker_service="postgresql",
        wait_host="127.0.0.1",
        wait_port=5432,
        jdbc_driver="org.postgresql.Driver",
        jdbc_url="jdbc:postgresql://127.0.0.1:5432/ycsb?reWriteBatchedInserts=true",
        jdbc_user="ycsb",
        jdbc_password="ycsb",
        jdbc_jar="https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.4/postgresql-42.7.4.jar",
    ),
    "mysql": Target(
        binding="jdbc",
        ycsb_db="jdbc",
        docker_service="mysql",
        wait_host="127.0.0.1",
        wait_port=3306,
        jdbc_driver="com.mysql.cj.jdbc.Driver",
        jdbc_url="jdbc:mysql://127.0.0.1:3306/ycsb?rewriteBatchedStatements=true",
        jdbc_user="ycsb",
        jdbc_password="ycsb",
        jdbc_jar="https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/8.4.0/mysql-connector-j-8.4.0.jar",
    ),
    "sqlite": Target(
        binding="jdbc",
        ycsb_db="jdbc",
        jdbc_driver="org.sqlite.JDBC",
        jdbc_jar=(
            "https://repo1.maven.org/maven2/org/xerial/sqlite-jdbc/3.46.1.0/sqlite-jdbc-3.46.1.0.jar,"
            "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/2.0.13/slf4j-api-2.0.13.jar,"
            "https://repo1.maven.org/maven2/org/slf4j/slf4j-simple/2.0.13/slf4j-simple-2.0.13.jar"
        ),
    ),
    "jdbc": Target(binding="jdbc", ycsb_db="jdbc"),
}


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    printable = " ".join(cmd)
    print(f"+ {printable}", flush=True)
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, env=env, timeout=timeout)


def docker_env() -> dict[str, str]:
    env = os.environ.copy()
    if "DOCKER_CONFIG" not in env:
        original_plugin_dir = Path.home() / ".docker" / "cli-plugins"
        config_dir = CACHE_DIR / "docker-config"
        config_file = config_dir / "config.json"
        config_dir.mkdir(parents=True, exist_ok=True)
        if not config_file.exists():
            config_file.write_text('{"auths":{}}\n', encoding="utf-8")
        plugin_link = config_dir / "cli-plugins"
        if original_plugin_dir.exists() and not plugin_link.exists():
            plugin_link.symlink_to(original_plugin_dir, target_is_directory=True)
        env["DOCKER_CONFIG"] = str(config_dir)
    return env


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    print(f"Downloading {url}", flush=True)
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(dest)


def ycsb_home(binding: str) -> Path:
    name = f"ycsb-{binding}-binding-{YCSB_VERSION}.tar.gz"
    archive = CACHE_DIR / "downloads" / name
    dest = CACHE_DIR / f"ycsb-{binding}-{YCSB_VERSION}"
    if (dest / "bin" / "ycsb.sh").exists():
        return dest

    download(f"{YCSB_RELEASE}/{name}", archive)
    extract_root = CACHE_DIR / "extract" / f"{binding}-{YCSB_VERSION}"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(extract_root, filter="data")

    candidates = [p for p in extract_root.iterdir() if (p / "bin" / "ycsb.sh").exists()]
    if not candidates:
        raise RuntimeError(f"Could not find YCSB executable after extracting {archive}")
    candidates[0].rename(dest)
    return dest


def download_jar(url_or_path: str) -> Path:
    if "://" not in url_or_path:
        return Path(url_or_path).expanduser().resolve()
    dest = CACHE_DIR / "drivers" / url_or_path.rsplit("/", 1)[1]
    download(url_or_path, dest)
    return dest


def download_jars(spec: str) -> list[Path]:
    return [download_jar(item.strip()) for item in spec.split(",") if item.strip()]


def write_ycsb_setenv(
    home: Path,
    classpath: Sequence[Path],
    java_opts: Sequence[str],
) -> None:
    setenv = home / "bin" / "setenv.sh"
    if not classpath and not java_opts:
        try:
            setenv.unlink()
        except FileNotFoundError:
            pass
        return
    lines = []
    if classpath:
        joined = ":".join(str(path) for path in classpath)
        lines.append(f'CLASSPATH="$CLASSPATH:{joined}"')
    if java_opts:
        joined_opts = " ".join(shlex.quote(opt) for opt in java_opts)
        lines.append(f'JAVA_OPTS="${{JAVA_OPTS:-}} {joined_opts}"')
    setenv.write_text("\n".join(lines) + "\n", encoding="utf-8")


def register_ycsb_binding(home: Path, name: str, class_name: str) -> None:
    bindings = home / "bin" / "bindings.properties"
    line = f"{name}:{class_name}"
    text = bindings.read_text(encoding="utf-8")
    if line not in text.splitlines():
        bindings.write_text(text.rstrip() + "\n" + line + "\n", encoding="utf-8")


def ensure_modern_mongo_binding(home: Path) -> list[Path]:
    driver_jars = download_jars(MODERN_MONGO_DRIVER_JARS)
    source = REPO_ROOT / "modern_mongo" / "src" / "site" / "ycsb" / "db" / "ModernMongoDbClient.java"
    build_dir = CACHE_DIR / "modern-mongo" / "classes"
    jar_file = CACHE_DIR / "modern-mongo" / "modern-mongo-binding.jar"
    class_file = build_dir / "site" / "ycsb" / "db" / "ModernMongoDbClient.class"
    core_jar = home / "lib" / f"core-{YCSB_VERSION}.jar"

    if not class_file.exists() or source.stat().st_mtime > class_file.stat().st_mtime:
        if build_dir.exists():
            shutil.rmtree(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)
        classpath = ":".join([str(core_jar), *(str(path) for path in driver_jars)])
        run(["javac", "-cp", classpath, "-d", str(build_dir), str(source)])
        jar_file.parent.mkdir(parents=True, exist_ok=True)
        run(["jar", "cf", str(jar_file), "-C", str(build_dir), "."])

    register_ycsb_binding(home, "modern-mongodb", "site.ycsb.db.ModernMongoDbClient")
    return [jar_file, *driver_jars]


def wait_for_port(host: str, port: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(1.0)
    raise TimeoutError(f"Timed out waiting for {host}:{port}")


def docker_compose(*args: str, timeout: float | None = None) -> None:
    run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        cwd=REPO_ROOT,
        timeout=timeout,
        env=docker_env(),
    )


def docker_compose_output(*args: str, timeout: float | None = None) -> str:
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    print(f"+ {' '.join(cmd)}", flush=True)
    return subprocess.check_output(
        cmd,
        cwd=REPO_ROOT,
        text=True,
        timeout=timeout,
        env=docker_env(),
    ).strip()


def wait_for_container_health(service: str, timeout: float) -> None:
    container_id = docker_compose_output("ps", "-q", service, timeout=30.0)
    if not container_id:
        return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = subprocess.check_output(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                container_id,
            ],
            text=True,
            env=docker_env(),
        ).strip()
        if status in {"none", "healthy"}:
            return
        if status == "unhealthy":
            raise RuntimeError(f"Docker service {service} became unhealthy")
        time.sleep(1.0)
    raise TimeoutError(f"Timed out waiting for Docker service {service} to become healthy")


def start_docker_service(target: Target, timeout: float, startup_timeout: float) -> None:
    if target.docker_service is None:
        return
    try:
        docker_compose("up", "-d", target.docker_service, timeout=startup_timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"timed out after {startup_timeout:g}s starting Docker service "
            f"{target.docker_service}; check Docker image pulls and registry access"
        ) from exc
    if target.wait_host is not None and target.wait_port is not None:
        wait_for_port(target.wait_host, target.wait_port, timeout)
    wait_for_container_health(target.docker_service, timeout)


def docker_compose_service_output(service: str, *cmd: str, timeout: float | None = None) -> str | None:
    try:
        return subprocess.check_output(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T", service, *cmd],
            cwd=REPO_ROOT,
            text=True,
            timeout=timeout,
            env=docker_env(),
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def mongo_server_version(uri: str, timeout: float) -> str | None:
    try:
        from pymongo import MongoClient
    except ImportError:
        return None

    client = MongoClient(uri, serverSelectionTimeoutMS=int(timeout * 1000))
    try:
        version = client.server_info().get("version")
    except Exception:
        return None
    finally:
        client.close()
    return str(version) if version else None


def target_version(name: str, args: argparse.Namespace, target: Target) -> str | None:
    if args.dry_run:
        return None
    if name == "sqlite":
        return sqlite3.sqlite_version
    if name == "postgresql" and not args.no_docker:
        version = docker_compose_service_output(
            "postgresql",
            "psql",
            "-U",
            "ycsb",
            "-d",
            "ycsb",
            "-tAc",
            "SHOW server_version",
            timeout=args.timeout,
        )
        return version or None
    if name == "mysql" and not args.no_docker:
        version = docker_compose_service_output(
            "mysql",
            "mysql",
            "-uycsb",
            "-pycsb",
            "-N",
            "-B",
            "ycsb",
            "-e",
            "SELECT VERSION()",
            timeout=args.timeout,
        )
        return version or None
    if target.binding == "mongodb":
        uri = args.uri or target.default_uri
        if uri:
            return mongo_server_version(uri, args.timeout)
    return None


def sql_fields() -> str:
    fields = ", ".join(f"FIELD{i} TEXT" for i in range(10))
    return f"YCSB_KEY VARCHAR(255) PRIMARY KEY, {fields}"


def sql_schema(table: str) -> str:
    return f"DROP TABLE IF EXISTS {table};\nCREATE TABLE {table} ({sql_fields()});\n"


def write_sql_schema(path: Path, table: str) -> None:
    path.write_text(sql_schema(table), encoding="utf-8")


def reset_sqlite(db_file: Path, table: str) -> None:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_file) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"CREATE TABLE {table} ({sql_fields()})")


def reset_postgresql(table: str) -> None:
    run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "exec",
            "-T",
            "postgresql",
            "psql",
            "-U",
            "ycsb",
            "-d",
            "ycsb",
            "-c",
            sql_schema(table),
        ],
        cwd=REPO_ROOT,
        env=docker_env(),
    )


def reset_mysql(table: str) -> None:
    run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "exec",
            "-T",
            "mysql",
            "mysql",
            "-uycsb",
            "-pycsb",
            "ycsb",
            "-e",
            sql_schema(table),
        ],
        cwd=REPO_ROOT,
        env=docker_env(),
    )


def write_jdbc_props(path: Path, *, driver: str, url: str, user: str, password: str) -> None:
    path.write_text(
        "\n".join(
            [
                f"db.driver={driver}",
                f"db.url={url}",
                f"db.user={user}",
                f"db.passwd={password}",
                "jdbc.autocommit=true",
                "jdbc.batchupdateapi=true",
                "db.batchsize=1000",
                "",
            ]
        ),
        encoding="utf-8",
    )


def smongo_root() -> Path:
    env_root = os.environ.get("SMONGO_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    candidate = REPO_ROOT.parents[1]
    if (candidate / "smongo" / "wire").exists():
        return candidate
    return Path.cwd()


def smongo_python(root: Path) -> str:
    env_python = os.environ.get("SMONGO_PYTHON")
    if env_python:
        return env_python
    venv_python = root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def start_smongo(args: argparse.Namespace, result_dir: Path) -> subprocess.Popen[str]:
    uri = args.uri or TARGETS["smongo"].default_uri or ""
    host, port = mongo_host_port(uri)
    db_path = Path(args.smongo_db_path or result_dir / "smongo-redb").resolve()
    root = smongo_root()
    cmd = [
        smongo_python(root),
        "-m",
        "smongo.wire",
        "--db-path",
        str(db_path),
        "--host",
        host,
        "--port",
        str(port),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    log_path = result_dir / "smongo-wire.log"
    log = log_path.open("w", encoding="utf-8")
    print(f"+ {' '.join(cmd)} > {log_path}", flush=True)
    process = subprocess.Popen(cmd, cwd=root, stdout=log, stderr=subprocess.STDOUT, text=True, env=env)
    try:
        wait_for_port(host, port, args.timeout)
    except Exception:
        process.terminate()
        raise
    return process


def mongo_host_port(uri: str) -> tuple[str, int]:
    stripped = uri.split("://", 1)[-1].split("/", 1)[0]
    if "@" in stripped:
        stripped = stripped.split("@", 1)[1]
    host_port = stripped.split(",", 1)[0]
    if ":" in host_port:
        host, port = host_port.rsplit(":", 1)
        return host, int(port)
    return host_port, 27017


def ycsb_command(
    home: Path,
    phase: str,
    target: Target,
    workload: str,
    props_file: Path | None,
    extra_props: list[str],
    args: argparse.Namespace,
) -> list[str]:
    cmd = [
        str(home / "bin" / "ycsb.sh"),
        phase,
        target.ycsb_db,
        "-s",
        "-P",
        str(home / "workloads" / workload),
        "-p",
        f"table={args.table}",
        "-p",
        f"recordcount={args.record_count}",
        "-p",
        f"operationcount={args.operation_count}",
        "-threads",
        str(args.threads),
    ]
    if props_file is not None:
        cmd.extend(["-P", str(props_file)])
    for prop in extra_props:
        cmd.extend(["-p", prop])
    return cmd


def tee_command(cmd: list[str], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    print(f"+ {' '.join(cmd)} | tee {output_file}", flush=True)
    hard_error = False
    error_markers = (
        "-FAILED]",
        "Exception in thread",
        "Exception while",
        "NoClassDefFoundError",
        "ClassNotFoundException",
        "MongoTimeoutException",
        "Return=ERROR",
        "Unknown option",
        "[ERROR]",
    )
    with output_file.open("w", encoding="utf-8") as out:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert process.stdout is not None
        for line in process.stdout:
            if any(marker in line for marker in error_markers):
                hard_error = True
            print(line, end="")
            out.write(line)
        rc = process.wait()
    if hard_error:
        raise RuntimeError(f"YCSB output contained an error marker; see {output_file}")
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)


def parse_ycsb_metrics(path: Path) -> dict[str, str]:
    metrics: dict[str, str] = {}
    if not path.exists():
        return metrics
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("["):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        section = parts[0].strip("[]")
        metrics[f"{section}.{parts[1]}"] = parts[2]
    return metrics


def metric_value(metrics: dict[str, str], keys: Sequence[str]) -> str:
    for key in keys:
        if key in metrics:
            return metrics[key]
    return ""


def format_metric(value: str, *, integer: bool = False) -> str:
    if not value:
        return "-"
    try:
        number = float(value)
    except ValueError:
        return value
    if integer:
        return str(int(number))
    return f"{number:,.2f}"


def comparison_rows(phase: str) -> list[ComparisonRow]:
    if phase == "load":
        return [
            ComparisonRow("Throughput ops/sec", ("OVERALL.Throughput(ops/sec)",)),
            ComparisonRow("Insert operations", ("INSERT.Operations",), integer=True, higher_is_better=None),
            ComparisonRow(
                "Insert avg latency us",
                ("INSERT.AverageLatency(us)",),
                higher_is_better=False,
            ),
            ComparisonRow(
                "Insert p95 latency us",
                ("INSERT.95thPercentileLatency(us)",),
                higher_is_better=False,
            ),
            ComparisonRow(
                "Insert p99 latency us",
                ("INSERT.99thPercentileLatency(us)",),
                higher_is_better=False,
            ),
        ]
    return [
        ComparisonRow("Throughput ops/sec", ("OVERALL.Throughput(ops/sec)",)),
        ComparisonRow("Read operations", ("READ.Operations",), integer=True, higher_is_better=None),
        ComparisonRow("Read avg latency us", ("READ.AverageLatency(us)",), higher_is_better=False),
        ComparisonRow("Read p95 latency us", ("READ.95thPercentileLatency(us)",), higher_is_better=False),
        ComparisonRow("Update operations", ("UPDATE.Operations",), integer=True, higher_is_better=None),
        ComparisonRow("Update avg latency us", ("UPDATE.AverageLatency(us)",), higher_is_better=False),
        ComparisonRow("Update p95 latency us", ("UPDATE.95thPercentileLatency(us)",), higher_is_better=False),
    ]


def parse_metric_number(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def difference_marker(source_value: float, target_value: float, higher_is_better: bool | None) -> str:
    if source_value == target_value:
        return "0"
    if higher_is_better is None:
        return "+" if source_value > target_value else "-"
    source_better = source_value > target_value if higher_is_better else source_value < target_value
    return "+" if source_better else "-"


def format_difference(source: str, target: str, *, integer: bool, higher_is_better: bool | None) -> str:
    source_number = parse_metric_number(source)
    target_number = parse_metric_number(target)
    if source_number is None or target_number is None:
        return "-"
    return difference_marker(source_number, target_number, higher_is_better)


def versioned_label(name: str, version: str | None) -> str:
    return f"{name} {version}" if version else name


def write_comparison_table(
    result_dir: Path,
    source_name: str,
    target_name: str,
    phases: Sequence[str],
    *,
    source_dir_name: str | None = None,
    target_dir_name: str | None = None,
    source_column_name: str | None = None,
    target_column_name: str | None = None,
) -> Path:
    path = result_dir / "comparison.md"
    source_dir = source_dir_name or source_name
    target_dir = target_dir_name or target_name
    source_column = source_column_name or source_name
    target_column = target_column_name or target_name
    lines = [
        f"# {source_name} vs {target_name}",
        "",
    ]
    for phase in phases:
        source_metrics = parse_ycsb_metrics(result_dir / source_dir / f"{phase}.txt")
        target_metrics = parse_ycsb_metrics(result_dir / target_dir / f"{phase}.txt")
        if not source_metrics and not target_metrics:
            continue
        lines.extend(
            [
                f"## {phase}",
                "",
                f"| Metric | {source_column} | {target_column} | Difference |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for row in comparison_rows(phase):
            source_raw = metric_value(source_metrics, row.keys)
            target_raw = metric_value(target_metrics, row.keys)
            source_value = format_metric(source_raw, integer=row.integer)
            target_value = format_metric(target_raw, integer=row.integer)
            difference = format_difference(
                source_raw,
                target_raw,
                integer=row.integer,
                higher_is_better=row.higher_is_better,
            )
            lines.append(f"| {row.label} | {source_value} | {target_value} | {difference} |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def resolved_phases(action: str) -> list[str]:
    if action == "prepare":
        return []
    if action == "all":
        return ["load", "run"]
    return [action]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run YCSB benchmarks against smongo and comparable targets.")
    parser.add_argument("--source", choices=sorted(TARGETS), help="Source database target to benchmark")
    parser.add_argument("--target", choices=sorted(TARGETS), help="Comparison database target")
    parser.add_argument("--action", choices=["prepare", "load", "run", "all"], default="all")
    parser.add_argument("--workload", default=DEFAULT_WORKLOAD)
    parser.add_argument("--record-count", type=int, default=DEFAULT_RECORD_COUNT)
    parser.add_argument("--operation-count", type=int, default=DEFAULT_OPERATION_COUNT)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--uri", help="MongoDB-compatible URI for smongo, mongodb, ferretdb, or documentdb")
    parser.add_argument("--jdbc-driver")
    parser.add_argument("--jdbc-url")
    parser.add_argument("--jdbc-user", default="")
    parser.add_argument("--jdbc-password", default="")
    parser.add_argument("--jdbc-jar", help="Path or URL to the JDBC driver jar")
    parser.add_argument(
        "--java-opt",
        action="append",
        default=[],
        help="Additional JVM option for YCSB; repeat for multiple options",
    )
    parser.add_argument("--smongo-db-path")
    parser.add_argument("--no-docker", action="store_true", help="Do not start Docker services")
    parser.add_argument("--no-reset", action="store_true", help="Do not reset SQL tables before load")
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved commands without running YCSB")
    parser.add_argument(
        "--compare-smongo",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--compare-to",
        choices=sorted(TARGETS),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--docker-start-timeout",
        type=float,
        default=600.0,
        help="Maximum seconds to allow docker compose startup, including image pulls",
    )
    args = parser.parse_args()
    normalize_args(parser, args)
    return args


def normalize_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.compare_to:
        if args.source is not None and args.target is not None and args.target != args.compare_to:
            parser.error("use only one comparison target")
        if args.source is None:
            if args.target is None:
                parser.error("--source is required when using --compare-to")
            args.source = args.target
        args.target = args.compare_to
    elif args.compare_smongo:
        if args.source is None:
            if args.target is None:
                parser.error("--target is required when using --compare-smongo")
            args.source = "smongo"
        elif args.source != "smongo":
            args.target = args.source
            args.source = "smongo"
        elif args.target is None:
            parser.error("--target is required when using --compare-smongo")
    elif args.source is None:
        if args.target is None:
            parser.error("--source is required")
        args.source = args.target
        args.target = None


def run_benchmark(args: argparse.Namespace, result_dir: Path) -> BenchmarkRun:
    target = TARGETS[args.target]
    if target.requires_uri and not args.uri and not target.default_uri:
        raise SystemExit(f"--target {args.target} requires --uri or DOCUMENTDB_URI")

    result_dir.mkdir(parents=True, exist_ok=True)

    if target.docker_service and not args.no_docker and not args.dry_run:
        start_docker_service(target, args.timeout, args.docker_start_timeout)

    smongo_process: subprocess.Popen[str] | None = None
    if target.starts_smongo and not args.dry_run:
        smongo_process = start_smongo(args, result_dir)

    try:
        version = target_version(args.target, args, target)
        home = ycsb_home(target.binding)
        props_file: Path | None = None
        classpath: list[Path] = []
        extra_props: list[str] = []

        if target.binding == "mongodb":
            uri = args.uri or target.default_uri
            if not uri:
                raise SystemExit(f"--target {args.target} requires --uri")
            if target.ycsb_db == "modern-mongodb":
                classpath = ensure_modern_mongo_binding(home)
                write_ycsb_setenv(home, classpath, args.java_opt)
            else:
                write_ycsb_setenv(home, [], args.java_opt)
            extra_props.extend([f"mongodb.url={uri}", "mongodb.upsert=true"])
        else:
            jdbc_driver = args.jdbc_driver or target.jdbc_driver
            jdbc_url = args.jdbc_url or target.jdbc_url
            jdbc_user = args.jdbc_user or target.jdbc_user
            jdbc_password = args.jdbc_password or target.jdbc_password
            jdbc_jar = args.jdbc_jar or target.jdbc_jar
            if args.target == "sqlite" and not jdbc_url:
                sqlite_file = (result_dir / "sqlite" / "ycsb.sqlite").resolve()
                jdbc_url = f"jdbc:sqlite:{sqlite_file}"
            if not jdbc_driver or not jdbc_url or not jdbc_jar:
                raise SystemExit("JDBC targets require --jdbc-driver, --jdbc-url, and --jdbc-jar")
            classpath = download_jars(jdbc_jar)
            write_ycsb_setenv(home, classpath, args.java_opt)
            props_file = result_dir / "jdbc.properties"
            write_jdbc_props(props_file, driver=jdbc_driver, url=jdbc_url, user=jdbc_user, password=jdbc_password)
            write_sql_schema(result_dir / "schema.sql", args.table)
            if args.target == "jdbc":
                print(f"Generic JDBC schema: {result_dir / 'schema.sql'}")

            if not args.no_reset and not args.dry_run and args.action in {"prepare", "load", "all"}:
                if args.target == "sqlite":
                    reset_sqlite(Path(jdbc_url.removeprefix("jdbc:sqlite:")), args.table)
                elif args.target == "postgresql" and not args.no_docker:
                    reset_postgresql(args.table)
                elif args.target == "mysql" and not args.no_docker:
                    reset_mysql(args.table)

        phases = resolved_phases(args.action)

        for phase in phases:
            cmd = ycsb_command(home, phase, target, args.workload, props_file, extra_props, args)
            output_file = result_dir / f"{phase}.txt"
            if args.dry_run:
                print(" ".join(cmd))
            else:
                tee_command(cmd, output_file)

        print(f"Results: {result_dir}")
        return BenchmarkRun(phases=phases, version=version)
    finally:
        if smongo_process is not None:
            smongo_process.terminate()
            try:
                smongo_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                smongo_process.kill()


def benchmark_args(args: argparse.Namespace, target_name: str) -> argparse.Namespace:
    copied = copy.copy(args)
    copied.target = target_name
    copied.source = target_name
    copied.compare_smongo = False
    copied.compare_to = None
    if target_name != args.source:
        copied.uri = None
        copied.jdbc_driver = None
        copied.jdbc_url = None
        copied.jdbc_user = ""
        copied.jdbc_password = ""
        copied.jdbc_jar = None
    return copied


def compare_targets(args: argparse.Namespace, source_name: str, target_name: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    result_dir = args.output_dir / f"compare-{source_name}-{target_name}" / stamp
    if source_name == target_name:
        source_dir = f"{source_name}-source"
        target_dir = f"{target_name}-target"
        source_suffix = " #1"
        target_suffix = " #2"
    else:
        source_dir = source_name
        target_dir = target_name
        source_suffix = ""
        target_suffix = ""

    source_args = benchmark_args(args, source_name)
    target_args = benchmark_args(args, target_name)

    source_run = run_benchmark(source_args, result_dir / source_dir)
    target_run = run_benchmark(target_args, result_dir / target_dir)
    source_label = f"{versioned_label(source_name, source_run.version)}{source_suffix}"
    target_label = f"{versioned_label(target_name, target_run.version)}{target_suffix}"
    comparison = write_comparison_table(
        result_dir,
        versioned_label(source_name, source_run.version),
        versioned_label(target_name, target_run.version),
        source_run.phases,
        source_dir_name=source_dir,
        target_dir_name=target_dir,
        source_column_name=source_label,
        target_column_name=target_label,
    )
    print(comparison.read_text(encoding="utf-8"))
    print(f"Comparison: {comparison}")


def main() -> int:
    args = parse_args()
    if args.target:
        compare_targets(args, args.source, args.target)
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        args.target = args.source
        run_benchmark(args, args.output_dir / args.source / stamp)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"error: command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1)
    except subprocess.TimeoutExpired as exc:
        print(f"error: command timed out after {exc.timeout:g}s: {' '.join(exc.cmd)}", file=sys.stderr)
        raise SystemExit(1)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
