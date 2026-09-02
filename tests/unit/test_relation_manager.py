# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the relation_manager module using the ops scenario framework."""

import dataclasses
import os
from pathlib import Path

import ops
import pytest
import yaml
from scenario import Context, Relation, State

import constants
from constants import (
    DatabaseRelationStatusEnum,
    MultipleRelationsToDBError,
    SysbenchMissingOptionsError,
)
from relation_manager import (
    DatabaseConfigUpdateNeededEvent,
    DatabaseRelationManager,
    SysbenchOptionsFactory,
)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
CONFIG = yaml.safe_load(Path("./config.yaml").read_text())


class SysbenchStubCharm(ops.CharmBase):
    """Minimal charm class wiring up the DatabaseRelationManager."""

    def __init__(self, *args):
        super().__init__(*args)
        self.manager = DatabaseRelationManager(self, ["mysql", "postgresql"])
        self.emitted = []
        self.framework.observe(self.manager.on.db_config_update, self._on_db_config_update)

    def _on_db_config_update(self, event):
        self.emitted.append(event)


def db_relation(endpoint="mysql", endpoints="db-host:3306", **extra):
    """Return a scenario Relation carrying a complete database databag."""
    data = {
        "endpoints": endpoints,
        "username": "db-user",
        "password": "db-pass",
        "database": "sysbench-db",
    }
    data.update(extra)
    return Relation(endpoint=endpoint, remote_app_data=data)


@pytest.fixture
def ctx():
    return Context(SysbenchStubCharm, meta=METADATA, config=CONFIG)


def run_with_relation(ctx, *relations, config=None):
    """Trigger update_status and return the charm instance."""
    state = State(relations=list(relations), config=config or {})
    with ctx(ctx.on.update_status(), state) as manager:
        pass
    return manager.charm


# ---------------------------------------------------------------------------
# relation_status
# ---------------------------------------------------------------------------


def test_relation_status_not_available_when_no_relation(ctx):
    charm = run_with_relation(ctx)
    assert charm.manager.relation_status("mysql") == DatabaseRelationStatusEnum.NOT_AVAILABLE


def test_relation_status_available_when_relation_has_no_data(ctx):
    relation = Relation(endpoint="mysql")
    charm = run_with_relation(ctx, relation)
    assert charm.manager.relation_status("mysql") == DatabaseRelationStatusEnum.AVAILABLE


def test_relation_status_configured_when_relation_data_complete(ctx):
    relation = db_relation("mysql")
    charm = run_with_relation(ctx, relation)
    assert charm.manager.relation_status("mysql") == DatabaseRelationStatusEnum.CONFIGURED


def test_relation_status_available_when_relation_data_incomplete(ctx):
    relation = Relation(
        endpoint="mysql",
        remote_app_data={"endpoints": "db-host:3306"},  # missing credentials
    )
    charm = run_with_relation(ctx, relation)
    assert charm.manager.relation_status("mysql") == DatabaseRelationStatusEnum.AVAILABLE


def test_relation_status_raises_with_multiple_relations_to_db(ctx):
    relations = [db_relation("mysql"), db_relation("mysql")]
    charm = run_with_relation(ctx, *relations)
    with pytest.raises(MultipleRelationsToDBError):
        charm.manager.relation_status("mysql")


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_not_available_without_relations(ctx):
    charm = run_with_relation(ctx)
    assert charm.manager.check() == DatabaseRelationStatusEnum.NOT_AVAILABLE


def test_check_configured_with_single_complete_relation(ctx):
    relation = db_relation("postgresql")
    charm = run_with_relation(ctx, relation)
    assert charm.manager.check() == DatabaseRelationStatusEnum.CONFIGURED


def test_check_raises_with_multiple_db_relations(ctx):
    relations = [db_relation("mysql"), db_relation("postgresql")]
    charm = run_with_relation(ctx, *relations)
    with pytest.raises(MultipleRelationsToDBError):
        charm.manager.check()


# ---------------------------------------------------------------------------
# get_db_config / get_execution_options
# ---------------------------------------------------------------------------


def test_get_db_config_returns_database_model(ctx):
    relation = db_relation("mysql")
    charm = run_with_relation(ctx, relation)
    db_config = charm.manager.get_db_config()
    assert db_config is not None
    assert db_config.host == "db-host"
    assert db_config.port == 3306
    assert db_config.unix_socket == ""
    assert db_config.username == "db-user"
    assert db_config.password == "db-pass"
    assert db_config.db_name == "sysbench-db"
    assert db_config.tables == 8  # default from config.yaml
    assert db_config.scale == 10


def test_get_db_config_converts_port_string_to_int(ctx):
    relation = db_relation("mysql", endpoints="db-host:5432")
    charm = run_with_relation(ctx, relation)
    assert charm.manager.get_db_config().port == 5432


def test_get_db_config_returns_none_when_no_data(ctx):
    relation = Relation(endpoint="mysql")
    charm = run_with_relation(ctx, relation)
    assert charm.manager.get_db_config() is None


