#!/usr/bin/python3
# Copyright 2023 pguimaraes
# See LICENSE file for licensing details.

"""This method runs the sysbench call, collects its output and forwards to prometheus."""

import os
import argparse
import signal
import subprocess

from prometheus_client import Gauge, start_http_server


class SysbenchService:
    """Sysbench service class."""

    def __init__(
        self,
        tpcc_script: str,
        threads: int,
        tables: int,
        scale: int,
        db_driver: str,
        db_name: str,
        db_user: str,
        db_password: str,
        db_host: str,
        db_port: int,
        db_socket: str,
        duration: int = 0,
    ):
        self.tpcc_script = tpcc_script
        driver = "mysql" if db_driver == "mysql" else "pgsql"
        socket = (
            f"--{driver}-host={db_host} --{driver}-port={db_port}"
            if db_socket is None
            else f"--{driver}-socket={db_socket}"
        )
        self.sysbench = f"/usr/bin/sysbench {tpcc_script} --threads={threads} --tables={tables} --scale={scale} --db-driver={driver} --report-interval=10 --time={duration} "
        if db_driver == "mysql":
            self.sysbench += f"--force_pk=1 --mysql-db={db_name} --mysql-user={db_user} --mysql-password={db_password} {socket}"
        elif db_driver == "postgresql":
            self.sysbench += f"--pgsql-db={db_name} --pgsql-user={db_user} --pgsql-password={db_password} {socket}"
        else:
            raise Exception("Wrong db driver chosen")

    def _exec(self, cmd):
        subprocess.check_output(self.sysbench.split(" ") + cmd, timeout=86400)

    def prepare(self):
        """Prepare the sysbench output."""
        return self._exec(["prepare"])

    def _process_line(self, line):
        if "tps" not in line or "qps" not in line or "lat" not in line:
            # This line does not have any data of interest
            return None
        return {
            "tps": line.split("tps: ")[1].split()[0],
            "qps": line.split("qps: ")[1].split()[0],
            "95p_latency": line.split("lat (ms,95%): ")[1].split()[0],
            "err-per-sec": line.split("err/s ")[1].split()[0],
            "reconn-per-sec": line.split("reconn/s: ")[1],
        }

    def run(self, proc, metrics, label, extra_labels):
        """Run one step of the main sysbench service loop."""
        for line in iter(proc.stdout.readline, ""):
            value = self._process_line(line)
            if not value:
                continue
            for m in ["tps", "qps", "95p_latency"]:
                add_benchmark_metric(
                    metrics, f"{label}_{m}", extra_labels, f"tpcc metrics for {m}", value[m]
                )

    def stop(self, proc):
        """Stop the service with SIGTERM."""
        proc.terminate()

    def clean(self):
        """Clean the sysbench database."""
        self._exec(["cleanup"])


def add_benchmark_metric(metrics, label, extra_labels, description, value):
    """Add the benchmark to the prometheus metric.

    labels:
        tpcc_{db_driver}_{tps|qps|95p_latency}
    """
    if label not in metrics:
        metrics[label] = Gauge(label, description, ["model", "unit"])
    metrics[label].labels(*extra_labels).set(value)


def main(args):
    """Run main method."""
    keep_running = True

    def _exit(*args, **kwargs):
        keep_running = False  # noqa: F841

    svc = SysbenchService(
        tpcc_script=args.tpcc_script,
        db_driver=args.db_driver,
        threads=args.threads,
        tables=args.tables,
        scale=args.scale,
        db_name=args.db_name,
        db_user=args.db_user,
        db_password=args.db_password,
        db_host=args.db_host,
        db_port=args.db_port,
        db_socket=args.db_socket,
        duration=args.duration,
    )

    signal.signal(signal.SIGINT, _exit)
    signal.signal(signal.SIGTERM, _exit)
    # Collects the status if the child process ends
    signal.signal(signal.SIGCHLD, _exit)
    start_http_server(8088)

    # Set LUA_PATH
    os.environ["LUA_PATH"] = os.path.join(
        os.path.dirname(args.tpcc_script), "?.lua"
    )

    if args.command == "prepare":
        svc.prepare()
        keep_running = False  # Gracefully shutdown
    elif args.command == "run":
        proc = subprocess.Popen(
            svc.sysbench.split(" ") + ["run"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        metrics = {}
        while keep_running and proc.poll() is None:
            svc.run(proc, metrics, f"tpcc_{args.db_driver}", args.extra_labels.split(","))
        svc.stop(proc)
    elif args.command == "clean":
        svc.clean()
    else:
        raise Exception(f"Command option {args.command} not known")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="sysbench_svc", description="Runs the sysbench command as an argument."
    )
    parser.add_argument("--tpcc_script", type=str, help="Path to the tpcc lua script.")
    parser.add_argument("--db_driver", type=str, help="")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--tables", type=int, default=10)
    parser.add_argument("--scale", type=int, default=10)
    parser.add_argument("--db_name", type=str)
    parser.add_argument("--db_user", type=str)
    parser.add_argument("--db_password", type=str)
    parser.add_argument("--db_host", type=str)
    parser.add_argument("--db_port", type=int)
    parser.add_argument("--db_socket", type=str)
    parser.add_argument("--duration", type=int)
    parser.add_argument("--command", type=str)
    parser.add_argument(
        "--extra_labels", type=str, help="comma-separated list of extra labels to be used."
    )

    args = parser.parse_args()

    main(args)
