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
from time import sleep

from cloudify import context
from cloudify.decorators import operation
from cloudify.exceptions import NonRecoverableError
from cloudstack_plugin.cloudstack_common import (
    CLOUDSTACK_ID_PROPERTY,
    CLOUDSTACK_NAME_PROPERTY,
    CLOUDSTACK_TYPE_PROPERTY,
    COMMON_RUNTIME_PROPERTIES_KEYS,
    USE_EXTERNAL_RESOURCE_PROPERTY,
    delete_runtime_properties,
    get_cloud_driver,
    get_cloudstack_ids_of_connected_nodes_by_cloudstack_type,
    get_location,
    get_nic_by_node_and_network_id,
    get_resource_id,
    provider
)
from cloudstack_plugin.keypair import KEYPAIR_CLOUDSTACK_TYPE, get_key_pair
from cloudstack_plugin.network import (
    NETWORK_CLOUDSTACK_TYPE,
    get_network,
    get_network_by_id
)
from cloudstack_plugin.volume import get_volume_by_id
from libcloud.compute.types import Provider


__author__ = 'adaml, boul'


SERVER_CLOUDSTACK_TYPE = 'VM'
NETWORKINGTYPE_CLOUDSTACK_TYPE = 'networking_type'

# Runtime properties
NETWORKS_PROPERTY = 'networks'  # all of the server's ips
IP_PROPERTY = 'ip'  # the server's private ip
RUNTIME_PROPERTIES_KEYS = COMMON_RUNTIME_PROPERTIES_KEYS + \
    [NETWORKS_PROPERTY, IP_PROPERTY, ]



@operation
def create(ctx, **kwargs):

    network_ids = get_cloudstack_ids_of_connected_nodes_by_cloudstack_type(
        ctx, NETWORK_CLOUDSTACK_TYPE)

    provider_context = provider(ctx)

    ctx.logger.info('Network IDs: {0}'.format(network_ids))

    # Cloudstack does not support _underscore in vm-name

    server_config = {
        'name': get_resource_id(ctx, SERVER_CLOUDSTACK_TYPE).replace('_', '-')
    }
    server_config.update(copy.deepcopy(ctx.node.properties['server']))

    ctx.logger.info("Initializing {0} cloud driver"
                    .format(Provider.CLOUDSTACK))
    cloud_driver = get_cloud_driver(ctx)

    # TODO Currently a generated network name (resource_id) \
    # TODO is not support for the default network

    network_config = ctx.node.properties['network']
    name = server_config['name']
    image_id = server_config['image_id']
    size_name = server_config['size']
    zone = server_config.get(['zone'][0], None)

    if zone is not None:
        location = get_location(cloud_driver, zone)
    else:
        location = None

    # server keypair handling
    # Cloudstack does not have id's for keys, just unique names which we store
    # as id.
    keypair_id = get_cloudstack_ids_of_connected_nodes_by_cloudstack_type(
        ctx, KEYPAIR_CLOUDSTACK_TYPE)

    if 'key_name' in server_config:
        if keypair_id:
            raise NonRecoverableError("server can't both have the "
                                      '"key_name" nested property and be '
                                      'connected to a keypair via a '
                                      'relationship at the same time')
        #server_config['key_name'] = rename(server_config['key_name'])
    elif keypair_id:

        # TODO pointfix, this must be UTF8, otherwise cloudstack interface breaks

        keyname = keypair_id[0].encode('UTF8')
        server_config['key_name'] = keyname

    elif provider_context.agents_keypair:
        server_config['key_name'] = provider_context.agents_keypair['name']
        print ('provider ')
    else:
        raise NonRecoverableError(
            'server must have a keypair, yet no keypair was connected to the '
            'server node, the "key_name" nested property'
            "wasn't used, and there is no agent keypair in the provider "
            "context")

   #keypair_name = server_config['keypair_name']
    keypair_name = server_config['key_name']
    default_security_group = network_config.get(['default_security_group'][0],
                                                None)
    default_network = network_config.get(['default_network'][0], None)
    ip_address = network_config.get(['ip_address'][0], None)
    external_id = ctx.instance.runtime_properties.get(
        [CLOUDSTACK_ID_PROPERTY][0], None)

    if external_id is not None:
        if get_vm_by_id(ctx, cloud_driver, ctx.instance.runtime_properties[
                CLOUDSTACK_ID_PROPERTY]):

            ctx.logger.info('VM already created, skipping creation')

            return

    ctx.logger.info('Getting service_offering: {0}'.format(size_name))
    sizes = [size for size in cloud_driver.list_sizes() if size.name
             == size_name]
    if sizes is None:
        raise RuntimeError(
            'Could not find service_offering with name {0}'.format(size_name))
    size = sizes[0]

    ctx.logger.info('Getting required image with ID {0}'.format(image_id))
    images = [template for template in cloud_driver.list_images()
              if image_id == template.id]
    if images is None:
        raise RuntimeError('Could not find image with ID {0}'.format(image_id))
    image = images[0]

    # TODO add check if default network is really existing!

    if default_network is None:
        if default_security_group is None:
            raise RuntimeError("We need either a default_security_group "
                               "or default_network, "
                               "none specified")

    if default_network is not None:
        if default_security_group is not None:
            raise RuntimeError("We need either a default_security_group "
                               "or default_network, "
                               "both are specified")

    if default_network is not None:

        _create_in_network(ctx=ctx,
                           cloud_driver=cloud_driver,
                           name=name,
                           image=image,
                           size=size,
                           keypair_name=keypair_name,
                           network_ids=network_ids,
                           default_network=default_network,
                           ip_address=ip_address,
                           location=location)

    if default_security_group is not None:
        ctx.logger.info('Creating this VM in default_security_group.'.
                        format(default_security_group))
        ctx.logger.info("Creating VM with the following details: {0}".format(
            server_config))
        _create_in_security_group(ctx=ctx,
                                  cloud_driver=cloud_driver,
                                  name=name,
                                  image=image,
                                  size=size,
                                  keypair_name=keypair_name,
                                  default_security_group_name=
                                  default_security_group,
                                  ip_address=ip_address,
                                  location=location)


