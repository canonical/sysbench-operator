# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""This module abstracts the different DBs and provide a single API set.

The DatabaseRelationManager listens to DB events and manages the relation lifecycles.
The charm interacts with the manager and requests data + listen to some key events such
as changes in the configuration.
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

from charms.data_platform_libs.v0.data_interfaces import (
    DatabaseRequirerData,
    DatabaseRequirerEventHandlers,
)
from ops import Model
from ops.charm import CharmBase, CharmEvents, RelationChangedEvent, RelationCreatedEvent
from ops.framework import EventBase, EventSource, Object
from ops.model import ModelError, Relation

from constants import (
    DATABASE_NAME,
    DatabaseRelationStatusEnum,
    MultipleRelationsToDBError,
    SysbenchBaseDatabaseModel,
    SysbenchExecutionModel,
)


class SysbenchDatabaseRequirerData(DatabaseRequirerData):
    """Requirer-side of the database relation."""

    def __init__(
        self,
        model: Model,
        relation_name: str,
        database_name: Optional[str] = "",
        extra_user_roles: Optional[str] = None,
        relations_aliases: Optional[List[str]] = None,
        additional_secret_fields: Optional[List[str]] = [],
        external_node_connectivity: bool = False,
    ):
        """Manager of database client relations."""
        super().__init__(
            model,
            relation_name,
            database_name,
            extra_user_roles,
            relations_aliases,
            additional_secret_fields,
            external_node_connectivity,
        )
        if not database_name:
            self.database = None


class SysbenchDatabaseRequirerEventHandlers(DatabaseRequirerEventHandlers):
    """Overloads the _on_relation_created_event to only trigger once db name is set."""

    def __init__(
        self,
        charm: CharmBase,
        relation_data: Optional[DatabaseRequirerData] = None,
        unique_key: str = "",
    ):
        super().__init__(charm, relation_data, unique_key)

    def _on_relation_created_event(self, event: RelationCreatedEvent) -> None:
        """Event emitted when the database relation is created."""
        if not self.relation_data or not self.relation_data.database:
            event.defer()
            return
        super()._on_relation_created_event(event)

    def _on_relation_changed_event(self, event: RelationChangedEvent) -> None:
        """Event emitted when the database relation changes."""
        if not self.relation_data or not self.relation_data.database:
            event.defer()
            return
        super()._on_relation_changed_event(event)


class SysbenchDatabaseRequires(SysbenchDatabaseRequirerEventHandlers):
    """Overloads the DatabaseRequirerHandlers object."""

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str,
        database_name: Optional[str] = None,
        extra_user_roles: Optional[str] = None,
        relations_aliases: Optional[List[str]] = None,
        additional_secret_fields: Optional[List[str]] = [],
        external_node_connectivity: bool = False,
    ):
        req_data = SysbenchDatabaseRequirerData(
            charm.model,
            relation_name,
            database_name,
            extra_user_roles,
            relations_aliases,
            additional_secret_fields,
            external_node_connectivity,
        )
        super().__init__(charm, req_data)


class NoRemoteDBUnitsAvailableError(Exception):
    """Reports that no remote units have been found.

    It means we cannot decide if we are dealing with a CMR or not yet. We should
    postpone the creation of DatabaseRequires with a DB name, which triggers a
    database_requested event on the provider side.
    """


class DatabaseConfigUpdateNeededEvent(EventBase):
    """informs the charm that we have an update in the DB config."""


class DatabaseManagerEvents(CharmEvents):
    """Events used by the Database Relation Manager to communicate with the charm."""

    db_config_update = EventSource(DatabaseConfigUpdateNeededEvent)


