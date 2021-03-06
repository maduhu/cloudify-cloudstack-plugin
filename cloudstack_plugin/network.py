########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# * See the License for the specific language governing permissions and
# * limitations under the License.

import copy
from cloudify.decorators import operation
from cloudify.exceptions import NonRecoverableError, RecoverableError
from cloudstack_plugin.cloudstack_common import (
    get_cloud_driver,
    CLOUDSTACK_ID_PROPERTY,
    CLOUDSTACK_TYPE_PROPERTY,
    CLOUDSTACK_NAME_PROPERTY,
    COMMON_RUNTIME_PROPERTIES_KEYS,
    delete_runtime_properties,
    USE_EXTERNAL_RESOURCE_PROPERTY,
    get_resource_id,
    get_location)
from cloudstack_plugin.vpc import get_vpc, create_acl_list, create_acl

__author__ = 'uri1803, boul'


NETWORK_CLOUDSTACK_TYPE = 'network'

# Runtime properties
RUNTIME_PROPERTIES_KEYS = COMMON_RUNTIME_PROPERTIES_KEYS

@operation
def create(ctx, **kwargs):
    """ Create network with rules.
    """

    cloud_driver = get_cloud_driver(ctx)

    network = {
        'description': None,
        'name': get_resource_id(ctx, NETWORK_CLOUDSTACK_TYPE),
    }

    ctx.logger.debug('reading network configuration.')
    #network.update(ctx.node.properties['network'])
    network.update(copy.deepcopy(ctx.node.properties['network']))

    network_name = network['name']
    zone = network['zone']
    location = get_location(cloud_driver, zone)
    netoffer = network['service_offering']
    network_offering = get_network_offering(cloud_driver, netoffer)
    existing_net = network_exists(cloud_driver, network_name)

    # TODO, bit messy below, should be reviewed.

    if 'vpc' in network:
        if network['vpc']:
            vpc = get_vpc(cloud_driver, network['vpc'])
            ctx.logger.info('DEBUG: VPC id: {0} '.format(vpc.id))
    else:
        vpc = None

    if not existing_net and ctx.node.properties[
            USE_EXTERNAL_RESOURCE_PROPERTY] is False:

        if vpc:
            ctx.logger.info('Creating network: {0} in VPC with ID: {1}'.
                            format(network_name, vpc.id))

            net = cloud_driver.ex_create_network(
                display_text=network_name,
                name=network_name,
                network_offering=network_offering,
                location=location,
                gateway=network.get(['gateway'][0], None),
                netmask=network.get(['netmask'][0], None),
                vpc_id=vpc.id)

            ctx.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY] = net.id
            ctx.instance.runtime_properties[CLOUDSTACK_NAME_PROPERTY] = \
                net.name
            ctx.instance.runtime_properties[CLOUDSTACK_TYPE_PROPERTY] = \
                NETWORK_CLOUDSTACK_TYPE

            # Create ACL for the network if it's is part of a VPC
            acl_list = create_acl_list(cloud_driver, network_name,
                                       vpc.id, net.id)

            if 'firewall' in ctx.node.properties:
                firewall_config = ctx.node.properties['firewall']

                for acl in firewall_config:
                    acl_cidr = acl.get('cidr')
                    acl_protocol = acl.get('protocol')
                    acl_ports = acl.get('ports')
                    acl_type = acl.get('type')

                    for port in acl_ports:
                        create_acl(cloud_driver, acl_protocol, acl_list.id,
                                   acl_cidr, port, port, acl_type)

        else:
            ctx.logger.info('Creating network: {0}'.format(network_name))

            net = cloud_driver.ex_create_network(
                display_text=network_name,
                name=network_name,
                gateway=network.get(['gateway'][0], None),
                netmask=network.get(['netmask'][0], None),
                network_offering=network_offering,
                location=location,
                )

            ctx.logger.info('Created Network: {0}'.format(net.name))

            # Create egress rules only as they are part of a network,
            # ingress rules are bound to a floating/public_ip so,
            # this will get arranged on the floating ip relationship

            _create_egress_rules(ctx, cloud_driver, net.id)

            ctx.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY] = net.id
            ctx.instance.runtime_properties[CLOUDSTACK_NAME_PROPERTY] = \
                net.name
            ctx.instance.runtime_properties[CLOUDSTACK_TYPE_PROPERTY] = \
                NETWORK_CLOUDSTACK_TYPE

    elif existing_net and ctx.node.properties[
            USE_EXTERNAL_RESOURCE_PROPERTY] is False:

            net = get_network(cloud_driver, network_name)

            ctx.logger.info('Using existing network: {0}'.
                            format(network_name))

            _create_egress_rules(ctx, cloud_driver, net.id)

            ctx.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY] = net.id
            ctx.instance.runtime_properties[CLOUDSTACK_NAME_PROPERTY] = \
                net.name
            ctx.instance.runtime_properties[CLOUDSTACK_TYPE_PROPERTY] = \
                NETWORK_CLOUDSTACK_TYPE

    elif existing_net and ctx.node.properties[
            USE_EXTERNAL_RESOURCE_PROPERTY] is True:

        net = get_network(cloud_driver, network_name)

        ctx.logger.warn('Using existing network: {0} while use'
                        '_external_resource'
                        ' is set to False, no egress rules will '
                        'be configured'.format(network_name))

        ctx.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY] = net.id
        ctx.instance.runtime_properties[CLOUDSTACK_NAME_PROPERTY] = \
            net.name
        ctx.instance.runtime_properties[CLOUDSTACK_TYPE_PROPERTY] = \
            NETWORK_CLOUDSTACK_TYPE


