#!/usr/bin/env python3
#
# Copyright (C) 2019-2020 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import re
import tempfile

from cryptography.hazmat.primitives.asymmetric import ec
from glob import glob
from sys import exit
from ipaddress import IPv4Address
from ipaddress import IPv4Network
from ipaddress import IPv6Address
from ipaddress import IPv6Network
from ipaddress import summarize_address_range
from pathlib import Path
from netifaces import interfaces
from secrets import SystemRandom
from shutil import rmtree

from vyos.config import Config
from vyos.configdict import get_interface_dict
from vyos.configverify import verify_vrf
from vyos.configverify import verify_bridge_delete
from vyos.ifconfig import VTunIf
from vyos.pki import load_dh_parameters
from vyos.pki import load_private_key
from vyos.pki import wrap_certificate
from vyos.pki import wrap_crl
from vyos.pki import wrap_dh_parameters
from vyos.pki import wrap_openvpn_key
from vyos.pki import wrap_private_key
from vyos.template import render
from vyos.template import is_ipv4
from vyos.template import is_ipv6
from vyos.util import call
from vyos.util import chown
from vyos.util import chmod_600
from vyos.util import dict_search
from vyos.util import dict_search_args
from vyos.validate import is_addr_assigned

from vyos import ConfigError
from vyos import airbag
airbag.enable()

user = 'openvpn'
group = 'openvpn'

cfg_dir = '/run/openvpn'
cfg_file = '/run/openvpn/{ifname}.conf'
otp_path = '/config/auth/openvpn'
otp_file = '/config/auth/openvpn/{ifname}-otp-secrets'
secret_chars = list('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567')

def get_config(config=None):
    """
    Retrive CLI config as dictionary. Dictionary can never be empty, as at least the
    interface name will be added or a deleted flag
    """
    if config:
        conf = config
    else:
        conf = Config()
    base = ['interfaces', 'openvpn']

    tmp_pki = conf.get_config_dict(['pki'], key_mangling=('-', '_'),
                                get_first_key=True, no_tag_node_value_mangle=True)

    openvpn = get_interface_dict(conf, base)

    if 'deleted' not in openvpn:
        openvpn['pki'] = tmp_pki

    openvpn['auth_user_pass_file'] = '/run/openvpn/{ifname}.pw'.format(**openvpn)
    openvpn['daemon_user'] = user
    openvpn['daemon_group'] = group

    return openvpn

def is_ec_private_key(pki, cert_name):
    if not pki or 'certificate' not in pki:
        return False
    if cert_name not in pki['certificate']:
        return False

    pki_cert = pki['certificate'][cert_name]
    if 'private' not in pki_cert or 'key' not in pki_cert['private']:
        return False

    key = load_private_key(pki_cert['private']['key'])
    return isinstance(key, ec.EllipticCurvePrivateKey)

