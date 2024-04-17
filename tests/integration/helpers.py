import uuid
from pathlib import Path

import pytest
import yaml

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]
MYSQL_APP_NAME = "mysql"
PGSQL_APP_NAME = "postgresql"
DURATION = 10
K8S_DB_MODEL_NAME = "database-" + str(uuid.uuid4())[0:5]
MICROK8S_CLOUD_NAME = "cloudk8s"


DB_CHARM = {
    "mysql": {
        "charm": "mysql",
        "channel": "8.0/edge",
        "config": {"profile": "testing"},
        "app_name": MYSQL_APP_NAME,
    },
    "postgresql": {
        "charm": "postgresql",
        "channel": "14/edge",
        "config": {"profile": "testing"},
        "app_name": PGSQL_APP_NAME,
    },
    "mysql-k8s": {
        "charm": "mysql-k8s",
        "channel": "8.0/edge",
        "config": {"profile": "testing"},
        "app_name": MYSQL_APP_NAME,
    },
    # "postgresql-k8s": {
    #     "charm": "postgresql-k8s",
    #     "channel": "14/edge",
    #     "config": {},
    #     "app_name": PGSQL_APP_NAME,
    # },
}


DB_ROUTER = {
    "mysql": {
        "charm": "mysql-router",
        "channel": "dpe/edge",
        "config": {},
        "app_name": "mysql-router",
    },
    "postgresql": {
        "charm": "pgbouncer",
        "channel": "1/edge",
        "config": {},
        "app_name": "pgbouncer",
    },
    "mysql-k8s": {
        "charm": "mysql-router-k8s",
        "channel": "8.0/edge",
        "config": {},
        "app_name": "mysql-router",
    },
    # "postgresql-k8s": {
    #     "charm": "pgbouncer-k8s",
    #      "channel": "1/edge",
    #      "config": {},
    #      "app_name": "pgbouncer",
    #  },
}


DEPLOY_ALL_GROUP_MARKS = [
    (
        pytest.param(
            app,
            router,
            id=f"{app}_router-{router}",
            marks=pytest.mark.group(f"{app}_router-{router}"),
        )
    )
    for app in ["mysql", "postgresql", "mysql-k8s"]  # , "postgresql-k8s"]
    for router in ([True, False] if not app.endswith("-k8s") else [True])
]


DEPLOY_VM_ONLY_GROUP_MARKS = [
    (
        pytest.param(
            app,
            router,
            id=f"{app}_router-{router}",
            marks=pytest.mark.group(f"{app}_router-{router}"),
        )
    )
    for app in ["mysql", "postgresql"]
    for router in [True, False]
]


DEPLOY_K8S_ONLY_GROUP_MARKS = [
    (
        pytest.param(
            app,
            router,
            id=f"{app}_router-{router}",
            marks=pytest.mark.group(f"{app}_router-{router}"),
        )
    )
    for app in ["mysql-k8s"]  # , "postgresql-k8s"] -> waiting for pgbouncer to support NodePort
    # There is no case in k8s where we do not consume the router endpoint
    for router in [True]
]
