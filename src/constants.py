# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""This module contains the constants and models used by the sysbench charm."""

import os
from enum import Enum
from typing import Optional

from pydantic import BaseModel, root_validator

VALID_LOG_LEVELS = ["info", "debug", "warning", "error", "critical"]


METRICS_PORT = 8088
TPCC_SCRIPT = "script"
SYSBENCH_SVC = "sysbench"
SYSBENCH_PATH = f"/etc/systemd/system/{SYSBENCH_SVC}.service"
LUA_SCRIPT_PATH = "/usr/share/sysbench/tpcc.lua"
SYSBENCH_SVC_READY_TARGET = f"{SYSBENCH_SVC}_prepared.target"

DATABASE_NAME = "sysbench-db"  # TODO: use a UUID here and publish its name in the peer relation

COS_AGENT_RELATION = "cos-agent"
PEER_RELATION = "benchmark-peer"


class SysbenchError(Exception):
    """Sysbench error."""


class SysbenchExecError(SysbenchError):
    """Sysbench failed to execute a command."""


class MultipleRelationsToDBError(SysbenchError):
    """Multiple relations to the same or multiple DBs exist."""


class SysbenchExecFailedError(SysbenchError):
    """Sysbench execution failed error."""


class SysbenchMissingOptionsError(SysbenchError):
    """Sysbench missing options error."""


class SysbenchBaseDatabaseModel(BaseModel):
    """Sysbench database model.

    Holds all the details of the sysbench database.
    """

    host: Optional[str]
    port: Optional[int]
    unix_socket: Optional[str]
    username: str
    password: str
    db_name: str
    tables: int
    scale: int

    @root_validator()
    @classmethod
    def validate_if_missing_params(cls, field_values):
        """Validate if missing params."""
        missing_param = []
        # Check if the required fields are present
        for f in ["username", "password", "db_name"]:
            if f not in field_values or field_values[f] is None:
                missing_param.append(f)
        if missing_param:
            raise SysbenchMissingOptionsError(f"{missing_param}")

        if os.path.exists(field_values.get("unix_socket") or ""):
            field_values["host"] = ""
            field_values["port"] = 443  # we do not need this value, as long as it is an int
        else:
            # Identify the port
            if (port := field_values.get("port")) and isinstance(port, str):
                field_values["port"] = int(port)
            elif not port and field_values.get("host"):
                field_values["port"] = 443
            field_values["unix_socket"] = ""

        # Check if we have the correct endpoint
        if not field_values.get("host") and not field_values.get("unix_socket"):
            raise SysbenchMissingOptionsError("Missing endpoint as unix_socket OR host:port")
        return field_values


class DatabaseRelationStatusEnum(Enum):
    """Represents the different status of the database relation.

    The ERROR in this case corresponds to the case, for example, more than one
    relation exists for a given DB, or for multiple DBs.
    """

    NOT_AVAILABLE = "not_available"
    AVAILABLE = "available"
    CONFIGURED = "configured"
    ERROR = "error"


class SysbenchExecutionModel(BaseModel):
    """Sysbench execution model.

    Holds all the details of the sysbench execution.
    """

    threads: int
    duration: int
    db_info: SysbenchBaseDatabaseModel


class SysbenchExecStatusEnum(Enum):
    """Sysbench execution status.

    The state-machine is the following:
    UNSET -> PREPARED -> RUNNING -> STOPPED -> UNSET

    ERROR can be set after any state apart from UNSET, PREPARED, STOPPED.

    UNSET means waiting for prepare to be executed. STOPPED means the sysbench is ready
    but the service is not running.
    """

    UNSET = "unset"
    PREPARED = "prepared"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class SysbenchIsInWrongStateError(SysbenchError):
    """Sysbench is in wrong state error."""

    def __init__(self, unit_state: SysbenchExecStatusEnum, app_state: SysbenchExecStatusEnum):
        self.unit_state = unit_state
        self.app_state = app_state
        super().__init__(f"Unit state: {unit_state}, App state: {app_state}")