@operation
def delete(ctx, **kwargs):

    network_name = ctx.instance.runtime_properties[CLOUDSTACK_NAME_PROPERTY]
    cloud_driver = get_cloud_driver(ctx)
    network = get_network(cloud_driver, network_name)

    if not ctx.node.properties[USE_EXTERNAL_RESOURCE_PROPERTY] is True:

        # firewall_rules = [rule for rule in cloud_driver.
        #                   ex_list_egress_firewall_rules() if
        #                   network.id == rule.network_id]
        #
        # for rule in firewall_rules:
        #
        #     ctx.logger.info('Deleting egress fw rule: {3}:{0}:{1}-{2} '
        #                     'from network: {4}'.format(
        #                     rule.cidr_list, rule.start_port, rule.end_port,
        #                     rule.protocol, network_name))
        #
        #     cloud_driver.ex_delete_egress_firewall_rule(rule)

        try:

            cloud_driver.ex_delete_network(network)
        except Exception as e:
            ctx.logger.warn('Network {0} may not have been deleted: {1}'
                            .format(network_name, str(e)))
            return False
            pass
    else:

        ctx.logger.info('This is an external resource only removing '
                        'runtime props')
    delete_runtime_properties(ctx, RUNTIME_PROPERTIES_KEYS)
    return True


def get_network_offering(cloud_driver, netoffer_name):
    netoffers = [offer for offer in cloud_driver
                 .ex_list_network_offerings() if offer.name == netoffer_name]
    if netoffers.__len__() == 0:
        return None
    return netoffers[0]


def get_network(cloud_driver, network_name):
    networks = [net for net in cloud_driver
                .ex_list_networks() if net.name == network_name]

    if networks.__len__() == 0:
        return None
    return networks[0]


def network_exists(cloud_driver, network_name):
    exists = get_network(cloud_driver, network_name)
    if not exists:
        return False
    return True


def get_network_by_id(ctx, cloud_driver, network_id):

    networks = [network for network in cloud_driver.ex_list_networks() if
                network_id == network.id]

    if not networks:
        ctx.logger.info('could not find network by ID {0}'.format(network_id))
        return None

    return networks[0]


def _create_egress_rules(ctx, cloud_driver, network_id):

    if 'firewall' in ctx.node.properties:
                firewall_config = ctx.node.properties['firewall']

                egress_rules = [rule for rule in firewall_config if
                                rule['type'] == 'egress']

                for rule in egress_rules:
                    rule_cidr = rule.get('cidr')
                    rule_protocol = rule.get('protocol')
                    rule_ports = rule.get('ports')

                    for port in rule_ports:
                        ctx.logger.info('Creating egress fw rule:'
                                        ' {3}:{0}:{1}-{2}'.format(
                                        rule_cidr,
                                        port,
                                        port,
                                        rule_protocol))

                        try:

                            cloud_driver.ex_create_egress_firewall_rule(
                                network_id=network_id,
                                cidr_list=rule_cidr,
                                protocol=rule_protocol,
                                start_port=port,
                                end_port=port
                            )

                        except Exception as e:
                            ctx.logger.warn('Could not create egress'
                                            ' firewall rule: {0}'
                                            .format(str(e)))
                            return
