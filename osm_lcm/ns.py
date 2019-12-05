# -*- coding: utf-8 -*-

##
# Copyright 2018 Telefonica S.A.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
##

import asyncio
import yaml
import logging
import logging.handlers
import traceback
from jinja2 import Environment, Template, meta, TemplateError, TemplateNotFound, TemplateSyntaxError

from osm_lcm import ROclient
from osm_lcm.lcm_utils import LcmException, LcmExceptionNoMgmtIP, LcmBase, deep_get
from n2vc.k8s_helm_conn import K8sHelmConnector
from n2vc.k8s_juju_conn import K8sJujuConnector

from osm_common.dbbase import DbException
from osm_common.fsbase import FsException

from n2vc.n2vc_juju_conn import N2VCJujuConnector
from n2vc.exceptions import N2VCException

from copy import copy, deepcopy
from http import HTTPStatus
from time import time
from uuid import uuid4

__author__ = "Alfonso Tierno"


def get_iterable(in_dict, in_key):
    """
    Similar to <dict>.get(), but if value is None, False, ..., An empty tuple is returned instead
    :param in_dict: a dictionary
    :param in_key: the key to look for at in_dict
    :return: in_dict[in_var] or () if it is None or not present
    """
    if not in_dict.get(in_key):
        return ()
    return in_dict[in_key]


def populate_dict(target_dict, key_list, value):
    """
    Update target_dict creating nested dictionaries with the key_list. Last key_list item is asigned the value.
    Example target_dict={K: J}; key_list=[a,b,c];  target_dict will be {K: J, a: {b: {c: value}}}
    :param target_dict: dictionary to be changed
    :param key_list: list of keys to insert at target_dict
    :param value:
    :return: None
    """
    for key in key_list[0:-1]:
        if key not in target_dict:
            target_dict[key] = {}
        target_dict = target_dict[key]
    target_dict[key_list[-1]] = value