def _create_in_network(ctx, cloud_driver, name, image, size, keypair_name,
                       network_ids, default_network, ip_address=None,
                       location=None):

    network_list = cloud_driver.ex_list_networks()

    # Set default network as first network in the list
    nets = [get_network(cloud_driver, default_network)]
    nets.extend([net for net in network_list if net.id in network_ids])

    #Remove duplicates this results in non-default networks e.g. without
    #default gateway
    seen_nets = set()
    dedup_nets = []
    for obj in nets:

        if obj.id not in seen_nets:
            dedup_nets.append(obj)
            seen_nets.add(obj.id)

    for i in range(len(dedup_nets)):
        if i == 0:
            ctx.logger.info('Adding VM with default_network: {0}'
                            .format(dedup_nets[i].name))
        else:
            ctx.logger.info('Adding VM to additional network: {0}'
                            .format(dedup_nets[i].name))

    try:
        node = cloud_driver.create_node(name=name,
                                        image=image,
                                        size=size,
                                        ex_keyname=keypair_name,
                                        ex_displayname=name,
                                        networks=dedup_nets,
                                        ex_ip_address=ip_address,
                                        ex_start_vm=False,
                                        location=location)
    except Exception as e:
        raise NonRecoverableError('VM creation failed: {0}'.format(str(e)))

    ctx.logger.info(
        'VM: {0} was created successfully'.format(
            node.name))

    ctx.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY] = node.id
    ctx.instance.runtime_properties[CLOUDSTACK_TYPE_PROPERTY] = \
        SERVER_CLOUDSTACK_TYPE
    ctx.instance.runtime_properties[NETWORKINGTYPE_CLOUDSTACK_TYPE] = 'network'
    ctx.instance.runtime_properties[CLOUDSTACK_NAME_PROPERTY] = node.name


@operation
def _create_in_security_group(ctx, cloud_driver, name, image, size,
                              keypair_name,
                              default_security_group_name, ip_address=None,
                              location=None):

    node = cloud_driver.create_node(name=name,
                                    image=image,
                                    size=size,
                                    ex_keyname=keypair_name,
                                    ex_security_groups=
                                    default_security_group_name,
                                    ex_start_vm=False,
                                    ex_ipaddress=ip_address,
                                    location=location)

    # Creating VM in stopped state, starting it in start_phase, this seems
    # quite a bit slower tough (vs starting on creation).
    # Starting immediatly could lead to interesting
    # condition where Cloudstack is acquire a SourceNAT address for the network
    # , while the connect_to_floating_ip relationship is acquireing an address
    # at the same time, resulting in a 'corrupt' network with two SNAT
    # addresses.

    ctx.logger.info(
        'VM: {0} was created successfully'.format(
            node.name))

    ctx.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY] = node.id
    ctx.instance.runtime_properties[CLOUDSTACK_TYPE_PROPERTY] = \
        SERVER_CLOUDSTACK_TYPE
    ctx.instance.runtime_properties[NETWORKINGTYPE_CLOUDSTACK_TYPE] = \
        'security_group'
    ctx.instance.runtime_properties[CLOUDSTACK_NAME_PROPERTY] = node.name

