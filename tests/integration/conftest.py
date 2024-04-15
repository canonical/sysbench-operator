# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import pathlib
import subprocess
from types import SimpleNamespace

import pytest
import pytest_operator.plugin
import yaml
from tenacity import Retrying, stop_after_delay, wait_fixed

from .helpers import MICROK8S_CLOUD_NAME


@pytest.fixture(scope="module")
async def microk8s(ops_test: pytest_operator.plugin.OpsTest) -> None:
    controller = yaml.safe_load(subprocess.check_output(["juju", "show-controller"]))

    for controller_name in controller.keys():
        # controller_data = details["details"]
        try:
            subprocess.run(["sudo", "snap", "install", "--classic", "microk8s"], check=True)
            subprocess.run(["sudo", "snap", "install", "--classic", "kubectl"], check=True)
            subprocess.run(["sudo", "microk8s", "enable", "dns", "hostpath-storage"], check=True)

            # Configure kubectl now
            subprocess.run(["mkdir", "-p", str(pathlib.Path.home() / ".kube")], check=True)
            kubeconfig = subprocess.check_output(["sudo", "microk8s", "config"])
            with open(str(pathlib.Path.home() / ".kube" / "config"), "w") as f:
                f.write(kubeconfig.decode())
            for attempt in Retrying(stop=stop_after_delay(150), wait=wait_fixed(15)):
                with attempt:
                    if (
                        len(
                            subprocess.check_output(
                                "kubectl get po -A  --field-selector=status.phase!=Running",
                                shell=True,
                                stderr=subprocess.DEVNULL,
                            ).decode()
                        )
                        != 0
                    ):  # We got sth different than "No resources found." in stderr
                        raise Exception()

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
