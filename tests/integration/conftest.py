# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

from types import SimpleNamespace

import pytest
import pytest_operator.plugin


@pytest.fixture(scope="module")
async def microk8s(ops_test: pytest_operator.plugin.OpsTest) -> None:
    return SimpleNamespace(cloud_name="concierge-microk8s")