class DatabaseRelationManager(Object):
    """Listens to all the DB-related events and react to them.

    This class will provide the charm with the necessary data to connect to the DB as
    well as the current relation status.
    """

    on = DatabaseManagerEvents()  # pyright: ignore [reportGeneralTypeIssues]

    def __init__(self, charm: CharmBase, relation_names: List[str]):
        super().__init__(charm, None)
        self.charm = charm
        self.relations = dict()
        for rel in relation_names:
            try:
                external_conn = self._use_external_connection(rel)
                db_name = DATABASE_NAME
                self.relations[rel] = SysbenchDatabaseRequires(
                    self.charm,
                    rel,
                    db_name,
                    external_node_connectivity=external_conn,
                )
                self.framework.observe(
                    getattr(self.relations[rel].on, "endpoints_changed"),
                    self._on_endpoints_changed,
                )
                self.framework.observe(
                    self.charm.on[rel].relation_broken, self._on_endpoints_changed
                )
            except NoRemoteDBUnitsAvailableError:
                # No members available yet, we should not set the DB name
                pass

    def _use_external_connection(self, relation_name: str) -> bool:
        if not self.charm.config.get("request-external-connectivity"):
            return False

        if not (relation := self.charm.model.get_relation(relation_name)) or not relation.units:
            raise NoRemoteDBUnitsAvailableError()

        return any(re.match(r"remote\-[a-f0-9]+/", unit.name) for unit in relation.units)

    def relation_status(self, relation_name) -> DatabaseRelationStatusEnum:
        """Returns the current relation status."""
        relation = self.charm.model.relations[relation_name]
        if len(relation) > 1:
            raise MultipleRelationsToDBError()
        elif len(relation) == 0:
            return DatabaseRelationStatusEnum.NOT_AVAILABLE
        if self._is_relation_active(relation[0]):
            # Relation exists and we have some data
            # Try to create an options object and see if it fails
            try:
                SysbenchOptionsFactory(self.charm, relation_name).get_database_options()
            except Exception:
                pass
            else:
                # We have data to build the config object
                return DatabaseRelationStatusEnum.CONFIGURED
        return DatabaseRelationStatusEnum.AVAILABLE

    def check(self) -> DatabaseRelationStatusEnum:
        """Returns the current status of all the relations, aggregated."""
        status = DatabaseRelationStatusEnum.NOT_AVAILABLE
        for rel in self.relations.keys():
            if self.relation_status(rel) != DatabaseRelationStatusEnum.NOT_AVAILABLE:
                if status != DatabaseRelationStatusEnum.NOT_AVAILABLE:
                    # It means we have the same relation to more than one DB
                    raise MultipleRelationsToDBError()
                status = self.relation_status(rel)
        return status

    def _is_relation_active(self, relation: Relation):
        """Whether the relation is active based on contained data."""
        try:
            _ = repr(relation.data)
            return True
        except (RuntimeError, ModelError):
            return False

    def get_db_config(self) -> Optional[SysbenchBaseDatabaseModel]:
        """Checks each relation: if there is a valid relation, build its options and return.

        This class does not raise: MultipleRelationsToSameDBTypeError. It either returns the
        data of the first valid relation or just returns None. The error above must be used
        to manage the final status of the charm only.
        """
        for rel in self.relations.keys():
            if self.relation_status(rel) == DatabaseRelationStatusEnum.CONFIGURED:
                return SysbenchOptionsFactory(self.charm, rel).get_database_options()

        return None

    def _on_endpoints_changed(self, _):
        """Handles the endpoints_changed event."""
        self.on.db_config_update.emit()

    def get_execution_options(self) -> Optional[SysbenchExecutionModel]:
        """Returns the execution options."""
        if not (db := self.get_db_config()):
            # It means we are not yet ready. Return None
            # This check also serves to ensure we have only one valid relation at the time
            return None
        return SysbenchExecutionModel(
            threads=self.charm.config.get("threads"),
            duration=self.charm.config.get("duration"),
            db_info=db,
        )

    def chosen_db_type(self) -> Optional[str]:
        """Returns the chosen DB type."""
        for rel in self.relations.keys():
            if self.relation_status(rel) in [
                DatabaseRelationStatusEnum.AVAILABLE,
                DatabaseRelationStatusEnum.CONFIGURED,
            ]:
                return rel
        return None

    def script(self) -> Optional[str]:
        """Returns the script path for the chosen DB."""
        type = self.chosen_db_type()
        if type == "mysql":
            return str(os.path.abspath("scripts/mysql.lua"))
        elif type == "postgresql":
            return str(os.path.abspath("scripts/pgsql.lua"))
        return None


class SysbenchOptionsFactory(Object):
    """Renders the database options and abstracts the main charm from the db type details.

    It uses the data coming from both relation and config.
    """

    def __init__(self, charm, relation_name):
        super().__init__(charm, relation_name)
        self.charm = charm
        self.relation_name = relation_name

    @property
    def relation_data(self):
        """Returns the relation data."""
        return self.charm.model.get_relation(self.relation_name).data

    def get_database_options(self) -> Dict[str, Any]:
        """Returns the database options."""
        raw = json.loads(self.relation_data[self.charm.unit]["data"])
        endpoints = raw.get("endpoints")
        credentials = self.framework.model.get_secret(id=raw.get("secret-user")).get_content()

        unix_socket, host, port = None, None, None
        if endpoints.startswith("file://"):
            unix_socket = endpoints[7:]
        else:
            host, port = endpoints.split(":")

        return SysbenchBaseDatabaseModel(
            host=host,
            port=port,
            unix_socket=unix_socket,
            username=credentials.get("username"),
            password=credentials.get("password"),
            db_name=raw.get("database"),
            tables=self.charm.config.get("tables"),
            scale=self.charm.config.get("scale"),
        )

    def get_execution_options(self) -> SysbenchExecutionModel:
        """Returns the execution options."""
        return SysbenchExecutionModel(
            threads=self.charm.config.get("threads"),
            duration=self.charm.config.get("duration"),
            db_info=self.get_database_options(),
        )