def verify_pki(openvpn):
    pki = openvpn['pki']
    interface = openvpn['ifname']
    mode = openvpn['mode']
    shared_secret_key = dict_search_args(openvpn, 'shared_secret_key')
    tls = dict_search_args(openvpn, 'tls')

    if not bool(shared_secret_key) ^ bool(tls): #  xor check if only one is set
        raise ConfigError('Must specify only one of "shared-secret-key" and "tls"')

    if mode in ['server', 'client'] and not tls:
        raise ConfigError('Must specify "tls" for server and client modes')

    if not pki:
        raise ConfigError('PKI is not configured')

    if shared_secret_key:
        if not dict_search_args(pki, 'openvpn', 'shared_secret'):
            raise ConfigError('There are no openvpn shared-secrets in PKI configuration')

        if shared_secret_key not in pki['openvpn']['shared_secret']:
            raise ConfigError(f'Invalid shared-secret on openvpn interface {interface}')

    if tls:
        if 'ca_certificate' not in tls:
            raise ConfigError(f'Must specify "tls ca-certificate" on openvpn interface {interface}')

        if tls['ca_certificate'] not in pki['ca']:
            raise ConfigError(f'Invalid CA certificate on openvpn interface {interface}')

        if not (mode == 'client' and 'auth_key' in tls):
            if 'certificate' not in tls:
                raise ConfigError(f'Missing "tls certificate" on openvpn interface {interface}')

        if 'certificate' in tls:
            if tls['certificate'] not in pki['certificate']:
                raise ConfigError(f'Invalid certificate on openvpn interface {interface}')

            if dict_search_args(pki, 'certificate', tls['certificate'], 'private', 'password_protected'):
                raise ConfigError(f'Cannot use encrypted private key on openvpn interface {interface}')

            if mode == 'server' and 'dh_params' not in tls and not is_ec_private_key(pki, tls['certificate']):
                raise ConfigError('Must specify "tls dh-params" when not using EC keys in server mode')

        if 'dh_params' in tls:
            if 'dh' not in pki:
                raise ConfigError('There are no DH parameters in PKI configuration')

            if tls['dh_params'] not in pki['dh']:
                raise ConfigError(f'Invalid dh-params on openvpn interface {interface}')

            pki_dh = pki['dh'][tls['dh_params']]
            dh_params = load_dh_parameters(pki_dh['parameters'])
            dh_numbers = dh_params.parameter_numbers()
            dh_bits = dh_numbers.p.bit_length()

            if dh_bits < 2048:
                raise ConfigError(f'Minimum DH key-size is 2048 bits')

        if 'auth_key' in tls or 'crypt_key' in tls:
            if not dict_search_args(pki, 'openvpn', 'shared_secret'):
                raise ConfigError('There are no openvpn shared-secrets in PKI configuration')

        if 'auth_key' in tls:
            if tls['auth_key'] not in pki['openvpn']['shared_secret']:
                raise ConfigError(f'Invalid auth-key on openvpn interface {interface}')

        if 'crypt_key' in tls:
            if tls['crypt_key'] not in pki['openvpn']['shared_secret']:
                raise ConfigError(f'Invalid crypt-key on openvpn interface {interface}')