@operation
def start(ctx, **kwargs):
    ctx.logger.info("initializing {0} cloud driver"
                    .format(Provider.CLOUDSTACK))
    cloud_driver = get_cloud_driver(ctx)

    instance_id = ctx.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY]
    if instance_id is None:
        raise RuntimeError(
            'Could not find node ID in runtime context: {0} '
            .format(instance_id))

    node = get_vm_by_id(ctx, cloud_driver, instance_id)
    if node is None:
        raise RuntimeError('Could not find node with ID {0}'
                           .format(instance_id))

    ctx.logger.info('Starting node: {0}'.format(node.name))
    cloud_driver.ex_start(node)


@operation
def delete(ctx, **kwargs):

    ctx.logger.info("Initializing {0} cloud driver"
                    .format(Provider.CLOUDSTACK))

    server_config = ctx.node.properties['server']
    expunge = server_config.get(['expunge'][0], False)

    cloud_driver = get_cloud_driver(ctx)

    instance_id = ctx.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY]
    if instance_id is None:
        raise NameError('Could not find node ID in runtime context: {0} '
                        .format(instance_id))

    node = get_vm_by_id(ctx, cloud_driver, instance_id)
    if node is None:
        raise NameError('Could not find node with ID: {0} '
                        .format(instance_id))

    ctx.logger.info('destroying vm: {0} expunge {1}'.format(node.name,
                                                            expunge))
    cloud_driver.destroy_node(node)

    delete_runtime_properties(ctx, RUNTIME_PROPERTIES_KEYS)



@operation
def stop(ctx, **kwargs):

    ctx.logger.info("initializing {0} cloud driver"
                    .format(Provider.CLOUDSTACK))
    cloud_driver = get_cloud_driver(ctx)

    instance_id = ctx.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY]
    if instance_id is None:
        raise RuntimeError(
            'could not find node ID in runtime context: {0} '
            .format(instance_id))

    node = get_vm_by_id(ctx, cloud_driver, instance_id)
    if node is None:
        raise RuntimeError('could not find node with ID {0}'
                           .format(instance_id))

    ctx.logger.info('Stopping VM: {0}'.format(node.name))
    cloud_driver.ex_stop(node)


@operation
def get_state(ctx, **kwargs):

    ctx.logger.info("initializing {0} cloud driver"
                    .format(Provider.CLOUDSTACK))
    cloud_driver = get_cloud_driver(ctx)

    instance_id = ctx.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY]
    networking_type = ctx.instance.runtime_properties[
        NETWORKINGTYPE_CLOUDSTACK_TYPE]

    node = get_vm_by_id(ctx, cloud_driver, instance_id)
    if node is None:
        return False

    if networking_type == 'network':

        if ctx.node.properties['management_network_name']:

            ctx.logger.info('Management network defined: {0}'
                            .format(ctx.node.properties[
                            'management_network_name']))

            mgt_net = get_network(cloud_driver, ctx.node.properties[
                'management_network_name'])

            nic = get_nic_by_node_and_network_id(ctx, cloud_driver, node,
                                                 mgt_net.id)

            ctx.logger.info('CFY will use {0} for management,'
                            ' overwriting previously set value'
                            .format(nic))

            ctx.instance.runtime_properties[IP_PROPERTY] = nic.ip_address

            return True

        else:

            ctx.instance.runtime_properties[IP_PROPERTY] = node.private_ips[0]

            ctx.logger.info('VM {1} started successfully with IP {0}'
                            .format(ctx.instance.runtime_properties[IP_PROPERTY],
                                    ctx.instance.runtime_properties[
                                        CLOUDSTACK_NAME_PROPERTY]))
            return True

    elif networking_type == 'security_group':
        ctx.runtime.properties[IP_PROPERTY] = node.public_ips[0]
        ctx.logger.info('instance started successfully with IP {0}'
                        .format(ctx.instance.runtime_properties[IP_PROPERTY]))
        return True

    else:
        ctx.instance.runtime_properties[IP_PROPERTY] = node.private_ips[0]
        ctx.logger.info('Cannot determine networking type,'
                        ' using private_ip as {0} ip'
                        .format(ctx.instance.runtime_properties[IP_PROPERTY]))
        return True


