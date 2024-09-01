#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import logging
import re
import subprocess
from types import SimpleNamespace

import juju
import pytest
import yaml
from pytest_operator.plugin import OpsTest
from tenacity import Retrying, stop_after_delay, wait_fixed

from .helpers import (
    APP_NAME,
    DB_CHARM,
    DB_ROUTER,
    DEPLOY_ALL_GROUP_MARKS,
    DEPLOY_K8S_ONLY_GROUP_MARKS,
    DEPLOY_VM_ONLY_GROUP_MARKS,
    DURATION,
    K8S_DB_MODEL_NAME,
    MICROK8S_CLOUD_NAME,
)

logger = logging.getLogger(__name__)


model_db = None


def check_service(svc_name: str, retry_if_fail: bool = True):
    if not retry_if_fail:
        return subprocess.check_output(
            ["juju", "ssh", f"{APP_NAME}/0", "--", "sudo", "systemctl", "is-active", svc_name],
            text=True,
        )
    for attempt in Retrying(stop=stop_after_delay(150), wait=wait_fixed(15)):
        with attempt:
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


@pytest.fixture(scope="module", autouse=True)
async def destroy_model_in_k8s(ops_test):
    yield

    if ops_test.keep_model:
        return
    controller = juju.controller.Controller()
    await controller.connect()
    await controller.destroy_model(K8S_DB_MODEL_NAME)
    await controller.disconnect()

    ctlname = list(yaml.safe_load(subprocess.check_output(["juju", "show-controller"])).keys())[0]

    # We have deployed microk8s, and we do not need it anymore
    subprocess.run(["sudo", "snap", "remove", "--purge", "microk8s"], check=True)
    subprocess.run(["sudo", "snap", "remove", "--purge", "kubectl"], check=True)
    subprocess.run(
        ["juju", "remove-cloud", "--client", "--controller", ctlname, MICROK8S_CLOUD_NAME],
        check=True,
    )


@pytest.mark.parametrize("db_driver,use_router", DEPLOY_K8S_ONLY_GROUP_MARKS)
@pytest.mark.abort_on_fail
@pytest.mark.skip_if_deployed
async def test_build_and_deploy_k8s_only(
    ops_test: OpsTest, microk8s, db_driver, use_router
) -> None:
    """Build the charm and deploy + 3 db units to ensure a cluster is formed."""
    # Create a new model for DB on k8s:
    logging.info(f"Creating k8s model {K8S_DB_MODEL_NAME}")
    controller = juju.controller.Controller()
    await controller.connect()
    await controller.add_model(K8S_DB_MODEL_NAME, cloud_name=microk8s.cloud_name)

    global model_db
    model_db = juju.model.Model()
    await model_db.connect(model_name=K8S_DB_MODEL_NAME)

    await model_db.deploy(
        DB_CHARM[db_driver]["charm"],
        application_name=DB_CHARM[db_driver]["app_name"],
        num_units=3,
        channel=DB_CHARM[db_driver]["channel"],
        config=DB_CHARM[db_driver]["config"],
        trust=True,
    )
    if use_router:
        await model_db.deploy(
            DB_ROUTER[db_driver]["charm"],
            application_name=DB_ROUTER[db_driver]["app_name"],
            channel=DB_ROUTER[db_driver]["channel"],
            config=DB_ROUTER[db_driver]["config"],
            trust=True,
        )
        await model_db.relate(
            f"{DB_CHARM[db_driver]['app_name']}:database",
            f"{DB_ROUTER[db_driver]['app_name']}",
        )

    # Now, set up the sysbench and relate to the CMR
    charm = await ops_test.build_charm(".")
    config = {
        "threads": 1,
        "tables": 1,
        "scale": 1,
        "duration": 0,
    }
    await ops_test.model.deploy(
        charm,
        application_name=APP_NAME,
        num_units=1,
        config=config,
    )

    if use_router:
        await model_db.create_offer(
            endpoint="database",
            offer_name="database",
            application_name=DB_ROUTER[db_driver]["app_name"],
        )
    else:
        await model_db.create_offer(
            endpoint="database",
            offer_name="database",
            application_name=DB_CHARM[db_driver]["app_name"],
        )
    await ops_test.model.consume(f"admin/{model_db.name}.database")
    await ops_test.model.relate("database", f"{APP_NAME}:{DB_CHARM[db_driver]['app_name']}")

    # Reduce the update_status frequency until the cluster is deployed
    async with ops_test.fast_forward("60s"):
        await ops_test.model.block_until(
            lambda: len(ops_test.model.applications[APP_NAME].units) == 1
        )
    await model_db.wait_for_idle(status="active")
    await controller.disconnect()
    await model_db.disconnect()


