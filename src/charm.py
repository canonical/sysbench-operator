#!/usr/bin/env python3
# Copyright 2023 pguimaraes
# See LICENSE file for licensing details.

"""This connects the sysbench service to the database and the grafana agent.

The first action after installing the sysbench charm and relating it to the different
apps, is to prepare the db. The user must run the prepare action to create the database.

The prepare action will run the sysbench prepare command to create the database and, at its
end, it sets a systemd target informing the service is ready.

The next step is to execute the run action. This action renders the systemd service file and
starts the service. If the target is missing, then service errors and returns an error to
the user.
"""

import logging
import os
import shutil
import subprocess
from typing import Dict, List

import ops
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.grafana_agent.v0.cos_agent import COSAgentProvider
from charms.operator_libs_linux.v0 import apt
from ops.main import main

from constants import (
    COS_AGENT_RELATION,
    DATABASE_NAME,
    DATABASE_RELATION,
    METRICS_PORT,
    PEER_RELATION,
    SysbenchExecStatusEnum,
    SysbenchIsInWrongStateError,
)
from sysbench import SysbenchOptionsFactory, SysbenchService, SysbenchStatus

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


class SysbenchOperator(ops.CharmBase):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.prepare_action, self.on_prepare_action)
        self.framework.observe(self.on.run_action, self.on_run_action)
        self.framework.observe(self.on.stop_action, self.on_stop_action)
        self.framework.observe(self.on.clean_action, self.on_clean_action)

        self.framework.observe(self.on[PEER_RELATION].relation_joined, self._on_peer_changed)
        self.framework.observe(self.on[PEER_RELATION].relation_changed, self._on_peer_changed)

        self.database = DatabaseRequires(self, DATABASE_RELATION, DATABASE_NAME)
        self.framework.observe(
            getattr(self.database.on, "endpoints_changed"), self._on_endpoints_changed
        )
        self.framework.observe(
            self.on[DATABASE_RELATION].relation_broken, self._on_relation_broken
        )
        self._grafana_agent = COSAgentProvider(
            self,
            scrape_configs=self.scrape_config,
            refresh_events=[],
        )
        self.sysbench_status = SysbenchStatus(self, PEER_RELATION, SysbenchService())
        self.labels = ",".join([self.model.name, self.unit.name])

    def _set_charm_status(self) -> SysbenchExecStatusEnum:
        """Recovers the sysbench status."""
        status = self.sysbench_status.check()
        if status == SysbenchExecStatusEnum.ERROR:
            self.unit.status = ops.model.BlockedStatus("Sysbench failed, please check logs")
        elif status == SysbenchExecStatusEnum.UNSET:
            self.unit.status = ops.model.ActiveStatus()
        if status == SysbenchExecStatusEnum.PREPARED:
            self.unit.status = ops.model.WaitingStatus(
                "Sysbench is prepared: execute run to start"
            )
        if status == SysbenchExecStatusEnum.RUNNING:
            self.unit.status = ops.model.ActiveStatus("Sysbench is running")
        if status == SysbenchExecStatusEnum.STOPPED:
            self.unit.status = ops.model.BlockedStatus("Sysbench is stopped after run")

    def __del__(self):
        """Set status for the operator and finishes the service."""
        self._set_charm_status()

    @property
    def is_tls_enabled(self):
        """Return tls status."""
        return False

    @property
    def _chosen_script(self) -> str:
        driver = self.config["driver"]
        return str(os.path.abspath(f"scripts/{driver}.lua"))

    @property
    def _unit_ip(self) -> str:
        """Current unit ip."""
        return self.model.get_binding(COS_AGENT_RELATION).network.bind_address

    def _on_config_changed(self, _):
        # For now, ignore the configuration
        svc = SysbenchService()
        if not svc.is_running():
            # Nothing to do, there was no setup yet
            return
        svc.stop()
        options = SysbenchOptionsFactory(self, DATABASE_RELATION).get_execution_options()
        svc.render_service_file(self._chosen_script, options, labels=self.labels)
        svc.run()

    def _on_relation_broken(self, _):
        SysbenchService().stop()

    def scrape_config(self) -> List[Dict]:
        """Generate scrape config for the Patroni metrics endpoint."""
        return [
            {
                "metrics_path": "/metrics",
                "static_configs": [{"targets": [f"{self._unit_ip}:{METRICS_PORT}"]}],
                "tls_config": {"insecure_skip_verify": True},
                "scheme": "https" if self.is_tls_enabled else "http",
            }
        ]

    def _on_install(self, _):
        """Installs the basic packages and python dependencies.

        No exceptions are captured as we need all the dependencies below to even start running.
        """
        self.unit.status = ops.model.MaintenanceStatus("Installing...")
        apt.update()
        apt.add_package(["sysbench", "python3-prometheus-client", "python3-jinja2", "unzip"])
        shutil.copyfile("templates/sysbench_svc.py", "/usr/bin/sysbench_svc.py")
        os.chmod("/usr/bin/sysbench_svc.py", 0o700)
        self.unit.status = ops.model.ActiveStatus()

    def _on_peer_changed(self, _):
        """Peer relation changed."""
        if (
            not self.unit.is_leader()
            and self.sysbench_status.app_status() == SysbenchExecStatusEnum.PREPARED
            and self.sysbench_status.service_status()
            not in [SysbenchExecStatusEnum.PREPARED, SysbenchExecStatusEnum.RUNNING]
        ):
            # We need to mark this unit as prepared so we can rerun the script later
            self.sysbench_status.set(SysbenchExecStatusEnum.PREPARED)

    def _execute_sysbench_cmd(self, extra_labels, command: str, driver: str):
        """Execute the sysbench command."""
        db = SysbenchOptionsFactory(self, DATABASE_RELATION).get_execution_options()
        output = subprocess.check_output(
            [
                "/usr/bin/sysbench_svc.py",
                f"--tpcc_script={self._chosen_script}",
                f"--db_driver={driver}",
                f"--threads={db.threads}",
                f"--tables={db.db_info.tables}",
                f"--scale={db.db_info.scale}",
                f"--db_name={db.db_info.db_name}",
                f"--db_user={db.db_info.username}",
                f"--db_password={db.db_info.password}",
                f"--db_host={db.db_info.host}",
                f"--db_port={db.db_info.port}",
                f"--duration={db.duration}",
                f"--command={command}",
                f"--extra_labels={extra_labels}",
            ],
            timeout=86400,
        )
        logger.debug("Sysbench output: %s", output)

    def check(self, event=None) -> SysbenchExecStatusEnum:
        """Wraps the status check and catches the wrong state error for processing."""
        try:
            return self.sysbench_status.check()
        except SysbenchIsInWrongStateError:
            # This error means we have a new app_status change coming down via peer relation
            # and we did not receive it yet. Defer the upstream event
            if event:
                event.defer()
        return None

    def on_prepare_action(self, event):
        """Prepare the database.

        There are two steps: the actual prepare command and setting a target to inform the
        prepare was successful.
        """
        if not self.unit.is_leader():
            event.fail("Failed: only leader can prepare the database")
            return
        if not (status := self.check()):
            event.fail(
                f"Failed: app level reports {self.sysbench_status.app_status()} and service level reports {self.sysbench_status.service_status()}"
            )
            return
        if status != SysbenchExecStatusEnum.UNSET:
            event.fail("Failed: sysbench is already prepared, stop and clean up the cluster first")

        driver = self.config["driver"]
        self.unit.status = ops.model.MaintenanceStatus("Running prepare command...")
        self._execute_sysbench_cmd(self.labels, "prepare", driver)
        SysbenchService().finished_preparing()
        self.sysbench_status.set(SysbenchExecStatusEnum.PREPARED)
        event.set_results({"status": "prepared"})

    def on_run_action(self, event):
        """Run benchmark action."""
        if not (status := self.check()):
            event.fail(
                f"Failed: app level reports {self.sysbench_status.app_status()} and service level reports {self.sysbench_status.service_status()}"
            )
            return
        if status == SysbenchExecStatusEnum.ERROR:
            logger.warning("Overriding ERROR status and restarting service")
        elif status not in [
            SysbenchExecStatusEnum.PREPARED,
            SysbenchExecStatusEnum.STOPPED,
        ]:
            event.fail("Failed: sysbench is not prepared")
            return

        self.unit.status = ops.model.MaintenanceStatus("Setting up benchmark")
        svc = SysbenchService()
        svc.stop()
        options = SysbenchOptionsFactory(self, DATABASE_RELATION).get_execution_options()
        svc.render_service_file(self._chosen_script, options, labels=self.labels)
        svc.run()
        self.sysbench_status.set(SysbenchExecStatusEnum.RUNNING)
        event.set_results({"status": "running"})

    def on_stop_action(self, event):
        """Stop benchmark service."""
        if not (status := self.check()):
            event.fail(
                f"Failed: app level reports {self.sysbench_status.app_status()} and service level reports {self.sysbench_status.service_status()}"
            )
            return
        if status != SysbenchExecStatusEnum.RUNNING:
            event.fail("Failed: sysbench is not running")
            return
        svc = SysbenchService()
        svc.stop()
        self.sysbench_status.set(SysbenchExecStatusEnum.STOPPED)
        event.set_results({"status": "stopped"})

    def on_clean_action(self, event):
        """Clean the database."""
        if not self.unit.is_leader():
            event.fail("Failed: only leader can prepare the database")
            return
        if not (status := self.check()):
            event.fail(
                f"Failed: app level reports {self.sysbench_status.app_status()} and service level reports {self.sysbench_status.service_status()}"
            )
            return
        if status == SysbenchExecStatusEnum.UNSET:
            event.fail("Nothing to do, sysbench units are idle")
            return
        if status == SysbenchExecStatusEnum.RUNNING:
            SysbenchService().stop()
            logger.info("Sysbench service stopped in clean action")

        driver = self.config["driver"]
        self.unit.status = ops.model.MaintenanceStatus("Cleaning up database")
        svc = SysbenchService()
        svc.stop()
        self._execute_sysbench_cmd(self.labels, "clean", driver)
        svc.unset()
        self.sysbench_status.set(SysbenchExecStatusEnum.UNSET)

    def _on_endpoints_changed(self, _) -> None:
        # TODO: update the service if it is already running
        pass


if __name__ == "__main__":
    main(SysbenchOperator)
