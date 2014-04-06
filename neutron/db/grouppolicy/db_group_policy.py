# Copyright (c) 2014 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import sqlalchemy as sa
from sqlalchemy.ext.orderinglist import ordering_list
from sqlalchemy import orm
from sqlalchemy.orm import exc

from neutron.common import exceptions as nexc
from neutron.common import log
from neutron.db import api as db
from neutron.db import db_base_plugin_v2
from neutron.db import model_base
from neutron.db import models_v2
from neutron.extensions import group_policy as gpolicy
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils
from neutron.plugins.common import constants as const


LOG = logging.getLogger(__name__)


class Endpoint(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents an Endpoint consumed by the Group Policy."""
    __tablename__ = 'gp_endpoints'
    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(1024))
    epg_id = sa.Column(sa.String(36),
                       sa.ForeignKey('gp_endpointgroups.id'),
                       nullable=True, unique=True)


class ContractScope(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Models an EndpointGroup's provider/consumer relation to a Contract."""
    __tablename__ = 'gp_contractscopes'
    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(1024))
    scope_type = sa.Column(sa.Enum(const.GP_PROVIDES,
                                   const.GP_CONSUMES,
                                   name='scope_type'))
    epg_id = sa.Column(sa.String(36),
                       sa.ForeignKey('gp_endpointgroups.id'),
                       nullable=True, unique=True)
    contract_id = sa.Column(sa.String(36),
                            sa.ForeignKey('gp_contracts.id'))
    # TODO(Sumit): Add policy_label for scope


class EndpointGroup(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents an Endpoint Group that is a collection of endpoints."""
    __tablename__ = 'gp_endpointgroups'
    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(1024))
    parent_id = sa.Column(sa.String(36),
                          sa.ForeignKey('gp_endpointgroups.id'),
                          nullable=True)
    children = orm.relationship('EndpointGroup')
    endpoints = orm.relationship(Endpoint, backref='gp_endpointgroups')
    contract_scopes = orm.relationship(ContractScope,
                                       backref='gp_endpointgroups')


class ContractPolicyRuleAssociation(model_base.BASEV2):
    """Models the many to many relation between Contract and Policy rules."""
    __tablename__ = 'gp_contract_policyrule_associations'
    contract_id = sa.Column(sa.String(36),
                            sa.ForeignKey('gp_contracts.id'),
                            primary_key=True)
    policyrule_id = sa.Column(sa.String(36),
                              sa.ForeignKey('gp_policyrules.id'),
                              primary_key=True)
    position = sa.Column(sa.Integer)


class PolicyRule(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents a Group Policy Rule."""
    __tablename__ = 'gp_policyrules'
    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(1024))
    enabled = sa.Column(sa.Boolean)
    contracts = orm.relationship(ContractPolicyRuleAssociation,
                                 backref='gp_policyrules')
    # Default value would be Null implying all protocols
    # TODO(Sumit): Confirm this
    protocol = sa.Column(sa.Enum(const.TCP, const.UDP, const.ICMP,
                                 name="protocol_type"),
                         nullable=True)
    port_range_min = sa.Column(sa.Integer)
    port_range_max = sa.Column(sa.Integer)
    action_type = sa.Column(sa.Enum(const.GP_ALLOW,
                                    const.GP_REDIRECT,
                                    name='action_type'))
    # Default value would be Null when action_type is allow
    # however, value is required if something meaningful needs to be done
    # for redirect
    # TODO(Sumit): Revisit when other action_types are defined
    action_value = sa.Column(sa.String(36),
                             sa.ForeignKey('gp_contractscopes.id'),
                             nullable=True, unique=True)
    direction = sa.Column(sa.Enum(const.GP_DIRECTION_IN,
                                  const.GP_DIRECTION_OUT,
                                  const.GP_DIRECTION_BI,
                                  name='direction'))
    # TODO(Sumit): Add policy_label


