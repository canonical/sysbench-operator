# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import pathlib
import subprocess
from types import SimpleNamespace

import pytest
import pytest_operator.plugin
import yaml
from tenacity import Retrying, stop_after_delay, wait_fixed


@pytest.fixture(scope="module")
def ops_test(
    ops_test: pytest_operator.plugin.OpsTest, pytestconfig
) -> pytest_operator.plugin.OpsTest:
    _build_charm = ops_test.build_charm

    async def build_charm(charm_path) -> pathlib.Path:
        if pathlib.Path(charm_path) == pathlib.Path("."):
            # Building sysbench charm
            return await _build_charm(
                charm_path,
                bases_index=pytestconfig.option.sysbench_charm_bases_index,
            )
        else:
            return await _build_charm(charm_path)

    ops_test.build_charm = build_charm
    return ops_test


@pytest.fixture(scope="module")
async def microk8s(ops_test: pytest_operator.plugin.OpsTest) -> None:
    controller = yaml.safe_load(subprocess.check_output(["juju", "show-controller"]))

    for controller_name, details in controller.items():
        controller_data = details["details"]
        if "localhost" in controller_data["cloud"]:
            continue
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
                ["juju", "add-k8s", "cloudk8s", "--client", "--controller", ctlname], check=True
            )

        except subprocess.CalledProcessError as e:
            pytest.exit(str(e))

    return SimpleNamespace(cloud_name="cloudk8s")