class NsLcm(LcmBase):
    timeout_vca_on_error = 5 * 60   # Time for charm from first time at blocked,error status to mark as failed
    total_deploy_timeout = 2 * 3600   # global timeout for deployment
    timeout_charm_delete = 10 * 60
    timeout_primitive = 10 * 60  # timeout for primitive execution

    SUBOPERATION_STATUS_NOT_FOUND = -1
    SUBOPERATION_STATUS_NEW = -2
    SUBOPERATION_STATUS_SKIP = -3

    def __init__(self, db, msg, fs, lcm_tasks, ro_config, vca_config, loop):
        """
        Init, Connect to database, filesystem storage, and messaging
        :param config: two level dictionary with configuration. Top level should contain 'database', 'storage',
        :return: None
        """
        super().__init__(
            db=db,
            msg=msg,
            fs=fs,
            logger=logging.getLogger('lcm.ns')
        )

        self.loop = loop
        self.lcm_tasks = lcm_tasks
        self.ro_config = ro_config
        self.vca_config = vca_config
        if 'pubkey' in self.vca_config:
            self.vca_config['public_key'] = self.vca_config['pubkey']
        if 'cacert' in self.vca_config:
            self.vca_config['ca_cert'] = self.vca_config['cacert']
        if 'apiproxy' in self.vca_config:
            self.vca_config['api_proxy'] = self.vca_config['apiproxy']

        # create N2VC connector
        self.n2vc = N2VCJujuConnector(
            db=self.db,
            fs=self.fs,
            log=self.logger,
            loop=self.loop,
            url='{}:{}'.format(self.vca_config['host'], self.vca_config['port']),
            username=self.vca_config.get('user', None),
            vca_config=self.vca_config,
            on_update_db=self._on_update_n2vc_db,
            # ca_cert=self.vca_config.get('cacert'),
            # api_proxy=self.vca_config.get('apiproxy'),
        )

        self.k8sclusterhelm = K8sHelmConnector(
            kubectl_command=self.vca_config.get("kubectlpath"),
            helm_command=self.vca_config.get("helmpath"),
            fs=self.fs,
            log=self.logger,
            db=self.db,
            on_update_db=None,
        )

        self.k8sclusterjuju = K8sJujuConnector(
            kubectl_command=self.vca_config.get("kubectlpath"),
            juju_command=self.vca_config.get("jujupath"),
            fs=self.fs,
            log=self.logger,
            db=self.db,
            on_update_db=None,
        )

        # create RO client
        self.RO = ROclient.ROClient(self.loop, **self.ro_config)

    def _on_update_n2vc_db(self, table, filter, path, updated_data):

        self.logger.debug('_on_update_n2vc_db(table={}, filter={}, path={}, updated_data={}'
                          .format(table, filter, path, updated_data))

        return
        # write NS status to database
        # try:
        #     # nsrs_id = filter.get('_id')
        #     # print(nsrs_id)
        #     # get ns record
        #     nsr = self.db.get_one(table=table, q_filter=filter)
        #     # get VCA deployed list
        #     vca_list = deep_get(target_dict=nsr, key_list=('_admin', 'deployed', 'VCA'))
        #     # get RO deployed
        #     # ro_list = deep_get(target_dict=nsr, key_list=('_admin', 'deployed', 'RO'))
        #     for vca in vca_list:
        #         # status = vca.get('status')
        #         # print(status)
        #         # detailed_status = vca.get('detailed-status')
        #         # print(detailed_status)
        #     # for ro in ro_list:
        #     #    print(ro)
        #
        # except Exception as e:
        #     self.logger.error('Error writing NS status to db: {}'.format(e))

    def vnfd2RO(self, vnfd, new_id=None, additionalParams=None, nsrId=None):
        """
        Converts creates a new vnfd descriptor for RO base on input OSM IM vnfd
        :param vnfd: input vnfd
        :param new_id: overrides vnf id if provided
        :param additionalParams: Instantiation params for VNFs provided
        :param nsrId: Id of the NSR
        :return: copy of vnfd
        """
        try:
            vnfd_RO = deepcopy(vnfd)
            # remove unused by RO configuration, monitoring, scaling and internal keys
            vnfd_RO.pop("_id", None)
            vnfd_RO.pop("_admin", None)
            vnfd_RO.pop("vnf-configuration", None)
            vnfd_RO.pop("monitoring-param", None)
            vnfd_RO.pop("scaling-group-descriptor", None)
            vnfd_RO.pop("kdu", None)
            vnfd_RO.pop("k8s-cluster", None)
            if new_id:
                vnfd_RO["id"] = new_id

            # parse cloud-init or cloud-init-file with the provided variables using Jinja2
            for vdu in get_iterable(vnfd_RO, "vdu"):
                cloud_init_file = None
                if vdu.get("cloud-init-file"):
                    base_folder = vnfd["_admin"]["storage"]
                    cloud_init_file = "{}/{}/cloud_init/{}".format(base_folder["folder"], base_folder["pkg-dir"],
                                                                   vdu["cloud-init-file"])
                    with self.fs.file_open(cloud_init_file, "r") as ci_file:
                        cloud_init_content = ci_file.read()
                    vdu.pop("cloud-init-file", None)
                elif vdu.get("cloud-init"):
                    cloud_init_content = vdu["cloud-init"]
                else:
                    continue

                env = Environment()
                ast = env.parse(cloud_init_content)
                mandatory_vars = meta.find_undeclared_variables(ast)
                if mandatory_vars:
                    for var in mandatory_vars:
                        if not additionalParams or var not in additionalParams.keys():
                            raise LcmException("Variable '{}' defined at vnfd[id={}]:vdu[id={}]:cloud-init/cloud-init-"
                                               "file, must be provided in the instantiation parameters inside the "
                                               "'additionalParamsForVnf' block".format(var, vnfd["id"], vdu["id"]))
                template = Template(cloud_init_content)
                cloud_init_content = template.render(additionalParams or {})
                vdu["cloud-init"] = cloud_init_content

            return vnfd_RO
        except FsException as e:
            raise LcmException("Error reading vnfd[id={}]:vdu[id={}]:cloud-init-file={}: {}".
                               format(vnfd["id"], vdu["id"], cloud_init_file, e))
        except (TemplateError, TemplateNotFound, TemplateSyntaxError) as e:
            raise LcmException("Error parsing Jinja2 to cloud-init content at vnfd[id={}]:vdu[id={}]: {}".
                               format(vnfd["id"], vdu["id"], e))

    def ns_params_2_RO(self, ns_params, nsd, vnfd_dict, n2vc_key_list):
        """
        Creates a RO ns descriptor from OSM ns_instantiate params
        :param ns_params: OSM instantiate params
        :return: The RO ns descriptor
        """
        vim_2_RO = {}
        wim_2_RO = {}
        # TODO feature 1417: Check that no instantiation is set over PDU
        # check if PDU forces a concrete vim-network-id and add it
        # check if PDU contains a SDN-assist info (dpid, switch, port) and pass it to RO

        def vim_account_2_RO(vim_account):
            if vim_account in vim_2_RO:
                return vim_2_RO[vim_account]

            db_vim = self.db.get_one("vim_accounts", {"_id": vim_account})
            if db_vim["_admin"]["operationalState"] != "ENABLED":
                raise LcmException("VIM={} is not available. operationalState={}".format(
                    vim_account, db_vim["_admin"]["operationalState"]))
            RO_vim_id = db_vim["_admin"]["deployed"]["RO"]
            vim_2_RO[vim_account] = RO_vim_id
            return RO_vim_id

        def wim_account_2_RO(wim_account):
            if isinstance(wim_account, str):
                if wim_account in wim_2_RO:
                    return wim_2_RO[wim_account]

                db_wim = self.db.get_one("wim_accounts", {"_id": wim_account})
                if db_wim["_admin"]["operationalState"] != "ENABLED":
                    raise LcmException("WIM={} is not available. operationalState={}".format(
                        wim_account, db_wim["_admin"]["operationalState"]))
                RO_wim_id = db_wim["_admin"]["deployed"]["RO-account"]
                wim_2_RO[wim_account] = RO_wim_id
                return RO_wim_id
            else:
                return wim_account

        def ip_profile_2_RO(ip_profile):
            RO_ip_profile = deepcopy((ip_profile))
            if "dns-server" in RO_ip_profile:
                if isinstance(RO_ip_profile["dns-server"], list):
                    RO_ip_profile["dns-address"] = []
                    for ds in RO_ip_profile.pop("dns-server"):
                        RO_ip_profile["dns-address"].append(ds['address'])
                else:
                    RO_ip_profile["dns-address"] = RO_ip_profile.pop("dns-server")
            if RO_ip_profile.get("ip-version") == "ipv4":
                RO_ip_profile["ip-version"] = "IPv4"
            if RO_ip_profile.get("ip-version") == "ipv6":
                RO_ip_profile["ip-version"] = "IPv6"
            if "dhcp-params" in RO_ip_profile:
                RO_ip_profile["dhcp"] = RO_ip_profile.pop("dhcp-params")
            return RO_ip_profile

        if not ns_params:
            return None
        RO_ns_params = {
            # "name": ns_params["nsName"],
            # "description": ns_params.get("nsDescription"),
            "datacenter": vim_account_2_RO(ns_params["vimAccountId"]),
            "wim_account": wim_account_2_RO(ns_params.get("wimAccountId")),
            # "scenario": ns_params["nsdId"],
        }

        n2vc_key_list = n2vc_key_list or []
        for vnfd_ref, vnfd in vnfd_dict.items():
            vdu_needed_access = []
            mgmt_cp = None
            if vnfd.get("vnf-configuration"):
                ssh_required = deep_get(vnfd, ("vnf-configuration", "config-access", "ssh-access", "required"))
                if ssh_required and vnfd.get("mgmt-interface"):
                    if vnfd["mgmt-interface"].get("vdu-id"):
                        vdu_needed_access.append(vnfd["mgmt-interface"]["vdu-id"])
                    elif vnfd["mgmt-interface"].get("cp"):
                        mgmt_cp = vnfd["mgmt-interface"]["cp"]

            for vdu in vnfd.get("vdu", ()):
                if vdu.get("vdu-configuration"):
                    ssh_required = deep_get(vdu, ("vdu-configuration", "config-access", "ssh-access", "required"))
                    if ssh_required:
                        vdu_needed_access.append(vdu["id"])
                elif mgmt_cp:
                    for vdu_interface in vdu.get("interface"):
                        if vdu_interface.get("external-connection-point-ref") and \
                                vdu_interface["external-connection-point-ref"] == mgmt_cp:
                            vdu_needed_access.append(vdu["id"])
                            mgmt_cp = None
                            break

            if vdu_needed_access:
                for vnf_member in nsd.get("constituent-vnfd"):
                    if vnf_member["vnfd-id-ref"] != vnfd_ref:
                        continue
                    for vdu in vdu_needed_access:
                        populate_dict(RO_ns_params,
                                      ("vnfs", vnf_member["member-vnf-index"], "vdus", vdu, "mgmt_keys"),
                                      n2vc_key_list)

        if ns_params.get("vduImage"):
            RO_ns_params["vduImage"] = ns_params["vduImage"]

        if ns_params.get("ssh_keys"):
            RO_ns_params["cloud-config"] = {"key-pairs": ns_params["ssh_keys"]}
        for vnf_params in get_iterable(ns_params, "vnf"):
            for constituent_vnfd in nsd["constituent-vnfd"]:
                if constituent_vnfd["member-vnf-index"] == vnf_params["member-vnf-index"]:
                    vnf_descriptor = vnfd_dict[constituent_vnfd["vnfd-id-ref"]]
                    break
            else:
                raise LcmException("Invalid instantiate parameter vnf:member-vnf-index={} is not present at nsd:"
                                   "constituent-vnfd".format(vnf_params["member-vnf-index"]))
            if vnf_params.get("vimAccountId"):
                populate_dict(RO_ns_params, ("vnfs", vnf_params["member-vnf-index"], "datacenter"),
                              vim_account_2_RO(vnf_params["vimAccountId"]))

            for vdu_params in get_iterable(vnf_params, "vdu"):
                # TODO feature 1417: check that this VDU exist and it is not a PDU
                if vdu_params.get("volume"):
                    for volume_params in vdu_params["volume"]:
                        if volume_params.get("vim-volume-id"):
                            populate_dict(RO_ns_params, ("vnfs", vnf_params["member-vnf-index"], "vdus",
                                                         vdu_params["id"], "devices", volume_params["name"], "vim_id"),
                                          volume_params["vim-volume-id"])
                if vdu_params.get("interface"):
                    for interface_params in vdu_params["interface"]:
                        if interface_params.get("ip-address"):
                            populate_dict(RO_ns_params, ("vnfs", vnf_params["member-vnf-index"], "vdus",
                                                         vdu_params["id"], "interfaces", interface_params["name"],
                                                         "ip_address"),
                                          interface_params["ip-address"])
                        if interface_params.get("mac-address"):
                            populate_dict(RO_ns_params, ("vnfs", vnf_params["member-vnf-index"], "vdus",
                                                         vdu_params["id"], "interfaces", interface_params["name"],
                                                         "mac_address"),
                                          interface_params["mac-address"])
                        if interface_params.get("floating-ip-required"):
                            populate_dict(RO_ns_params, ("vnfs", vnf_params["member-vnf-index"], "vdus",
                                                         vdu_params["id"], "interfaces", interface_params["name"],
                                                         "floating-ip"),
                                          interface_params["floating-ip-required"])

            for internal_vld_params in get_iterable(vnf_params, "internal-vld"):
                if internal_vld_params.get("vim-network-name"):
                    populate_dict(RO_ns_params, ("vnfs", vnf_params["member-vnf-index"], "networks",
                                                 internal_vld_params["name"], "vim-network-name"),
                                  internal_vld_params["vim-network-name"])
                if internal_vld_params.get("vim-network-id"):
                    populate_dict(RO_ns_params, ("vnfs", vnf_params["member-vnf-index"], "networks",
                                                 internal_vld_params["name"], "vim-network-id"),
                                  internal_vld_params["vim-network-id"])
                if internal_vld_params.get("ip-profile"):
                    populate_dict(RO_ns_params, ("vnfs", vnf_params["member-vnf-index"], "networks",
                                                 internal_vld_params["name"], "ip-profile"),
                                  ip_profile_2_RO(internal_vld_params["ip-profile"]))
                if internal_vld_params.get("provider-network"):

                    populate_dict(RO_ns_params, ("vnfs", vnf_params["member-vnf-index"], "networks",
                                                 internal_vld_params["name"], "provider-network"),
                                  internal_vld_params["provider-network"].copy())

                for icp_params in get_iterable(internal_vld_params, "internal-connection-point"):
                    # look for interface
                    iface_found = False
                    for vdu_descriptor in vnf_descriptor["vdu"]:
                        for vdu_interface in vdu_descriptor["interface"]:
                            if vdu_interface.get("internal-connection-point-ref") == icp_params["id-ref"]:
                                if icp_params.get("ip-address"):
                                    populate_dict(RO_ns_params, ("vnfs", vnf_params["member-vnf-index"], "vdus",
                                                                 vdu_descriptor["id"], "interfaces",
                                                                 vdu_interface["name"], "ip_address"),
                                                  icp_params["ip-address"])

                                if icp_params.get("mac-address"):
                                    populate_dict(RO_ns_params, ("vnfs", vnf_params["member-vnf-index"], "vdus",
                                                                 vdu_descriptor["id"], "interfaces",
                                                                 vdu_interface["name"], "mac_address"),
                                                  icp_params["mac-address"])
                                iface_found = True
                                break
                        if iface_found:
                            break
                    else:
                        raise LcmException("Invalid instantiate parameter vnf:member-vnf-index[{}]:"
                                           "internal-vld:id-ref={} is not present at vnfd:internal-"
                                           "connection-point".format(vnf_params["member-vnf-index"],
                                                                     icp_params["id-ref"]))

        for vld_params in get_iterable(ns_params, "vld"):
            if "ip-profile" in vld_params:
                populate_dict(RO_ns_params, ("networks", vld_params["name"], "ip-profile"),
                              ip_profile_2_RO(vld_params["ip-profile"]))

            if vld_params.get("provider-network"):

                populate_dict(RO_ns_params, ("networks", vld_params["name"], "provider-network"),
                              vld_params["provider-network"].copy())

            if "wimAccountId" in vld_params and vld_params["wimAccountId"] is not None:
                populate_dict(RO_ns_params, ("networks", vld_params["name"], "wim_account"),
                              wim_account_2_RO(vld_params["wimAccountId"])),
            if vld_params.get("vim-network-name"):
                RO_vld_sites = []
                if isinstance(vld_params["vim-network-name"], dict):
                    for vim_account, vim_net in vld_params["vim-network-name"].items():
                        RO_vld_sites.append({
                            "netmap-use": vim_net,
                            "datacenter": vim_account_2_RO(vim_account)
                        })
                else:  # isinstance str
                    RO_vld_sites.append({"netmap-use": vld_params["vim-network-name"]})
                if RO_vld_sites:
                    populate_dict(RO_ns_params, ("networks", vld_params["name"], "sites"), RO_vld_sites)

            if vld_params.get("vim-network-id"):
                RO_vld_sites = []
                if isinstance(vld_params["vim-network-id"], dict):
                    for vim_account, vim_net in vld_params["vim-network-id"].items():
                        RO_vld_sites.append({
                            "netmap-use": vim_net,
                            "datacenter": vim_account_2_RO(vim_account)
                        })
                else:  # isinstance str
                    RO_vld_sites.append({"netmap-use": vld_params["vim-network-id"]})
                if RO_vld_sites:
                    populate_dict(RO_ns_params, ("networks", vld_params["name"], "sites"), RO_vld_sites)
            if vld_params.get("ns-net"):
                if isinstance(vld_params["ns-net"], dict):
                    for vld_id, instance_scenario_id in vld_params["ns-net"].items():
                        RO_vld_ns_net = {"instance_scenario_id": instance_scenario_id, "osm_id": vld_id}
                        populate_dict(RO_ns_params, ("networks", vld_params["name"], "use-network"), RO_vld_ns_net)
            if "vnfd-connection-point-ref" in vld_params:
                for cp_params in vld_params["vnfd-connection-point-ref"]:
                    # look for interface
                    for constituent_vnfd in nsd["constituent-vnfd"]:
                        if constituent_vnfd["member-vnf-index"] == cp_params["member-vnf-index-ref"]:
                            vnf_descriptor = vnfd_dict[constituent_vnfd["vnfd-id-ref"]]
                            break
                    else:
                        raise LcmException(
                            "Invalid instantiate parameter vld:vnfd-connection-point-ref:member-vnf-index-ref={} "
                            "is not present at nsd:constituent-vnfd".format(cp_params["member-vnf-index-ref"]))
                    match_cp = False
                    for vdu_descriptor in vnf_descriptor["vdu"]:
                        for interface_descriptor in vdu_descriptor["interface"]:
                            if interface_descriptor.get("external-connection-point-ref") == \
                                    cp_params["vnfd-connection-point-ref"]:
                                match_cp = True
                                break
                        if match_cp:
                            break
                    else:
                        raise LcmException(
                            "Invalid instantiate parameter vld:vnfd-connection-point-ref:member-vnf-index-ref={}:"
                            "vnfd-connection-point-ref={} is not present at vnfd={}".format(
                                cp_params["member-vnf-index-ref"],
                                cp_params["vnfd-connection-point-ref"],
                                vnf_descriptor["id"]))
                    if cp_params.get("ip-address"):
                        populate_dict(RO_ns_params, ("vnfs", cp_params["member-vnf-index-ref"], "vdus",
                                                     vdu_descriptor["id"], "interfaces",
                                                     interface_descriptor["name"], "ip_address"),
                                      cp_params["ip-address"])
                    if cp_params.get("mac-address"):
                        populate_dict(RO_ns_params, ("vnfs", cp_params["member-vnf-index-ref"], "vdus",
                                                     vdu_descriptor["id"], "interfaces",
                                                     interface_descriptor["name"], "mac_address"),
                                      cp_params["mac-address"])
        return RO_ns_params

    def scale_vnfr(self, db_vnfr, vdu_create=None, vdu_delete=None):
        # make a copy to do not change
        vdu_create = copy(vdu_create)
        vdu_delete = copy(vdu_delete)

        vdurs = db_vnfr.get("vdur")
        if vdurs is None:
            vdurs = []
        vdu_index = len(vdurs)
        while vdu_index:
            vdu_index -= 1
            vdur = vdurs[vdu_index]
            if vdur.get("pdu-type"):
                continue
            vdu_id_ref = vdur["vdu-id-ref"]
            if vdu_create and vdu_create.get(vdu_id_ref):
                for index in range(0, vdu_create[vdu_id_ref]):
                    vdur = deepcopy(vdur)
                    vdur["_id"] = str(uuid4())
                    vdur["count-index"] += 1
                    vdurs.insert(vdu_index+1+index, vdur)
                del vdu_create[vdu_id_ref]
            if vdu_delete and vdu_delete.get(vdu_id_ref):
                del vdurs[vdu_index]
                vdu_delete[vdu_id_ref] -= 1
                if not vdu_delete[vdu_id_ref]:
                    del vdu_delete[vdu_id_ref]
        # check all operations are done
        if vdu_create or vdu_delete:
            raise LcmException("Error scaling OUT VNFR for {}. There is not any existing vnfr. Scaled to 0?".format(
                vdu_create))
        if vdu_delete:
            raise LcmException("Error scaling IN VNFR for {}. There is not any existing vnfr. Scaled to 0?".format(
                vdu_delete))

        vnfr_update = {"vdur": vdurs}
        db_vnfr["vdur"] = vdurs
        self.update_db_2("vnfrs", db_vnfr["_id"], vnfr_update)

    def ns_update_nsr(self, ns_update_nsr, db_nsr, nsr_desc_RO):
        """
        Updates database nsr with the RO info for the created vld
        :param ns_update_nsr: dictionary to be filled with the updated info
        :param db_nsr: content of db_nsr. This is also modified
        :param nsr_desc_RO: nsr descriptor from RO
        :return: Nothing, LcmException is raised on errors
        """

        for vld_index, vld in enumerate(get_iterable(db_nsr, "vld")):
            for net_RO in get_iterable(nsr_desc_RO, "nets"):
                if vld["id"] != net_RO.get("ns_net_osm_id"):
                    continue
                vld["vim-id"] = net_RO.get("vim_net_id")
                vld["name"] = net_RO.get("vim_name")
                vld["status"] = net_RO.get("status")
                vld["status-detailed"] = net_RO.get("error_msg")
                ns_update_nsr["vld.{}".format(vld_index)] = vld
                break
            else:
                raise LcmException("ns_update_nsr: Not found vld={} at RO info".format(vld["id"]))

    def ns_update_vnfr(self, db_vnfrs, nsr_desc_RO):
        """
        Updates database vnfr with the RO info, e.g. ip_address, vim_id... Descriptor db_vnfrs is also updated
        :param db_vnfrs: dictionary with member-vnf-index: vnfr-content
        :param nsr_desc_RO: nsr descriptor from RO
        :return: Nothing, LcmException is raised on errors
        """
        for vnf_index, db_vnfr in db_vnfrs.items():
            for vnf_RO in nsr_desc_RO["vnfs"]:
                if vnf_RO["member_vnf_index"] != vnf_index:
                    continue
                vnfr_update = {}
                if vnf_RO.get("ip_address"):
                    db_vnfr["ip-address"] = vnfr_update["ip-address"] = vnf_RO["ip_address"].split(";")[0]
                elif not db_vnfr.get("ip-address"):
                    raise LcmExceptionNoMgmtIP("ns member_vnf_index '{}' has no IP address".format(vnf_index))

                for vdu_index, vdur in enumerate(get_iterable(db_vnfr, "vdur")):
                    vdur_RO_count_index = 0
                    if vdur.get("pdu-type"):
                        continue
                    for vdur_RO in get_iterable(vnf_RO, "vms"):
                        if vdur["vdu-id-ref"] != vdur_RO["vdu_osm_id"]:
                            continue
                        if vdur["count-index"] != vdur_RO_count_index:
                            vdur_RO_count_index += 1
                            continue
                        vdur["vim-id"] = vdur_RO.get("vim_vm_id")
                        if vdur_RO.get("ip_address"):
                            vdur["ip-address"] = vdur_RO["ip_address"].split(";")[0]
                        else:
                            vdur["ip-address"] = None
                        vdur["vdu-id-ref"] = vdur_RO.get("vdu_osm_id")
                        vdur["name"] = vdur_RO.get("vim_name")
                        vdur["status"] = vdur_RO.get("status")
                        vdur["status-detailed"] = vdur_RO.get("error_msg")
                        for ifacer in get_iterable(vdur, "interfaces"):
                            for interface_RO in get_iterable(vdur_RO, "interfaces"):
                                if ifacer["name"] == interface_RO.get("internal_name"):
                                    ifacer["ip-address"] = interface_RO.get("ip_address")
                                    ifacer["mac-address"] = interface_RO.get("mac_address")
                                    break
                            else:
                                raise LcmException("ns_update_vnfr: Not found member_vnf_index={} vdur={} interface={} "
                                                   "from VIM info"
                                                   .format(vnf_index, vdur["vdu-id-ref"], ifacer["name"]))
                        vnfr_update["vdur.{}".format(vdu_index)] = vdur
                        break
                    else:
                        raise LcmException("ns_update_vnfr: Not found member_vnf_index={} vdur={} count_index={} from "
                                           "VIM info".format(vnf_index, vdur["vdu-id-ref"], vdur["count-index"]))

                for vld_index, vld in enumerate(get_iterable(db_vnfr, "vld")):
                    for net_RO in get_iterable(nsr_desc_RO, "nets"):
                        if vld["id"] != net_RO.get("vnf_net_osm_id"):
                            continue
                        vld["vim-id"] = net_RO.get("vim_net_id")
                        vld["name"] = net_RO.get("vim_name")
                        vld["status"] = net_RO.get("status")
                        vld["status-detailed"] = net_RO.get("error_msg")
                        vnfr_update["vld.{}".format(vld_index)] = vld
                        break
                    else:
                        raise LcmException("ns_update_vnfr: Not found member_vnf_index={} vld={} from VIM info".format(
                            vnf_index, vld["id"]))

                self.update_db_2("vnfrs", db_vnfr["_id"], vnfr_update)
                break

            else:
                raise LcmException("ns_update_vnfr: Not found member_vnf_index={} from VIM info".format(vnf_index))

    def _get_ns_config_info(self, nsr_id):
        """
        Generates a mapping between vnf,vdu elements and the N2VC id
        :param nsr_id: id of nsr to get last  database _admin.deployed.VCA that contains this list
        :return: a dictionary with {osm-config-mapping: {}} where its element contains:
            "<member-vnf-index>": <N2VC-id>  for a vnf configuration, or
            "<member-vnf-index>.<vdu.id>.<vdu replica(0, 1,..)>": <N2VC-id>  for a vdu configuration
        """
        db_nsr = self.db.get_one("nsrs", {"_id": nsr_id})
        vca_deployed_list = db_nsr["_admin"]["deployed"]["VCA"]
        mapping = {}
        ns_config_info = {"osm-config-mapping": mapping}
        for vca in vca_deployed_list:
            if not vca["member-vnf-index"]:
                continue
            if not vca["vdu_id"]:
                mapping[vca["member-vnf-index"]] = vca["application"]
            else:
                mapping["{}.{}.{}".format(vca["member-vnf-index"], vca["vdu_id"], vca["vdu_count_index"])] =\
                    vca["application"]
        return ns_config_info

    @staticmethod
    def _get_initial_config_primitive_list(desc_primitive_list, vca_deployed):
        """
        Generates a list of initial-config-primitive based on the list provided by the descriptor. It includes internal
        primitives as verify-ssh-credentials, or config when needed
        :param desc_primitive_list: information of the descriptor
        :param vca_deployed: information of the deployed, needed for known if it is related to an NS, VNF, VDU and if
            this element contains a ssh public key
        :return: The modified list. Can ba an empty list, but always a list
        """
        if desc_primitive_list:
            primitive_list = desc_primitive_list.copy()
        else:
            primitive_list = []
        # look for primitive config, and get the position. None if not present
        config_position = None
        for index, primitive in enumerate(primitive_list):
            if primitive["name"] == "config":
                config_position = index
                break

        # for NS, add always a config primitive if not present (bug 874)
        if not vca_deployed["member-vnf-index"] and config_position is None:
            primitive_list.insert(0, {"name": "config", "parameter": []})
            config_position = 0
        # for VNF/VDU add verify-ssh-credentials after config
        if vca_deployed["member-vnf-index"] and config_position is not None and vca_deployed.get("ssh-public-key"):
            primitive_list.insert(config_position + 1, {"name": "verify-ssh-credentials", "parameter": []})
        return primitive_list

    async def instantiate_RO(self, logging_text, nsr_id, nsd, db_nsr,
                             db_nslcmop, db_vnfrs, db_vnfds_ref, n2vc_key_list):

        db_nsr_update = {}
        RO_descriptor_number = 0   # number of descriptors created at RO
        vnf_index_2_RO_id = {}    # map between vnfd/nsd id to the id used at RO
        start_deploy = time()
        vdu_flag = False  # If any of the VNFDs has VDUs
        ns_params = db_nslcmop.get("operationParams")

        # deploy RO

        # get vnfds, instantiate at RO

        for c_vnf in nsd.get("constituent-vnfd", ()):
            member_vnf_index = c_vnf["member-vnf-index"]
            vnfd = db_vnfds_ref[c_vnf['vnfd-id-ref']]
            if vnfd.get("vdu"):
                vdu_flag = True
            vnfd_ref = vnfd["id"]
            step = db_nsr_update["_admin.deployed.RO.detailed-status"] = "Creating vnfd='{}' member_vnf_index='{}' at" \
                                                                         " RO".format(vnfd_ref, member_vnf_index)
            # self.logger.debug(logging_text + step)
            vnfd_id_RO = "{}.{}.{}".format(nsr_id, RO_descriptor_number, member_vnf_index[:23])
            vnf_index_2_RO_id[member_vnf_index] = vnfd_id_RO
            RO_descriptor_number += 1

            # look position at deployed.RO.vnfd if not present it will be appended at the end
            for index, vnf_deployed in enumerate(db_nsr["_admin"]["deployed"]["RO"]["vnfd"]):
                if vnf_deployed["member-vnf-index"] == member_vnf_index:
                    break
            else:
                index = len(db_nsr["_admin"]["deployed"]["RO"]["vnfd"])
                db_nsr["_admin"]["deployed"]["RO"]["vnfd"].append(None)

            # look if present
            RO_update = {"member-vnf-index": member_vnf_index}
            vnfd_list = await self.RO.get_list("vnfd", filter_by={"osm_id": vnfd_id_RO})
            if vnfd_list:
                RO_update["id"] = vnfd_list[0]["uuid"]
                self.logger.debug(logging_text + "vnfd='{}'  member_vnf_index='{}' exists at RO. Using RO_id={}".
                                  format(vnfd_ref, member_vnf_index, vnfd_list[0]["uuid"]))
            else:
                vnfd_RO = self.vnfd2RO(vnfd, vnfd_id_RO, db_vnfrs[c_vnf["member-vnf-index"]].
                                       get("additionalParamsForVnf"), nsr_id)
                desc = await self.RO.create("vnfd", descriptor=vnfd_RO)
                RO_update["id"] = desc["uuid"]
                self.logger.debug(logging_text + "vnfd='{}' member_vnf_index='{}' created at RO. RO_id={}".format(
                    vnfd_ref, member_vnf_index, desc["uuid"]))
            db_nsr_update["_admin.deployed.RO.vnfd.{}".format(index)] = RO_update
            db_nsr["_admin"]["deployed"]["RO"]["vnfd"][index] = RO_update
            self.update_db_2("nsrs", nsr_id, db_nsr_update)
            self._on_update_n2vc_db("nsrs", {"_id": nsr_id}, "_admin.deployed", db_nsr_update)

        # create nsd at RO
        nsd_ref = nsd["id"]
        step = db_nsr_update["_admin.deployed.RO.detailed-status"] = "Creating nsd={} at RO".format(nsd_ref)
        # self.logger.debug(logging_text + step)

        RO_osm_nsd_id = "{}.{}.{}".format(nsr_id, RO_descriptor_number, nsd_ref[:23])
        RO_descriptor_number += 1
        nsd_list = await self.RO.get_list("nsd", filter_by={"osm_id": RO_osm_nsd_id})
        if nsd_list:
            db_nsr_update["_admin.deployed.RO.nsd_id"] = RO_nsd_uuid = nsd_list[0]["uuid"]
            self.logger.debug(logging_text + "nsd={} exists at RO. Using RO_id={}".format(
                nsd_ref, RO_nsd_uuid))
        else:
            nsd_RO = deepcopy(nsd)
            nsd_RO["id"] = RO_osm_nsd_id
            nsd_RO.pop("_id", None)
            nsd_RO.pop("_admin", None)
            for c_vnf in nsd_RO.get("constituent-vnfd", ()):
                member_vnf_index = c_vnf["member-vnf-index"]
                c_vnf["vnfd-id-ref"] = vnf_index_2_RO_id[member_vnf_index]
            for c_vld in nsd_RO.get("vld", ()):
                for cp in c_vld.get("vnfd-connection-point-ref", ()):
                    member_vnf_index = cp["member-vnf-index-ref"]
                    cp["vnfd-id-ref"] = vnf_index_2_RO_id[member_vnf_index]

            desc = await self.RO.create("nsd", descriptor=nsd_RO)
            db_nsr_update["_admin.nsState"] = "INSTANTIATED"
            db_nsr_update["_admin.deployed.RO.nsd_id"] = RO_nsd_uuid = desc["uuid"]
            self.logger.debug(logging_text + "nsd={} created at RO. RO_id={}".format(nsd_ref, RO_nsd_uuid))
        self.update_db_2("nsrs", nsr_id, db_nsr_update)
        self._on_update_n2vc_db("nsrs", {"_id": nsr_id}, "_admin.deployed", db_nsr_update)

        # Crate ns at RO
        # if present use it unless in error status
        RO_nsr_id = deep_get(db_nsr, ("_admin", "deployed", "RO", "nsr_id"))
        if RO_nsr_id:
            try:
                step = db_nsr_update["_admin.deployed.RO.detailed-status"] = "Looking for existing ns at RO"
                # self.logger.debug(logging_text + step + " RO_ns_id={}".format(RO_nsr_id))
                desc = await self.RO.show("ns", RO_nsr_id)
            except ROclient.ROClientException as e:
                if e.http_code != HTTPStatus.NOT_FOUND:
                    raise
                RO_nsr_id = db_nsr_update["_admin.deployed.RO.nsr_id"] = None
            if RO_nsr_id:
                ns_status, ns_status_info = self.RO.check_ns_status(desc)
                db_nsr_update["_admin.deployed.RO.nsr_status"] = ns_status
                if ns_status == "ERROR":
                    step = db_nsr_update["_admin.deployed.RO.detailed-status"] = "Deleting ns at RO. RO_ns_id={}"\
                        .format(RO_nsr_id)
                    self.logger.debug(logging_text + step)
                    await self.RO.delete("ns", RO_nsr_id)
                    RO_nsr_id = db_nsr_update["_admin.deployed.RO.nsr_id"] = None
        if not RO_nsr_id:
            step = db_nsr_update["_admin.deployed.RO.detailed-status"] = "Checking dependencies"
            # self.logger.debug(logging_text + step)

            # check if VIM is creating and wait  look if previous tasks in process
            task_name, task_dependency = self.lcm_tasks.lookfor_related("vim_account", ns_params["vimAccountId"])
            if task_dependency:
                step = "Waiting for related tasks to be completed: {}".format(task_name)
                self.logger.debug(logging_text + step)
                await asyncio.wait(task_dependency, timeout=3600)
            if ns_params.get("vnf"):
                for vnf in ns_params["vnf"]:
                    if "vimAccountId" in vnf:
                        task_name, task_dependency = self.lcm_tasks.lookfor_related("vim_account",
                                                                                    vnf["vimAccountId"])
                    if task_dependency:
                        step = "Waiting for related tasks to be completed: {}".format(task_name)
                        self.logger.debug(logging_text + step)
                        await asyncio.wait(task_dependency, timeout=3600)

            step = db_nsr_update["_admin.deployed.RO.detailed-status"] = "Checking instantiation parameters"

            RO_ns_params = self.ns_params_2_RO(ns_params, nsd, db_vnfds_ref, n2vc_key_list)

            step = db_nsr_update["detailed-status"] = "Deploying ns at VIM"
            # step = db_nsr_update["_admin.deployed.RO.detailed-status"] = "Deploying ns at VIM"
            desc = await self.RO.create("ns", descriptor=RO_ns_params, name=db_nsr["name"], scenario=RO_nsd_uuid)
            RO_nsr_id = db_nsr_update["_admin.deployed.RO.nsr_id"] = desc["uuid"]
            db_nsr_update["_admin.nsState"] = "INSTANTIATED"
            db_nsr_update["_admin.deployed.RO.nsr_status"] = "BUILD"
            self.logger.debug(logging_text + "ns created at RO. RO_id={}".format(desc["uuid"]))
        self.update_db_2("nsrs", nsr_id, db_nsr_update)
        self._on_update_n2vc_db("nsrs", {"_id": nsr_id}, "_admin.deployed", db_nsr_update)

        # wait until NS is ready
        step = ns_status_detailed = detailed_status = "Waiting VIM to deploy ns. RO_ns_id={}".format(RO_nsr_id)
        detailed_status_old = None
        self.logger.debug(logging_text + step)

        while time() <= start_deploy + self.total_deploy_timeout:
            desc = await self.RO.show("ns", RO_nsr_id)
            ns_status, ns_status_info = self.RO.check_ns_status(desc)
            db_nsr_update["_admin.deployed.RO.nsr_status"] = ns_status
            if ns_status == "ERROR":
                raise ROclient.ROClientException(ns_status_info)
            elif ns_status == "BUILD":
                detailed_status = ns_status_detailed + "; {}".format(ns_status_info)
            elif ns_status == "ACTIVE":
                step = detailed_status = "Waiting for management IP address reported by the VIM. Updating VNFRs"
                try:
                    if vdu_flag:
                        self.ns_update_vnfr(db_vnfrs, desc)
                    break
                except LcmExceptionNoMgmtIP:
                    pass
            else:
                assert False, "ROclient.check_ns_status returns unknown {}".format(ns_status)
            if detailed_status != detailed_status_old:
                detailed_status_old = db_nsr_update["_admin.deployed.RO.detailed-status"] = detailed_status
                self.update_db_2("nsrs", nsr_id, db_nsr_update)
                self._on_update_n2vc_db("nsrs", {"_id": nsr_id}, "_admin.deployed", db_nsr_update)
            await asyncio.sleep(5, loop=self.loop)
        else:  # total_deploy_timeout
            raise ROclient.ROClientException("Timeout waiting ns to be ready")

        step = "Updating NSR"
        self.ns_update_nsr(db_nsr_update, db_nsr, desc)

        db_nsr_update["_admin.deployed.RO.operational-status"] = "running"
        db_nsr["_admin.deployed.RO.detailed-status"] = "Deployed at VIM"
        db_nsr_update["_admin.deployed.RO.detailed-status"] = "Deployed at VIM"
        self.update_db_2("nsrs", nsr_id, db_nsr_update)
        self._on_update_n2vc_db("nsrs", {"_id": nsr_id}, "_admin.deployed", db_nsr_update)

        step = "Deployed at VIM"
        self.logger.debug(logging_text + step)

    async def wait_vm_up_insert_key_ro(self, logging_text, nsr_id, vnfr_id, vdu_id, vdu_index, pub_key=None, user=None):
        """
        Wait for ip addres at RO, and optionally, insert public key in virtual machine
        :param logging_text: prefix use for logging
        :param nsr_id:
        :param vnfr_id:
        :param vdu_id:
        :param vdu_index:
        :param pub_key: public ssh key to inject, None to skip
        :param user: user to apply the public ssh key
        :return: IP address
        """

        # self.logger.debug(logging_text + "Starting wait_vm_up_insert_key_ro")
        ro_nsr_id = None
        ip_address = None
        nb_tries = 0
        target_vdu_id = None
        ro_retries = 0

        while True:

            ro_retries += 1
            if ro_retries >= 360:  # 1 hour
                raise LcmException("Not found _admin.deployed.RO.nsr_id for nsr_id: {}".format(nsr_id))

            await asyncio.sleep(10, loop=self.loop)
            # wait until NS is deployed at RO
            if not ro_nsr_id:
                db_nsrs = self.db.get_one("nsrs", {"_id": nsr_id})
                ro_nsr_id = deep_get(db_nsrs, ("_admin", "deployed", "RO", "nsr_id"))
            if not ro_nsr_id:
                continue

            # get ip address
            if not target_vdu_id:
                db_vnfr = self.db.get_one("vnfrs", {"_id": vnfr_id})

                if not vdu_id:  # for the VNF case
                    ip_address = db_vnfr.get("ip-address")
                    if not ip_address:
                        continue
                    vdur = next((x for x in get_iterable(db_vnfr, "vdur") if x.get("ip-address") == ip_address), None)
                else:  # VDU case
                    vdur = next((x for x in get_iterable(db_vnfr, "vdur")
                                 if x.get("vdu-id-ref") == vdu_id and x.get("count-index") == vdu_index), None)

                if not vdur:
                    raise LcmException("Not found vnfr_id={}, vdu_index={}, vdu_index={}".format(
                        vnfr_id, vdu_id, vdu_index
                    ))

                if vdur.get("status") == "ACTIVE":
                    ip_address = vdur.get("ip-address")
                    if not ip_address:
                        continue
                    target_vdu_id = vdur["vdu-id-ref"]
                elif vdur.get("status") == "ERROR":
                    raise LcmException("Cannot inject ssh-key because target VM is in error state")

            if not target_vdu_id:
                continue

            # self.logger.debug(logging_text + "IP address={}".format(ip_address))

            # inject public key into machine
            if pub_key and user:
                # self.logger.debug(logging_text + "Inserting RO key")
                try:
                    ro_vm_id = "{}-{}".format(db_vnfr["member-vnf-index-ref"], target_vdu_id)  # TODO add vdu_index
                    result_dict = await self.RO.create_action(
                        item="ns",
                        item_id_name=ro_nsr_id,
                        descriptor={"add_public_key": pub_key, "vms": [ro_vm_id], "user": user}
                    )
                    # result_dict contains the format {VM-id: {vim_result: 200, description: text}}
                    if not result_dict or not isinstance(result_dict, dict):
                        raise LcmException("Unknown response from RO when injecting key")
                    for result in result_dict.values():
                        if result.get("vim_result") == 200:
                            break
                        else:
                            raise ROclient.ROClientException("error injecting key: {}".format(
                                result.get("description")))
                    break
                except ROclient.ROClientException as e:
                    if not nb_tries:
                        self.logger.debug(logging_text + "error injecting key: {}. Retrying until {} seconds".
                                          format(e, 20*10))
                    nb_tries += 1
                    if nb_tries >= 20:
                        raise LcmException("Reaching max tries injecting key. Error: {}".format(e))
            else:
                break

        return ip_address

    async def _wait_dependent_n2vc(self, nsr_id, vca_deployed_list, vca_index):
        """
        Wait until dependent VCA deployments have been finished. NS wait for VNFs and VDUs. VNFs for VDUs
        """
        my_vca = vca_deployed_list[vca_index]
        if my_vca.get("vdu_id") or my_vca.get("kdu_name"):
            return
        timeout = 300
        while timeout >= 0:
            for index, vca_deployed in enumerate(vca_deployed_list):
                if index == vca_index:
                    continue
                if not my_vca.get("member-vnf-index") or \
                        (vca_deployed.get("member-vnf-index") == my_vca.get("member-vnf-index")):
                    if not vca_deployed.get("instantiation"):
                        break   # wait
                    if vca_deployed["instantiation"] == "FAILED":
                        raise LcmException("Configuration aborted because dependent charm/s has failed")
            else:
                return
            await asyncio.sleep(10)
            timeout -= 1
            db_nsr = self.db.get_one("nsrs", {"_id": nsr_id})
            vca_deployed_list = db_nsr["_admin"]["deployed"]["VCA"]

        raise LcmException("Configuration aborted because dependent charm/s timeout")

    async def instantiate_N2VC(self, logging_text, vca_index, nsi_id, db_nsr, db_vnfr, vdu_id,
                               kdu_name, vdu_index, config_descriptor, deploy_params, base_folder):
        nsr_id = db_nsr["_id"]
        db_update_entry = "_admin.deployed.VCA.{}.".format(vca_index)
        vca_deployed_list = db_nsr["_admin"]["deployed"]["VCA"]
        vca_deployed = db_nsr["_admin"]["deployed"]["VCA"][vca_index]
        db_dict = {
            'collection': 'nsrs',
            'filter': {'_id': nsr_id},
            'path': db_update_entry
        }
        step = ""
        try:
            vnfr_id = None
            if db_vnfr:
                vnfr_id = db_vnfr["_id"]

            namespace = "{nsi}.{ns}".format(
                nsi=nsi_id if nsi_id else "",
                ns=nsr_id)
            if vnfr_id:
                namespace += "." + vnfr_id
                if vdu_id:
                    namespace += ".{}-{}".format(vdu_id, vdu_index or 0)

            # Get artifact path
            artifact_path = "{}/{}/charms/{}".format(
                base_folder["folder"],
                base_folder["pkg-dir"],
                config_descriptor["juju"]["charm"]
            )

            is_proxy_charm = deep_get(config_descriptor, ('juju', 'charm')) is not None
            if deep_get(config_descriptor, ('juju', 'proxy')) is False:
                is_proxy_charm = False

            # n2vc_redesign STEP 3.1

            # find old ee_id if exists
            ee_id = vca_deployed.get("ee_id")

            # create or register execution environment in VCA
            if is_proxy_charm:
                step = "create execution environment"
                self.logger.debug(logging_text + step)
                ee_id, credentials = await self.n2vc.create_execution_environment(namespace=namespace,
                                                                                  reuse_ee_id=ee_id,
                                                                                  db_dict=db_dict)
            else:
                step = "Waiting to VM being up and getting IP address"
                self.logger.debug(logging_text + step)
                rw_mgmt_ip = await self.wait_vm_up_insert_key_ro(logging_text, nsr_id, vnfr_id, vdu_id, vdu_index,
                                                                 user=None, pub_key=None)
                credentials = {"hostname": rw_mgmt_ip}
                # get username
                username = deep_get(config_descriptor, ("config-access", "ssh-access", "default-user"))
                # TODO remove this when changes on IM regarding config-access:ssh-access:default-user were
                #  merged. Meanwhile let's get username from initial-config-primitive
                if not username and config_descriptor.get("initial-config-primitive"):
                    for config_primitive in config_descriptor["initial-config-primitive"]:
                        for param in config_primitive.get("parameter", ()):
                            if param["name"] == "ssh-username":
                                username = param["value"]
                                break
                if not username:
                    raise LcmException("Cannot determine the username neither with 'initial-config-promitive' nor with "
                                       "'config-access.ssh-access.default-user'")
                credentials["username"] = username
                # n2vc_redesign STEP 3.2

                step = "register execution environment {}".format(credentials)
                self.logger.debug(logging_text + step)
                ee_id = await self.n2vc.register_execution_environment(credentials=credentials, namespace=namespace,
                                                                       db_dict=db_dict)

            # for compatibility with MON/POL modules, the need model and application name at database
            # TODO ask to N2VC instead of assuming the format "model_name.application_name"
            ee_id_parts = ee_id.split('.')
            model_name = ee_id_parts[0]
            application_name = ee_id_parts[1]
            self.update_db_2("nsrs", nsr_id, {db_update_entry + "model": model_name,
                                              db_update_entry + "application": application_name,
                                              db_update_entry + "ee_id": ee_id})

            # n2vc_redesign STEP 3.3

            step = "Install configuration Software"
            # TODO check if already done
            self.logger.debug(logging_text + step)
            await self.n2vc.install_configuration_sw(ee_id=ee_id, artifact_path=artifact_path, db_dict=db_dict)

            # if SSH access is required, then get execution environment SSH public
            if is_proxy_charm:  # if native charm we have waited already to VM be UP
                pub_key = None
                user = None
                if deep_get(config_descriptor, ("config-access", "ssh-access", "required")):
                    # Needed to inject a ssh key
                    user = deep_get(config_descriptor, ("config-access", "ssh-access", "default-user"))
                    step = "Install configuration Software, getting public ssh key"
                    pub_key = await self.n2vc.get_ee_ssh_public__key(ee_id=ee_id, db_dict=db_dict)

                    step = "Insert public key into VM"
                else:
                    step = "Waiting to VM being up and getting IP address"
                self.logger.debug(logging_text + step)

                # n2vc_redesign STEP 5.1
                # wait for RO (ip-address) Insert pub_key into VM
                if vnfr_id:
                    rw_mgmt_ip = await self.wait_vm_up_insert_key_ro(logging_text, nsr_id, vnfr_id, vdu_id, vdu_index,
                                                                     user=user, pub_key=pub_key)
                else:
                    rw_mgmt_ip = None   # This is for a NS configuration

                self.logger.debug(logging_text + ' VM_ip_address={}'.format(rw_mgmt_ip))

            # store rw_mgmt_ip in deploy params for later replacement
            deploy_params["rw_mgmt_ip"] = rw_mgmt_ip

            # n2vc_redesign STEP 6  Execute initial config primitive
            step = 'execute initial config primitive'
            initial_config_primitive_list = config_descriptor.get('initial-config-primitive')

            # sort initial config primitives by 'seq'
            try:
                initial_config_primitive_list.sort(key=lambda val: int(val['seq']))
            except Exception as e:
                self.logger.error(logging_text + step + ": " + str(e))

            # add config if not present for NS charm
            initial_config_primitive_list = self._get_initial_config_primitive_list(initial_config_primitive_list,
                                                                                    vca_deployed)
            if initial_config_primitive_list:
                await self._wait_dependent_n2vc(nsr_id, vca_deployed_list, vca_index)
            for initial_config_primitive in initial_config_primitive_list:
                # adding information on the vca_deployed if it is a NS execution environment
                if not vca_deployed["member-vnf-index"]:
                    deploy_params["ns_config_info"] = self._get_ns_config_info(nsr_id)
                # TODO check if already done
                primitive_params_ = self._map_primitive_params(initial_config_primitive, {}, deploy_params)

                step = "execute primitive '{}' params '{}'".format(initial_config_primitive["name"], primitive_params_)
                self.logger.debug(logging_text + step)
                await self.n2vc.exec_primitive(
                    ee_id=ee_id,
                    primitive_name=initial_config_primitive["name"],
                    params_dict=primitive_params_,
                    db_dict=db_dict
                )
                # TODO register in database that primitive is done

            step = "instantiated at VCA"
            self.update_db_2("nsrs", nsr_id, {db_update_entry + "instantiation": "COMPLETED"})
            self.logger.debug(logging_text + step)

        except Exception as e:  # TODO not use Exception but N2VC exception
            self.update_db_2("nsrs", nsr_id, {db_update_entry + "instantiation": "FAILED"})
            raise Exception("{} {}".format(step, e)) from e
            # TODO raise N2VC exception with 'step' extra information

    def _write_ns_status(self, nsr_id: str, ns_state: str, current_operation: str, current_operation_id: str,
                         error_description: str = None, error_detail: str = None):
        try:
            db_dict = dict()
            if ns_state:
                db_dict["nsState"] = ns_state
            db_dict["currentOperation"] = current_operation
            db_dict["currentOperationID"] = current_operation_id
            db_dict["errorDescription"] = error_description
            db_dict["errorDetail"] = error_detail
            self.update_db_2("nsrs", nsr_id, db_dict)
        except Exception as e:
            self.logger.warn('Error writing NS status: {}'.format(e))

    async def instantiate(self, nsr_id, nslcmop_id):
        """

        :param nsr_id: ns instance to deploy
        :param nslcmop_id: operation to run
        :return:
        """

        # Try to lock HA task here
        task_is_locked_by_me = self.lcm_tasks.lock_HA('ns', 'nslcmops', nslcmop_id)
        if not task_is_locked_by_me:
            self.logger.debug('instantiate() task is not locked by me')
            return

        logging_text = "Task ns={} instantiate={} ".format(nsr_id, nslcmop_id)
        self.logger.debug(logging_text + "Enter")

        # get all needed from database

        # database nsrs record
        db_nsr = None

        # database nslcmops record
        db_nslcmop = None

        # update operation on nsrs
        db_nsr_update = {"_admin.nslcmop": nslcmop_id,
                         "_admin.current-operation": nslcmop_id,
                         "_admin.operation-type": "instantiate"}
        self.update_db_2("nsrs", nsr_id, db_nsr_update)

        # update operation on nslcmops
        db_nslcmop_update = {}

        nslcmop_operation_state = None
        db_vnfrs = {}     # vnf's info indexed by member-index
        # n2vc_info = {}
        task_instantiation_list = []
        task_instantiation_info = {}  # from task to info text
        exc = None
        try:
            # wait for any previous tasks in process
            step = "Waiting for previous operations to terminate"
            await self.lcm_tasks.waitfor_related_HA('ns', 'nslcmops', nslcmop_id)

            # STEP 0: Reading database (nslcmops, nsrs, nsds, vnfrs, vnfds)

            # nsState="BUILDING", currentOperation="INSTANTIATING", currentOperationID=nslcmop_id
            self._write_ns_status(
                nsr_id=nsr_id,
                ns_state="BUILDING",
                current_operation="INSTANTIATING",
                current_operation_id=nslcmop_id
            )

            # read from db: operation
            step = "Getting nslcmop={} from db".format(nslcmop_id)
            db_nslcmop = self.db.get_one("nslcmops", {"_id": nslcmop_id})

            # read from db: ns
            step = "Getting nsr={} from db".format(nsr_id)
            db_nsr = self.db.get_one("nsrs", {"_id": nsr_id})
            # nsd is replicated into ns (no db read)
            nsd = db_nsr["nsd"]
            # nsr_name = db_nsr["name"]   # TODO short-name??

            # read from db: vnf's of this ns
            step = "Getting vnfrs from db"
            self.logger.debug(logging_text + step)
            db_vnfrs_list = self.db.get_list("vnfrs", {"nsr-id-ref": nsr_id})

            # read from db: vnfd's for every vnf
            db_vnfds_ref = {}     # every vnfd data indexed by vnf name
            db_vnfds = {}         # every vnfd data indexed by vnf id
            db_vnfds_index = {}   # every vnfd data indexed by vnf member-index

            # for each vnf in ns, read vnfd
            for vnfr in db_vnfrs_list:
                db_vnfrs[vnfr["member-vnf-index-ref"]] = vnfr   # vnf's dict indexed by member-index: '1', '2', etc
                vnfd_id = vnfr["vnfd-id"]                       # vnfd uuid for this vnf
                vnfd_ref = vnfr["vnfd-ref"]                     # vnfd name for this vnf
                # if we haven't this vnfd, read it from db
                if vnfd_id not in db_vnfds:
                    # read from cb
                    step = "Getting vnfd={} id='{}' from db".format(vnfd_id, vnfd_ref)
                    self.logger.debug(logging_text + step)
                    vnfd = self.db.get_one("vnfds", {"_id": vnfd_id})

                    # store vnfd
                    db_vnfds_ref[vnfd_ref] = vnfd     # vnfd's indexed by name
                    db_vnfds[vnfd_id] = vnfd          # vnfd's indexed by id
                db_vnfds_index[vnfr["member-vnf-index-ref"]] = db_vnfds[vnfd_id]  # vnfd's indexed by member-index

            # Get or generates the _admin.deployed.VCA list
            vca_deployed_list = None
            if db_nsr["_admin"].get("deployed"):
                vca_deployed_list = db_nsr["_admin"]["deployed"].get("VCA")
            if vca_deployed_list is None:
                vca_deployed_list = []
                db_nsr_update["_admin.deployed.VCA"] = vca_deployed_list
                # add _admin.deployed.VCA to db_nsr dictionary, value=vca_deployed_list
                populate_dict(db_nsr, ("_admin", "deployed", "VCA"), vca_deployed_list)
            elif isinstance(vca_deployed_list, dict):
                # maintain backward compatibility. Change a dict to list at database
                vca_deployed_list = list(vca_deployed_list.values())
                db_nsr_update["_admin.deployed.VCA"] = vca_deployed_list
                populate_dict(db_nsr, ("_admin", "deployed", "VCA"), vca_deployed_list)

            db_nsr_update["detailed-status"] = "creating"
            db_nsr_update["operational-status"] = "init"

            if not isinstance(deep_get(db_nsr, ("_admin", "deployed", "RO", "vnfd")), list):
                populate_dict(db_nsr, ("_admin", "deployed", "RO", "vnfd"), [])
                db_nsr_update["_admin.deployed.RO.vnfd"] = []

            # set state to INSTANTIATED. When instantiated NBI will not delete directly
            db_nsr_update["_admin.nsState"] = "INSTANTIATED"
            self.update_db_2("nsrs", nsr_id, db_nsr_update)
            self.logger.debug(logging_text + "Before deploy_kdus")
            # Call to deploy_kdus in case exists the "vdu:kdu" param
            task_kdu = asyncio.ensure_future(
                self.deploy_kdus(
                    logging_text=logging_text,
                    nsr_id=nsr_id,
                    db_nsr=db_nsr,
                    db_vnfrs=db_vnfrs,
                )
            )
            self.lcm_tasks.register("ns", nsr_id, nslcmop_id, "instantiate_KDUs", task_kdu)
            task_instantiation_info[task_kdu] = "Deploy KDUs"
            task_instantiation_list.append(task_kdu)
            # n2vc_redesign STEP 1 Get VCA public ssh-key
            # feature 1429. Add n2vc public key to needed VMs
            n2vc_key = self.n2vc.get_public_key()
            n2vc_key_list = [n2vc_key]
            if self.vca_config.get("public_key"):
                n2vc_key_list.append(self.vca_config["public_key"])

            # n2vc_redesign STEP 2 Deploy Network Scenario
            task_ro = asyncio.ensure_future(
                self.instantiate_RO(
                    logging_text=logging_text,
                    nsr_id=nsr_id,
                    nsd=nsd,
                    db_nsr=db_nsr,
                    db_nslcmop=db_nslcmop,
                    db_vnfrs=db_vnfrs,
                    db_vnfds_ref=db_vnfds_ref,
                    n2vc_key_list=n2vc_key_list
                )
            )
            self.lcm_tasks.register("ns", nsr_id, nslcmop_id, "instantiate_RO", task_ro)
            task_instantiation_info[task_ro] = "Deploy at VIM"
            task_instantiation_list.append(task_ro)

            # n2vc_redesign STEP 3 to 6 Deploy N2VC
            step = "Looking for needed vnfd to configure with proxy charm"
            self.logger.debug(logging_text + step)

            nsi_id = None  # TODO put nsi_id when this nsr belongs to a NSI
            # get_iterable() returns a value from a dict or empty tuple if key does not exist
            for c_vnf in get_iterable(nsd, "constituent-vnfd"):
                vnfd_id = c_vnf["vnfd-id-ref"]
                vnfd = db_vnfds_ref[vnfd_id]
                member_vnf_index = str(c_vnf["member-vnf-index"])
                db_vnfr = db_vnfrs[member_vnf_index]
                base_folder = vnfd["_admin"]["storage"]
                vdu_id = None
                vdu_index = 0
                vdu_name = None
                kdu_name = None

                # Get additional parameters
                deploy_params = {}
                if db_vnfr.get("additionalParamsForVnf"):
                    deploy_params = self._format_additional_params(db_vnfr["additionalParamsForVnf"].copy())

                descriptor_config = vnfd.get("vnf-configuration")
                if descriptor_config and descriptor_config.get("juju"):
                    self._deploy_n2vc(
                        logging_text=logging_text + "member_vnf_index={} ".format(member_vnf_index),
                        db_nsr=db_nsr,
                        db_vnfr=db_vnfr,
                        nslcmop_id=nslcmop_id,
                        nsr_id=nsr_id,
                        nsi_id=nsi_id,
                        vnfd_id=vnfd_id,
                        vdu_id=vdu_id,
                        kdu_name=kdu_name,
                        member_vnf_index=member_vnf_index,
                        vdu_index=vdu_index,
                        vdu_name=vdu_name,
                        deploy_params=deploy_params,
                        descriptor_config=descriptor_config,
                        base_folder=base_folder,
                        task_instantiation_list=task_instantiation_list,
                        task_instantiation_info=task_instantiation_info
                    )

                # Deploy charms for each VDU that supports one.
                for vdud in get_iterable(vnfd, 'vdu'):
                    vdu_id = vdud["id"]
                    descriptor_config = vdud.get('vdu-configuration')
                    vdur = next((x for x in db_vnfr["vdur"] if x["vdu-id-ref"] == vdu_id), None)
                    if vdur.get("additionalParams"):
                        deploy_params_vdu = self._format_additional_params(vdur["additionalParams"])
                    else:
                        deploy_params_vdu = deploy_params
                    if descriptor_config and descriptor_config.get("juju"):
                        # look for vdu index in the db_vnfr["vdu"] section
                        # for vdur_index, vdur in enumerate(db_vnfr["vdur"]):
                        #     if vdur["vdu-id-ref"] == vdu_id:
                        #         break
                        # else:
                        #     raise LcmException("Mismatch vdu_id={} not found in the vnfr['vdur'] list for "
                        #                        "member_vnf_index={}".format(vdu_id, member_vnf_index))
                        # vdu_name = vdur.get("name")
                        vdu_name = None
                        kdu_name = None
                        for vdu_index in range(int(vdud.get("count", 1))):
                            # TODO vnfr_params["rw_mgmt_ip"] = vdur["ip-address"]
                            self._deploy_n2vc(
                                logging_text=logging_text + "member_vnf_index={}, vdu_id={}, vdu_index={} ".format(
                                    member_vnf_index, vdu_id, vdu_index),
                                db_nsr=db_nsr,
                                db_vnfr=db_vnfr,
                                nslcmop_id=nslcmop_id,
                                nsr_id=nsr_id,
                                nsi_id=nsi_id,
                                vnfd_id=vnfd_id,
                                vdu_id=vdu_id,
                                kdu_name=kdu_name,
                                member_vnf_index=member_vnf_index,
                                vdu_index=vdu_index,
                                vdu_name=vdu_name,
                                deploy_params=deploy_params_vdu,
                                descriptor_config=descriptor_config,
                                base_folder=base_folder,
                                task_instantiation_list=task_instantiation_list,
                                task_instantiation_info=task_instantiation_info
                            )
                for kdud in get_iterable(vnfd, 'kdu'):
                    kdu_name = kdud["name"]
                    descriptor_config = kdud.get('kdu-configuration')
                    if descriptor_config and descriptor_config.get("juju"):
                        vdu_id = None
                        vdu_index = 0
                        vdu_name = None
                        # look for vdu index in the db_vnfr["vdu"] section
                        # for vdur_index, vdur in enumerate(db_vnfr["vdur"]):
                        #     if vdur["vdu-id-ref"] == vdu_id:
                        #         break
                        # else:
                        #     raise LcmException("Mismatch vdu_id={} not found in the vnfr['vdur'] list for "
                        #                        "member_vnf_index={}".format(vdu_id, member_vnf_index))
                        # vdu_name = vdur.get("name")
                        # vdu_name = None

                        self._deploy_n2vc(
                            logging_text=logging_text,
                            db_nsr=db_nsr,
                            db_vnfr=db_vnfr,
                            nslcmop_id=nslcmop_id,
                            nsr_id=nsr_id,
                            nsi_id=nsi_id,
                            vnfd_id=vnfd_id,
                            vdu_id=vdu_id,
                            kdu_name=kdu_name,
                            member_vnf_index=member_vnf_index,
                            vdu_index=vdu_index,
                            vdu_name=vdu_name,
                            deploy_params=deploy_params,
                            descriptor_config=descriptor_config,
                            base_folder=base_folder,
                            task_instantiation_list=task_instantiation_list,
                            task_instantiation_info=task_instantiation_info
                        )

            # Check if this NS has a charm configuration
            descriptor_config = nsd.get("ns-configuration")
            if descriptor_config and descriptor_config.get("juju"):
                vnfd_id = None
                db_vnfr = None
                member_vnf_index = None
                vdu_id = None
                kdu_name = None
                vdu_index = 0
                vdu_name = None

                # Get additional parameters
                deploy_params = {}
                if db_nsr.get("additionalParamsForNs"):
                    deploy_params = self._format_additional_params(db_nsr["additionalParamsForNs"].copy())
                base_folder = nsd["_admin"]["storage"]
                self._deploy_n2vc(
                    logging_text=logging_text,
                    db_nsr=db_nsr,
                    db_vnfr=db_vnfr,
                    nslcmop_id=nslcmop_id,
                    nsr_id=nsr_id,
                    nsi_id=nsi_id,
                    vnfd_id=vnfd_id,
                    vdu_id=vdu_id,
                    kdu_name=kdu_name,
                    member_vnf_index=member_vnf_index,
                    vdu_index=vdu_index,
                    vdu_name=vdu_name,
                    deploy_params=deploy_params,
                    descriptor_config=descriptor_config,
                    base_folder=base_folder,
                    task_instantiation_list=task_instantiation_list,
                    task_instantiation_info=task_instantiation_info
                )

            # Wait until all tasks of "task_instantiation_list" have been finished

            # while time() <= start_deploy + self.total_deploy_timeout:
            error_text_list = []
            timeout = 3600

            # let's begin with all OK
            instantiated_ok = True
            # let's begin with RO 'running' status (later we can change it)
            db_nsr_update["operational-status"] = "running"
            # let's begin with VCA 'configured' status (later we can change it)
            db_nsr_update["config-status"] = "configured"

            if task_instantiation_list:
                # wait for all tasks completion
                done, pending = await asyncio.wait(task_instantiation_list, timeout=timeout)

                for task in pending:
                    instantiated_ok = False
                    if task == task_ro:
                        db_nsr_update["operational-status"] = "failed"
                    else:
                        db_nsr_update["config-status"] = "failed"
                    self.logger.error(logging_text + task_instantiation_info[task] + ": Timeout")
                    error_text_list.append(task_instantiation_info[task] + ": Timeout")
                for task in done:
                    if task.cancelled():
                        instantiated_ok = False
                        if task == task_ro:
                            db_nsr_update["operational-status"] = "failed"
                        else:
                            db_nsr_update["config-status"] = "failed"
                        self.logger.warn(logging_text + task_instantiation_info[task] + ": Cancelled")
                        error_text_list.append(task_instantiation_info[task] + ": Cancelled")
                    else:
                        exc = task.exception()
                        if exc:
                            instantiated_ok = False
                            if task == task_ro:
                                db_nsr_update["operational-status"] = "failed"
                            else:
                                db_nsr_update["config-status"] = "failed"
                            self.logger.error(logging_text + task_instantiation_info[task] + ": Failed")
                            if isinstance(exc, (N2VCException, ROclient.ROClientException)):
                                error_text_list.append(task_instantiation_info[task] + ": {}".format(exc))
                            else:
                                exc_traceback = "".join(traceback.format_exception(None, exc, exc.__traceback__))
                                self.logger.error(logging_text + task_instantiation_info[task] + exc_traceback)
                                error_text_list.append(task_instantiation_info[task] + ": " + exc_traceback)
                        else:
                            self.logger.debug(logging_text + task_instantiation_info[task] + ": Done")

            if error_text_list:
                error_text = "\n".join(error_text_list)
                db_nsr_update["detailed-status"] = error_text
                db_nslcmop_update["operationState"] = nslcmop_operation_state = "FAILED_TEMP"
                db_nslcmop_update["detailed-status"] = error_text
                db_nslcmop_update["statusEnteredTime"] = time()
            else:
                # all is done
                db_nslcmop_update["operationState"] = nslcmop_operation_state = "COMPLETED"
                db_nslcmop_update["statusEnteredTime"] = time()
                db_nslcmop_update["detailed-status"] = "done"
                db_nsr_update["detailed-status"] = "done"

        except (ROclient.ROClientException, DbException, LcmException) as e:
            self.logger.error(logging_text + "Exit Exception while '{}': {}".format(step, e))
            exc = e
        except asyncio.CancelledError:
            self.logger.error(logging_text + "Cancelled Exception while '{}'".format(step))
            exc = "Operation was cancelled"
        except Exception as e:
            exc = traceback.format_exc()
            self.logger.critical(logging_text + "Exit Exception {} while '{}': {}".format(type(e).__name__, step, e),
                                 exc_info=True)
        finally:
            if exc:
                if db_nsr:
                    db_nsr_update["detailed-status"] = "ERROR {}: {}".format(step, exc)
                    db_nsr_update["operational-status"] = "failed"
                    db_nsr_update["config-status"] = "failed"
                if db_nslcmop:
                    db_nslcmop_update["detailed-status"] = "FAILED {}: {}".format(step, exc)
                    db_nslcmop_update["operationState"] = nslcmop_operation_state = "FAILED"
                    db_nslcmop_update["statusEnteredTime"] = time()
            try:
                if db_nsr:
                    db_nsr_update["_admin.nslcmop"] = None
                    db_nsr_update["_admin.current-operation"] = None
                    db_nsr_update["_admin.operation-type"] = None
                    self.update_db_2("nsrs", nsr_id, db_nsr_update)

                    # nsState="READY/BROKEN", currentOperation="IDLE", currentOperationID=None
                    ns_state = None
                    error_description = None
                    error_detail = None
                    if instantiated_ok:
                        ns_state = "READY"
                    else:
                        ns_state = "BROKEN"
                        error_description = 'Operation: INSTANTIATING.{}, step: {}'.format(nslcmop_id, step)
                        error_detail = error_text
                    self._write_ns_status(
                        nsr_id=nsr_id,
                        ns_state=ns_state,
                        current_operation="IDLE",
                        current_operation_id=None,
                        error_description=error_description,
                        error_detail=error_detail
                    )

                if db_nslcmop_update:
                    self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
            except DbException as e:
                self.logger.error(logging_text + "Cannot update database: {}".format(e))
            if nslcmop_operation_state:
                try:
                    await self.msg.aiowrite("ns", "instantiated", {"nsr_id": nsr_id, "nslcmop_id": nslcmop_id,
                                                                   "operationState": nslcmop_operation_state},
                                            loop=self.loop)
                except Exception as e:
                    self.logger.error(logging_text + "kafka_write notification Exception {}".format(e))

            self.logger.debug(logging_text + "Exit")
            self.lcm_tasks.remove("ns", nsr_id, nslcmop_id, "ns_instantiate")

    async def deploy_kdus(self, logging_text, nsr_id, db_nsr, db_vnfrs):
        # Launch kdus if present in the descriptor

        k8scluster_id_2_uuic = {"helm-chart": {}, "juju-bundle": {}}

        def _get_cluster_id(cluster_id, cluster_type):
            nonlocal k8scluster_id_2_uuic
            if cluster_id in k8scluster_id_2_uuic[cluster_type]:
                return k8scluster_id_2_uuic[cluster_type][cluster_id]

            db_k8scluster = self.db.get_one("k8sclusters", {"_id": cluster_id}, fail_on_empty=False)
            if not db_k8scluster:
                raise LcmException("K8s cluster {} cannot be found".format(cluster_id))
            k8s_id = deep_get(db_k8scluster, ("_admin", cluster_type, "id"))
            if not k8s_id:
                raise LcmException("K8s cluster '{}' has not been initilized for '{}'".format(cluster_id, cluster_type))
            k8scluster_id_2_uuic[cluster_type][cluster_id] = k8s_id
            return k8s_id

        logging_text += "Deploy kdus: "
        try:
            db_nsr_update = {"_admin.deployed.K8s": []}
            self.update_db_2("nsrs", nsr_id, db_nsr_update)

            # Look for all vnfds
            pending_tasks = {}
            index = 0
            for vnfr_data in db_vnfrs.values():
                for kdur in get_iterable(vnfr_data, "kdur"):
                    desc_params = self._format_additional_params(kdur.get("additionalParams"))
                    kdumodel = None
                    k8sclustertype = None
                    error_text = None
                    cluster_uuid = None
                    if kdur.get("helm-chart"):
                        kdumodel = kdur["helm-chart"]
                        k8sclustertype = "chart"
                        k8sclustertype_full = "helm-chart"
                    elif kdur.get("juju-bundle"):
                        kdumodel = kdur["juju-bundle"]
                        k8sclustertype = "juju"
                        k8sclustertype_full = "juju-bundle"
                    else:
                        error_text = "kdu type is neither helm-chart not juju-bundle. Maybe an old NBI version is" \
                                     " running"
                    try:
                        if not error_text:
                            cluster_uuid = _get_cluster_id(kdur["k8s-cluster"]["id"], k8sclustertype_full)
                    except LcmException as e:
                        error_text = str(e)
                    step = "Instantiate KDU {} in k8s cluster {}".format(kdur["kdu-name"], cluster_uuid)

                    k8s_instace_info = {"kdu-instance": None, "k8scluster-uuid": cluster_uuid,
                                        "k8scluster-type": k8sclustertype,
                                        "kdu-name": kdur["kdu-name"], "kdu-model": kdumodel}
                    if error_text:
                        k8s_instace_info["detailed-status"] = error_text
                    db_nsr_update["_admin.deployed.K8s.{}".format(index)] = k8s_instace_info
                    self.update_db_2("nsrs", nsr_id, db_nsr_update)
                    if error_text:
                        continue

                    db_dict = {"collection": "nsrs", "filter": {"_id": nsr_id}, "path": "_admin.deployed.K8s."
                                                                                        "{}".format(index)}
                    if k8sclustertype == "chart":
                        task = asyncio.ensure_future(
                            self.k8sclusterhelm.install(cluster_uuid=cluster_uuid, kdu_model=kdumodel, atomic=True,
                                                        params=desc_params, db_dict=db_dict, timeout=3600)
                        )
                    else:
                        task = self.k8sclusterjuju.install(cluster_uuid=cluster_uuid, kdu_model=kdumodel,
                                                           atomic=True, params=desc_params,
                                                           db_dict=db_dict, timeout=600)

                    pending_tasks[task] = "_admin.deployed.K8s.{}.".format(index)
                    index += 1
            if not pending_tasks:
                return
            self.logger.debug(logging_text + 'Waiting for terminate pending tasks...')
            pending_list = list(pending_tasks.keys())
            while pending_list:
                done_list, pending_list = await asyncio.wait(pending_list, timeout=30*60,
                                                             return_when=asyncio.FIRST_COMPLETED)
                if not done_list:   # timeout
                    for task in pending_list:
                        db_nsr_update[pending_tasks(task) + "detailed-status"] = "Timeout"
                    break
                for task in done_list:
                    exc = task.exception()
                    if exc:
                        db_nsr_update[pending_tasks[task] + "detailed-status"] = "{}".format(exc)
                    else:
                        db_nsr_update[pending_tasks[task] + "kdu-instance"] = task.result()

        except Exception as e:
            self.logger.critical(logging_text + "Exit Exception {} while '{}': {}".format(type(e).__name__, step, e))
            raise LcmException("{} Exit Exception {} while '{}': {}".format(logging_text, type(e).__name__, step, e))
        finally:
            # TODO Write in data base
            if db_nsr_update:
                self.update_db_2("nsrs", nsr_id, db_nsr_update)

    def _deploy_n2vc(self, logging_text, db_nsr, db_vnfr, nslcmop_id, nsr_id, nsi_id, vnfd_id, vdu_id,
                     kdu_name, member_vnf_index, vdu_index, vdu_name, deploy_params, descriptor_config,
                     base_folder, task_instantiation_list, task_instantiation_info):
        # launch instantiate_N2VC in a asyncio task and register task object
        # Look where information of this charm is at database <nsrs>._admin.deployed.VCA
        # if not found, create one entry and update database

        # fill db_nsr._admin.deployed.VCA.<index>
        vca_index = -1
        for vca_index, vca_deployed in enumerate(db_nsr["_admin"]["deployed"]["VCA"]):
            if not vca_deployed:
                continue
            if vca_deployed.get("member-vnf-index") == member_vnf_index and \
                    vca_deployed.get("vdu_id") == vdu_id and \
                    vca_deployed.get("kdu_name") == kdu_name and \
                    vca_deployed.get("vdu_count_index", 0) == vdu_index:
                break
        else:
            # not found, create one.
            vca_deployed = {
                "member-vnf-index": member_vnf_index,
                "vdu_id": vdu_id,
                "kdu_name": kdu_name,
                "vdu_count_index": vdu_index,
                "operational-status": "init",  # TODO revise
                "detailed-status": "",  # TODO revise
                "step": "initial-deploy",   # TODO revise
                "vnfd_id": vnfd_id,
                "vdu_name": vdu_name,
            }
            vca_index += 1
            self.update_db_2("nsrs", nsr_id, {"_admin.deployed.VCA.{}".format(vca_index): vca_deployed})
            db_nsr["_admin"]["deployed"]["VCA"].append(vca_deployed)

        # Launch task
        task_n2vc = asyncio.ensure_future(
            self.instantiate_N2VC(
                logging_text=logging_text,
                vca_index=vca_index,
                nsi_id=nsi_id,
                db_nsr=db_nsr,
                db_vnfr=db_vnfr,
                vdu_id=vdu_id,
                kdu_name=kdu_name,
                vdu_index=vdu_index,
                deploy_params=deploy_params,
                config_descriptor=descriptor_config,
                base_folder=base_folder,
            )
        )
        self.lcm_tasks.register("ns", nsr_id, nslcmop_id, "instantiate_N2VC-{}".format(vca_index), task_n2vc)
        task_instantiation_info[task_n2vc] = "Deploy VCA {}.{}".format(member_vnf_index or "", vdu_id or "")
        task_instantiation_list.append(task_n2vc)

    # Check if this VNFD has a configured terminate action
    def _has_terminate_config_primitive(self, vnfd):
        vnf_config = vnfd.get("vnf-configuration")
        if vnf_config and vnf_config.get("terminate-config-primitive"):
            return True
        else:
            return False

    @staticmethod
    def _get_terminate_config_primitive_seq_list(vnfd):
        """ Get a numerically sorted list of the sequences for this VNFD's terminate action """
        # No need to check for existing primitive twice, already done before
        vnf_config = vnfd.get("vnf-configuration")
        seq_list = vnf_config.get("terminate-config-primitive")
        # Get all 'seq' tags in seq_list, order sequences numerically, ascending.
        seq_list_sorted = sorted(seq_list, key=lambda x: int(x['seq']))
        return seq_list_sorted

    @staticmethod
    def _create_nslcmop(nsr_id, operation, params):
        """
        Creates a ns-lcm-opp content to be stored at database.
        :param nsr_id: internal id of the instance
        :param operation: instantiate, terminate, scale, action, ...
        :param params: user parameters for the operation
        :return: dictionary following SOL005 format
        """
        # Raise exception if invalid arguments
        if not (nsr_id and operation and params):
            raise LcmException(
                "Parameters 'nsr_id', 'operation' and 'params' needed to create primitive not provided")
        now = time()
        _id = str(uuid4())
        nslcmop = {
            "id": _id,
            "_id": _id,
            # COMPLETED,PARTIALLY_COMPLETED,FAILED_TEMP,FAILED,ROLLING_BACK,ROLLED_BACK
            "operationState": "PROCESSING",
            "statusEnteredTime": now,
            "nsInstanceId": nsr_id,
            "lcmOperationType": operation,
            "startTime": now,
            "isAutomaticInvocation": False,
            "operationParams": params,
            "isCancelPending": False,
            "links": {
                "self": "/osm/nslcm/v1/ns_lcm_op_occs/" + _id,
                "nsInstance": "/osm/nslcm/v1/ns_instances/" + nsr_id,
            }
        }
        return nslcmop

    def _format_additional_params(self, params):
        params = params or {}
        for key, value in params.items():
            if str(value).startswith("!!yaml "):
                params[key] = yaml.safe_load(value[7:])
        return params

    def _get_terminate_primitive_params(self, seq, vnf_index):
        primitive = seq.get('name')
        primitive_params = {}
        params = {
            "member_vnf_index": vnf_index,
            "primitive": primitive,
            "primitive_params": primitive_params,
        }
        desc_params = {}
        return self._map_primitive_params(seq, params, desc_params)

    # sub-operations

    def _reintent_or_skip_suboperation(self, db_nslcmop, op_index):
        op = db_nslcmop.get('_admin', {}).get('operations', [])[op_index]
        if (op.get('operationState') == 'COMPLETED'):
            # b. Skip sub-operation
            # _ns_execute_primitive() or RO.create_action() will NOT be executed
            return self.SUBOPERATION_STATUS_SKIP
        else:
            # c. Reintent executing sub-operation
            # The sub-operation exists, and operationState != 'COMPLETED'
            # Update operationState = 'PROCESSING' to indicate a reintent.
            operationState = 'PROCESSING'
            detailed_status = 'In progress'
            self._update_suboperation_status(
                db_nslcmop, op_index, operationState, detailed_status)
            # Return the sub-operation index
            # _ns_execute_primitive() or RO.create_action() will be called from scale()
            # with arguments extracted from the sub-operation
            return op_index

    # Find a sub-operation where all keys in a matching dictionary must match
    # Returns the index of the matching sub-operation, or SUBOPERATION_STATUS_NOT_FOUND if no match
    def _find_suboperation(self, db_nslcmop, match):
        if (db_nslcmop and match):
            op_list = db_nslcmop.get('_admin', {}).get('operations', [])
            for i, op in enumerate(op_list):
                if all(op.get(k) == match[k] for k in match):
                    return i
        return self.SUBOPERATION_STATUS_NOT_FOUND

    # Update status for a sub-operation given its index
    def _update_suboperation_status(self, db_nslcmop, op_index, operationState, detailed_status):
        # Update DB for HA tasks
        q_filter = {'_id': db_nslcmop['_id']}
        update_dict = {'_admin.operations.{}.operationState'.format(op_index): operationState,
                       '_admin.operations.{}.detailed-status'.format(op_index): detailed_status}
        self.db.set_one("nslcmops",
                        q_filter=q_filter,
                        update_dict=update_dict,
                        fail_on_empty=False)

    # Add sub-operation, return the index of the added sub-operation
    # Optionally, set operationState, detailed-status, and operationType
    # Status and type are currently set for 'scale' sub-operations:
    # 'operationState' : 'PROCESSING' | 'COMPLETED' | 'FAILED'
    # 'detailed-status' : status message
    # 'operationType': may be any type, in the case of scaling: 'PRE-SCALE' | 'POST-SCALE'
    # Status and operation type are currently only used for 'scale', but NOT for 'terminate' sub-operations.
    def _add_suboperation(self, db_nslcmop, vnf_index, vdu_id, vdu_count_index, vdu_name, primitive, 
                          mapped_primitive_params, operationState=None, detailed_status=None, operationType=None,
                          RO_nsr_id=None, RO_scaling_info=None):
        if not (db_nslcmop):
            return self.SUBOPERATION_STATUS_NOT_FOUND
        # Get the "_admin.operations" list, if it exists
        db_nslcmop_admin = db_nslcmop.get('_admin', {})
        op_list = db_nslcmop_admin.get('operations')
        # Create or append to the "_admin.operations" list
        new_op = {'member_vnf_index': vnf_index,
                  'vdu_id': vdu_id,
                  'vdu_count_index': vdu_count_index,
                  'primitive': primitive,
                  'primitive_params': mapped_primitive_params}
        if operationState:
            new_op['operationState'] = operationState
        if detailed_status:
            new_op['detailed-status'] = detailed_status
        if operationType:
            new_op['lcmOperationType'] = operationType
        if RO_nsr_id:
            new_op['RO_nsr_id'] = RO_nsr_id
        if RO_scaling_info:
            new_op['RO_scaling_info'] = RO_scaling_info
        if not op_list:
            # No existing operations, create key 'operations' with current operation as first list element
            db_nslcmop_admin.update({'operations': [new_op]})
            op_list = db_nslcmop_admin.get('operations')
        else:
            # Existing operations, append operation to list
            op_list.append(new_op)

        db_nslcmop_update = {'_admin.operations': op_list}
        self.update_db_2("nslcmops", db_nslcmop['_id'], db_nslcmop_update)
        op_index = len(op_list) - 1
        return op_index

    # Helper methods for scale() sub-operations

    # pre-scale/post-scale:
    # Check for 3 different cases:
    # a. New: First time execution, return SUBOPERATION_STATUS_NEW
    # b. Skip: Existing sub-operation exists, operationState == 'COMPLETED', return SUBOPERATION_STATUS_SKIP
    # c. Reintent: Existing sub-operation exists, operationState != 'COMPLETED', return op_index to re-execute
    def _check_or_add_scale_suboperation(self, db_nslcmop, vnf_index, vnf_config_primitive, primitive_params,
                                         operationType, RO_nsr_id=None, RO_scaling_info=None):
        # Find this sub-operation
        if (RO_nsr_id and RO_scaling_info):
            operationType = 'SCALE-RO'
            match = {
                'member_vnf_index': vnf_index,
                'RO_nsr_id': RO_nsr_id,
                'RO_scaling_info': RO_scaling_info,
            }
        else:
            match = {
                'member_vnf_index': vnf_index,
                'primitive': vnf_config_primitive,
                'primitive_params': primitive_params,
                'lcmOperationType': operationType
            }
        op_index = self._find_suboperation(db_nslcmop, match)
        if (op_index == self.SUBOPERATION_STATUS_NOT_FOUND):
            # a. New sub-operation
            # The sub-operation does not exist, add it.
            # _ns_execute_primitive() will be called from scale() as usual, with non-modified arguments
            # The following parameters are set to None for all kind of scaling:
            vdu_id = None
            vdu_count_index = None
            vdu_name = None
            if (RO_nsr_id and RO_scaling_info):
                vnf_config_primitive = None
                primitive_params = None
            else:
                RO_nsr_id = None
                RO_scaling_info = None
            # Initial status for sub-operation
            operationState = 'PROCESSING'
            detailed_status = 'In progress'
            # Add sub-operation for pre/post-scaling (zero or more operations)
            self._add_suboperation(db_nslcmop,
                                   vnf_index,
                                   vdu_id,
                                   vdu_count_index,
                                   vdu_name,
                                   vnf_config_primitive,
                                   primitive_params,
                                   operationState,
                                   detailed_status,
                                   operationType,
                                   RO_nsr_id,
                                   RO_scaling_info)
            return self.SUBOPERATION_STATUS_NEW
        else:
            # Return either SUBOPERATION_STATUS_SKIP (operationState == 'COMPLETED'),
            # or op_index (operationState != 'COMPLETED')
            return self._reintent_or_skip_suboperation(db_nslcmop, op_index)

    # Helper methods for terminate()

    async def _terminate_action(self, db_nslcmop, nslcmop_id, nsr_id):
        """ Create a primitive with params from VNFD
            Called from terminate() before deleting instance
            Calls action() to execute the primitive """
        logging_text = "Task ns={} _terminate_action={} ".format(nsr_id, nslcmop_id)
        db_vnfrs_list = self.db.get_list("vnfrs", {"nsr-id-ref": nsr_id})
        db_vnfds = {}
        # Loop over VNFRs
        for vnfr in db_vnfrs_list:
            vnfd_id = vnfr["vnfd-id"]
            vnf_index = vnfr["member-vnf-index-ref"]
            if vnfd_id not in db_vnfds:
                step = "Getting vnfd={} id='{}' from db".format(vnfd_id, vnfd_id)
                vnfd = self.db.get_one("vnfds", {"_id": vnfd_id})
                db_vnfds[vnfd_id] = vnfd
            vnfd = db_vnfds[vnfd_id]
            if not self._has_terminate_config_primitive(vnfd):
                continue
            # Get the primitive's sorted sequence list
            seq_list = self._get_terminate_config_primitive_seq_list(vnfd)
            for seq in seq_list:
                # For each sequence in list, get primitive and call _ns_execute_primitive()
                step = "Calling terminate action for vnf_member_index={} primitive={}".format(
                    vnf_index, seq.get("name"))
                self.logger.debug(logging_text + step)
                # Create the primitive for each sequence, i.e. "primitive": "touch"
                primitive = seq.get('name')
                mapped_primitive_params = self._get_terminate_primitive_params(seq, vnf_index)
                # The following 3 parameters are currently set to None for 'terminate':
                # vdu_id, vdu_count_index, vdu_name
                vdu_id = db_nslcmop["operationParams"].get("vdu_id")
                vdu_count_index = db_nslcmop["operationParams"].get("vdu_count_index")
                vdu_name = db_nslcmop["operationParams"].get("vdu_name")
                # Add sub-operation
                self._add_suboperation(db_nslcmop,
                                       nslcmop_id,
                                       vnf_index,
                                       vdu_id,
                                       vdu_count_index,
                                       vdu_name,
                                       primitive,
                                       mapped_primitive_params)
                # Sub-operations: Call _ns_execute_primitive() instead of action()
                # db_nsr = self.db.get_one("nsrs", {"_id": nsr_id})
                # nsr_deployed = db_nsr["_admin"]["deployed"]

                # nslcmop_operation_state, nslcmop_operation_state_detail = await self.action(
                #    nsr_id, nslcmop_terminate_action_id)
                # Launch Exception if action() returns other than ['COMPLETED', 'PARTIALLY_COMPLETED']
                # result_ok = ['COMPLETED', 'PARTIALLY_COMPLETED']
                # if result not in result_ok:
                #     raise LcmException(
                #         "terminate_primitive_action for vnf_member_index={}",
                #         " primitive={} fails with error {}".format(
                #             vnf_index, seq.get("name"), result_detail))

                # TODO: find ee_id
                ee_id = None
                try:
                    await self.n2vc.exec_primitive(
                        ee_id=ee_id,
                        primitive_name=primitive,
                        params_dict=mapped_primitive_params
                    )
                except Exception as e:
                    self.logger.error('Error executing primitive {}: {}'.format(primitive, e))
                    raise LcmException(
                        "terminate_primitive_action for vnf_member_index={}, primitive={} fails with error {}"
                        .format(vnf_index, seq.get("name"), e),
                    )

    async def terminate(self, nsr_id, nslcmop_id):

        # Try to lock HA task here
        task_is_locked_by_me = self.lcm_tasks.lock_HA('ns', 'nslcmops', nslcmop_id)
        if not task_is_locked_by_me:
            return

        logging_text = "Task ns={} terminate={} ".format(nsr_id, nslcmop_id)
        self.logger.debug(logging_text + "Enter")
        db_nsr = None
        db_nslcmop = None
        exc = None
        failed_detail = []   # annotates all failed error messages
        db_nsr_update = {"_admin.nslcmop": nslcmop_id,
                         "_admin.current-operation": nslcmop_id,
                         "_admin.operation-type": "terminate"}
        self.update_db_2("nsrs", nsr_id, db_nsr_update)
        db_nslcmop_update = {}
        nslcmop_operation_state = None
        autoremove = False  # autoremove after terminated
        pending_tasks = []
        try:
            # wait for any previous tasks in process
            step = "Waiting for previous operations to terminate"
            await self.lcm_tasks.waitfor_related_HA("ns", 'nslcmops', nslcmop_id)

            self._write_ns_status(
                nsr_id=nsr_id,
                ns_state="TERMINATING",
                current_operation="TERMINATING",
                current_operation_id=nslcmop_id
            )

            step = "Getting nslcmop={} from db".format(nslcmop_id)
            db_nslcmop = self.db.get_one("nslcmops", {"_id": nslcmop_id})
            step = "Getting nsr={} from db".format(nsr_id)
            db_nsr = self.db.get_one("nsrs", {"_id": nsr_id})
            # nsd = db_nsr["nsd"]
            nsr_deployed = deepcopy(db_nsr["_admin"].get("deployed"))
            if db_nsr["_admin"]["nsState"] == "NOT_INSTANTIATED":
                return
            # #TODO check if VIM is creating and wait
            # RO_vim_id = db_vim["_admin"]["deployed"]["RO"]
            # Call internal terminate action
            await self._terminate_action(db_nslcmop, nslcmop_id, nsr_id)

            pending_tasks = []

            db_nsr_update["operational-status"] = "terminating"
            db_nsr_update["config-status"] = "terminating"

            # remove NS
            try:
                step = "delete execution environment"
                self.logger.debug(logging_text + step)

                task_delete_ee = asyncio.ensure_future(self.n2vc.delete_namespace(namespace="." + nsr_id))
                pending_tasks.append(task_delete_ee)
            except Exception as e:
                msg = "Failed while deleting NS in VCA: {}".format(e)
                self.logger.error(msg)
                failed_detail.append(msg)

            try:
                # Delete from k8scluster
                step = "delete kdus"
                self.logger.debug(logging_text + step)
                # print(nsr_deployed)
                if nsr_deployed:
                    for kdu in nsr_deployed.get("K8s", ()):
                        kdu_instance = kdu.get("kdu-instance")
                        if not kdu_instance:
                            continue
                        if kdu.get("k8scluster-type") == "chart":
                            task_delete_kdu_instance = asyncio.ensure_future(
                                self.k8sclusterhelm.uninstall(cluster_uuid=kdu.get("k8scluster-uuid"),
                                                              kdu_instance=kdu_instance))
                        elif kdu.get("k8scluster-type") == "juju":
                            task_delete_kdu_instance = asyncio.ensure_future(
                                self.k8sclusterjuju.uninstall(cluster_uuid=kdu.get("k8scluster-uuid"),
                                                              kdu_instance=kdu_instance))
                        else:
                            self.error(logging_text + "Unknown k8s deployment type {}".
                                       format(kdu.get("k8scluster-type")))
                            continue
                        pending_tasks.append(task_delete_kdu_instance)
            except LcmException as e:
                msg = "Failed while deleting KDUs from NS: {}".format(e)
                self.logger.error(msg)
                failed_detail.append(msg)

            # remove from RO
            RO_fail = False

            # Delete ns
            RO_nsr_id = RO_delete_action = None
            if nsr_deployed and nsr_deployed.get("RO"):
                RO_nsr_id = nsr_deployed["RO"].get("nsr_id")
                RO_delete_action = nsr_deployed["RO"].get("nsr_delete_action_id")
            try:
                if RO_nsr_id:
                    step = db_nsr_update["detailed-status"] = db_nslcmop_update["detailed-status"] = \
                        "Deleting ns from VIM"
                    self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
                    self.update_db_2("nsrs", nsr_id, db_nsr_update)
                    self.logger.debug(logging_text + step)
                    desc = await self.RO.delete("ns", RO_nsr_id)
                    RO_delete_action = desc["action_id"]
                    db_nsr_update["_admin.deployed.RO.nsr_delete_action_id"] = RO_delete_action
                    db_nsr_update["_admin.deployed.RO.nsr_id"] = None
                    db_nsr_update["_admin.deployed.RO.nsr_status"] = "DELETED"
                if RO_delete_action:
                    # wait until NS is deleted from VIM
                    step = detailed_status = "Waiting ns deleted from VIM. RO_id={} RO_delete_action={}".\
                        format(RO_nsr_id, RO_delete_action)
                    detailed_status_old = None
                    self.logger.debug(logging_text + step)

                    delete_timeout = 20 * 60   # 20 minutes
                    while delete_timeout > 0:
                        desc = await self.RO.show(
                            "ns",
                            item_id_name=RO_nsr_id,
                            extra_item="action",
                            extra_item_id=RO_delete_action)
                        ns_status, ns_status_info = self.RO.check_action_status(desc)
                        if ns_status == "ERROR":
                            raise ROclient.ROClientException(ns_status_info)
                        elif ns_status == "BUILD":
                            detailed_status = step + "; {}".format(ns_status_info)
                        elif ns_status == "ACTIVE":
                            db_nsr_update["_admin.deployed.RO.nsr_delete_action_id"] = None
                            db_nsr_update["_admin.deployed.RO.nsr_status"] = "DELETED"
                            break
                        else:
                            assert False, "ROclient.check_action_status returns unknown {}".format(ns_status)
                        if detailed_status != detailed_status_old:
                            detailed_status_old = db_nslcmop_update["detailed-status"] = \
                                db_nsr_update["detailed-status"] = detailed_status
                            self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
                            self.update_db_2("nsrs", nsr_id, db_nsr_update)
                        await asyncio.sleep(5, loop=self.loop)
                        delete_timeout -= 5
                    else:  # delete_timeout <= 0:
                        raise ROclient.ROClientException("Timeout waiting ns deleted from VIM")

            except ROclient.ROClientException as e:
                if e.http_code == 404:  # not found
                    db_nsr_update["_admin.deployed.RO.nsr_id"] = None
                    db_nsr_update["_admin.deployed.RO.nsr_status"] = "DELETED"
                    db_nsr_update["_admin.deployed.RO.nsr_delete_action_id"] = None
                    self.logger.debug(logging_text + "RO_ns_id={} already deleted".format(RO_nsr_id))
                elif e.http_code == 409:   # conflict
                    failed_detail.append("RO_ns_id={} delete conflict: {}".format(RO_nsr_id, e))
                    self.logger.debug(logging_text + failed_detail[-1])
                    RO_fail = True
                else:
                    failed_detail.append("RO_ns_id={} delete error: {}".format(RO_nsr_id, e))
                    self.logger.error(logging_text + failed_detail[-1])
                    RO_fail = True

            # Delete nsd
            if not RO_fail and nsr_deployed and nsr_deployed.get("RO") and nsr_deployed["RO"].get("nsd_id"):
                RO_nsd_id = nsr_deployed["RO"]["nsd_id"]
                try:
                    step = db_nsr_update["detailed-status"] = db_nslcmop_update["detailed-status"] =\
                        "Deleting nsd from RO"
                    await self.RO.delete("nsd", RO_nsd_id)
                    self.logger.debug(logging_text + "RO_nsd_id={} deleted".format(RO_nsd_id))
                    db_nsr_update["_admin.deployed.RO.nsd_id"] = None
                except ROclient.ROClientException as e:
                    if e.http_code == 404:  # not found
                        db_nsr_update["_admin.deployed.RO.nsd_id"] = None
                        self.logger.debug(logging_text + "RO_nsd_id={} already deleted".format(RO_nsd_id))
                    elif e.http_code == 409:   # conflict
                        failed_detail.append("RO_nsd_id={} delete conflict: {}".format(RO_nsd_id, e))
                        self.logger.debug(logging_text + failed_detail[-1])
                        RO_fail = True
                    else:
                        failed_detail.append("RO_nsd_id={} delete error: {}".format(RO_nsd_id, e))
                        self.logger.error(logging_text + failed_detail[-1])
                        RO_fail = True

            if not RO_fail and nsr_deployed and nsr_deployed.get("RO") and nsr_deployed["RO"].get("vnfd"):
                for index, vnf_deployed in enumerate(nsr_deployed["RO"]["vnfd"]):
                    if not vnf_deployed or not vnf_deployed["id"]:
                        continue
                    try:
                        RO_vnfd_id = vnf_deployed["id"]
                        step = db_nsr_update["detailed-status"] = db_nslcmop_update["detailed-status"] =\
                            "Deleting member_vnf_index={} RO_vnfd_id={} from RO".format(
                                vnf_deployed["member-vnf-index"], RO_vnfd_id)
                        await self.RO.delete("vnfd", RO_vnfd_id)
                        self.logger.debug(logging_text + "RO_vnfd_id={} deleted".format(RO_vnfd_id))
                        db_nsr_update["_admin.deployed.RO.vnfd.{}.id".format(index)] = None
                    except ROclient.ROClientException as e:
                        if e.http_code == 404:  # not found
                            db_nsr_update["_admin.deployed.RO.vnfd.{}.id".format(index)] = None
                            self.logger.debug(logging_text + "RO_vnfd_id={} already deleted ".format(RO_vnfd_id))
                        elif e.http_code == 409:   # conflict
                            failed_detail.append("RO_vnfd_id={} delete conflict: {}".format(RO_vnfd_id, e))
                            self.logger.debug(logging_text + failed_detail[-1])
                        else:
                            failed_detail.append("RO_vnfd_id={} delete error: {}".format(RO_vnfd_id, e))
                            self.logger.error(logging_text + failed_detail[-1])

            if failed_detail:
                terminate_ok = False
                self.logger.error(logging_text + " ;".join(failed_detail))
                db_nsr_update["operational-status"] = "failed"
                db_nsr_update["detailed-status"] = "Deletion errors " + "; ".join(failed_detail)
                db_nslcmop_update["detailed-status"] = "; ".join(failed_detail)
                db_nslcmop_update["operationState"] = nslcmop_operation_state = "FAILED"
                db_nslcmop_update["statusEnteredTime"] = time()
            else:
                terminate_ok = True
                db_nsr_update["operational-status"] = "terminated"
                db_nsr_update["detailed-status"] = "Done"
                db_nsr_update["_admin.nsState"] = "NOT_INSTANTIATED"
                db_nslcmop_update["detailed-status"] = "Done"
                db_nslcmop_update["operationState"] = nslcmop_operation_state = "COMPLETED"
                db_nslcmop_update["statusEnteredTime"] = time()
                if db_nslcmop["operationParams"].get("autoremove"):
                    autoremove = True

        except (ROclient.ROClientException, DbException, LcmException) as e:
            self.logger.error(logging_text + "Exit Exception {}".format(e))
            exc = e
        except asyncio.CancelledError:
            self.logger.error(logging_text + "Cancelled Exception while '{}'".format(step))
            exc = "Operation was cancelled"
        except Exception as e:
            exc = traceback.format_exc()
            self.logger.critical(logging_text + "Exit Exception {}".format(e), exc_info=True)
        finally:
            if exc and db_nslcmop:
                db_nslcmop_update["detailed-status"] = "FAILED {}: {}".format(step, exc)
                db_nslcmop_update["operationState"] = nslcmop_operation_state = "FAILED"
                db_nslcmop_update["statusEnteredTime"] = time()
            try:
                if db_nslcmop and db_nslcmop_update:
                    self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
                if db_nsr:
                    db_nsr_update["_admin.nslcmop"] = None
                    db_nsr_update["_admin.current-operation"] = None
                    db_nsr_update["_admin.operation-type"] = None
                    self.update_db_2("nsrs", nsr_id, db_nsr_update)

                    if terminate_ok:
                        ns_state = "IDLE"
                        error_description = None
                        error_detail = None
                    else:
                        ns_state = "BROKEN"
                        error_description = 'Operation: TERMINATING.{}, step: {}'.format(nslcmop_id, step)
                        error_detail = "; ".join(failed_detail)

                    self._write_ns_status(
                        nsr_id=nsr_id,
                        ns_state=ns_state,
                        current_operation="IDLE",
                        current_operation_id=None,
                        error_description=error_description,
                        error_detail=error_detail
                    )

            except DbException as e:
                self.logger.error(logging_text + "Cannot update database: {}".format(e))
            if nslcmop_operation_state:
                try:
                    await self.msg.aiowrite("ns", "terminated", {"nsr_id": nsr_id, "nslcmop_id": nslcmop_id,
                                                                 "operationState": nslcmop_operation_state,
                                                                 "autoremove": autoremove},
                                            loop=self.loop)
                except Exception as e:
                    self.logger.error(logging_text + "kafka_write notification Exception {}".format(e))

            # wait for pending tasks
            done = None
            pending = None
            if pending_tasks:
                self.logger.debug(logging_text + 'Waiting for terminate pending tasks...')
                done, pending = await asyncio.wait(pending_tasks, timeout=3600)
                if not pending:
                    self.logger.debug(logging_text + 'All tasks finished...')
                else:
                    self.logger.info(logging_text + 'There are pending tasks: {}'.format(pending))

            self.logger.debug(logging_text + "Exit")
            self.lcm_tasks.remove("ns", nsr_id, nslcmop_id, "ns_terminate")

    @staticmethod
    def _map_primitive_params(primitive_desc, params, instantiation_params):
        """
        Generates the params to be provided to charm before executing primitive. If user does not provide a parameter,
        The default-value is used. If it is between < > it look for a value at instantiation_params
        :param primitive_desc: portion of VNFD/NSD that describes primitive
        :param params: Params provided by user
        :param instantiation_params: Instantiation params provided by user
        :return: a dictionary with the calculated params
        """
        calculated_params = {}
        for parameter in primitive_desc.get("parameter", ()):
            param_name = parameter["name"]
            if param_name in params:
                calculated_params[param_name] = params[param_name]
            elif "default-value" in parameter or "value" in parameter:
                if "value" in parameter:
                    calculated_params[param_name] = parameter["value"]
                else:
                    calculated_params[param_name] = parameter["default-value"]
                if isinstance(calculated_params[param_name], str) and calculated_params[param_name].startswith("<") \
                        and calculated_params[param_name].endswith(">"):
                    if calculated_params[param_name][1:-1] in instantiation_params:
                        calculated_params[param_name] = instantiation_params[calculated_params[param_name][1:-1]]
                    else:
                        raise LcmException("Parameter {} needed to execute primitive {} not provided".
                                           format(calculated_params[param_name], primitive_desc["name"]))
            else:
                raise LcmException("Parameter {} needed to execute primitive {} not provided".
                                   format(param_name, primitive_desc["name"]))

            if isinstance(calculated_params[param_name], (dict, list, tuple)):
                calculated_params[param_name] = yaml.safe_dump(calculated_params[param_name], default_flow_style=True,
                                                               width=256)
            elif isinstance(calculated_params[param_name], str) and calculated_params[param_name].startswith("!!yaml "):
                calculated_params[param_name] = calculated_params[param_name][7:]

        # add always ns_config_info if primitive name is config
        if primitive_desc["name"] == "config":
            if "ns_config_info" in instantiation_params:
                calculated_params["ns_config_info"] = instantiation_params["ns_config_info"]
        return calculated_params

    async def _ns_execute_primitive(self, db_deployed, member_vnf_index, vdu_id, vdu_name, vdu_count_index,
                                    primitive, primitive_params, retries=0, retries_interval=30) -> (str, str):

        # find vca_deployed record for this action
        try:
            for vca_deployed in db_deployed["VCA"]:
                if not vca_deployed:
                    continue
                if member_vnf_index != vca_deployed["member-vnf-index"] or vdu_id != vca_deployed["vdu_id"]:
                    continue
                if vdu_name and vdu_name != vca_deployed["vdu_name"]:
                    continue
                if vdu_count_index and vdu_count_index != vca_deployed["vdu_count_index"]:
                    continue
                break
            else:
                # vca_deployed not found
                raise LcmException("charm for member_vnf_index={} vdu_id={} vdu_name={} vdu_count_index={} is not "
                                   "deployed".format(member_vnf_index, vdu_id, vdu_name, vdu_count_index))

            # get ee_id
            ee_id = vca_deployed.get("ee_id")
            if not ee_id:
                raise LcmException("charm for member_vnf_index={} vdu_id={} vdu_name={} vdu_count_index={} has not "
                                   "execution environment"
                                   .format(member_vnf_index, vdu_id, vdu_name, vdu_count_index))

            if primitive == "config":
                primitive_params = {"params": primitive_params}

            while retries >= 0:
                try:
                    output = await self.n2vc.exec_primitive(
                        ee_id=ee_id,
                        primitive_name=primitive,
                        params_dict=primitive_params
                    )
                    # execution was OK
                    break
                except Exception as e:
                    retries -= 1
                    if retries >= 0:
                        self.logger.debug('Error executing action {} on {} -> {}'.format(primitive, ee_id, e))
                        # wait and retry
                        await asyncio.sleep(retries_interval, loop=self.loop)
                    else:
                        return 'Cannot execute action {} on {}: {}'.format(primitive, ee_id, e), 'FAIL'

            return output, 'OK'

        except Exception as e:
            return 'Error executing action {}: {}'.format(primitive, e), 'FAIL'

    async def action(self, nsr_id, nslcmop_id):

        # Try to lock HA task here
        task_is_locked_by_me = self.lcm_tasks.lock_HA('ns', 'nslcmops', nslcmop_id)
        if not task_is_locked_by_me:
            return

        logging_text = "Task ns={} action={} ".format(nsr_id, nslcmop_id)
        self.logger.debug(logging_text + "Enter")
        # get all needed from database
        db_nsr = None
        db_nslcmop = None
        db_nsr_update = {"_admin.nslcmop": nslcmop_id,
                         "_admin.current-operation": nslcmop_id,
                         "_admin.operation-type": "action"}
        self.update_db_2("nsrs", nsr_id, db_nsr_update)
        db_nslcmop_update = {}
        nslcmop_operation_state = None
        nslcmop_operation_state_detail = None
        exc = None
        try:
            # wait for any previous tasks in process
            step = "Waiting for previous operations to terminate"
            await self.lcm_tasks.waitfor_related_HA('ns', 'nslcmops', nslcmop_id)

            self._write_ns_status(
                nsr_id=nsr_id,
                ns_state=None,
                current_operation="RUNNING ACTION",
                current_operation_id=nslcmop_id
            )

            step = "Getting information from database"
            db_nslcmop = self.db.get_one("nslcmops", {"_id": nslcmop_id})
            db_nsr = self.db.get_one("nsrs", {"_id": nsr_id})

            nsr_deployed = db_nsr["_admin"].get("deployed")
            vnf_index = db_nslcmop["operationParams"].get("member_vnf_index")
            vdu_id = db_nslcmop["operationParams"].get("vdu_id")
            kdu_name = db_nslcmop["operationParams"].get("kdu_name")
            vdu_count_index = db_nslcmop["operationParams"].get("vdu_count_index")
            vdu_name = db_nslcmop["operationParams"].get("vdu_name")

            if vnf_index:
                step = "Getting vnfr from database"
                db_vnfr = self.db.get_one("vnfrs", {"member-vnf-index-ref": vnf_index, "nsr-id-ref": nsr_id})
                step = "Getting vnfd from database"
                db_vnfd = self.db.get_one("vnfds", {"_id": db_vnfr["vnfd-id"]})
            else:
                if db_nsr.get("nsd"):
                    db_nsd = db_nsr.get("nsd")    # TODO this will be removed
                else:
                    step = "Getting nsd from database"
                    db_nsd = self.db.get_one("nsds", {"_id": db_nsr["nsd-id"]})

            # for backward compatibility
            if nsr_deployed and isinstance(nsr_deployed.get("VCA"), dict):
                nsr_deployed["VCA"] = list(nsr_deployed["VCA"].values())
                db_nsr_update["_admin.deployed.VCA"] = nsr_deployed["VCA"]
                self.update_db_2("nsrs", nsr_id, db_nsr_update)

            primitive = db_nslcmop["operationParams"]["primitive"]
            primitive_params = db_nslcmop["operationParams"]["primitive_params"]

            # look for primitive
            config_primitive_desc = None
            if vdu_id:
                for vdu in get_iterable(db_vnfd, "vdu"):
                    if vdu_id == vdu["id"]:
                        for config_primitive in vdu.get("vdu-configuration", {}).get("config-primitive", ()):
                            if config_primitive["name"] == primitive:
                                config_primitive_desc = config_primitive
                                break
            elif kdu_name:
                self.logger.debug(logging_text + "Checking actions in KDUs")
                kdur = next((x for x in db_vnfr["kdur"] if x["kdu-name"] == kdu_name), None)
                desc_params = self._format_additional_params(kdur.get("additionalParams")) or {}
                if primitive_params:
                    desc_params.update(primitive_params)
                # TODO Check if we will need something at vnf level
                index = 0
                for kdu in get_iterable(nsr_deployed, "K8s"):
                    if kdu_name == kdu["kdu-name"]:
                        db_dict = {"collection": "nsrs", "filter": {"_id": nsr_id},
                                   "path": "_admin.deployed.K8s.{}".format(index)}
                        if primitive == "upgrade":
                            if desc_params.get("kdu_model"):
                                kdu_model = desc_params.get("kdu_model")
                                del desc_params["kdu_model"]
                            else:
                                kdu_model = kdu.get("kdu-model")
                                parts = kdu_model.split(sep=":")
                                if len(parts) == 2:
                                    kdu_model = parts[0]

                            if kdu.get("k8scluster-type") == "chart":
                                output = await self.k8sclusterhelm.upgrade(cluster_uuid=kdu.get("k8scluster-uuid"),
                                                                           kdu_instance=kdu.get("kdu-instance"),
                                                                           atomic=True, kdu_model=kdu_model,
                                                                           params=desc_params, db_dict=db_dict,
                                                                           timeout=300)
                            elif kdu.get("k8scluster-type") == "juju":
                                output = await self.k8sclusterjuju.upgrade(cluster_uuid=kdu.get("k8scluster-uuid"),
                                                                           kdu_instance=kdu.get("kdu-instance"),
                                                                           atomic=True, kdu_model=kdu_model,
                                                                           params=desc_params, db_dict=db_dict,
                                                                           timeout=300)

                            else:
                                msg = "k8scluster-type not defined"
                                raise LcmException(msg)

                            self.logger.debug(logging_text + " Upgrade of kdu {} done".format(output))
                            break
                        elif primitive == "rollback":
                            if kdu.get("k8scluster-type") == "chart":
                                output = await self.k8sclusterhelm.rollback(cluster_uuid=kdu.get("k8scluster-uuid"),
                                                                            kdu_instance=kdu.get("kdu-instance"),
                                                                            db_dict=db_dict)
                            elif kdu.get("k8scluster-type") == "juju":
                                output = await self.k8sclusterjuju.rollback(cluster_uuid=kdu.get("k8scluster-uuid"),
                                                                            kdu_instance=kdu.get("kdu-instance"),
                                                                            db_dict=db_dict)
                            else:
                                msg = "k8scluster-type not defined"
                                raise LcmException(msg)
                            break
                        elif primitive == "status":
                            if kdu.get("k8scluster-type") == "chart":
                                output = await self.k8sclusterhelm.status_kdu(cluster_uuid=kdu.get("k8scluster-uuid"),
                                                                              kdu_instance=kdu.get("kdu-instance"))
                            elif kdu.get("k8scluster-type") == "juju":
                                output = await self.k8sclusterjuju.status_kdu(cluster_uuid=kdu.get("k8scluster-uuid"),
                                                                              kdu_instance=kdu.get("kdu-instance"))
                            else:
                                msg = "k8scluster-type not defined"
                                raise LcmException(msg)
                            break
                    index += 1

                else:
                    raise LcmException("KDU '{}' not found".format(kdu_name))
                if output:
                    db_nslcmop_update["detailed-status"] = output
                    db_nslcmop_update["operationState"] = 'COMPLETED'
                    db_nslcmop_update["statusEnteredTime"] = time()
                else:
                    db_nslcmop_update["detailed-status"] = ''
                    db_nslcmop_update["operationState"] = 'FAILED'
                    db_nslcmop_update["statusEnteredTime"] = time()
                return
            elif vnf_index:
                for config_primitive in db_vnfd.get("vnf-configuration", {}).get("config-primitive", ()):
                    if config_primitive["name"] == primitive:
                        config_primitive_desc = config_primitive
                        break
            else:
                for config_primitive in db_nsd.get("ns-configuration", {}).get("config-primitive", ()):
                    if config_primitive["name"] == primitive:
                        config_primitive_desc = config_primitive
                        break

            if not config_primitive_desc:
                raise LcmException("Primitive {} not found at [ns|vnf|vdu]-configuration:config-primitive ".
                                   format(primitive))

            desc_params = {}
            if vnf_index:
                if db_vnfr.get("additionalParamsForVnf"):
                    desc_params = self._format_additional_params(db_vnfr["additionalParamsForVnf"])
                if vdu_id:
                    vdur = next((x for x in db_vnfr["vdur"] if x["vdu-id-ref"] == vdu_id), None)
                    if vdur.get("additionalParams"):
                        desc_params = self._format_additional_params(vdur["additionalParams"])
            else:
                if db_nsr.get("additionalParamsForNs"):
                    desc_params.update(self._format_additional_params(db_nsr["additionalParamsForNs"]))

            # TODO check if ns is in a proper status
            output, detail = await self._ns_execute_primitive(
                db_deployed=nsr_deployed,
                member_vnf_index=vnf_index,
                vdu_id=vdu_id,
                vdu_name=vdu_name,
                vdu_count_index=vdu_count_index,
                primitive=primitive,
                primitive_params=self._map_primitive_params(config_primitive_desc, primitive_params, desc_params))

            detailed_status = output
            if detail == 'OK':
                result = 'COMPLETED'
            else:
                result = 'FAILED'

            db_nslcmop_update["detailed-status"] = nslcmop_operation_state_detail = detailed_status
            db_nslcmop_update["operationState"] = nslcmop_operation_state = result
            db_nslcmop_update["statusEnteredTime"] = time()
            self.logger.debug(logging_text + " task Done with result {} {}".format(result, detailed_status))
            return  # database update is called inside finally

        except (DbException, LcmException) as e:
            self.logger.error(logging_text + "Exit Exception {}".format(e))
            exc = e
        except asyncio.CancelledError:
            self.logger.error(logging_text + "Cancelled Exception while '{}'".format(step))
            exc = "Operation was cancelled"
        except Exception as e:
            exc = traceback.format_exc()
            self.logger.critical(logging_text + "Exit Exception {} {}".format(type(e).__name__, e), exc_info=True)
        finally:
            if exc and db_nslcmop:
                db_nslcmop_update["detailed-status"] = nslcmop_operation_state_detail = \
                    "FAILED {}: {}".format(step, exc)
                db_nslcmop_update["operationState"] = nslcmop_operation_state = "FAILED"
                db_nslcmop_update["statusEnteredTime"] = time()
            try:
                if db_nslcmop_update:
                    self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
                if db_nsr:
                    db_nsr_update["_admin.nslcmop"] = None
                    db_nsr_update["_admin.operation-type"] = None
                    db_nsr_update["_admin.nslcmop"] = None
                    db_nsr_update["_admin.current-operation"] = None
                    self.update_db_2("nsrs", nsr_id, db_nsr_update)
                    self._write_ns_status(
                        nsr_id=nsr_id,
                        ns_state=None,
                        current_operation="IDLE",
                        current_operation_id=None
                    )
            except DbException as e:
                self.logger.error(logging_text + "Cannot update database: {}".format(e))
            self.logger.debug(logging_text + "Exit")
            if nslcmop_operation_state:
                try:
                    await self.msg.aiowrite("ns", "actioned", {"nsr_id": nsr_id, "nslcmop_id": nslcmop_id,
                                                               "operationState": nslcmop_operation_state},
                                            loop=self.loop)
                except Exception as e:
                    self.logger.error(logging_text + "kafka_write notification Exception {}".format(e))
            self.logger.debug(logging_text + "Exit")
            self.lcm_tasks.remove("ns", nsr_id, nslcmop_id, "ns_action")
            return nslcmop_operation_state, nslcmop_operation_state_detail

    async def scale(self, nsr_id, nslcmop_id):

        # Try to lock HA task here
        task_is_locked_by_me = self.lcm_tasks.lock_HA('ns', 'nslcmops', nslcmop_id)
        if not task_is_locked_by_me:
            return

        logging_text = "Task ns={} scale={} ".format(nsr_id, nslcmop_id)
        self.logger.debug(logging_text + "Enter")
        # get all needed from database
        db_nsr = None
        db_nslcmop = None
        db_nslcmop_update = {}
        nslcmop_operation_state = None
        db_nsr_update = {"_admin.nslcmop": nslcmop_id,
                         "_admin.current-operation": nslcmop_id,
                         "_admin.operation-type": "scale"}
        self.update_db_2("nsrs", nsr_id, db_nsr_update)
        exc = None
        # in case of error, indicates what part of scale was failed to put nsr at error status
        scale_process = None
        old_operational_status = ""
        old_config_status = ""
        vnfr_scaled = False
        try:
            # wait for any previous tasks in process
            step = "Waiting for previous operations to terminate"
            await self.lcm_tasks.waitfor_related_HA('ns', 'nslcmops', nslcmop_id)

            self._write_ns_status(
                nsr_id=nsr_id,
                ns_state=None,
                current_operation="SCALING",
                current_operation_id=nslcmop_id
            )

            step = "Getting nslcmop from database"
            self.logger.debug(step + " after having waited for previous tasks to be completed")
            db_nslcmop = self.db.get_one("nslcmops", {"_id": nslcmop_id})
            step = "Getting nsr from database"
            db_nsr = self.db.get_one("nsrs", {"_id": nsr_id})

            old_operational_status = db_nsr["operational-status"]
            old_config_status = db_nsr["config-status"]
            step = "Parsing scaling parameters"
            # self.logger.debug(step)
            db_nsr_update["operational-status"] = "scaling"
            self.update_db_2("nsrs", nsr_id, db_nsr_update)
            nsr_deployed = db_nsr["_admin"].get("deployed")

            #######
            nsr_deployed = db_nsr["_admin"].get("deployed")
            vnf_index = db_nslcmop["operationParams"].get("member_vnf_index")
            # vdu_id = db_nslcmop["operationParams"].get("vdu_id")
            # vdu_count_index = db_nslcmop["operationParams"].get("vdu_count_index")
            # vdu_name = db_nslcmop["operationParams"].get("vdu_name")
            #######

            RO_nsr_id = nsr_deployed["RO"]["nsr_id"]
            vnf_index = db_nslcmop["operationParams"]["scaleVnfData"]["scaleByStepData"]["member-vnf-index"]
            scaling_group = db_nslcmop["operationParams"]["scaleVnfData"]["scaleByStepData"]["scaling-group-descriptor"]
            scaling_type = db_nslcmop["operationParams"]["scaleVnfData"]["scaleVnfType"]
            # scaling_policy = db_nslcmop["operationParams"]["scaleVnfData"]["scaleByStepData"].get("scaling-policy")

            # for backward compatibility
            if nsr_deployed and isinstance(nsr_deployed.get("VCA"), dict):
                nsr_deployed["VCA"] = list(nsr_deployed["VCA"].values())
                db_nsr_update["_admin.deployed.VCA"] = nsr_deployed["VCA"]
                self.update_db_2("nsrs", nsr_id, db_nsr_update)

            step = "Getting vnfr from database"
            db_vnfr = self.db.get_one("vnfrs", {"member-vnf-index-ref": vnf_index, "nsr-id-ref": nsr_id})
            step = "Getting vnfd from database"
            db_vnfd = self.db.get_one("vnfds", {"_id": db_vnfr["vnfd-id"]})

            step = "Getting scaling-group-descriptor"
            for scaling_descriptor in db_vnfd["scaling-group-descriptor"]:
                if scaling_descriptor["name"] == scaling_group:
                    break
            else:
                raise LcmException("input parameter 'scaleByStepData':'scaling-group-descriptor':'{}' is not present "
                                   "at vnfd:scaling-group-descriptor".format(scaling_group))

            # cooldown_time = 0
            # for scaling_policy_descriptor in scaling_descriptor.get("scaling-policy", ()):
            #     cooldown_time = scaling_policy_descriptor.get("cooldown-time", 0)
            #     if scaling_policy and scaling_policy == scaling_policy_descriptor.get("name"):
            #         break

            # TODO check if ns is in a proper status
            step = "Sending scale order to VIM"
            nb_scale_op = 0
            if not db_nsr["_admin"].get("scaling-group"):
                self.update_db_2("nsrs", nsr_id, {"_admin.scaling-group": [{"name": scaling_group, "nb-scale-op": 0}]})
                admin_scale_index = 0
            else:
                for admin_scale_index, admin_scale_info in enumerate(db_nsr["_admin"]["scaling-group"]):
                    if admin_scale_info["name"] == scaling_group:
                        nb_scale_op = admin_scale_info.get("nb-scale-op", 0)
                        break
                else:  # not found, set index one plus last element and add new entry with the name
                    admin_scale_index += 1
                    db_nsr_update["_admin.scaling-group.{}.name".format(admin_scale_index)] = scaling_group
            RO_scaling_info = []
            vdu_scaling_info = {"scaling_group_name": scaling_group, "vdu": []}
            if scaling_type == "SCALE_OUT":
                # count if max-instance-count is reached
                max_instance_count = scaling_descriptor.get("max-instance-count", 10)
                # self.logger.debug("MAX_INSTANCE_COUNT is {}".format(max_instance_count))
                if nb_scale_op >= max_instance_count:
                    raise LcmException("reached the limit of {} (max-instance-count) "
                                       "scaling-out operations for the "
                                       "scaling-group-descriptor '{}'".format(nb_scale_op, scaling_group))

                nb_scale_op += 1
                vdu_scaling_info["scaling_direction"] = "OUT"
                vdu_scaling_info["vdu-create"] = {}
                for vdu_scale_info in scaling_descriptor["vdu"]:
                    RO_scaling_info.append({"osm_vdu_id": vdu_scale_info["vdu-id-ref"], "member-vnf-index": vnf_index,
                                            "type": "create", "count": vdu_scale_info.get("count", 1)})
                    vdu_scaling_info["vdu-create"][vdu_scale_info["vdu-id-ref"]] = vdu_scale_info.get("count", 1)

            elif scaling_type == "SCALE_IN":
                # count if min-instance-count is reached
                min_instance_count = 0
                if "min-instance-count" in scaling_descriptor and scaling_descriptor["min-instance-count"] is not None:
                    min_instance_count = int(scaling_descriptor["min-instance-count"])
                if nb_scale_op <= min_instance_count:
                    raise LcmException("reached the limit of {} (min-instance-count) scaling-in operations for the "
                                       "scaling-group-descriptor '{}'".format(nb_scale_op, scaling_group))
                nb_scale_op -= 1
                vdu_scaling_info["scaling_direction"] = "IN"
                vdu_scaling_info["vdu-delete"] = {}
                for vdu_scale_info in scaling_descriptor["vdu"]:
                    RO_scaling_info.append({"osm_vdu_id": vdu_scale_info["vdu-id-ref"], "member-vnf-index": vnf_index,
                                            "type": "delete", "count": vdu_scale_info.get("count", 1)})
                    vdu_scaling_info["vdu-delete"][vdu_scale_info["vdu-id-ref"]] = vdu_scale_info.get("count", 1)

            # update VDU_SCALING_INFO with the VDUs to delete ip_addresses
            vdu_create = vdu_scaling_info.get("vdu-create")
            vdu_delete = copy(vdu_scaling_info.get("vdu-delete"))
            if vdu_scaling_info["scaling_direction"] == "IN":
                for vdur in reversed(db_vnfr["vdur"]):
                    if vdu_delete.get(vdur["vdu-id-ref"]):
                        vdu_delete[vdur["vdu-id-ref"]] -= 1
                        vdu_scaling_info["vdu"].append({
                            "name": vdur["name"],
                            "vdu_id": vdur["vdu-id-ref"],
                            "interface": []
                        })
                        for interface in vdur["interfaces"]:
                            vdu_scaling_info["vdu"][-1]["interface"].append({
                                "name": interface["name"],
                                "ip_address": interface["ip-address"],
                                "mac_address": interface.get("mac-address"),
                            })
                vdu_delete = vdu_scaling_info.pop("vdu-delete")

            # PRE-SCALE BEGIN
            step = "Executing pre-scale vnf-config-primitive"
            if scaling_descriptor.get("scaling-config-action"):
                for scaling_config_action in scaling_descriptor["scaling-config-action"]:
                    if (scaling_config_action.get("trigger") == "pre-scale-in" and scaling_type == "SCALE_IN") \
                       or (scaling_config_action.get("trigger") == "pre-scale-out" and scaling_type == "SCALE_OUT"):
                        vnf_config_primitive = scaling_config_action["vnf-config-primitive-name-ref"]
                        step = db_nslcmop_update["detailed-status"] = \
                            "executing pre-scale scaling-config-action '{}'".format(vnf_config_primitive)

                        # look for primitive
                        for config_primitive in db_vnfd.get("vnf-configuration", {}).get("config-primitive", ()):
                            if config_primitive["name"] == vnf_config_primitive:
                                break
                        else:
                            raise LcmException(
                                "Invalid vnfd descriptor at scaling-group-descriptor[name='{}']:scaling-config-action"
                                "[vnf-config-primitive-name-ref='{}'] does not match any vnf-configuration:config-"
                                "primitive".format(scaling_group, config_primitive))

                        vnfr_params = {"VDU_SCALE_INFO": vdu_scaling_info}
                        if db_vnfr.get("additionalParamsForVnf"):
                            vnfr_params.update(db_vnfr["additionalParamsForVnf"])

                        scale_process = "VCA"
                        db_nsr_update["config-status"] = "configuring pre-scaling"
                        primitive_params = self._map_primitive_params(config_primitive, {}, vnfr_params)

                        # Pre-scale reintent check: Check if this sub-operation has been executed before
                        op_index = self._check_or_add_scale_suboperation(
                            db_nslcmop, nslcmop_id, vnf_index, vnf_config_primitive, primitive_params, 'PRE-SCALE')
                        if (op_index == self.SUBOPERATION_STATUS_SKIP):
                            # Skip sub-operation
                            result = 'COMPLETED'
                            result_detail = 'Done'
                            self.logger.debug(logging_text +
                                              "vnf_config_primitive={} Skipped sub-operation, result {} {}".format(
                                                  vnf_config_primitive, result, result_detail))
                        else:
                            if (op_index == self.SUBOPERATION_STATUS_NEW):
                                # New sub-operation: Get index of this sub-operation
                                op_index = len(db_nslcmop.get('_admin', {}).get('operations')) - 1
                                self.logger.debug(logging_text + "vnf_config_primitive={} New sub-operation".
                                                  format(vnf_config_primitive))
                            else:
                                # Reintent:  Get registered params for this existing sub-operation
                                op = db_nslcmop.get('_admin', {}).get('operations', [])[op_index]
                                vnf_index = op.get('member_vnf_index')
                                vnf_config_primitive = op.get('primitive')
                                primitive_params = op.get('primitive_params')
                                self.logger.debug(logging_text + "vnf_config_primitive={} Sub-operation reintent".
                                                  format(vnf_config_primitive))
                            # Execute the primitive, either with new (first-time) or registered (reintent) args
                            result, result_detail = await self._ns_execute_primitive(
                                nsr_deployed, vnf_index, None, None, None, vnf_config_primitive, primitive_params)
                            self.logger.debug(logging_text + "vnf_config_primitive={} Done with result {} {}".format(
                                vnf_config_primitive, result, result_detail))
                            # Update operationState = COMPLETED | FAILED
                            self._update_suboperation_status(
                                db_nslcmop, op_index, result, result_detail)

                        if result == "FAILED":
                            raise LcmException(result_detail)
                        db_nsr_update["config-status"] = old_config_status
                        scale_process = None
            # PRE-SCALE END

            # SCALE RO - BEGIN
            # Should this block be skipped if 'RO_nsr_id' == None ?
            # if (RO_nsr_id and RO_scaling_info):
            if RO_scaling_info:
                scale_process = "RO"
                # Scale RO reintent check: Check if this sub-operation has been executed before
                op_index = self._check_or_add_scale_suboperation(
                    db_nslcmop, vnf_index, None, None, 'SCALE-RO', RO_nsr_id, RO_scaling_info)
                if (op_index == self.SUBOPERATION_STATUS_SKIP):
                    # Skip sub-operation
                    result = 'COMPLETED'
                    result_detail = 'Done'
                    self.logger.debug(logging_text + "Skipped sub-operation RO, result {} {}".format(
                        result, result_detail))
                else:
                    if (op_index == self.SUBOPERATION_STATUS_NEW):
                        # New sub-operation: Get index of this sub-operation
                        op_index = len(db_nslcmop.get('_admin', {}).get('operations')) - 1
                        self.logger.debug(logging_text + "New sub-operation RO")
                    else:
                        # Reintent:  Get registered params for this existing sub-operation
                        op = db_nslcmop.get('_admin', {}).get('operations', [])[op_index]
                        RO_nsr_id = op.get('RO_nsr_id')
                        RO_scaling_info = op.get('RO_scaling_info')
                        self.logger.debug(logging_text + "Sub-operation RO reintent".format(
                            vnf_config_primitive))

                    RO_desc = await self.RO.create_action("ns", RO_nsr_id, {"vdu-scaling": RO_scaling_info})
                    db_nsr_update["_admin.scaling-group.{}.nb-scale-op".format(admin_scale_index)] = nb_scale_op
                    db_nsr_update["_admin.scaling-group.{}.time".format(admin_scale_index)] = time()
                    # wait until ready
                    RO_nslcmop_id = RO_desc["instance_action_id"]
                    db_nslcmop_update["_admin.deploy.RO"] = RO_nslcmop_id

                    RO_task_done = False
                    step = detailed_status = "Waiting RO_task_id={} to complete the scale action.".format(RO_nslcmop_id)
                    detailed_status_old = None
                    self.logger.debug(logging_text + step)

                    deployment_timeout = 1 * 3600   # One hour
                    while deployment_timeout > 0:
                        if not RO_task_done:
                            desc = await self.RO.show("ns", item_id_name=RO_nsr_id, extra_item="action",
                                                      extra_item_id=RO_nslcmop_id)
                            ns_status, ns_status_info = self.RO.check_action_status(desc)
                            if ns_status == "ERROR":
                                raise ROclient.ROClientException(ns_status_info)
                            elif ns_status == "BUILD":
                                detailed_status = step + "; {}".format(ns_status_info)
                            elif ns_status == "ACTIVE":
                                RO_task_done = True
                                step = detailed_status = "Waiting ns ready at RO. RO_id={}".format(RO_nsr_id)
                                self.logger.debug(logging_text + step)
                            else:
                                assert False, "ROclient.check_action_status returns unknown {}".format(ns_status)
                        else:

                            if ns_status == "ERROR":
                                raise ROclient.ROClientException(ns_status_info)
                            elif ns_status == "BUILD":
                                detailed_status = step + "; {}".format(ns_status_info)
                            elif ns_status == "ACTIVE":
                                step = detailed_status = \
                                    "Waiting for management IP address reported by the VIM. Updating VNFRs"
                                if not vnfr_scaled:
                                    self.scale_vnfr(db_vnfr, vdu_create=vdu_create, vdu_delete=vdu_delete)
                                    vnfr_scaled = True
                                try:
                                    desc = await self.RO.show("ns", RO_nsr_id)
                                    # nsr_deployed["nsr_ip"] = RO.get_ns_vnf_info(desc)
                                    self.ns_update_vnfr({db_vnfr["member-vnf-index-ref"]: db_vnfr}, desc)
                                    break
                                except LcmExceptionNoMgmtIP:
                                    pass
                            else:
                                assert False, "ROclient.check_ns_status returns unknown {}".format(ns_status)
                        if detailed_status != detailed_status_old:
                            self._update_suboperation_status(
                                db_nslcmop, op_index, 'COMPLETED', detailed_status)
                            detailed_status_old = db_nslcmop_update["detailed-status"] = detailed_status
                            self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)

                        await asyncio.sleep(5, loop=self.loop)
                        deployment_timeout -= 5
                    if deployment_timeout <= 0:
                        self._update_suboperation_status(
                            db_nslcmop, nslcmop_id, op_index, 'FAILED', "Timeout when waiting for ns to get ready")
                        raise ROclient.ROClientException("Timeout waiting ns to be ready")

                    # update VDU_SCALING_INFO with the obtained ip_addresses
                    if vdu_scaling_info["scaling_direction"] == "OUT":
                        for vdur in reversed(db_vnfr["vdur"]):
                            if vdu_scaling_info["vdu-create"].get(vdur["vdu-id-ref"]):
                                vdu_scaling_info["vdu-create"][vdur["vdu-id-ref"]] -= 1
                                vdu_scaling_info["vdu"].append({
                                    "name": vdur["name"],
                                    "vdu_id": vdur["vdu-id-ref"],
                                    "interface": []
                                })
                                for interface in vdur["interfaces"]:
                                    vdu_scaling_info["vdu"][-1]["interface"].append({
                                        "name": interface["name"],
                                        "ip_address": interface["ip-address"],
                                        "mac_address": interface.get("mac-address"),
                                    })
                        del vdu_scaling_info["vdu-create"]

                    self._update_suboperation_status(db_nslcmop, op_index, 'COMPLETED', 'Done')
            # SCALE RO - END

            scale_process = None
            if db_nsr_update:
                self.update_db_2("nsrs", nsr_id, db_nsr_update)

            # POST-SCALE BEGIN
            # execute primitive service POST-SCALING
            step = "Executing post-scale vnf-config-primitive"
            if scaling_descriptor.get("scaling-config-action"):
                for scaling_config_action in scaling_descriptor["scaling-config-action"]:
                    if (scaling_config_action.get("trigger") == "post-scale-in" and scaling_type == "SCALE_IN") \
                       or (scaling_config_action.get("trigger") == "post-scale-out" and scaling_type == "SCALE_OUT"):
                        vnf_config_primitive = scaling_config_action["vnf-config-primitive-name-ref"]
                        step = db_nslcmop_update["detailed-status"] = \
                            "executing post-scale scaling-config-action '{}'".format(vnf_config_primitive)

                        vnfr_params = {"VDU_SCALE_INFO": vdu_scaling_info}
                        if db_vnfr.get("additionalParamsForVnf"):
                            vnfr_params.update(db_vnfr["additionalParamsForVnf"])

                        # look for primitive
                        for config_primitive in db_vnfd.get("vnf-configuration", {}).get("config-primitive", ()):
                            if config_primitive["name"] == vnf_config_primitive:
                                break
                        else:
                            raise LcmException("Invalid vnfd descriptor at scaling-group-descriptor[name='{}']:"
                                               "scaling-config-action[vnf-config-primitive-name-ref='{}'] does not "
                                               "match any vnf-configuration:config-primitive".format(scaling_group,
                                                                                                     config_primitive))
                        scale_process = "VCA"
                        db_nsr_update["config-status"] = "configuring post-scaling"
                        primitive_params = self._map_primitive_params(config_primitive, {}, vnfr_params)

                        # Post-scale reintent check: Check if this sub-operation has been executed before
                        op_index = self._check_or_add_scale_suboperation(
                            db_nslcmop, nslcmop_id, vnf_index, vnf_config_primitive, primitive_params, 'POST-SCALE')
                        if op_index == self.SUBOPERATION_STATUS_SKIP:
                            # Skip sub-operation
                            result = 'COMPLETED'
                            result_detail = 'Done'
                            self.logger.debug(logging_text +
                                              "vnf_config_primitive={} Skipped sub-operation, result {} {}".
                                              format(vnf_config_primitive, result, result_detail))
                        else:
                            if op_index == self.SUBOPERATION_STATUS_NEW:
                                # New sub-operation: Get index of this sub-operation
                                op_index = len(db_nslcmop.get('_admin', {}).get('operations')) - 1
                                self.logger.debug(logging_text + "vnf_config_primitive={} New sub-operation".
                                                  format(vnf_config_primitive))
                            else:
                                # Reintent:  Get registered params for this existing sub-operation
                                op = db_nslcmop.get('_admin', {}).get('operations', [])[op_index]
                                vnf_index = op.get('member_vnf_index')
                                vnf_config_primitive = op.get('primitive')
                                primitive_params = op.get('primitive_params')
                                self.logger.debug(logging_text + "vnf_config_primitive={} Sub-operation reintent".
                                                  format(vnf_config_primitive))
                            # Execute the primitive, either with new (first-time) or registered (reintent) args
                            result, result_detail = await self._ns_execute_primitive(
                                nsr_deployed, vnf_index, None, None, None, vnf_config_primitive, primitive_params)
                            self.logger.debug(logging_text + "vnf_config_primitive={} Done with result {} {}".format(
                                vnf_config_primitive, result, result_detail))
                            # Update operationState = COMPLETED | FAILED
                            self._update_suboperation_status(
                                db_nslcmop, op_index, result, result_detail)

                        if result == "FAILED":
                            raise LcmException(result_detail)
                        db_nsr_update["config-status"] = old_config_status
                        scale_process = None
            # POST-SCALE END

            db_nslcmop_update["operationState"] = nslcmop_operation_state = "COMPLETED"
            db_nslcmop_update["statusEnteredTime"] = time()
            db_nslcmop_update["detailed-status"] = "done"
            db_nsr_update["detailed-status"] = ""  # "scaled {} {}".format(scaling_group, scaling_type)
            db_nsr_update["operational-status"] = "running" if old_operational_status == "failed" \
                else old_operational_status
            db_nsr_update["config-status"] = old_config_status
            return
        except (ROclient.ROClientException, DbException, LcmException) as e:
            self.logger.error(logging_text + "Exit Exception {}".format(e))
            exc = e
        except asyncio.CancelledError:
            self.logger.error(logging_text + "Cancelled Exception while '{}'".format(step))
            exc = "Operation was cancelled"
        except Exception as e:
            exc = traceback.format_exc()
            self.logger.critical(logging_text + "Exit Exception {} {}".format(type(e).__name__, e), exc_info=True)
        finally:
            if exc:
                if db_nslcmop:
                    db_nslcmop_update["detailed-status"] = "FAILED {}: {}".format(step, exc)
                    db_nslcmop_update["operationState"] = nslcmop_operation_state = "FAILED"
                    db_nslcmop_update["statusEnteredTime"] = time()
                if db_nsr:
                    db_nsr_update["operational-status"] = old_operational_status
                    db_nsr_update["config-status"] = old_config_status
                    db_nsr_update["detailed-status"] = ""
                    db_nsr_update["_admin.nslcmop"] = None
                    if scale_process:
                        if "VCA" in scale_process:
                            db_nsr_update["config-status"] = "failed"
                        if "RO" in scale_process:
                            db_nsr_update["operational-status"] = "failed"
                        db_nsr_update["detailed-status"] = "FAILED scaling nslcmop={} {}: {}".format(nslcmop_id, step,
                                                                                                     exc)
            try:
                if db_nslcmop and db_nslcmop_update:
                    self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
                if db_nsr:
                    db_nsr_update["_admin.current-operation"] = None
                    db_nsr_update["_admin.operation-type"] = None
                    db_nsr_update["_admin.nslcmop"] = None
                    self.update_db_2("nsrs", nsr_id, db_nsr_update)

                    self._write_ns_status(
                        nsr_id=nsr_id,
                        ns_state=None,
                        current_operation="IDLE",
                        current_operation_id=None
                    )

            except DbException as e:
                self.logger.error(logging_text + "Cannot update database: {}".format(e))
            if nslcmop_operation_state:
                try:
                    await self.msg.aiowrite("ns", "scaled", {"nsr_id": nsr_id, "nslcmop_id": nslcmop_id,
                                                             "operationState": nslcmop_operation_state},
                                            loop=self.loop)
                    # if cooldown_time:
                    #     await asyncio.sleep(cooldown_time, loop=self.loop)
                    # await self.msg.aiowrite("ns","scaled-cooldown-time", {"nsr_id": nsr_id, "nslcmop_id": nslcmop_id})
                except Exception as e:
                    self.logger.error(logging_text + "kafka_write notification Exception {}".format(e))
            self.logger.debug(logging_text + "Exit")
            self.lcm_tasks.remove("ns", nsr_id, nslcmop_id, "ns_scale")
