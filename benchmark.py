#!/usr/bin/env python3
"""Run YCSB benchmarks against smongo and comparable database targets."""

from __future__ import annotations

import argparse
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
        default_uri=os.environ.get("DOCUMENTDB_URI"),
        requires_uri=True,
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


def sql_fields() -> str:
    fields = ", ".join(f"FIELD{i} TEXT" for i in range(10))
    return f"YCSB_KEY VARCHAR(255) PRIMARY KEY, {fields}"


def reset_sqlite(db_file: Path, table: str) -> None:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_file) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"CREATE TABLE {table} ({sql_fields()})")


def reset_postgresql(table: str) -> None:
    sql = f"DROP TABLE IF EXISTS {table}; CREATE TABLE {table} ({sql_fields()});"
    run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T", "postgresql", "psql", "-U", "ycsb", "-d", "ycsb", "-c", sql],
        cwd=REPO_ROOT,
        env=docker_env(),
    )


def reset_mysql(table: str) -> None:
    fields = ", ".join(f"FIELD{i} TEXT" for i in range(10))
    sql = f"DROP TABLE IF EXISTS {table}; CREATE TABLE {table} (YCSB_KEY VARCHAR(255) PRIMARY KEY, {fields});"
    run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T", "mysql", "mysql", "-uycsb", "-pycsb", "ycsb", "-e", sql],
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run YCSB benchmarks against smongo and comparable targets.")
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
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
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--docker-start-timeout",
        type=float,
        default=600.0,
        help="Maximum seconds to allow docker compose startup, including image pulls",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target = TARGETS[args.target]
    if target.requires_uri and not args.uri and not target.default_uri:
        raise SystemExit(f"--target {args.target} requires --uri or DOCUMENTDB_URI")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    result_dir = args.output_dir / args.target / stamp
    result_dir.mkdir(parents=True, exist_ok=True)

    if target.docker_service and not args.no_docker:
        start_docker_service(target, args.timeout, args.docker_start_timeout)

    smongo_process: subprocess.Popen[str] | None = None
    if target.starts_smongo:
        smongo_process = start_smongo(args, result_dir)

    try:
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

            if not args.no_reset and args.action in {"prepare", "load", "all"}:
                if args.target == "sqlite":
                    reset_sqlite(Path(jdbc_url.removeprefix("jdbc:sqlite:")), args.table)
                elif args.target == "postgresql" and not args.no_docker:
                    reset_postgresql(args.table)
                elif args.target == "mysql" and not args.no_docker:
                    reset_mysql(args.table)

        phases = []
        if args.action == "prepare":
            phases = []
        elif args.action == "all":
            phases = ["load", "run"]
        else:
            phases = [args.action]

        for phase in phases:
            cmd = ycsb_command(home, phase, target, args.workload, props_file, extra_props, args)
            output_file = result_dir / f"{phase}.txt"
            if args.dry_run:
                print(" ".join(cmd))
            else:
                tee_command(cmd, output_file)

        print(f"Results: {result_dir}")
        return 0
    finally:
        if smongo_process is not None:
            smongo_process.terminate()
            try:
                smongo_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                smongo_process.kill()


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