def verify(openvpn):
    if 'deleted' in openvpn:
        verify_bridge_delete(openvpn)
        return None

    if 'mode' not in openvpn:
        raise ConfigError('Must specify OpenVPN operation mode!')

    #
    # OpenVPN client mode - VERIFY
    #
    if openvpn['mode'] == 'client':
        if 'local_port' in openvpn:
            raise ConfigError('Cannot specify "local-port" in client mode')

        if 'local_host' in openvpn:
            raise ConfigError('Cannot specify "local-host" in client mode')

        if 'remote_host' not in openvpn:
            raise ConfigError('Must specify "remote-host" in client mode')

        if openvpn['protocol'] == 'tcp-passive':
            raise ConfigError('Protocol "tcp-passive" is not valid in client mode')

        if dict_search('tls.dh_params', openvpn):
            raise ConfigError('Cannot specify "tls dh-params" in client mode')

    #
    # OpenVPN site-to-site - VERIFY
    #
    elif openvpn['mode'] == 'site-to-site':
        if 'local_address' not in openvpn and 'is_bridge_member' not in openvpn:
            raise ConfigError('Must specify "local-address" or add interface to bridge')

        if len([addr for addr in openvpn['local_address'] if is_ipv4(addr)]) > 1:
            raise ConfigError('Only one IPv4 local-address can be specified')

        if len([addr for addr in openvpn['local_address'] if is_ipv6(addr)]) > 1:
            raise ConfigError('Only one IPv6 local-address can be specified')

        if openvpn['device_type'] == 'tun':
            if 'remote_address' not in openvpn:
                raise ConfigError('Must specify "remote-address"')

        if 'remote_address' in openvpn:
            if len([addr for addr in openvpn['remote_address'] if is_ipv4(addr)]) > 1:
                raise ConfigError('Only one IPv4 remote-address can be specified')

            if len([addr for addr in openvpn['remote_address'] if is_ipv6(addr)]) > 1:
                raise ConfigError('Only one IPv6 remote-address can be specified')

            if not 'local_address' in openvpn:
                raise ConfigError('"remote-address" requires "local-address"')

            v4loAddr = [addr for addr in openvpn['local_address'] if is_ipv4(addr)]
            v4remAddr = [addr for addr in openvpn['remote_address'] if is_ipv4(addr)]
            if v4loAddr and not v4remAddr:
                raise ConfigError('IPv4 "local-address" requires IPv4 "remote-address"')
            elif v4remAddr and not v4loAddr:
                raise ConfigError('IPv4 "remote-address" requires IPv4 "local-address"')

            v6remAddr = [addr for addr in openvpn['remote_address'] if is_ipv6(addr)]
            v6loAddr = [addr for addr in openvpn['local_address'] if is_ipv6(addr)]
            if v6loAddr and not v6remAddr:
                raise ConfigError('IPv6 "local-address" requires IPv6 "remote-address"')
            elif v6remAddr and not v6loAddr:
                raise ConfigError('IPv6 "remote-address" requires IPv6 "local-address"')

            if (v4loAddr == v4remAddr) or (v6remAddr == v4remAddr):
                raise ConfigError('"local-address" and "remote-address" cannot be the same')

            if dict_search('local_host', openvpn) in dict_search('local_address', openvpn):
                raise ConfigError('"local-address" cannot be the same as "local-host"')

            if dict_search('remote_host', openvpn) in dict_search('remote_address', openvpn):
                raise ConfigError('"remote-address" and "remote-host" can not be the same')

        if openvpn['device_type'] == 'tap':
            # we can only have one local_address, this is ensured above
            v4addr = None
            for laddr in openvpn['local_address']:
                if is_ipv4(laddr):
                    v4addr = laddr
                    break

            if v4addr in openvpn['local_address'] and 'subnet_mask' not in openvpn['local_address'][v4addr]:
                raise ConfigError('Must specify IPv4 "subnet-mask" for local-address')

        if dict_search('encryption.ncp_ciphers', openvpn):
            raise ConfigError('NCP ciphers can only be used in client or server mode')

    else:
        # checks for client-server or site-to-site bridged
        if 'local_address' in openvpn or 'remote_address' in openvpn:
            raise ConfigError('Cannot specify "local-address" or "remote-address" ' \
                              'in client/server or bridge mode')

    #
    # OpenVPN server mode - VERIFY
    #
    if openvpn['mode'] == 'server':
        if openvpn['protocol'] == 'tcp-active':
            raise ConfigError('Protocol "tcp-active" is not valid in server mode')

        if 'remote_port' in openvpn:
            raise ConfigError('Cannot specify "remote-port" in server mode')

        if 'remote_host' in openvpn:
            raise ConfigError('Cannot specify "remote-host" in server mode')

        tmp = dict_search('server.subnet', openvpn)
        if tmp:
            v4_subnets = len([subnet for subnet in tmp if is_ipv4(subnet)])
            v6_subnets = len([subnet for subnet in tmp if is_ipv6(subnet)])
            if v4_subnets > 1:
                raise ConfigError('Cannot specify more than 1 IPv4 server subnet')
            if v6_subnets > 1:
                raise ConfigError('Cannot specify more than 1 IPv6 server subnet')

            if v6_subnets > 0 and v4_subnets == 0:
                raise ConfigError('IPv6 server requires an IPv4 server subnet')

            for subnet in tmp:
                if is_ipv4(subnet):
                    subnet = IPv4Network(subnet)

                    if openvpn['device_type'] == 'tun' and subnet.prefixlen > 29:
                        raise ConfigError('Server subnets smaller than /29 with device type "tun" are not supported')
                    elif openvpn['device_type'] == 'tap' and subnet.prefixlen > 30:
                        raise ConfigError('Server subnets smaller than /30 with device type "tap" are not supported')

                    for client in (dict_search('client', openvpn) or []):
                        if client['ip'] and not IPv4Address(client['ip'][0]) in subnet:
                            raise ConfigError(f'Client "{client["name"]}" IP {client["ip"][0]} not in server subnet {subnet}')

        else:
            if 'is_bridge_member' not in openvpn:
                raise ConfigError('Must specify "server subnet" or add interface to bridge in server mode')

        if hasattr(dict_search('server.client', openvpn), '__iter__'):
            for client_k, client_v in dict_search('server.client', openvpn).items():
                if (client_v.get('ip') and len(client_v['ip']) > 1) or (client_v.get('ipv6_ip') and len(client_v['ipv6_ip']) > 1):
                    raise ConfigError(f'Server client "{client_k}": cannot specify more than 1 IPv4 and 1 IPv6 IP')

        if dict_search('server.client_ip_pool', openvpn):
            if not (dict_search('server.client_ip_pool.start', openvpn) and dict_search('server.client_ip_pool.stop', openvpn)):
                raise ConfigError('Server client-ip-pool requires both start and stop addresses')
            else:
                v4PoolStart = IPv4Address(dict_search('server.client_ip_pool.start', openvpn))
                v4PoolStop = IPv4Address(dict_search('server.client_ip_pool.stop', openvpn))
                if v4PoolStart > v4PoolStop:
                    raise ConfigError(f'Server client-ip-pool start address {v4PoolStart} is larger than stop address {v4PoolStop}')

                v4PoolSize = int(v4PoolStop) - int(v4PoolStart)
                if v4PoolSize >= 65536:
                    raise ConfigError(f'Server client-ip-pool is too large [{v4PoolStart} -> {v4PoolStop} = {v4PoolSize}], maximum is 65536 addresses.')

                v4PoolNets = list(summarize_address_range(v4PoolStart, v4PoolStop))
                for client in (dict_search('client', openvpn) or []):
                    if client['ip']:
                        for v4PoolNet in v4PoolNets:
                            if IPv4Address(client['ip'][0]) in v4PoolNet:
                                print(f'Warning: Client "{client["name"]}" IP {client["ip"][0]} is in server IP pool, it is not reserved for this client.')

        for subnet in (dict_search('server.subnet', openvpn) or []):
            if is_ipv6(subnet):
                tmp = dict_search('client_ipv6_pool.base', openvpn)
                if tmp:
                    if not dict_search('server.client_ip_pool', openvpn):
                        raise ConfigError('IPv6 server pool requires an IPv4 server pool')

                    if int(tmp.split('/')[1]) >= 112:
                        raise ConfigError('IPv6 server pool must be larger than /112')

                    #
                    # todo - weird logic
                    #
                    v6PoolStart = IPv6Address(tmp)
                    v6PoolStop = IPv6Network((v6PoolStart, openvpn['server_ipv6_pool_prefixlen']), strict=False)[-1] # don't remove the parentheses, it's a 2-tuple
                    v6PoolSize = int(v6PoolStop) - int(v6PoolStart) if int(openvpn['server_ipv6_pool_prefixlen']) > 96 else 65536
                    if v6PoolSize < v4PoolSize:
                        raise ConfigError(f'IPv6 server pool must be at least as large as the IPv4 pool (current sizes: IPv6={v6PoolSize} IPv4={v4PoolSize})')

                    v6PoolNets = list(summarize_address_range(v6PoolStart, v6PoolStop))
                    for client in (dict_search('client', openvpn) or []):
                        if client['ipv6_ip']:
                            for v6PoolNet in v6PoolNets:
                                if IPv6Address(client['ipv6_ip'][0]) in v6PoolNet:
                                    print(f'Warning: Client "{client["name"]}" IP {client["ipv6_ip"][0]} is in server IP pool, it is not reserved for this client.')

        # add 2fa users to the file the 2fa plugin uses
        if dict_search('server.2fa.totp', openvpn):
            if not Path(otp_file.format(**openvpn)).is_file():
                Path(otp_path).mkdir(parents=True, exist_ok=True)
                Path(otp_file.format(**openvpn)).touch()

            with tempfile.TemporaryFile(mode='w+') as fp:
                with open(otp_file.format(**openvpn), 'r+') as f:
                    ovpn_users = f.readlines()
                    for client in (dict_search('server.client', openvpn) or []):
                        exists = None
                        for ovpn_user in ovpn_users:
                            if re.search('^' + client + ' ', user):
                                fp.write(ovpn_user)
                                exists = 'true'

                        if not exists:
                            random = SystemRandom()
                            totp_secret = ''.join(random.choice(secret_chars) for _ in range(16))
                            fp.write("{0} otp totp:sha1:base32:{1}::xxx *\n".format(client, totp_secret))

                    f.seek(0)
                    fp.seek(0)
                    for tmp_user in fp.readlines():
                        f.write(tmp_user)
                    f.truncate()

            chown(otp_file.format(**openvpn), user, group)

    else:
        # checks for both client and site-to-site go here
        if dict_search('server.reject_unconfigured_clients', openvpn):
            raise ConfigError('Option reject-unconfigured-clients only supported in server mode')

        if 'replace_default_route' in openvpn and 'remote_host' not in openvpn:
            raise ConfigError('Cannot set "replace-default-route" without "remote-host"')

    #
    # OpenVPN common verification section
    # not depending on any operation mode
    #

    # verify specified IP address is present on any interface on this system
    if 'local_host' in openvpn:
        if not is_addr_assigned(openvpn['local_host']):
            raise ConfigError('local-host IP address "{local_host}" not assigned' \
                              ' to any interface'.format(**openvpn))

    # TCP active
    if openvpn['protocol'] == 'tcp-active':
        if 'local_port' in openvpn:
            raise ConfigError('Cannot specify "local-port" with "tcp-active"')

        if 'remote_host' not in openvpn:
            raise ConfigError('Must specify "remote-host" with "tcp-active"')

    #
    # TLS/encryption
    #
    if 'shared_secret_key' in openvpn:
        if dict_search('encryption.cipher', openvpn) in ['aes128gcm', 'aes192gcm', 'aes256gcm']:
            raise ConfigError('GCM encryption with shared-secret-key not supported')

    if 'tls' in openvpn:
        if {'auth_key', 'crypt_key'} <= set(openvpn['tls']):
            raise ConfigError('TLS auth and crypt keys are mutually exclusive')

        tmp = dict_search('tls.role', openvpn)
        if tmp:
            if openvpn['mode'] in ['client', 'server']:
                if not dict_search('tls.auth_key', openvpn):
                    raise ConfigError('Cannot specify "tls role" in client-server mode')

            if tmp == 'active':
                if openvpn['protocol'] == 'tcp-passive':
                    raise ConfigError('Cannot specify "tcp-passive" when "tls role" is "active"')

                if dict_search('tls.dh_params', openvpn):
                    raise ConfigError('Cannot specify "tls dh-params" when "tls role" is "active"')

            elif tmp == 'passive':
                if openvpn['protocol'] == 'tcp-active':
                    raise ConfigError('Cannot specify "tcp-active" when "tls role" is "passive"')

                if not dict_search('tls.dh_params', openvpn):
                    raise ConfigError('Must specify "tls dh-params" when "tls role" is "passive"')

        if 'certificate' in openvpn['tls'] and is_ec_private_key(openvpn['pki'], openvpn['tls']['certificate']):
            if 'dh_params' in openvpn['tls']:
                print('Warning: using dh-params and EC keys simultaneously will ' \
                      'lead to DH ciphers being used instead of ECDH')

    if dict_search('encryption.cipher', openvpn) == 'none':
        print('Warning: "encryption none" was specified!')
        print('No encryption will be performed and data is transmitted in ' \
              'plain text over the network!')

    verify_pki(openvpn)

    #
    # Auth user/pass
    #
    if (dict_search('authentication.username', openvpn) and not
        dict_search('authentication.password', openvpn)):
            raise ConfigError('Password for authentication is missing')

    if (dict_search('authentication.password', openvpn) and not
        dict_search('authentication.username', openvpn)):
            raise ConfigError('Username for authentication is missing')

    verify_vrf(openvpn)

    return None

