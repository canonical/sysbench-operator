#!/usr/bin/env python3
# Copyright 2023 pguimaraes
# See LICENSE file for licensing details.

import asyncio
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]
MYSQL_APP_NAME = "mysql"
PGSQL_APP_NAME = "postgresql"
DURATION = 10


DB_CHARM = {
    "mysql": {
        "charm": "mysql",
        "channel": "8.0/edge",
        "config": {"profile": "testing"},
        "app_name": MYSQL_APP_NAME,
    },
    "pgsql": {
        "charm": "postgresql",
        "channel": "14/edge",
        "config": {},
        "app_name": PGSQL_APP_NAME,
    },
}


def check_service(svc_name):
    return subprocess.check_output(
        ["juju", "ssh", f"{APP_NAME}/0", "--", "sudo", "systemctl", "is-active", svc_name],
        text=True,
    )


async def run_action(
    ops_test, action_name: str, unit_name: str, timeout: int = 30, **action_kwargs
):
    """Runs the given action on the given unit."""
    client_unit = ops_test.model.units.get(unit_name)
    action = await client_unit.run_action(action_name, **action_kwargs)
    result = await action.wait()
    logging.info(f"request results: {result.results}")
    return SimpleNamespace(status=result.status or "completed", response=result.results)


@pytest.mark.parametrize(
    "db_driver",
    [
        (pytest.param("mysql", marks=pytest.mark.group("mysql"))),
        (pytest.param("pgsql", marks=pytest.mark.group("postgresql"))),
    ],
)
@pytest.mark.abort_on_fail
@pytest.mark.skip_if_deployed
async def test_build_and_deploy(ops_test: OpsTest, db_driver) -> None:
    """Build the charm and deploy + 3 mysql units to ensure a cluster is formed."""
    charm = await ops_test.build_charm(".")

    config = {
        "threads": 1,
        "tables": 1,
        "scale": 1,
        "driver": db_driver,
        "duration": 10,
    }

    await asyncio.gather(
        ops_test.model.deploy(
            DB_CHARM[db_driver]["charm"],
            application_name=DB_CHARM[db_driver]["app_name"],
            num_units=3,
            channel=DB_CHARM[db_driver]["channel"],
            config=DB_CHARM[db_driver]["config"],
        ),
        ops_test.model.deploy(
            charm,
            application_name=APP_NAME,
            num_units=1,
            config=config,
        ),
    )

    await ops_test.model.relate(f"{APP_NAME}:database", f"{MYSQL_APP_NAME}:database")

    # Reduce the update_status frequency until the cluster is deployed
    async with ops_test.fast_forward("60s"):
        await ops_test.model.block_until(
            lambda: len(ops_test.model.applications[APP_NAME].units) == 1
        )
        await ops_test.model.wait_for_idle(
            apps=[APP_NAME, DB_CHARM[db_driver]["app_name"]],
            status="active",
            raise_on_blocked=True,
            timeout=15 * 60,
        )


@pytest.mark.parametrize(
    "db_driver",
    [
        (pytest.param("mysql", marks=pytest.mark.group("mysql"))),
        (pytest.param("pgsql", marks=pytest.mark.group("postgresql"))),
    ],
)
@pytest.mark.abort_on_fail
async def test_prepare_action(ops_test: OpsTest, db_driver) -> None:
    """Build the charm and deploy + 3 mysql units to ensure a cluster is formed."""
    output = await run_action(ops_test, "prepare", f"{APP_NAME}/0")
    assert output.status == "completed"

    await ops_test.model.wait_for_idle(
        apps=[APP_NAME],
        status="waiting",
        raise_on_blocked=True,
        timeout=15 * 60,
    )
    svc_output = check_service("sysbench_prepared.target")
    # Looks silly, but we "active" is in "inactive" string :(
    assert "inactive" not in svc_output and "active" in svc_output


@pytest.mark.parametrize(
    "db_driver",
    [
        (pytest.param("mysql", marks=pytest.mark.group("mysql"))),
        (pytest.param("pgsql", marks=pytest.mark.group("postgresql"))),
    ],
)
@pytest.mark.abort_on_fail
async def test_run_action(ops_test: OpsTest, db_driver) -> None:
    """Build the charm and deploy + 3 mysql units to ensure a cluster is formed."""
    output = await run_action(ops_test, "run", f"{APP_NAME}/0")
    assert output.status == "completed"

    svc_output = check_service("sysbench.service")
    # Looks silly, but we "active" is in "inactive" string :(
    assert "inactive" not in svc_output and "active" in svc_output
    # Wait until it is finished, and retry
    await asyncio.sleep(3 * DURATION)
    try:
        svc_output = check_service("sysbench.service")
    except subprocess.CalledProcessError:
        # Finished running and check_output for "systemctl is-active" will fail
        return
    # Did not fail, so check if we got a "inactive" in the output
    assert "inactive" in svc_output


@pytest.mark.parametrize(
    "db_driver",
    [
        (pytest.param("mysql", marks=pytest.mark.group("mysql"))),
        (pytest.param("pgsql", marks=pytest.mark.group("postgresql"))),
    ],
)
@pytest.mark.abort_on_fail
async def test_clean_action(ops_test: OpsTest, db_driver) -> None:
    """Build the charm and deploy + 3 mysql units to ensure a cluster is formed."""
    output = await run_action(ops_test, "clean", f"{APP_NAME}/0")
    assert output.status == "completed"

    await ops_test.model.wait_for_idle(
        apps=[APP_NAME, DB_CHARM[db_driver]["app_name"]],
        status="active",
        raise_on_blocked=True,
        timeout=15 * 60,
    )
    try:
        svc_output = check_service("sysbench.service")
    except subprocess.CalledProcessError:
        # Finished running and check_output for "systemctl is-active" will fail
        pass
    else:
        assert "inactive" in svc_output

    try:
        svc_output = check_service("sysbench_prepared.target")
    except subprocess.CalledProcessError:
        # Finished running and check_output for "systemctl is-active" will fail
        return
    assert "inactive" in svc_output
