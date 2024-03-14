# Copyright 2023 pguimaraes
# See LICENSE file for licensing details.

"""This module contains the sysbench service and status classes."""

import json
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

import ops
from charms.operator_libs_linux.v1.systemd import (
    daemon_reload,
    service_failed,
    service_restart,
    service_running,
    service_stop,
)
from jinja2 import Environment, FileSystemLoader, exceptions

from constants import (
    SYSBENCH_SVC,
    SYSBENCH_SVC_READY_TARGET,
    SysbenchBaseDatabaseModel,
    SysbenchExecStatusEnum,
    SysbenchExecutionModel,
    SysbenchIsInWrongStateError,
)


def _render(src_template_file: str, dst_filepath: str, values: Dict[str, Any]):
    templates_dir = os.path.join(os.environ.get("CHARM_DIR", ""), "templates")
    template_env = Environment(loader=FileSystemLoader(templates_dir))
    try:
        template = template_env.get_template(src_template_file)
        content = template.render(values)
    except exceptions.TemplateNotFound as e:
        raise e
    # save the file in the destination
    with open(dst_filepath, "w") as f:
        f.write(content)
        os.chmod(dst_filepath, 0o640)


class SysbenchService:
    """Represents the sysbench service."""

    def __init__(
        self,
        svc_name: str = SYSBENCH_SVC,
        ready_target: str = SYSBENCH_SVC_READY_TARGET,
    ):
        self.svc = svc_name
        self.ready_target = ready_target

    @property
    def svc_path(self) -> str:
        """Returns the path to the service file."""
        return f"/etc/systemd/system/{self.svc}.service"

    def render_service_file(
        self, script: str, db: SysbenchExecutionModel, labels: Optional[str] = ""
    ) -> bool:
        """Render the systemd service file."""
        _render(
            "sysbench.service.j2",
            self.svc_path,
            {
                "db_driver": "mysql",
                "threads": db.threads,
                "tables": db.db_info.tables,
                "scale": db.db_info.scale,
                "db_name": db.db_info.db_name,
                "db_user": db.db_info.username,
                "db_password": db.db_info.password,
                "db_host": db.db_info.host,
                "db_port": db.db_info.port,
                "duration": db.duration,
                "script_path": script,
                "extra_labels": labels,
            },
        )
        return daemon_reload()

    def is_prepared(self) -> bool:
        """Checks if the sysbench service is prepared."""
        try:
            return "active" in subprocess.check_output(
                [
                    "systemctl",
                    "is-active",
                    self.ready_target,
                ],
                text=True,
            )
        except Exception:
            return False

    def finished_preparing(self) -> bool:
        """Wraps the prepare step by setting the prepared target."""
        try:
            shutil.copyfile(
                f"templates/{self.ready_target}", f"/etc/systemd/system/{self.ready_target}"
            )
            return daemon_reload() and service_restart(self.ready_target)
        except Exception:
            return False

    def is_running(self) -> bool:
        """Checks if the sysbench service is running."""
        return self.is_prepared() and os.path.exists(self.svc_path) and service_running(self.svc)

    def is_stopped(self) -> bool:
        """Checks if the sysbench service has stopped."""
        return (
            self.is_prepared()
            and os.path.exists(self.svc_path)
            and not self.is_running()
            and not self.is_failed()
        )

    def is_failed(self) -> bool:
        """Checks if the sysbench service has failed."""
        return self.is_prepared() and os.path.exists(self.svc_path) and service_failed(self.svc)

    def run(self) -> bool:
        """Run the sysbench service."""
        if self.is_stopped() or self.is_failed():
            return service_restart(self.svc)
        return self.is_running()

    def stop(self) -> bool:
        """Stop the sysbench service."""
        if self.is_running():
            return service_stop(self.svc)
        return self.is_stopped()

    def unset(self) -> bool:
        """Unset the sysbench service."""
        try:
            result = self.stop()
            result ^= service_stop(self.ready_target)
            os.remove(f"/etc/systemd/system/{self.ready_target}")
            os.remove(self.svc_path)
            return daemon_reload() and result
        except Exception:
            pass


