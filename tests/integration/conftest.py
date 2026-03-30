# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import pathlib
import subprocess
from os import getenv
from types import SimpleNamespace

import pytest
import pytest_operator.plugin
import yaml

from .helpers import MICROK8S_CLOUD_NAME


@pytest.fixture(scope="module")
async def microk8s(ops_test: pytest_operator.plugin.OpsTest) -> None:
    if "k8s" in getenv("SPREAD_VARIANT", ""):
        controller = yaml.safe_load(subprocess.check_output(["juju", "show-controller"]))

        for controller_name in controller.keys():
            # controller_data = details["details"]
            try:
                subprocess.run(["mkdir", "-p", str(pathlib.Path.home() / ".kube")], check=True)
                kubeconfig = subprocess.check_output(["sudo", "microk8s", "config"])
                with open(str(pathlib.Path.home() / ".kube" / "config"), "w") as f:
                    f.write(kubeconfig.decode())
                # Get controller name
                ctlname = controller_name

                # Add microk8s to the kubeconfig
                subprocess.run(
                    ["juju", "add-k8s", MICROK8S_CLOUD_NAME, "--client", "--controller", ctlname],
                    check=True,
                )

            except subprocess.CalledProcessError as e:
                pytest.exit(str(e))

        return SimpleNamespace(cloud_name="cloudk8s")
    return None


@pytest.fixture(scope="module")
def db_driver(microk8s) -> str:
    if db := getenv("DATABASE"):
        if not microk8s:
            return db
        return db + "-k8s"
    raise Exception("No db driver set")


@pytest.fixture(scope="session")
def use_router() -> bool:
    return getenv("ROUTER") == "true"