@pytest.mark.parametrize("db_driver,use_router", DEPLOY_VM_ONLY_GROUP_MARKS)
@pytest.mark.abort_on_fail
@pytest.mark.skip_if_deployed
async def test_build_and_deploy_vm_only(ops_test: OpsTest, db_driver, use_router) -> None:
    """Build the charm and deploy + 3 db units to ensure a cluster is formed."""
    charm = await ops_test.build_charm(".")

    config = {
        "threads": 1,
        "tables": 1,
        "scale": 1,
        "duration": 0,
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

    if use_router:
        await ops_test.model.deploy(
            DB_ROUTER[db_driver]["charm"],
            application_name=DB_ROUTER[db_driver]["app_name"],
            channel=DB_ROUTER[db_driver]["channel"],
            config=DB_ROUTER[db_driver]["config"],
        )
        await ops_test.model.relate(
            f"{APP_NAME}:{db_driver}", f"{DB_ROUTER[db_driver]['app_name']}"
        )
        await ops_test.model.relate(
            f"{DB_CHARM[db_driver]['app_name']}:database", f"{DB_ROUTER[db_driver]['app_name']}"
        )
    else:
        await ops_test.model.relate(
            f"{APP_NAME}:{db_driver}", f"{DB_CHARM[db_driver]['app_name']}:database"
        )

    # Reduce the update_status frequency until the cluster is deployed
    apps = [DB_CHARM[db_driver]["app_name"]]
    if use_router:
        apps.append(DB_ROUTER[db_driver]["app_name"])
    async with ops_test.fast_forward("60s"):
        await ops_test.model.block_until(
            lambda: len(ops_test.model.applications[APP_NAME].units) == 1
        )
        await ops_test.model.wait_for_idle(
            apps=apps,
            status="active",
            timeout=30 * 60,
        )

    # set the model to the global model_db
    global model_db
    model_db = ops_test.model


@pytest.mark.parametrize("db_driver,use_router", DEPLOY_ALL_GROUP_MARKS)
@pytest.mark.abort_on_fail
async def test_prepare_action(ops_test: OpsTest, db_driver, use_router) -> None:
    """Validate the prepare action."""
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME],
        status="active",
        raise_on_blocked=False,
        timeout=100,
    )

    output = await run_action(ops_test, "prepare", f"{APP_NAME}/0")
    assert output.status == "completed"

    await ops_test.model.wait_for_idle(
        apps=[APP_NAME],
        status="waiting",
        raise_on_blocked=True,
        timeout=15 * 60,
    )
    for attempt in Retrying(stop=stop_after_delay(40), wait=wait_fixed(10)):
        with attempt:
            svc_output = check_service("sysbench_prepared.target")
            # Looks silly, but we "active" is in "inactive" string :(
            assert "inactive" not in svc_output and "active" in svc_output