@operation
def connect_network(ctx, **kwargs):

    # instance_id = ctx.source.instance.runtime_properties[
    #     CLOUDSTACK_ID_PROPERTY]
    # network_id = ctx.target.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY]
    #
    # cloud_driver = get_cloud_driver(ctx)
    #
    # node = get_node_by_id(ctx, cloud_driver, instance_id)
    # network = get_network_by_id(ctx, cloud_driver, network_id)
    # #
    # # ctx.logger.info('Checking if there is a nic for  '
    # #                 'vm: {0} with id: {1} in network {2} with id: {3}'
    # #                 .format(node.name, network.name, instance_id, network_id,))
    #
    # nic = get_nic_by_node_and_network_id(ctx, cloud_driver, node,
    #                                             network_id)
    #
    # ctx.logger.info('Adding a NIC to VM {0} in Network {1}'.format(
    #                 node.name, network.name))
    #
    # if nic is not None:
    #     ctx.logger.info('No need to connect network {0}, '
    #                     'already connected to nic {1}'
    #                     .format(network.name, nic.id))
    #     return False
    #
    # cloud_driver.ex_attach_nic_to_node(node=node, network=network)
    #
    # if ctx.source.node.properties['management_network_name']:
    #
    #         ctx.logger.info('Management network defined: {0}'
    #                         .format(ctx.node.properties[
    #                         'management_network_name']))
    #
    #         mgt_net = get_network(cloud_driver, ctx.node.properties[
    #             'management_network_name'])
    #
    #         #nic = get_nic_by_node_and_network_id(ctx, cloud_driver, node,
    #         #                                     mgt_net.id)
    #         # nics = cloud_driver.ex_list_nics(node)
    #         # mgmt_nic = [nic for nic in nics if nic.network_id == mgt_net.id]
    #
    #         ctx.logger.info('CFY will use {0} for management,'
    #                         ' overwriting previously set value'
    #                         .format(nic))
    #
    #         ctx.instance.runtime_properties[IP_PROPERTY] = nic.ip_address

    return True


@operation
def disconnect_network(ctx, **kwargs):

    # instance_id = ctx.source.instance.runtime_properties[
    #     CLOUDSTACK_ID_PROPERTY]
    # network_id = ctx.target.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY]
    #
    # cloud_driver = get_cloud_driver(ctx)
    #
    # node = get_node_by_id(ctx, cloud_driver, instance_id)
    # nic = get_nic_by_node_and_network_id(ctx, cloud_driver, node, network_id)
    #
    #
    # ctx.logger.info('Removing NIC from VM {0} in Network with: {1}'.
    #                 format(node.name, nic.network_id))
    #
    # try:
    #     cloud_driver.ex_detach_nic_from_node(nic=nic, node=node)
    # except Exception as e:
    #     ctx.logger.warn('NIC may not have been removed: {0}'.format(str(e)))
    #     return False

    return True


@operation
def connect_floating_ip(ctx, **kwargs):

    cloud_driver = get_cloud_driver(ctx)
    # network_ids = get_cloudstack_ids_of_connected_nodes_by_cloudstack_type(
    #     ctx, NETWORK_CLOUDSTACK_TYPE)

    network_config = ctx.source.node.properties['network']
    default_network_config = network_config['default_network']

    default_net = get_network(cloud_driver, default_network_config)

    ctx.logger.debug('reading portmap configuration.')
    portmaps = ctx.source.node.properties['portmaps']

    if not portmaps:
        raise NonRecoverableError('Relation defined but no portmaps set'
                                  ' either remove relation or'
                                  ' define the portmaps')

    server_id = ctx.source.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY]
    floating_ip_id = ctx.target.instance.runtime_properties[
        CLOUDSTACK_ID_PROPERTY]

    # TODO Needs more elegant solution, problem only seen on VPC with CS4.3
    # TODO Should be fixed in Cloudstack
    ctx.logger.info('Sleeping for 15 secs so router can stabilize, '
                    'so we wont have to wait for ARP timeouts '
                    'when reusing public IPs')
    sleep(15)

    for portmap in portmaps:

        protocol = portmap.get(['protocol'][0], None)
        pub_port = portmap.get(['public_port'][0], None)
        pub_end_port = portmap.get(['public_end_port'][0], None)
        priv_port = portmap.get(['private_port'][0], None)
        priv_end_port = portmap.get(['private_end_port'][0], None)

        #If not specified assume closed
        open_fw = portmap.get(['open_firewall'][0], False)

        if pub_port is None:
            raise NonRecoverableError('Please specify the public_port')
        elif pub_end_port is None:
            pub_end_port = pub_port

        if priv_port is None:
            raise NonRecoverableError('Please specify the private_port')
        elif priv_end_port is None:
            priv_end_port = priv_port

        if protocol is None:
            raise NonRecoverableError('Please specify the protocol TCP or UDP')

        node = get_vm_by_id(ctx, cloud_driver, server_id)
        public_ip = get_public_ip_by_id(ctx, cloud_driver, floating_ip_id)

        try:
            ctx.logger.info('Creating portmap for node: {0}:{1}-{2} on'
                            ' {3}:{4}-{5}'.
                            format(node.name, priv_port, priv_end_port,
                                   public_ip.address, pub_port, pub_end_port))

            cloud_driver.ex_create_port_forwarding_rule(node=node,
                                                        address=public_ip,
                                                        protocol=protocol,
                                                        public_port=pub_port,
                                                        public_end_port=
                                                        pub_end_port,
                                                        private_port=priv_port,
                                                        private_end_port=
                                                        priv_end_port,
                                                        openfirewall=open_fw,
                                                        network_id=
                                                        default_net.id)
        except Exception as e:
            ctx.logger.warn('Port forward creation failed: '
                            '{0}'.format(str(e)))
            return False

    return True


