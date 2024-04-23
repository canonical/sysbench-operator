#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
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
from charms.grafana_agent.v0.cos_agent import COSAgentProvider
from charms.operator_libs_linux.v0 import apt
from ops.main import main

from constants import (
    COS_AGENT_RELATION,
    METRICS_PORT,
    PEER_RELATION,
    DatabaseRelationStatusEnum,
    MultipleRelationsToDBError,
    SysbenchExecError,
    SysbenchExecStatusEnum,
    SysbenchIsInWrongStateError,
    SysbenchMissingOptionsError,
)
from relation_manager import DatabaseRelationManager
from sysbench import SysbenchService, SysbenchStatus

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

        self.database = DatabaseRelationManager(self, ["mysql", "postgresql"])
        self.framework.observe(self.database.on.db_config_update, self._on_config_changed)

        self._grafana_agent = COSAgentProvider(
            self,
            scrape_configs=self.scrape_config,
            refresh_events=[],
        )
        self.sysbench_status = SysbenchStatus(self, PEER_RELATION, SysbenchService())
        self.labels = ",".join([self.model.name, self.unit.name])

    def _set_sysbench_status(self) -> SysbenchExecStatusEnum:
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
        """Set status for the operator and finishes the service.

        First, we check if there are relations with any meaningful data. If not, then
        this is the most important status to report. Then, we check the details of the
        sysbench service and the sysbench status.
        """
        try:
            status = self.database.check()
        except MultipleRelationsToDBError:
            self.unit.status = ops.model.BlockedStatus("Multiple DB relations at once forbidden!")
            return
        if status == DatabaseRelationStatusEnum.NOT_AVAILABLE:
            self.unit.status = ops.model.BlockedStatus("No database relation available")
            return
        if status == DatabaseRelationStatusEnum.AVAILABLE:
            self.unit.status = ops.model.WaitingStatus("Waiting on data from relation")
            return
        if status == DatabaseRelationStatusEnum.ERROR:
            self.unit.status = ops.model.BlockedStatus(
                "Unexpected error with db relation: check logs"
            )
            return
        self._set_sysbench_status()

    @property
    def is_tls_enabled(self):
        """Return tls status."""
        return False

    @property
    def _unit_ip(self) -> str:
        """Current unit ip."""
        return self.model.get_binding(COS_AGENT_RELATION).network.bind_address

    def _on_config_changed(self, _):
        # For now, ignore the configuration
        svc_was_running = False
        svc = SysbenchService()
        if svc.is_running():
            # Nothing to do, there was no setup yet
            svc_was_running = True
            svc.stop()
        if not (options := self.database.get_execution_options()):
            # Nothing to do, we can abandon this event and wait for the next changes
            return
        svc.render_service_file(
            self.database.script(), self.database.chosen_db_type(), options, labels=self.labels
        )
        if svc_was_running:
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

    def _execute_sysbench_cmd(self, extra_labels, command: str):
        """Execute the sysbench command."""
        if not (db := self.database.get_execution_options()):
            raise SysbenchMissingOptionsError("Missing database options")
        try:
            output = subprocess.check_output(
                [
                    "/usr/bin/sysbench_svc.py",
                    f"--tpcc_script={self.database.script()}",
                    f"--db_driver={self.database.chosen_db_type()}",
                    f"--threads={db.threads}",
                    f"--tables={db.db_info.tables}",
                    f"--scale={db.db_info.scale}",
                    f"--db_name={db.db_info.db_name}",
                    f"--db_user={db.db_info.username}",
                    f"--db_password={db.db_info.password}",
                    f"--db_host={db.db_info.host}",
                    f"--db_port={db.db_info.port}",
                    f"--db_socket={db.db_info.unix_socket}",
                    f"--duration={db.duration}",
                    f"--command={command}",
                    f"--extra_labels={extra_labels}",
                ],
                timeout=86400,
            )
        except subprocess.CalledProcessError as e:
            logger.warning(f"Process failed with: {e}")
            self.sysbench_status.set(SysbenchExecStatusEnum.ERROR)
            raise SysbenchExecError()
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

        self.unit.status = ops.model.MaintenanceStatus("Running prepare command...")
        try:
            self._execute_sysbench_cmd(self.labels, "prepare")
        except SysbenchMissingOptionsError:
            event.fail("Failed: missing database options")
            return
        except SysbenchExecError:
            event.fail("Failed: error in sysbench while executing prepare")
            return
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
        if not (options := self.database.get_execution_options()):
            event.fail("Failed: missing database options")
            return
        svc.render_service_file(
            self.database.script(), self.database.chosen_db_type(), options, labels=self.labels
        )
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
        svc = SysbenchService()
        if status == SysbenchExecStatusEnum.UNSET:
            logger.warning("Sysbench units are idle, but continuing anyways")
        if status == SysbenchExecStatusEnum.RUNNING:
            logger.info("Sysbench service stopped in clean action")
            svc.stop()

        self.unit.status = ops.model.MaintenanceStatus("Cleaning up database")
        try:
            self._execute_sysbench_cmd(self.labels, "clean")
        except SysbenchMissingOptionsError:
            event.fail("Failed: missing database options")
            return
        except SysbenchExecError:
            event.fail("Failed: error in sysbench while executing clean")
            return
        svc.unset()
        self.sysbench_status.set(SysbenchExecStatusEnum.UNSET)


if __name__ == "__main__":
    main(SysbenchOperator)