@pytest.mark.parametrize("db_driver,use_router", DEPLOY_ALL_GROUP_MARKS)
@pytest.mark.abort_on_fail
async def test_run_action_and_cause_failure(ops_test: OpsTest, db_driver, use_router) -> None:
    """Starts a run and then kills the sysbench process. Systemd must then report it as failed."""
    app = ops_test.model.applications[APP_NAME]
    await app.set_config({"duration": "0"})

    output = await run_action(ops_test, "run", f"{APP_NAME}/0")
    assert output.status == "completed"

    svc = "sysbench.service"

    # Make sure we are currently running
    assert "inactive" not in check_service(svc)
    # Now, figure out sysbench's PID itself
    # The check_output is getting strange chars in the output as we, filter it out
    sysbench_svc_pid = re.findall(
        r"MainPID=[0-9.]+",
        subprocess.check_output(
            f"juju ssh {APP_NAME}/0 -- systemctl show --property=MainPID {svc}",
            text=True,
            shell=True,
        ),
    )[0].split("MainPID=")[1]
    pid = (
        subprocess.check_output(
            [
                "juju",
                "ssh",
                f"{APP_NAME}/0",
                "--",
                "sudo",
                "cat",
                f"/proc/{sysbench_svc_pid}/task/{sysbench_svc_pid}/children",
            ],
            text=True,
        )
        .split("\n")[0]
        .split(" ")[0]
    )
    # Now, kill the sysbench process
    subprocess.check_output(["juju", "ssh", f"{APP_NAME}/0", "--", "sudo", "kill", "-9", str(pid)])

    # Finally, check if the service is now in a failed state in systemd
    try:
        subprocess.check_output(
            ["juju", "ssh", f"{APP_NAME}/0", "--", "sudo", "systemctl", "is-failed", svc]
        )
    except subprocess.CalledProcessError as e:
        # We expect "is-failed" to succeed, i.e. we have a failed service
        raise AssertionError(f"Service {svc} is not in a failed state") from e

    async with ops_test.fast_forward("60s"):
        # Check if the charm is now blocked:
        await ops_test.model.wait_for_idle(
            apps=[APP_NAME],
            status="blocked",
            timeout=15 * 60,
        )


@pytest.mark.parametrize("db_driver,use_router", DEPLOY_ALL_GROUP_MARKS)
@pytest.mark.abort_on_fail
async def test_run_action(ops_test: OpsTest, db_driver, use_router) -> None:
    """Try to run the benchmark for DURATION and then wait until it is finished."""
    app = ops_test.model.applications[APP_NAME]
    await app.set_config({"duration": f"{DURATION}"})

    output = await run_action(ops_test, "run", f"{APP_NAME}/0")
    assert output.status == "completed"

    svc_output = check_service("sysbench.service")
    logger.info(f"sysbench.service output: {svc_output}")

    # Looks silly, but we "active" is in "inactive" string :(
    assert "inactive" not in svc_output and "active" in svc_output

    async with ops_test.fast_forward("60s"):
        # Wait until it is finished, and retry
        await asyncio.sleep(3 * DURATION)

        await ops_test.model.wait_for_idle(
            apps=[APP_NAME],
            status="blocked",
            timeout=15 * 60,
        )

        try:
            logger.info("Checking if sysbench.service is inactive")
            svc_output = check_service("sysbench.service", retry_if_fail=False)
        except subprocess.CalledProcessError:
            # Finished running and check_output for "systemctl is-active" will fail
            return
        # Did not fail, so check if we got a "inactive" in the output
        assert "inactive" in svc_output


@pytest.mark.parametrize("db_driver,use_router", DEPLOY_ALL_GROUP_MARKS)
@pytest.mark.abort_on_fail
async def test_clean_action(ops_test: OpsTest, db_driver, use_router) -> None:
    """Validate clean action."""
    output = await run_action(ops_test, "clean", f"{APP_NAME}/0")
    assert output.status == "completed"

    await ops_test.model.wait_for_idle(
        apps=[APP_NAME],
        status="active",
        raise_on_blocked=True,
        timeout=15 * 60,
    )

    for svc_name in ["sysbench.service", "sysbench_prepared.target"]:
        try:
            svc_output = check_service(svc_name, retry_if_fail=False)
        except subprocess.CalledProcessError:
            # Finished running and check_output for "systemctl is-active" will fail
            pass
        else:
            assert "inactive" in svc_output