def test_get_db_config_returns_first_valid_relation(ctx):
    # postgresql has no data, mysql is fully configured
    relations = [Relation(endpoint="postgresql"), db_relation("mysql")]
    charm = run_with_relation(ctx, *relations)
    db_config = charm.manager.get_db_config()
    assert db_config is not None
    assert db_config.host == "db-host"


def test_get_execution_options(ctx):
    relation = db_relation("mysql")
    config = {"threads": 8, "duration": 60}
    charm = run_with_relation(ctx, relation, config=config)
    options = charm.manager.get_execution_options()
    assert options is not None
    assert options.threads == 8
    assert options.duration == 60
    assert options.db_info.host == "db-host"


def test_get_execution_options_returns_none_when_not_configured(ctx):
    charm = run_with_relation(ctx, Relation(endpoint="mysql"))
    assert charm.manager.get_execution_options() is None


def test_get_db_config_unix_socket_endpoint(ctx, tmp_path):
    socket_path = str(tmp_path / "sysbench.sock")
    open(socket_path, "w").close()  # validator requires an existing path
    relation = db_relation("mysql", endpoints=f"file://{socket_path}")
    charm = run_with_relation(ctx, relation)
    db_config = charm.manager.get_db_config()
    assert db_config.unix_socket == socket_path
    assert db_config.host == ""
    assert db_config.port == 443


def test_get_db_config_missing_credentials_raises(ctx):
    relation = Relation(
        endpoint="mysql",
        remote_app_data={"endpoints": "db-host:3306", "username": "u"},
    )
    charm = run_with_relation(ctx, relation)
    factory = SysbenchOptionsFactory(charm, charm.manager.relations["mysql"])
    with pytest.raises(SysbenchMissingOptionsError):
        factory.get_database_options()


# ---------------------------------------------------------------------------
# chosen_db_type / script
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "expected_script"),
    [
        ("mysql", "scripts/mysql.lua"),
        ("postgresql", "scripts/pgsql.lua"),
    ],
)
def test_script_returns_expected_path(ctx, endpoint, expected_script):
    relation = db_relation(endpoint)
    charm = run_with_relation(ctx, relation)
    script = charm.manager.script()
    assert script is not None
    assert script.endswith(expected_script)
    assert os.path.isabs(script)


def test_script_returns_none_when_no_db(ctx):
    charm = run_with_relation(ctx)
    assert charm.manager.script() is None


def test_chosen_db_type(ctx):
    relation = db_relation("postgresql")
    charm = run_with_relation(ctx, relation)
    assert charm.manager.chosen_db_type() == "postgresql"


def test_chosen_db_type_none_when_not_available(ctx):
    charm = run_with_relation(ctx)
    assert charm.manager.chosen_db_type() is None


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_db_config_update_event_on_endpoints_changed(ctx):
    relation = db_relation("mysql")
    charm = run_with_relation(ctx, relation)
    assert not charm.emitted

    changed_relation = dataclasses.replace(
        relation, remote_app_data={"endpoints": "other-host:3306"}
    )
    state = State(relations=[changed_relation])
    with ctx(ctx.on.relation_changed(changed_relation), state) as manager:
        pass
    charm = manager.charm
    assert len(charm.emitted) == 1
    assert isinstance(charm.emitted[0], DatabaseConfigUpdateNeededEvent)


def test_db_config_update_event_on_relation_broken(ctx):
    relation = db_relation("mysql")
    charm = run_with_relation(ctx, relation)
    assert not charm.emitted

    with ctx(ctx.on.relation_broken(relation), State(relations=[relation])) as manager:
        pass
    charm = manager.charm
    assert len(charm.emitted) == 1
    assert isinstance(charm.emitted[0], DatabaseConfigUpdateNeededEvent)


# ---------------------------------------------------------------------------
# external connectivity config
# ---------------------------------------------------------------------------


def test_external_connectivity_default_from_config(ctx):
    # request-external-connectivity defaults to true in config.yaml
    charm = run_with_relation(ctx)
    for requirer in charm.manager.relations.values():
        assert requirer.external_node_connectivity is True


def test_external_connectivity_can_be_disabled(ctx):
    charm = run_with_relation(ctx, config={"request-external-connectivity": False})
    for requirer in charm.manager.relations.values():
        assert requirer.external_node_connectivity is False


# ---------------------------------------------------------------------------
# SysbenchOptionsFactory
# ---------------------------------------------------------------------------


def test_options_factory_relation_data(ctx):
    relation = db_relation("mysql")
    charm = run_with_relation(ctx, relation)
    factory = SysbenchOptionsFactory(charm, charm.manager.relations["mysql"])
    assert factory.relation_data["endpoints"] == "db-host:3306"
    assert factory.relation_data["username"] == "db-user"


def test_database_name_constant_unchanged():
    # DatabaseRequires is set up with this database name
    assert constants.DATABASE_NAME == "sysbench-db"