class SysbenchStatus:
    """Renders the sysbench status updates the relation databag."""

    def __init__(self, charm: ops.charm.CharmBase, relation: str, svc: SysbenchService):
        self.charm = charm
        self.svc = svc
        self.relation = relation

    @property
    def _relation(self) -> Dict[str, Any]:
        return self.charm.model.get_relation(self.relation)

    def app_status(self) -> SysbenchExecStatusEnum:
        """Returns the app status."""
        if not self._relation:
            return None
        return SysbenchExecStatusEnum(
            self._relation.data[self.charm.app].get("status", SysbenchExecStatusEnum.UNSET.value)
        )

    def unit_status(self) -> SysbenchExecStatusEnum:
        """Returns the unit status."""
        if not self._relation:
            return None
        return SysbenchExecStatusEnum(
            self._relation.data[self.charm.unit].get("status", SysbenchExecStatusEnum.UNSET.value)
        )

    def set(self, status: SysbenchExecStatusEnum) -> None:
        """Sets the status in the relation."""
        if not self._relation:
            return
        if self.charm.unit.is_leader():
            self._relation.data[self.charm.app]["status"] = status.value
        self._relation.data[self.charm.unit]["status"] = status.value

    def _has_error_happened(self) -> bool:
        for unit in self._relation.units:
            if self._relation.data[unit].get("status") == SysbenchExecStatusEnum.ERROR.value:
                return True
        return (
            self.unit_status() == SysbenchExecStatusEnum.ERROR
            or self.app_status() == SysbenchExecStatusEnum.ERROR
        )

    def service_status(self) -> SysbenchExecStatusEnum:
        """Returns the status of the sysbench service."""
        if not self.svc.is_prepared():
            return SysbenchExecStatusEnum.UNSET
        if self.svc.is_failed():
            return SysbenchExecStatusEnum.ERROR
        if self.svc.is_running():
            return SysbenchExecStatusEnum.RUNNING
        if self.svc.is_stopped():
            return SysbenchExecStatusEnum.STOPPED
        return SysbenchExecStatusEnum.PREPARED

    def check(self) -> SysbenchExecStatusEnum:
        """Implements the state machine.

        This charm will also update the databag accordingly. It is built of three
        different data sources: this unit last status (from relation), app status and
        the current status of the sysbench service.
        """
        if not self.app_status() or not self.unit_status() or self.charm.app.planned_units() <= 1:
            # Trivial case, the cluster does not exist. Return the service status
            return self.service_status()

        if self._has_error_happened():
            return SysbenchExecStatusEnum.ERROR

        if self.charm.unit.is_leader():
            # Either we are waiting for PREPARE to happen, or it has happened, as
            # the prepare command runs synchronously with the charm. Check if the
            # target exists:
            self.set(self.service_status())
            return self.service_status()

        # Now, we need to execute the unit state
        self.set(self.service_status())
        # If we have a failure, then we should react to it
        if self.service_status() != self.app_status():
            raise SysbenchIsInWrongStateError(self.service_status(), self.app_status())
        return self.service_status()


class SysbenchOptionsFactory(ops.Object):
    """Renders the database options and abstracts the main charm from the db type details.

    It uses the data coming from both relation and config.
    """

    def __init__(self, charm, relation_name):
        super().__init__(charm, relation_name)
        self.charm = charm
        self.relation_name = relation_name

    @property
    def relation_data(self):
        """Returns the relation data."""
        return self.charm.model.get_relation(self.relation_name).data

    def get_database_options(self) -> Dict[str, Any]:
        """Returns the database options."""
        raw = json.loads(self.relation_data[self.charm.unit]["data"])
        endpoints = raw.get("endpoints")
        credentials = self.framework.model.get_secret(id=raw.get("secret-user")).get_content()

        unix_socket, host, port = None, None, None
        if endpoints.startswith("file://"):
            unix_socket = endpoints[7:]
        else:
            host, port = endpoints.split(":")

        return SysbenchBaseDatabaseModel(
            host=host,
            port=int(port),
            unix_socket=unix_socket,
            username=credentials.get("username"),
            password=credentials.get("password"),
            db_name=raw.get("database"),
            tables=self.charm.config.get("tables"),
            scale=self.charm.config.get("scale"),
        )

    def get_execution_options(self) -> Dict[str, Any]:
        """Returns the execution options."""
        return SysbenchExecutionModel(
            threads=self.charm.config.get("threads"),
            duration=self.charm.config.get("duration"),
            db_info=self.get_database_options(),
        )