@operation
def disconnect_floating_ip(ctx, **kwargs):

    cloud_driver = get_cloud_driver(ctx)
    node_id = ctx.source.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY]
    node = get_vm_by_id(ctx, cloud_driver, node_id)
    portmaps = get_portmaps_by_vm_id(ctx, cloud_driver, node_id)

    for portmap in portmaps:

        try:

            ctx.logger.info('Deleting portmap for node: {0}:{1}-{2} on'
                            ' {3}:{4}-{5}'.
                            format(node.name, portmap.private_port,
                                   portmap.private_end_port,
                                   portmap.address, portmap.public_port,
                                   portmap.public_end_port))

            cloud_driver.ex_delete_port_forwarding_rule(node=node,
                                                        rule=portmap)
        except Exception as e:
            ctx.logger.warn('Port forward may not have been removed: '
                            '{0}'.format(str(e)))
        return False

    return True


@operation
def attach_volume(ctx, **kwargs):
    """ Attach a volume to virtual machine.
    """

    cloud_driver = get_cloud_driver(ctx)

    server_id = ctx.source.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY]
    volume_id = ctx.target.instance.runtime_properties.get(
        CLOUDSTACK_ID_PROPERTY, None)

    server = get_vm_by_id(ctx, cloud_driver, server_id)
    volume = get_volume_by_id(cloud_driver, volume_id)

    if server is None:
        raise NonRecoverableError('Server with id {0} not found'
                                  .format(server_id))

    if volume is None:
        raise NonRecoverableError('Volume with id {0} not found'
                                  .format(volume_id))

    cloud_driver.attach_volume(node=server,
                               volume=volume)


@operation
def detach_volume(ctx, **kwargs):
    '''Detaches a volume and deletes if expunge is requested.'''
    cloud_driver = get_cloud_driver(ctx)

    volume_id = ctx.target.instance.runtime_properties[CLOUDSTACK_ID_PROPERTY]
    volume = get_volume_by_id(cloud_driver, volume_id)

    if not volume:
        raise NonRecoverableError('Volume with id {0} not found'.format(
            volume_id))

    ctx.logger.info('Detaching volume {0}'.format(volume))
    cloud_driver.detach_volume(volume=volume)


def get_vm_by_id(ctx, cloud_driver, instance_id):

    nodes = [node for node in cloud_driver.list_nodes() if
             instance_id == node.id]

    if not nodes:
        ctx.logger.info('could not find node by ID {0}'.format(instance_id))
        return None

    return nodes[0]


def get_portmaps_by_vm_id(ctx, cloud_driver, node_id):

    portmaps = [portmap for portmap in
                cloud_driver.ex_list_port_forwarding_rules()
                if node_id == portmap.node.id]

    return portmaps


def get_public_ip_by_id(ctx, cloud_driver, public_ip_id):

    public_ips = [pubip for pubip in cloud_driver.ex_list_public_ips() if
                  public_ip_id == pubip.id]

    if not public_ips:
        ctx.logger.info('could not find public_ip by id {0}'
                        .format(public_ip_id))
        return None

    return public_ips[0]