class Contract(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents a Contract that is a collection of Policy rules."""
    __tablename__ = 'gp_contracts'
    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(1024))
    policy_rules = orm.relationship(ContractPolicyRuleAssociation,
                                    backref='gp_contract',
                                    lazy="joined",
                                    order_by=
                                    'ContractPolicyRuleAssociation.position',
                                    collection_class=
                                    ordering_list('position', count_from=1))
    contract_scopes = orm.relationship(ContractScope,
                                       backref='gp_contract')


class GroupPolicyDbMixin(gpolicy.GroupPolicyPluginBase,
                         db_base_plugin_v2.CommonDbMixin):
    """Group Policy plugin interface implementation using SQLAlchemy models.

    Whenever a non-read call happens the plugin will call an event handler
    class method (e.g., endpoint_created()).  The result is that this class
    can be sub-classed by other classes that add custom behaviors on certain
    events.
    """

    # This attribute specifies whether the plugin supports or not
    # bulk/pagination/sorting operations. Name mangling is used in
    # order to ensure it is qualified by class
    # TODO(Sumit): native bulk support
    __native_bulk_support = False
    __native_pagination_support = True
    __native_sorting_support = True

    def __init__(self):
        db.configure_db()

    @classmethod
    def register_dict_extend_funcs(cls, resource, funcs):
        cur_funcs = cls._dict_extend_functions.get(resource, [])
        cur_funcs.extend(funcs)
        cls._dict_extend_functions[resource] = cur_funcs

    def _filter_non_model_columns(self, data, model):
        """Remove all the attributes from data which are not columns of
        the model passed as second parameter.
        """
        columns = [c.name for c in model.__table__.columns]
        return dict((k, v) for (k, v) in
                    data.iteritems() if k in columns)

    def _get_endpoint(self, context, id):
        try:
            endpoint = self._get_by_id(context, Endpoint, id)
        except exc.NoResultFound:
            raise nexc.EndpointNotFound(endpoint_id=id)
        return endpoint

    def _get_endpoint_group(self, context, id):
        try:
            endpoint_group = self._get_by_id(context, EndpointGroup, id)
        except exc.NoResultFound:
            raise nexc.EndpointGroupNotFound(endpoint_group_id=id)
        return endpoint_group

    def _get_contract(self, context, id):
        try:
            contract = self._get_by_id(context, Contract, id)
        except exc.NoResultFound:
            raise nexc.ContractNotFound(contract_id=id)
        return contract

    def _get_contract_scope(self, context, id):
        try:
            contract_scope = self._get_by_id(context, ContractScope, id)
        except exc.NoResultFound:
            raise nexc.ContractScopeNotFound(contract_scope_id=id)
        return contract_scope

    def _get_policy_rule(self, context, id):
        try:
            policy_rule = self._get_by_id(context, PolicyRule, id)
        except exc.NoResultFound:
            raise nexc.PolicyRuleNotFound(policy_rule_id=id)
        return policy_rule

    def _make_endpoint_dict(self, ep, fields=None):
        res = {'id': ep['id'],
               'tenant_id': ep['tenant_id'],
               'name': ep['name'],
               'description': ep['description']}
        return self._fields(res, fields)

    def _make_endpoint_group_dict(self, epg, fields=None):
        res = {'id': epg['id'],
               'tenant_id': epg['tenant_id'],
               'name': epg['name'],
               'description': epg['description'],
               'parent_id': epg['parent_id'],
               'endpoints': epg['endpoints'],
               'provided_contract_scopes': epg['provided_contract_scopes'],
               'consumed_contract_scopes': epg['consumed_contract_scopes']}
        return self._fields(res, fields)

    @log.log
    def create_endpoint(self, context, endpoint):
        ep = endpoint['endpoint']
        tenant_id = self._get_tenant_id_for_create(context, ep)
        with context.session.begin(subtransactions=True):
            ep_db = Endpoint(id=uuidutils.generate_uuid(),
                             tenant_id=tenant_id,
                             name=ep['name'],
                             description=ep['description'])
            context.session.add(ep_db)
        return self._make_endpoint_dict(ep_db)

    @log.log
    def update_endpoint(self, context, id, endpoint):
        ep = endpoint['endpoint']
        with context.session.begin(subtransactions=True):
            ep_query = context.session.query(
                Endpoint).with_lockmode('update')
            ep_db = ep_query.filter_by(id=id).one()
            ep_db.update(ep)
        return self._make_endpoint_dict(ep_db)

    @log.log
    def delete_endpoint(self, context, id):
        with context.session.begin(subtransactions=True):
            ep_query = context.session.query(
                Endpoint).with_lockmode('update')
            ep_db = ep_query.filter_by(id=id).one()
            context.session.delete(ep_db)

    @log.log
    def get_endpoint(self, context, id, fields=None):
        ep = self._get_endpoint(context, id)
        return self._make_endpoint_dict(ep, fields)

    @log.log
    def get_endpoints(self, context, filters=None, fields=None):
        return self._get_collection(context, Endpoint,
                                    self._make_endpoint_dict,
                                    filters=filters, fields=fields)

    @log.log
    def get_endpoints_count(self, context, filters=None):
        return self._get_collection_count(context, Endpoint,
                                          filters=filters)

    @log.log
    def create_endpoint_group(self, context, endpoint_group):
        epg = endpoint_group['endpoint_group']
        tenant_id = self._get_tenant_id_for_create(context, epg)
        with context.session.begin(subtransactions=True):
            epg_db = EndpointGroup(id=uuidutils.generate_uuid(),
                                   tenant_id=tenant_id,
                                   name=epg['name'],
                                   description=epg['description'])
            context.session.add(epg_db)
        return self._make_endpoint_group_dict(epg_db)

    @log.log
    def update_endpoint_group(self, context, id, endpoint_group):
        epg = endpoint_group['endpoint_group']
        with context.session.begin(subtransactions=True):
            epg_query = context.session.query(
                EndpointGroup).with_lockmode('update')
            epg_db = epg_query.filter_by(id=id).one()
            epg_db.update(epg)
        return self._make_endpoint_group_dict(epg_db)

    @log.log
    def delete_endpoint_group(self, context, id):
        with context.session.begin(subtransactions=True):
            epg_query = context.session.query(
                EndpointGroup).with_lockmode('update')
            epg_db = epg_query.filter_by(id=id).one()
            context.session.delete(epg_db)

    @log.log
    def get_endpoint_group(self, context, id, fields=None):
        epg = self._get_endpoint_group(context, id)
        return self._make_endpoint_group_dict(epg, fields)

    @log.log
    def get_endpoint_groups(self, context, filters=None, fields=None):
        return self._get_collection(context, EndpointGroup,
                                    self._make_endpoint_group_dict,
                                    filters=filters, fields=fields)

    @log.log
    def get_endpoint_groups_count(self, context, filters=None):
        return self._get_collection_count(context, EndpointGroup,
                                          filters=filters)

    @log.log
    def create_contract(self, context, contract):
        pass

    @log.log
    def update_contract(self, context, id, contract):
        pass

    @log.log
    def get_contracts(self, context, filters=None, fields=None):
        pass

    @log.log
    def get_contract(self, context, id, fields=None):
        pass

    @log.log
    def delete_contract(self, context, id):
        pass

    @log.log
    def create_contract_scope(self, context, contract_scope):
        pass

    @log.log
    def update_contract_scope(self, context, id, contract_scope):
        pass

    @log.log
    def get_contract_scopes(self, context, filters=None, fields=None):
        pass

    @log.log
    def get_contract_scope(self, context, id, fields=None):
        pass

    @log.log
    def delete_contract_scope(self, context, id):
        pass

    @log.log
    def get_policy_rules(self, context, filters=None, fields=None):
        pass

    @log.log
    def get_policy_rule(self, context, id, fields=None):
        pass

    @log.log
    def create_policy_rule(self, context, policy_rule):
        pass

    @log.log
    def update_policy_rule(self, context, id, policy_rule):
        pass

    @log.log
    def delete_policy_rule(self, context, id):
        pass