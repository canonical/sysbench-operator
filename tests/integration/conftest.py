# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

from os import getenv
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="session")
def microk8s() -> SimpleNamespace | None:
    if "k8s" in getenv("SPREAD_VARIANT", ""):
        return SimpleNamespace(cloud_name="microk8s")
    return None


@pytest.fixture(scope="session")
def db_driver(microk8s) -> str:
    if db := getenv("DATABASE"):
        if not microk8s:
            return db
        return db + "-k8s"
    raise Exception("No db driver set")


@pytest.fixture(scope="session")
def use_router() -> bool:
    return getenv("ROUTER") == "true"