def generate_pki_files(openvpn):
    pki = openvpn['pki']

    if not pki:
        return None

    interface = openvpn['ifname']
    shared_secret_key = dict_search_args(openvpn, 'shared_secret_key')
    tls = dict_search_args(openvpn, 'tls')

    files = []

    if shared_secret_key:
        pki_key = pki['openvpn']['shared_secret'][shared_secret_key]
        key_path = os.path.join(cfg_dir, f'{interface}_shared.key')

        with open(key_path, 'w') as f:
            f.write(wrap_openvpn_key(pki_key['key']))

        files.append(key_path)

    if tls:
        if 'ca_certificate' in tls:
            cert_name = tls['ca_certificate']
            pki_ca = pki['ca'][cert_name]

            if 'certificate' in pki_ca:
                cert_path = os.path.join(cfg_dir, f'{interface}_ca.pem')

                with open(cert_path, 'w') as f:
                    f.write(wrap_certificate(pki_ca['certificate']))

                files.append(cert_path)

            if 'crl' in pki_ca:
                for crl in pki_ca['crl']:
                    crl_path = os.path.join(cfg_dir, f'{interface}_crl.pem')

                    with open(crl_path, 'w') as f:
                        f.write(wrap_crl(crl))

                    files.append(crl_path)
                openvpn['tls']['crl'] = True

        if 'certificate' in tls:
            cert_name = tls['certificate']
            pki_cert = pki['certificate'][cert_name]

            if 'certificate' in pki_cert:
                cert_path = os.path.join(cfg_dir, f'{interface}_cert.pem')

                with open(cert_path, 'w') as f:
                    f.write(wrap_certificate(pki_cert['certificate']))

                files.append(cert_path)

            if 'private' in pki_cert and 'key' in pki_cert['private']:
                key_path = os.path.join(cfg_dir, f'{interface}_cert.key')

                with open(key_path, 'w') as f:
                    f.write(wrap_private_key(pki_cert['private']['key']))

                files.append(key_path)
                openvpn['tls']['private_key'] = True

        if 'dh_params' in tls:
            dh_name = tls['dh_params']
            pki_dh = pki['dh'][dh_name]

            if 'parameters' in pki_dh:
                dh_path = os.path.join(cfg_dir, f'{interface}_dh.pem')

                with open(dh_path, 'w') as f:
                    f.write(wrap_dh_parameters(pki_dh['parameters']))

                files.append(dh_path)

        if 'auth_key' in tls:
            key_name = tls['auth_key']
            pki_key = pki['openvpn']['shared_secret'][key_name]

            if 'key' in pki_key:
                key_path = os.path.join(cfg_dir, f'{interface}_auth.key')

                with open(key_path, 'w') as f:
                    f.write(wrap_openvpn_key(pki_key['key']))

                files.append(key_path)

        if 'crypt_key' in tls:
            key_name = tls['crypt_key']
            pki_key = pki['openvpn']['shared_secret'][key_name]

            if 'key' in pki_key:
                key_path = os.path.join(cfg_dir, f'{interface}_crypt.key')

                with open(key_path, 'w') as f:
                    f.write(wrap_openvpn_key(pki_key['key']))

                files.append(key_path)

    return files


def generate(openvpn):
    interface = openvpn['ifname']
    directory = os.path.dirname(cfg_file.format(**openvpn))

    # we can't know in advance which clients have been removed,
    # thus all client configs will be removed and re-added on demand
    ccd_dir = os.path.join(directory, 'ccd', interface)
    if os.path.isdir(ccd_dir):
        rmtree(ccd_dir, ignore_errors=True)

    if 'deleted' in openvpn or 'disable' in openvpn:
        return None

    # create client config directory on demand
    if not os.path.exists(ccd_dir):
        os.makedirs(ccd_dir, 0o755)
        chown(ccd_dir, user, group)

    # Fix file permissons for keys
    fix_permissions = generate_pki_files(openvpn)

    # Generate User/Password authentication file
    if 'authentication' in openvpn:
        render(openvpn['auth_user_pass_file'], 'openvpn/auth.pw.tmpl', openvpn,
               user=user, group=group, permission=0o600)
    else:
        # delete old auth file if present
        if os.path.isfile(openvpn['auth_user_pass_file']):
            os.remove(openvpn['auth_user_pass_file'])

    # Generate client specific configuration
    server_client = dict_search_args(openvpn, 'server', 'client')
    if server_client:
        for client, client_config in server_client.items():
            client_file = os.path.join(ccd_dir, client)

            # Our client need's to know its subnet mask ...
            client_config['server_subnet'] = dict_search('server.subnet', openvpn)

            render(client_file, 'openvpn/client.conf.tmpl', client_config,
                   user=user, group=group)

    # we need to support quoting of raw parameters from OpenVPN CLI
    # see https://phabricator.vyos.net/T1632
    render(cfg_file.format(**openvpn), 'openvpn/server.conf.tmpl', openvpn,
           formater=lambda _: _.replace("&quot;", '"'), user=user, group=group)

    # Fixup file permissions
    for file in fix_permissions:
        chmod_600(file)

    return None

def apply(openvpn):
    interface = openvpn['ifname']
    call(f'systemctl stop openvpn@{interface}.service')

    # Do some cleanup when OpenVPN is disabled/deleted
    if 'deleted' in openvpn or 'disable' in openvpn:
        for cleanup_file in glob(f'/run/openvpn/{interface}.*'):
            if os.path.isfile(cleanup_file):
                os.unlink(cleanup_file)

        if interface in interfaces():
            VTunIf(interface).remove()

        return None

    # No matching OpenVPN process running - maybe it got killed or none
    # existed - nevertheless, spawn new OpenVPN process
    call(f'systemctl start openvpn@{interface}.service')

    o = VTunIf(**openvpn)
    o.update(openvpn)

    return None


if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)

