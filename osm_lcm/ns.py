#!/usr/bin/python3
# -*- coding: utf-8 -*-

import asyncio
import yaml
import logging
import logging.handlers
import functools
import traceback

import ROclient
from lcm_utils import LcmException, LcmBase

from osm_common.dbbase import DbException, deep_update
from osm_common.fsbase import FsException
from n2vc.vnf import N2VC

from copy import deepcopy
from http import HTTPStatus
from time import time


__author__ = "Alfonso Tierno"


class NsLcm(LcmBase):

    def __init__(self, db, msg, fs, lcm_tasks, ro_config, vca_config, loop):
        """
        Init, Connect to database, filesystem storage, and messaging
        :param config: two level dictionary with configuration. Top level should contain 'database', 'storage',
        :return: None
        """
        # logging
        self.logger = logging.getLogger('lcm.ns')
        self.loop = loop
        self.lcm_tasks = lcm_tasks

        super().__init__(db, msg, fs, self.logger)

        self.ro_config = ro_config

        self.n2vc = N2VC(
            log=self.logger,
            server=vca_config['host'],
            port=vca_config['port'],
            user=vca_config['user'],
            secret=vca_config['secret'],
            # TODO: This should point to the base folder where charms are stored,
            # if there is a common one (like object storage). Otherwise, leave
            # it unset and pass it via DeployCharms
            # artifacts=vca_config[''],
            artifacts=None,
        )

    def vnfd2RO(self, vnfd, new_id=None):
        """
        Converts creates a new vnfd descriptor for RO base on input OSM IM vnfd
        :param vnfd: input vnfd
        :param new_id: overrides vnf id if provided
        :return: copy of vnfd
        """
        ci_file = None
        try:
            vnfd_RO = deepcopy(vnfd)
            vnfd_RO.pop("_id", None)
            vnfd_RO.pop("_admin", None)
            if new_id:
                vnfd_RO["id"] = new_id
            for vdu in vnfd_RO["vdu"]:
                if "cloud-init-file" in vdu:
                    base_folder = vnfd["_admin"]["storage"]
                    clout_init_file = "{}/{}/cloud_init/{}".format(
                        base_folder["folder"],
                        base_folder["pkg-dir"],
                        vdu["cloud-init-file"]
                    )
                    ci_file = self.fs.file_open(clout_init_file, "r")
                    # TODO: detect if binary or text. Propose to read as binary and try to decode to utf8. If fails
                    #  convert to base 64 or similar
                    clout_init_content = ci_file.read()
                    ci_file.close()
                    ci_file = None
                    vdu.pop("cloud-init-file", None)
                    vdu["cloud-init"] = clout_init_content
            # remnove unused by RO configuration, monitoring, scaling
            vnfd_RO.pop("vnf-configuration", None)
            vnfd_RO.pop("monitoring-param", None)
            vnfd_RO.pop("scaling-group-descriptor", None)
            return vnfd_RO
        except FsException as e:
            raise LcmException("Error reading file at vnfd {}: {} ".format(vnfd["_id"], e))
        finally:
            if ci_file:
                ci_file.close()

    def n2vc_callback(self, model_name, application_name, status, message, n2vc_info, task=None):
        """
        Callback both for charm status change and task completion
        :param model_name: Charm model name
        :param application_name: Charm application name
        :param status: Can be
            - blocked: The unit needs manual intervention
            - maintenance: The unit is actively deploying/configuring
            - waiting: The unit is waiting for another charm to be ready
            - active: The unit is deployed, configured, and ready
            - error: The charm has failed and needs attention.
            - terminated: The charm has been destroyed
            - removing,
            - removed
        :param message: detailed message error
        :param n2vc_info dictionary with information shared with instantiate task. Contains:
            nsr_id:
            nslcmop_id:
            lcmOperationType: currently "instantiate"
            deployed: dictionary with {<application>: {operational-status: <status>, detailed-status: <text>}}
            db_update: dictionary to be filled with the changes to be wrote to database with format key.key.key: value
            n2vc_event: event used to notify instantiation task that some change has been produced
        :param task: None for charm status change, or task for completion task callback
        :return:
        """
        try:
            nsr_id = n2vc_info["nsr_id"]
            deployed = n2vc_info["deployed"]
            db_nsr_update = n2vc_info["db_update"]
            nslcmop_id = n2vc_info["nslcmop_id"]
            ns_operation = n2vc_info["lcmOperationType"]
            n2vc_event = n2vc_info["n2vc_event"]
            logging_text = "Task ns={} {}={} [n2vc_callback] application={}".format(nsr_id, ns_operation, nslcmop_id,
                                                                                    application_name)
            vca_deployed = deployed.get(application_name)
            if not vca_deployed:
                self.logger.error(logging_text + " Not present at nsr._admin.deployed.VCA")
                return

            if task:
                if task.cancelled():
                    self.logger.debug(logging_text + " task Cancelled")
                    vca_deployed['operational-status'] = "error"
                    db_nsr_update["_admin.deployed.VCA.{}.operational-status".format(application_name)] = "error"
                    vca_deployed['detailed-status'] = "Task Cancelled"
                    db_nsr_update["_admin.deployed.VCA.{}.detailed-status".format(application_name)] = "Task Cancelled"

                elif task.done():
                    exc = task.exception()
                    if exc:
                        self.logger.error(logging_text + " task Exception={}".format(exc))
                        vca_deployed['operational-status'] = "error"
                        db_nsr_update["_admin.deployed.VCA.{}.operational-status".format(application_name)] = "error"
                        vca_deployed['detailed-status'] = str(exc)
                        db_nsr_update["_admin.deployed.VCA.{}.detailed-status".format(application_name)] = str(exc)
                    else:
                        self.logger.debug(logging_text + " task Done")
                        # task is Done, but callback is still ongoing. So ignore
                        return
            elif status:
                self.logger.debug(logging_text + " Enter status={}".format(status))
                if vca_deployed['operational-status'] == status:
                    return  # same status, ignore
                vca_deployed['operational-status'] = status
                db_nsr_update["_admin.deployed.VCA.{}.operational-status".format(application_name)] = status
                vca_deployed['detailed-status'] = str(message)
                db_nsr_update["_admin.deployed.VCA.{}.detailed-status".format(application_name)] = str(message)
            else:
                self.logger.critical(logging_text + " Enter with bad parameters", exc_info=True)
                return
            # wake up instantiate task
            n2vc_event.set()
        except Exception as e:
            self.logger.critical(logging_text + " Exception {}".format(e), exc_info=True)

    def ns_params_2_RO(self, ns_params, nsd, vnfd_dict):
        """
        Creates a RO ns descriptor from OSM ns_instantite params
        :param ns_params: OSM instantiate params
        :return: The RO ns descriptor
        """
        vim_2_RO = {}

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
            # "scenario": ns_params["nsdId"],
            "vnfs": {},
            "networks": {},
        }
        if ns_params.get("ssh-authorized-key"):
            RO_ns_params["cloud-config"] = {"key-pairs": ns_params["ssh-authorized-key"]}
        if ns_params.get("vnf"):
            for vnf_params in ns_params["vnf"]:
                for constituent_vnfd in nsd["constituent-vnfd"]:
                    if constituent_vnfd["member-vnf-index"] == vnf_params["member-vnf-index"]:
                        vnf_descriptor = vnfd_dict[constituent_vnfd["vnfd-id-ref"]]
                        break
                else:
                    raise LcmException("Invalid instantiate parameter vnf:member-vnf-index={} is not present at nsd:"
                                       "constituent-vnfd".format(vnf_params["member-vnf-index"]))
                RO_vnf = {"vdus": {}, "networks": {}}
                if vnf_params.get("vimAccountId"):
                    RO_vnf["datacenter"] = vim_account_2_RO(vnf_params["vimAccountId"])
                if vnf_params.get("vdu"):
                    for vdu_params in vnf_params["vdu"]:
                        RO_vnf["vdus"][vdu_params["id"]] = {}
                        if vdu_params.get("volume"):
                            RO_vnf["vdus"][vdu_params["id"]]["devices"] = {}
                            for volume_params in vdu_params["volume"]:
                                RO_vnf["vdus"][vdu_params["id"]]["devices"][volume_params["name"]] = {}
                                if volume_params.get("vim-volume-id"):
                                    RO_vnf["vdus"][vdu_params["id"]]["devices"][volume_params["name"]]["vim_id"] = \
                                        volume_params["vim-volume-id"]
                        if vdu_params.get("interface"):
                            RO_vnf["vdus"][vdu_params["id"]]["interfaces"] = {}
                            for interface_params in vdu_params["interface"]:
                                RO_interface = {}
                                RO_vnf["vdus"][vdu_params["id"]]["interfaces"][interface_params["name"]] = RO_interface
                                if interface_params.get("ip-address"):
                                    RO_interface["ip_address"] = interface_params["ip-address"]
                                if interface_params.get("mac-address"):
                                    RO_interface["mac_address"] = interface_params["mac-address"]
                                if interface_params.get("floating-ip-required"):
                                    RO_interface["floating-ip"] = interface_params["floating-ip-required"]
                if vnf_params.get("internal-vld"):
                    for internal_vld_params in vnf_params["internal-vld"]:
                        RO_vnf["networks"][internal_vld_params["name"]] = {}
                        if internal_vld_params.get("vim-network-name"):
                            RO_vnf["networks"][internal_vld_params["name"]]["vim-network-name"] = \
                                internal_vld_params["vim-network-name"]
                        if internal_vld_params.get("ip-profile"):
                            RO_vnf["networks"][internal_vld_params["name"]]["ip-profile"] = \
                                ip_profile_2_RO(internal_vld_params["ip-profile"])
                        if internal_vld_params.get("internal-connection-point"):
                            for icp_params in internal_vld_params["internal-connection-point"]:
                                # look for interface
                                iface_found = False
                                for vdu_descriptor in vnf_descriptor["vdu"]:
                                    for vdu_interface in vdu_descriptor["interface"]:
                                        if vdu_interface.get("internal-connection-point-ref") == icp_params["id-ref"]:
                                            RO_interface_update = {}
                                            if icp_params.get("ip-address"):
                                                RO_interface_update["ip_address"] = icp_params["ip-address"]
                                            if icp_params.get("mac-address"):
                                                RO_interface_update["mac_address"] = icp_params["mac-address"]
                                            if RO_interface_update:
                                                RO_vnf_update = {"vdus": {vdu_descriptor["id"]: {
                                                    "interfaces": {vdu_interface["name"]: RO_interface_update}}}}
                                                deep_update(RO_vnf, RO_vnf_update)
                                            iface_found = True
                                            break
                                    if iface_found:
                                        break
                                else:
                                    raise LcmException("Invalid instantiate parameter vnf:member-vnf-index[{}]:"
                                                       "internal-vld:id-ref={} is not present at vnfd:internal-"
                                                       "connection-point".format(vnf_params["member-vnf-index"],
                                                                                 icp_params["id-ref"]))

                if not RO_vnf["vdus"]:
                    del RO_vnf["vdus"]
                if not RO_vnf["networks"]:
                    del RO_vnf["networks"]
                if RO_vnf:
                    RO_ns_params["vnfs"][vnf_params["member-vnf-index"]] = RO_vnf
        if ns_params.get("vld"):
            for vld_params in ns_params["vld"]:
                RO_vld = {}
                if "ip-profile" in vld_params:
                    RO_vld["ip-profile"] = ip_profile_2_RO(vld_params["ip-profile"])
                if "vim-network-name" in vld_params:
                    RO_vld["sites"] = []
                    if isinstance(vld_params["vim-network-name"], dict):
                        for vim_account, vim_net in vld_params["vim-network-name"].items():
                            RO_vld["sites"].append({
                                "netmap-use": vim_net,
                                "datacenter": vim_account_2_RO(vim_account)
                            })
                    else:  # isinstance str
                        RO_vld["sites"].append({"netmap-use": vld_params["vim-network-name"]})
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
                        RO_cp_params = {}
                        if cp_params.get("ip-address"):
                            RO_cp_params["ip_address"] = cp_params["ip-address"]
                        if cp_params.get("mac-address"):
                            RO_cp_params["mac_address"] = cp_params["mac-address"]
                        if RO_cp_params:
                            RO_vnf_params = {
                                cp_params["member-vnf-index-ref"]: {
                                    "vdus": {
                                        vdu_descriptor["id"]: {
                                            "interfaces": {
                                                interface_descriptor["name"]: RO_cp_params
                                            }
                                        }
                                    }
                                }
                            }
                            deep_update(RO_ns_params["vnfs"], RO_vnf_params)
                if RO_vld:
                    RO_ns_params["networks"][vld_params["name"]] = RO_vld
        return RO_ns_params

    def ns_update_vnfr(self, db_vnfrs, nsr_desc_RO):
        """
        Updates database vnfr with the RO info, e.g. ip_address, vim_id... Descriptor db_vnfrs is also updated
        :param db_vnfrs:
        :param nsr_desc_RO:
        :return:
        """
        for vnf_index, db_vnfr in db_vnfrs.items():
            for vnf_RO in nsr_desc_RO["vnfs"]:
                if vnf_RO["member_vnf_index"] == vnf_index:
                    vnfr_update = {}
                    db_vnfr["ip-address"] = vnfr_update["ip-address"] = vnf_RO.get("ip_address")
                    vdur_list = []
                    for vdur_RO in vnf_RO.get("vms", ()):
                        vdur = {
                            "vim-id": vdur_RO.get("vim_vm_id"),
                            "ip-address": vdur_RO.get("ip_address"),
                            "vdu-id-ref": vdur_RO.get("vdu_osm_id"),
                            "name": vdur_RO.get("vim_name"),
                            "status": vdur_RO.get("status"),
                            "status-detailed": vdur_RO.get("error_msg"),
                            "interfaces": []
                        }

                        for interface_RO in vdur_RO.get("interfaces", ()):
                            vdur["interfaces"].append({
                                "ip-address": interface_RO.get("ip_address"),
                                "mac-address": interface_RO.get("mac_address"),
                                "name": interface_RO.get("internal_name"),
                            })
                        vdur_list.append(vdur)
                    db_vnfr["vdur"] = vnfr_update["vdur"] = vdur_list
                    self.update_db_2("vnfrs", db_vnfr["_id"], vnfr_update)
                    break

            else:
                raise LcmException("ns_update_vnfr: Not found member_vnf_index={} at RO info".format(vnf_index))

    async def create_monitoring(self, nsr_id, vnf_member_index, vnfd_desc):
        if not vnfd_desc.get("scaling-group-descriptor"):
            return
        for scaling_group in vnfd_desc["scaling-group-descriptor"]:
            scaling_policy_desc = {}
            scaling_desc = {
                "ns_id": nsr_id,
                "scaling_group_descriptor": {
                    "name": scaling_group["name"],
                    "scaling_policy": scaling_policy_desc
                }
            }
            for scaling_policy in scaling_group.get("scaling-policy"):
                scaling_policy_desc["scale_in_operation_type"] = scaling_policy_desc["scale_out_operation_type"] = \
                    scaling_policy["scaling-type"]
                scaling_policy_desc["threshold_time"] = scaling_policy["threshold-time"]
                scaling_policy_desc["cooldown_time"] = scaling_policy["cooldown-time"]
                scaling_policy_desc["scaling_criteria"] = []
                for scaling_criteria in scaling_policy.get("scaling-criteria"):
                    scaling_criteria_desc = {"scale_in_threshold": scaling_criteria.get("scale-in-threshold"),
                                             "scale_out_threshold": scaling_criteria.get("scale-out-threshold"),
                                             }
                    if not scaling_criteria.get("vnf-monitoring-param-ref"):
                        continue
                    for monitoring_param in vnfd_desc.get("monitoring-param", ()):
                        if monitoring_param["id"] == scaling_criteria["vnf-monitoring-param-ref"]:
                            scaling_criteria_desc["monitoring_param"] = {
                                "id": monitoring_param["id"],
                                "name": monitoring_param["name"],
                                "aggregation_type": monitoring_param.get("aggregation-type"),
                                "vdu_name": monitoring_param.get("vdu-ref"),
                                "vnf_member_index": vnf_member_index,
                            }

                            scaling_policy_desc["scaling_criteria"].append(scaling_criteria_desc)
                            break
                    else:
                        self.logger.error(
                            "Task ns={} member_vnf_index={} Invalid vnfd vnf-monitoring-param-ref={} not in "
                            "monitoring-param list".format(nsr_id, vnf_member_index,
                                                           scaling_criteria["vnf-monitoring-param-ref"]))

            await self.msg.aiowrite("lcm_pm", "configure_scaling", scaling_desc, self.loop)

    async def instantiate(self, nsr_id, nslcmop_id):
        logging_text = "Task ns={} instantiate={} ".format(nsr_id, nslcmop_id)
        self.logger.debug(logging_text + "Enter")
        # get all needed from database
        db_nsr = None
        db_nslcmop = None
        db_nsr_update = {}
        db_nslcmop_update = {}
        nslcmop_operation_state = None
        db_vnfrs = {}
        RO_descriptor_number = 0   # number of descriptors created at RO
        descriptor_id_2_RO = {}    # map between vnfd/nsd id to the id used at RO
        n2vc_info = {}
        exc = None
        try:
            step = "Getting nslcmop={} from db".format(nslcmop_id)
            db_nslcmop = self.db.get_one("nslcmops", {"_id": nslcmop_id})
            step = "Getting nsr={} from db".format(nsr_id)
            db_nsr = self.db.get_one("nsrs", {"_id": nsr_id})
            ns_params = db_nsr.get("instantiate_params")
            nsd = db_nsr["nsd"]
            nsr_name = db_nsr["name"]   # TODO short-name??
            needed_vnfd = {}
            vnfr_filter = {"nsr-id-ref": nsr_id, "member-vnf-index-ref": None}
            for c_vnf in nsd["constituent-vnfd"]:
                vnfd_id = c_vnf["vnfd-id-ref"]
                vnfr_filter["member-vnf-index-ref"] = c_vnf["member-vnf-index"]
                step = "Getting vnfr={} of nsr={} from db".format(c_vnf["member-vnf-index"], nsr_id)
                db_vnfrs[c_vnf["member-vnf-index"]] = self.db.get_one("vnfrs", vnfr_filter)
                if vnfd_id not in needed_vnfd:
                    step = "Getting vnfd={} from db".format(vnfd_id)
                    needed_vnfd[vnfd_id] = self.db.get_one("vnfds", {"id": vnfd_id})

            nsr_lcm = db_nsr["_admin"].get("deployed")
            if not nsr_lcm:
                nsr_lcm = db_nsr["_admin"]["deployed"] = {
                    "id": nsr_id,
                    "RO": {"vnfd_id": {}, "nsd_id": None, "nsr_id": None, "nsr_status": "SCHEDULED"},
                    "nsr_ip": {},
                    "VCA": {},
                }
            db_nsr_update["detailed-status"] = "creating"
            db_nsr_update["operational-status"] = "init"

            RO = ROclient.ROClient(self.loop, **self.ro_config)

            # get vnfds, instantiate at RO
            for vnfd_id, vnfd in needed_vnfd.items():
                step = db_nsr_update["detailed-status"] = "Creating vnfd={} at RO".format(vnfd_id)
                # self.logger.debug(logging_text + step)
                vnfd_id_RO = "{}.{}.{}".format(nsr_id, RO_descriptor_number, vnfd_id[:23])
                descriptor_id_2_RO[vnfd_id] = vnfd_id_RO
                RO_descriptor_number += 1

                # look if present
                vnfd_list = await RO.get_list("vnfd", filter_by={"osm_id": vnfd_id_RO})
                if vnfd_list:
                    db_nsr_update["_admin.deployed.RO.vnfd_id.{}".format(vnfd_id)] = vnfd_list[0]["uuid"]
                    self.logger.debug(logging_text + "vnfd={} exists at RO. Using RO_id={}".format(
                        vnfd_id, vnfd_list[0]["uuid"]))
                else:
                    vnfd_RO = self.vnfd2RO(vnfd, vnfd_id_RO)
                    desc = await RO.create("vnfd", descriptor=vnfd_RO)
                    db_nsr_update["_admin.deployed.RO.vnfd_id.{}".format(vnfd_id)] = desc["uuid"]
                    db_nsr_update["_admin.nsState"] = "INSTANTIATED"
                    self.logger.debug(logging_text + "vnfd={} created at RO. RO_id={}".format(
                        vnfd_id, desc["uuid"]))
                self.update_db_2("nsrs", nsr_id, db_nsr_update)

            # create nsd at RO
            nsd_id = nsd["id"]
            step = db_nsr_update["detailed-status"] = "Creating nsd={} at RO".format(nsd_id)
            # self.logger.debug(logging_text + step)

            RO_osm_nsd_id = "{}.{}.{}".format(nsr_id, RO_descriptor_number, nsd_id[:23])
            descriptor_id_2_RO[nsd_id] = RO_osm_nsd_id
            RO_descriptor_number += 1
            nsd_list = await RO.get_list("nsd", filter_by={"osm_id": RO_osm_nsd_id})
            if nsd_list:
                db_nsr_update["_admin.deployed.RO.nsd_id"] = RO_nsd_uuid = nsd_list[0]["uuid"]
                self.logger.debug(logging_text + "nsd={} exists at RO. Using RO_id={}".format(
                    nsd_id, RO_nsd_uuid))
            else:
                nsd_RO = deepcopy(nsd)
                nsd_RO["id"] = RO_osm_nsd_id
                nsd_RO.pop("_id", None)
                nsd_RO.pop("_admin", None)
                for c_vnf in nsd_RO["constituent-vnfd"]:
                    vnfd_id = c_vnf["vnfd-id-ref"]
                    c_vnf["vnfd-id-ref"] = descriptor_id_2_RO[vnfd_id]
                desc = await RO.create("nsd", descriptor=nsd_RO)
                db_nsr_update["_admin.nsState"] = "INSTANTIATED"
                db_nsr_update["_admin.deployed.RO.nsd_id"] = RO_nsd_uuid = desc["uuid"]
                self.logger.debug(logging_text + "nsd={} created at RO. RO_id={}".format(nsd_id, RO_nsd_uuid))
            self.update_db_2("nsrs", nsr_id, db_nsr_update)

            # Crate ns at RO
            # if present use it unless in error status
            RO_nsr_id = db_nsr["_admin"].get("deployed", {}).get("RO", {}).get("nsr_id")
            if RO_nsr_id:
                try:
                    step = db_nsr_update["detailed-status"] = "Looking for existing ns at RO"
                    # self.logger.debug(logging_text + step + " RO_ns_id={}".format(RO_nsr_id))
                    desc = await RO.show("ns", RO_nsr_id)
                except ROclient.ROClientException as e:
                    if e.http_code != HTTPStatus.NOT_FOUND:
                        raise
                    RO_nsr_id = db_nsr_update["_admin.deployed.RO.nsr_id"] = None
                if RO_nsr_id:
                    ns_status, ns_status_info = RO.check_ns_status(desc)
                    db_nsr_update["_admin.deployed.RO.nsr_status"] = ns_status
                    if ns_status == "ERROR":
                        step = db_nsr_update["detailed-status"] = "Deleting ns at RO. RO_ns_id={}".format(RO_nsr_id)
                        self.logger.debug(logging_text + step)
                        await RO.delete("ns", RO_nsr_id)
                        RO_nsr_id = db_nsr_update["_admin.deployed.RO.nsr_id"] = None
            if not RO_nsr_id:
                step = db_nsr_update["detailed-status"] = "Checking dependencies"
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

                step = db_nsr_update["detailed-status"] = "Checking instantiation parameters"
                RO_ns_params = self.ns_params_2_RO(ns_params, nsd, needed_vnfd)
                step = db_nsr_update["detailed-status"] = "Creating ns at RO"
                desc = await RO.create("ns", descriptor=RO_ns_params,
                                       name=db_nsr["name"],
                                       scenario=RO_nsd_uuid)
                RO_nsr_id = db_nsr_update["_admin.deployed.RO.nsr_id"] = desc["uuid"]
                db_nsr_update["_admin.nsState"] = "INSTANTIATED"
                db_nsr_update["_admin.deployed.RO.nsr_status"] = "BUILD"
                self.logger.debug(logging_text + "ns created at RO. RO_id={}".format(desc["uuid"]))
            self.update_db_2("nsrs", nsr_id, db_nsr_update)

            # update VNFR vimAccount
            step = "Updating VNFR vimAcccount"
            for vnf_index, vnfr in db_vnfrs.items():
                if vnfr.get("vim-account-id"):
                    continue
                vnfr_update = {"vim-account-id": db_nsr["instantiate_params"]["vimAccountId"]}
                if db_nsr["instantiate_params"].get("vnf"):
                    for vnf_params in db_nsr["instantiate_params"]["vnf"]:
                        if vnf_params.get("member-vnf-index") == vnf_index:
                            if vnf_params.get("vimAccountId"):
                                vnfr_update["vim-account-id"] = vnf_params.get("vimAccountId")
                            break
                self.update_db_2("vnfrs", vnfr["_id"], vnfr_update)

            # wait until NS is ready
            step = ns_status_detailed = detailed_status = "Waiting ns ready at RO. RO_id={}".format(RO_nsr_id)
            detailed_status_old = None
            self.logger.debug(logging_text + step)

            deployment_timeout = 2 * 3600   # Two hours
            while deployment_timeout > 0:
                desc = await RO.show("ns", RO_nsr_id)
                ns_status, ns_status_info = RO.check_ns_status(desc)
                db_nsr_update["admin.deployed.RO.nsr_status"] = ns_status
                if ns_status == "ERROR":
                    raise ROclient.ROClientException(ns_status_info)
                elif ns_status == "BUILD":
                    detailed_status = ns_status_detailed + "; {}".format(ns_status_info)
                elif ns_status == "ACTIVE":
                    step = detailed_status = "Waiting for management IP address reported by the VIM"
                    try:
                        nsr_lcm["nsr_ip"] = RO.get_ns_vnf_info(desc)
                        break
                    except ROclient.ROClientException as e:
                        if e.http_code != 409:  # IP address is not ready return code is 409 CONFLICT
                            raise e
                else:
                    assert False, "ROclient.check_ns_status returns unknown {}".format(ns_status)
                if detailed_status != detailed_status_old:
                    detailed_status_old = db_nsr_update["detailed-status"] = detailed_status
                    self.update_db_2("nsrs", nsr_id, db_nsr_update)
                await asyncio.sleep(5, loop=self.loop)
                deployment_timeout -= 5
            if deployment_timeout <= 0:
                raise ROclient.ROClientException("Timeout waiting ns to be ready")

            step = "Updating VNFRs"
            self.ns_update_vnfr(db_vnfrs, desc)

            db_nsr["detailed-status"] = "Configuring vnfr"
            self.update_db_2("nsrs", nsr_id, db_nsr_update)

            # The parameters we'll need to deploy a charm
            number_to_configure = 0

            def deploy(vnf_index, vdu_id, mgmt_ip_address, n2vc_info, config_primitive=None):
                """An inner function to deploy the charm from either vnf or vdu
                vnf_index is mandatory. vdu_id can be None for a vnf configuration or the id for vdu configuration
                """
                if not mgmt_ip_address:
                    raise LcmException("vnfd/vdu has not management ip address to configure it")
                # Login to the VCA.
                # if number_to_configure == 0:
                #     self.logger.debug("Logging into N2VC...")
                #     task = asyncio.ensure_future(self.n2vc.login())
                #     yield from asyncio.wait_for(task, 30.0)
                #     self.logger.debug("Logged into N2VC!")

                # # await self.n2vc.login()

                # Note: The charm needs to exist on disk at the location
                # specified by charm_path.
                base_folder = vnfd["_admin"]["storage"]
                storage_params = self.fs.get_params()
                charm_path = "{}{}/{}/charms/{}".format(
                    storage_params["path"],
                    base_folder["folder"],
                    base_folder["pkg-dir"],
                    proxy_charm
                )

                # Setup the runtime parameters for this VNF
                params = {'rw_mgmt_ip': mgmt_ip_address}
                if config_primitive:
                    params["initial-config-primitive"] = config_primitive

                # ns_name will be ignored in the current version of N2VC
                # but will be implemented for the next point release.
                model_name = 'default'
                vdu_id_text = "vnfd"
                if vdu_id:
                    vdu_id_text = vdu_id
                application_name = self.n2vc.FormatApplicationName(
                    nsr_name,
                    vnf_index,
                    vdu_id_text
                )
                if not nsr_lcm.get("VCA"):
                    nsr_lcm["VCA"] = {}
                nsr_lcm["VCA"][application_name] = db_nsr_update["_admin.deployed.VCA.{}".format(application_name)] = {
                    "member-vnf-index": vnf_index,
                    "vdu_id": vdu_id,
                    "model": model_name,
                    "application": application_name,
                    "operational-status": "init",
                    "detailed-status": "",
                    "vnfd_id": vnfd_id,
                }
                self.update_db_2("nsrs", nsr_id, db_nsr_update)

                self.logger.debug("Task create_ns={} Passing artifacts path '{}' for {}".format(nsr_id, charm_path,
                                                                                                proxy_charm))
                if not n2vc_info:
                    n2vc_info["nsr_id"] = nsr_id
                    n2vc_info["nslcmop_id"] = nslcmop_id
                    n2vc_info["n2vc_event"] = asyncio.Event(loop=self.loop)
                    n2vc_info["lcmOperationType"] = "instantiate"
                    n2vc_info["deployed"] = nsr_lcm["VCA"]
                    n2vc_info["db_update"] = db_nsr_update
                task = asyncio.ensure_future(
                    self.n2vc.DeployCharms(
                        model_name,          # The network service name
                        application_name,    # The application name
                        vnfd,                # The vnf descriptor
                        charm_path,          # Path to charm
                        params,              # Runtime params, like mgmt ip
                        {},                  # for native charms only
                        self.n2vc_callback,  # Callback for status changes
                        n2vc_info,              # Callback parameter
                        None,                # Callback parameter (task)
                    )
                )
                task.add_done_callback(functools.partial(self.n2vc_callback, model_name, application_name, None, None,
                                                         n2vc_info))
                self.lcm_tasks.register("ns", nsr_id, nslcmop_id, "create_charm:" + application_name, task)

            step = "Looking for needed vnfd to configure"
            self.logger.debug(logging_text + step)

            for c_vnf in nsd["constituent-vnfd"]:
                vnfd_id = c_vnf["vnfd-id-ref"]
                vnf_index = str(c_vnf["member-vnf-index"])
                vnfd = needed_vnfd[vnfd_id]

                # Check if this VNF has a charm configuration
                vnf_config = vnfd.get("vnf-configuration")

                if vnf_config and vnf_config.get("juju"):
                    proxy_charm = vnf_config["juju"]["charm"]
                    config_primitive = None

                    if proxy_charm:
                        if 'initial-config-primitive' in vnf_config:
                            config_primitive = vnf_config['initial-config-primitive']

                        # Login to the VCA. If there are multiple calls to login(),
                        # subsequent calls will be a nop and return immediately.
                        step = "connecting to N2VC to configure vnf {}".format(vnf_index)
                        await self.n2vc.login()
                        deploy(vnf_index, None, db_vnfrs[vnf_index]["ip-address"], n2vc_info, config_primitive)
                        number_to_configure += 1

                # Deploy charms for each VDU that supports one.
                vdu_index = 0
                for vdu in vnfd['vdu']:
                    vdu_config = vdu.get('vdu-configuration')
                    proxy_charm = None
                    config_primitive = None

                    if vdu_config and vdu_config.get("juju"):
                        proxy_charm = vdu_config["juju"]["charm"]

                        if 'initial-config-primitive' in vdu_config:
                            config_primitive = vdu_config['initial-config-primitive']

                        if proxy_charm:
                            step = "connecting to N2VC to configure vdu {} from vnf {}".format(vdu["id"], vnf_index)
                            await self.n2vc.login()
                            deploy(vnf_index, vdu["id"], db_vnfrs[vnf_index]["vdur"][vdu_index]["ip-address"],
                                   n2vc_info, config_primitive)
                            number_to_configure += 1
                    vdu_index += 1

            db_nsr_update["operational-status"] = "running"
            configuration_failed = False
            if number_to_configure:
                old_status = "configuring: init: {}".format(number_to_configure)
                db_nsr_update["config-status"] = old_status
                db_nsr_update["detailed-status"] = old_status
                db_nslcmop_update["detailed-status"] = old_status

                # wait until all are configured.
                while True:
                    if db_nsr_update:
                        self.update_db_2("nsrs", nsr_id, db_nsr_update)
                    if db_nslcmop_update:
                        self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
                    await n2vc_info["n2vc_event"].wait()
                    n2vc_info["n2vc_event"].clear()
                    all_active = True
                    status_map = {}
                    n2vc_error_text = []  # contain text error list. If empty no one is in error status
                    for _, vca_info in nsr_lcm["VCA"].items():
                        vca_status = vca_info["operational-status"]
                        if vca_status not in status_map:
                            # Initialize it
                            status_map[vca_status] = 0
                        status_map[vca_status] += 1

                        if vca_status != "active":
                            all_active = False
                        if vca_status in ("error", "blocked"):
                            n2vc_error_text.append(
                                "member_vnf_index={} vdu_id={} {}: {}".format(vca_info["member-vnf-index"],
                                                                              vca_info["vdu_id"], vca_status,
                                                                              vca_info["detailed-status"]))

                    if all_active:
                        break
                    elif n2vc_error_text:
                        db_nsr_update["config-status"] = "failed"
                        error_text = "fail configuring " + ";".join(n2vc_error_text)
                        db_nsr_update["detailed-status"] = error_text
                        db_nslcmop_update["operationState"] = nslcmop_operation_state = "FAILED_TEMP"
                        db_nslcmop_update["detailed-status"] = error_text
                        db_nslcmop_update["statusEnteredTime"] = time()
                        configuration_failed = True
                        break
                    else:
                        cs = "configuring: "
                        separator = ""
                        for status, num in status_map.items():
                            cs += separator + "{}: {}".format(status, num)
                            separator = ", "
                        if old_status != cs:
                            db_nsr_update["config-status"] = cs
                            db_nsr_update["detailed-status"] = cs
                            db_nslcmop_update["detailed-status"] = cs
                            old_status = cs

            if not configuration_failed:
                # all is done
                db_nslcmop_update["operationState"] = nslcmop_operation_state = "COMPLETED"
                db_nslcmop_update["statusEnteredTime"] = time()
                db_nslcmop_update["detailed-status"] = "done"
                db_nsr_update["config-status"] = "configured"
                db_nsr_update["detailed-status"] = "done"

            # step = "Sending monitoring parameters to PM"
            # for c_vnf in nsd["constituent-vnfd"]:
            #     await self.create_monitoring(nsr_id, c_vnf["member-vnf-index"], needed_vnfd[c_vnf["vnfd-id-ref"]])
            return

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
                if db_nslcmop:
                    db_nslcmop_update["detailed-status"] = "FAILED {}: {}".format(step, exc)
                    db_nslcmop_update["operationState"] = nslcmop_operation_state = "FAILED"
                    db_nslcmop_update["statusEnteredTime"] = time()
            if db_nsr_update:
                self.update_db_2("nsrs", nsr_id, db_nsr_update)
            if db_nslcmop_update:
                self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
            if nslcmop_operation_state:
                try:
                    await self.msg.aiowrite("ns", "instantiated", {"nsr_id": nsr_id, "nslcmop_id": nslcmop_id,
                                                                   "operationState": nslcmop_operation_state})
                except Exception as e:
                    self.logger.error(logging_text + "kafka_write notification Exception {}".format(e))

            self.logger.debug(logging_text + "Exit")
            self.lcm_tasks.remove("ns", nsr_id, nslcmop_id, "ns_instantiate")

    async def terminate(self, nsr_id, nslcmop_id):
        logging_text = "Task ns={} terminate={} ".format(nsr_id, nslcmop_id)
        self.logger.debug(logging_text + "Enter")
        db_nsr = None
        db_nslcmop = None
        exc = None
        failed_detail = []   # annotates all failed error messages
        vca_task_list = []
        vca_task_dict = {}
        db_nsr_update = {}
        db_nslcmop_update = {}
        nslcmop_operation_state = None
        try:
            step = "Getting nslcmop={} from db".format(nslcmop_id)
            db_nslcmop = self.db.get_one("nslcmops", {"_id": nslcmop_id})
            step = "Getting nsr={} from db".format(nsr_id)
            db_nsr = self.db.get_one("nsrs", {"_id": nsr_id})
            # nsd = db_nsr["nsd"]
            nsr_lcm = deepcopy(db_nsr["_admin"].get("deployed"))
            if db_nsr["_admin"]["nsState"] == "NOT_INSTANTIATED":
                return
            # TODO ALF remove
            # db_vim = self.db.get_one("vim_accounts", {"_id":  db_nsr["datacenter"]})
            # #TODO check if VIM is creating and wait
            # RO_vim_id = db_vim["_admin"]["deployed"]["RO"]

            db_nsr_update["operational-status"] = "terminating"
            db_nsr_update["config-status"] = "terminating"

            if nsr_lcm and nsr_lcm.get("VCA"):
                try:
                    step = "Scheduling configuration charms removing"
                    db_nsr_update["detailed-status"] = "Deleting charms"
                    self.logger.debug(logging_text + step)
                    self.update_db_2("nsrs", nsr_id, db_nsr_update)
                    for application_name, deploy_info in nsr_lcm["VCA"].items():
                        if deploy_info:  # TODO it would be desirable having a and deploy_info.get("deployed"):
                            task = asyncio.ensure_future(
                                self.n2vc.RemoveCharms(
                                    deploy_info['model'],
                                    application_name,
                                    # self.n2vc_callback,
                                    # db_nsr,
                                    # db_nslcmop,
                                )
                            )
                            vca_task_list.append(task)
                            vca_task_dict[application_name] = task
                            # task.add_done_callback(functools.partial(self.n2vc_callback, deploy_info['model'],
                            #                                          deploy_info['application'], None, db_nsr,
                            #                                          db_nslcmop, vnf_index))
                            self.lcm_tasks.register("ns", nsr_id, nslcmop_id, "delete_charm:" + application_name, task)
                except Exception as e:
                    self.logger.debug(logging_text + "Failed while deleting charms: {}".format(e))

            # remove from RO
            RO_fail = False
            RO = ROclient.ROClient(self.loop, **self.ro_config)

            # Delete ns
            RO_nsr_id = RO_delete_action = None
            if nsr_lcm and nsr_lcm.get("RO"):
                RO_nsr_id = nsr_lcm["RO"].get("nsr_id")
                RO_delete_action = nsr_lcm["RO"].get("nsr_delete_action_id")
            try:
                if RO_nsr_id:
                    step = db_nsr_update["detailed-status"] = db_nslcmop_update["detailed-status"] = "Deleting ns at RO"
                    self.logger.debug(logging_text + step)
                    desc = await RO.delete("ns", RO_nsr_id)
                    RO_delete_action = desc["action_id"]
                    db_nsr_update["_admin.deployed.RO.nsr_delete_action_id"] = RO_delete_action
                    db_nsr_update["_admin.deployed.RO.nsr_id"] = None
                    db_nsr_update["_admin.deployed.RO.nsr_status"] = "DELETED"
                if RO_delete_action:
                    # wait until NS is deleted from VIM
                    step = detailed_status = "Waiting ns deleted from VIM. RO_id={}".format(RO_nsr_id)
                    detailed_status_old = None
                    self.logger.debug(logging_text + step)

                    delete_timeout = 20 * 60   # 20 minutes
                    while delete_timeout > 0:
                        desc = await RO.show("ns", item_id_name=RO_nsr_id, extra_item="action",
                                             extra_item_id=RO_delete_action)
                        ns_status, ns_status_info = RO.check_action_status(desc)
                        if ns_status == "ERROR":
                            raise ROclient.ROClientException(ns_status_info)
                        elif ns_status == "BUILD":
                            detailed_status = step + "; {}".format(ns_status_info)
                        elif ns_status == "ACTIVE":
                            break
                        else:
                            assert False, "ROclient.check_action_status returns unknown {}".format(ns_status)
                        await asyncio.sleep(5, loop=self.loop)
                        delete_timeout -= 5
                        if detailed_status != detailed_status_old:
                            detailed_status_old = db_nslcmop_update["detailed-status"] = detailed_status
                            self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
                    else:  # delete_timeout <= 0:
                        raise ROclient.ROClientException("Timeout waiting ns deleted from VIM")

            except ROclient.ROClientException as e:
                if e.http_code == 404:  # not found
                    db_nsr_update["_admin.deployed.RO.nsr_id"] = None
                    db_nsr_update["_admin.deployed.RO.nsr_status"] = "DELETED"
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
            if not RO_fail and nsr_lcm and nsr_lcm.get("RO") and nsr_lcm["RO"].get("nsd_id"):
                RO_nsd_id = nsr_lcm["RO"]["nsd_id"]
                try:
                    step = db_nsr_update["detailed-status"] = db_nslcmop_update["detailed-status"] =\
                        "Deleting nsd at RO"
                    await RO.delete("nsd", RO_nsd_id)
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

            if not RO_fail and nsr_lcm and nsr_lcm.get("RO") and nsr_lcm["RO"].get("vnfd_id"):
                for vnf_id, RO_vnfd_id in nsr_lcm["RO"]["vnfd_id"].items():
                    if not RO_vnfd_id:
                        continue
                    try:
                        step = db_nsr_update["detailed-status"] = db_nslcmop_update["detailed-status"] =\
                            "Deleting vnfd={} at RO".format(vnf_id)
                        await RO.delete("vnfd", RO_vnfd_id)
                        self.logger.debug(logging_text + "RO_vnfd_id={} deleted".format(RO_vnfd_id))
                        db_nsr_update["_admin.deployed.RO.vnfd_id.{}".format(vnf_id)] = None
                    except ROclient.ROClientException as e:
                        if e.http_code == 404:  # not found
                            db_nsr_update["_admin.deployed.RO.vnfd_id.{}".format(vnf_id)] = None
                            self.logger.debug(logging_text + "RO_vnfd_id={} already deleted ".format(RO_vnfd_id))
                        elif e.http_code == 409:   # conflict
                            failed_detail.append("RO_vnfd_id={} delete conflict: {}".format(RO_vnfd_id, e))
                            self.logger.debug(logging_text + failed_detail[-1])
                        else:
                            failed_detail.append("RO_vnfd_id={} delete error: {}".format(RO_vnfd_id, e))
                            self.logger.error(logging_text + failed_detail[-1])

            if vca_task_list:
                db_nsr_update["detailed-status"] = db_nslcmop_update["detailed-status"] =\
                    "Waiting for deletion of configuration charms"
                self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
                self.update_db_2("nsrs", nsr_id, db_nsr_update)
                await asyncio.wait(vca_task_list, timeout=300)
            for application_name, task in vca_task_dict.items():
                if task.cancelled():
                    failed_detail.append("VCA[{}] Deletion has been cancelled".format(application_name))
                elif task.done():
                    exc = task.exception()
                    if exc:
                        failed_detail.append("VCA[{}] Deletion exception: {}".format(application_name, exc))
                    else:
                        db_nsr_update["_admin.deployed.VCA.{}".format(application_name)] = None
                else:  # timeout
                    # TODO Should it be cancelled?!!
                    task.cancel()
                    failed_detail.append("VCA[{}] Deletion timeout".format(application_name))

            if failed_detail:
                self.logger.error(logging_text + " ;".join(failed_detail))
                db_nsr_update["operational-status"] = "failed"
                db_nsr_update["detailed-status"] = "Deletion errors " + "; ".join(failed_detail)
                db_nslcmop_update["detailed-status"] = "; ".join(failed_detail)
                db_nslcmop_update["operationState"] = nslcmop_operation_state = "FAILED"
                db_nslcmop_update["statusEnteredTime"] = time()
            elif db_nslcmop["operationParams"].get("autoremove"):
                self.db.del_one("nsrs", {"_id": nsr_id})
                db_nsr_update.clear()
                self.db.del_list("nslcmops", {"nsInstanceId": nsr_id})
                nslcmop_operation_state = "COMPLETED"
                db_nslcmop_update.clear()
                self.db.del_list("vnfrs", {"nsr-id-ref": nsr_id})
                self.logger.debug(logging_text + "Delete from database")
            else:
                db_nsr_update["operational-status"] = "terminated"
                db_nsr_update["detailed-status"] = "Done"
                db_nsr_update["_admin.nsState"] = "NOT_INSTANTIATED"
                db_nslcmop_update["detailed-status"] = "Done"
                db_nslcmop_update["operationState"] = nslcmop_operation_state = "COMPLETED"
                db_nslcmop_update["statusEnteredTime"] = time()

        except (ROclient.ROClientException, DbException) as e:
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
            if db_nslcmop_update:
                self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
            if db_nsr_update:
                self.update_db_2("nsrs", nsr_id, db_nsr_update)
            if nslcmop_operation_state:
                try:
                    await self.msg.aiowrite("ns", "terminated", {"nsr_id": nsr_id, "nslcmop_id": nslcmop_id,
                                                                 "operationState": nslcmop_operation_state})
                except Exception as e:
                    self.logger.error(logging_text + "kafka_write notification Exception {}".format(e))
            self.logger.debug(logging_text + "Exit")
            self.lcm_tasks.remove("ns", nsr_id, nslcmop_id, "ns_terminate")

    async def _ns_execute_primitive(self, db_deployed, nsr_name, member_vnf_index, vdu_id, primitive, primitive_params):

        vdu_id_text = "vnfd"
        if vdu_id:
            vdu_id_text = vdu_id
        application_name = self.n2vc.FormatApplicationName(
            nsr_name,
            member_vnf_index,
            vdu_id_text
        )
        vca_deployed = db_deployed["VCA"].get(application_name)
        if not vca_deployed:
            raise LcmException("charm for member_vnf_index={} vdu_id={} is not deployed".format(member_vnf_index,
                                                                                                vdu_id))
        model_name = vca_deployed.get("model")
        application_name = vca_deployed.get("application")
        if not model_name or not application_name:
            raise LcmException("charm for member_vnf_index={} is not properly deployed".format(member_vnf_index))
        if vca_deployed["operational-status"] != "active":
            raise LcmException("charm for member_vnf_index={} operational_status={} not 'active'".format(
                member_vnf_index, vca_deployed["operational-status"]))
        callback = None  # self.n2vc_callback
        callback_args = ()  # [db_nsr, db_nslcmop, member_vnf_index, None]
        await self.n2vc.login()
        task = asyncio.ensure_future(
            self.n2vc.ExecutePrimitive(
                model_name,
                application_name,
                primitive, callback,
                *callback_args,
                **primitive_params
            )
        )
        # task.add_done_callback(functools.partial(self.n2vc_callback, model_name, application_name, None,
        #                                          db_nsr, db_nslcmop, member_vnf_index))
        # self.lcm_tasks.register("ns", nsr_id, nslcmop_id, "action:" + primitive, task)
        # wait until completed with timeout
        await asyncio.wait((task,), timeout=600)

        result = "FAILED"  # by default
        result_detail = ""
        if task.cancelled():
            result_detail = "Task has been cancelled"
        elif task.done():
            exc = task.exception()
            if exc:
                result_detail = str(exc)
            else:
                # TODO revise with Adam if action is finished and ok when task is done or callback is needed
                result = "COMPLETED"
                result_detail = "Done"
        else:  # timeout
            # TODO Should it be cancelled?!!
            task.cancel()
            result_detail = "timeout"
        return result, result_detail

    async def action(self, nsr_id, nslcmop_id):
        logging_text = "Task ns={} action={} ".format(nsr_id, nslcmop_id)
        self.logger.debug(logging_text + "Enter")
        # get all needed from database
        db_nsr = None
        db_nslcmop = None
        db_nslcmop_update = {}
        nslcmop_operation_state = None
        exc = None
        try:
            step = "Getting information from database"
            db_nslcmop = self.db.get_one("nslcmops", {"_id": nslcmop_id})
            db_nsr = self.db.get_one("nsrs", {"_id": nsr_id})
            nsr_lcm = db_nsr["_admin"].get("deployed")
            nsr_name = db_nsr["name"]
            vnf_index = db_nslcmop["operationParams"]["member_vnf_index"]
            vdu_id = db_nslcmop["operationParams"].get("vdu_id")

            # TODO check if ns is in a proper status
            primitive = db_nslcmop["operationParams"]["primitive"]
            primitive_params = db_nslcmop["operationParams"]["primitive_params"]
            result, result_detail = await self._ns_execute_primitive(nsr_lcm, nsr_name, vnf_index, vdu_id, primitive,
                                                                     primitive_params)
            db_nslcmop_update["detailed-status"] = result_detail
            db_nslcmop_update["operationState"] = nslcmop_operation_state = result
            db_nslcmop_update["statusEnteredTime"] = time()
            self.logger.debug(logging_text + " task Done with result {} {}".format(result, result_detail))
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
                db_nslcmop_update["detailed-status"] = "FAILED {}: {}".format(step, exc)
                db_nslcmop_update["operationState"] = nslcmop_operation_state = "FAILED"
                db_nslcmop_update["statusEnteredTime"] = time()
            if db_nslcmop_update:
                self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
            self.logger.debug(logging_text + "Exit")
            if nslcmop_operation_state:
                try:
                    await self.msg.aiowrite("ns", "actioned", {"nsr_id": nsr_id, "nslcmop_id": nslcmop_id,
                                                               "operationState": nslcmop_operation_state})
                except Exception as e:
                    self.logger.error(logging_text + "kafka_write notification Exception {}".format(e))
            self.logger.debug(logging_text + "Exit")
            self.lcm_tasks.remove("ns", nsr_id, nslcmop_id, "ns_action")

    async def scale(self, nsr_id, nslcmop_id):
        logging_text = "Task ns={} scale={} ".format(nsr_id, nslcmop_id)
        self.logger.debug(logging_text + "Enter")
        # get all needed from database
        db_nsr = None
        db_nslcmop = None
        db_nslcmop_update = {}
        nslcmop_operation_state = None
        db_nsr_update = {}
        exc = None
        try:
            step = "Getting nslcmop from database"
            db_nslcmop = self.db.get_one("nslcmops", {"_id": nslcmop_id})
            step = "Getting nsr from database"
            db_nsr = self.db.get_one("nsrs", {"_id": nsr_id})
            step = "Parsing scaling parameters"
            db_nsr_update["operational-status"] = "scaling"
            self.update_db_2("nsrs", nsr_id, db_nsr_update)
            nsr_lcm = db_nsr["_admin"].get("deployed")
            RO_nsr_id = nsr_lcm["RO"]["nsr_id"]
            vnf_index = db_nslcmop["operationParams"]["scaleVnfData"]["scaleByStepData"]["member-vnf-index"]
            scaling_group = db_nslcmop["operationParams"]["scaleVnfData"]["scaleByStepData"]["scaling-group-descriptor"]
            scaling_type = db_nslcmop["operationParams"]["scaleVnfData"]["scaleVnfType"]
            # scaling_policy = db_nslcmop["operationParams"]["scaleVnfData"]["scaleByStepData"].get("scaling-policy")

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
            step = "Sending scale order to RO"
            nb_scale_op = 0
            if not db_nsr["_admin"].get("scaling-group"):
                self.update_db_2("nsrs", nsr_id, {"_admin.scaling-group": [{"name": scaling_group, "nb-scale-op": 0}]})
                admin_scale_index = 0
            else:
                for admin_scale_index, admin_scale_info in enumerate(db_nsr["_admin"]["scaling-group"]):
                    if admin_scale_info["name"] == scaling_group:
                        nb_scale_op = admin_scale_info.get("nb-scale-op", 0)
                        break
            RO_scaling_info = []
            vdu_scaling_info = {"scaling_group_name": scaling_group, "vdu": []}
            if scaling_type == "SCALE_OUT":
                # count if max-instance-count is reached
                if "max-instance-count" in scaling_descriptor and scaling_descriptor["max-instance-count"] is not None:
                    max_instance_count = int(scaling_descriptor["max-instance-count"])
                    if nb_scale_op >= max_instance_count:
                        raise LcmException("reached the limit of {} (max-instance-count) scaling-out operations for the"
                                           " scaling-group-descriptor '{}'".format(nb_scale_op, scaling_group))
                nb_scale_op = nb_scale_op + 1
                vdu_scaling_info["scaling_direction"] = "OUT"
                vdu_scaling_info["vdu-create"] = {}
                for vdu_scale_info in scaling_descriptor["vdu"]:
                    RO_scaling_info.append({"osm_vdu_id": vdu_scale_info["vdu-id-ref"], "member-vnf-index": vnf_index,
                                            "type": "create", "count": vdu_scale_info.get("count", 1)})
                    vdu_scaling_info["vdu-create"][vdu_scale_info["vdu-id-ref"]] = vdu_scale_info.get("count", 1)
            elif scaling_type == "SCALE_IN":
                # count if min-instance-count is reached
                if "min-instance-count" in scaling_descriptor and scaling_descriptor["min-instance-count"] is not None:
                    min_instance_count = int(scaling_descriptor["min-instance-count"])
                    if nb_scale_op <= min_instance_count:
                        raise LcmException("reached the limit of {} (min-instance-count) scaling-in operations for the "
                                           "scaling-group-descriptor '{}'".format(nb_scale_op, scaling_group))
                nb_scale_op = nb_scale_op - 1
                vdu_scaling_info["scaling_direction"] = "IN"
                vdu_scaling_info["vdu-delete"] = {}
                for vdu_scale_info in scaling_descriptor["vdu"]:
                    RO_scaling_info.append({"osm_vdu_id": vdu_scale_info["vdu-id-ref"], "member-vnf-index": vnf_index,
                                            "type": "delete", "count": vdu_scale_info.get("count", 1)})
                    vdu_scaling_info["vdu-delete"][vdu_scale_info["vdu-id-ref"]] = vdu_scale_info.get("count", 1)

            # update VDU_SCALING_INFO with the VDUs to delete ip_addresses
            if vdu_scaling_info["scaling_direction"] == "IN":
                for vdur in reversed(db_vnfr["vdur"]):
                    if vdu_scaling_info["vdu-delete"].get(vdur["vdu-id-ref"]):
                        vdu_scaling_info["vdu-delete"][vdur["vdu-id-ref"]] -= 1
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
                del vdu_scaling_info["vdu-delete"]

            # execute primitive service PRE-SCALING
            step = "Executing pre-scale vnf-config-primitive"
            if scaling_descriptor.get("scaling-config-action"):
                for scaling_config_action in scaling_descriptor["scaling-config-action"]:
                    if scaling_config_action.get("trigger") and scaling_config_action["trigger"] == "pre-scale-in" \
                            and scaling_type == "SCALE_IN":
                        vnf_config_primitive = scaling_config_action["vnf-config-primitive-name-ref"]
                        step = db_nslcmop_update["detailed-status"] = \
                            "executing pre-scale scaling-config-action '{}'".format(vnf_config_primitive)
                        # look for primitive
                        primitive_params = {}
                        for config_primitive in db_vnfd.get("vnf-configuration", {}).get("config-primitive", ()):
                            if config_primitive["name"] == vnf_config_primitive:
                                for parameter in config_primitive.get("parameter", ()):
                                    if 'default-value' in parameter and \
                                            parameter['default-value'] == "<VDU_SCALE_INFO>":
                                        primitive_params[parameter["name"]] = yaml.safe_dump(vdu_scaling_info,
                                                                                             default_flow_style=True,
                                                                                             width=256)
                                break
                        else:
                            raise LcmException(
                                "Invalid vnfd descriptor at scaling-group-descriptor[name='{}']:scaling-config-action"
                                "[vnf-config-primitive-name-ref='{}'] does not match any vnf-cnfiguration:config-"
                                "primitive".format(scaling_group, config_primitive))
                        result, result_detail = await self._ns_execute_primitive(nsr_lcm, vnf_index,
                                                                                 vnf_config_primitive, primitive_params)
                        self.logger.debug(logging_text + "vnf_config_primitive={} Done with result {} {}".format(
                            vnf_config_primitive, result, result_detail))
                        if result == "FAILED":
                            raise LcmException(result_detail)

            if RO_scaling_info:
                RO = ROclient.ROClient(self.loop, **self.ro_config)
                RO_desc = await RO.create_action("ns", RO_nsr_id, {"vdu-scaling": RO_scaling_info})
                db_nsr_update["_admin.scaling-group.{}.nb-scale-op".format(admin_scale_index)] = nb_scale_op
                db_nsr_update["_admin.scaling-group.{}.time".format(admin_scale_index)] = time()
                # TODO mark db_nsr_update as scaling
                # wait until ready
                RO_nslcmop_id = RO_desc["instance_action_id"]
                db_nslcmop_update["_admin.deploy.RO"] = RO_nslcmop_id

                RO_task_done = False
                step = detailed_status = "Waiting RO_task_id={} to complete the scale action.".format(RO_nslcmop_id)
                detailed_status_old = None
                self.logger.debug(logging_text + step)

                deployment_timeout = 1 * 3600   # One hours
                while deployment_timeout > 0:
                    if not RO_task_done:
                        desc = await RO.show("ns", item_id_name=RO_nsr_id, extra_item="action",
                                             extra_item_id=RO_nslcmop_id)
                        ns_status, ns_status_info = RO.check_action_status(desc)
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
                        desc = await RO.show("ns", RO_nsr_id)
                        ns_status, ns_status_info = RO.check_ns_status(desc)
                        if ns_status == "ERROR":
                            raise ROclient.ROClientException(ns_status_info)
                        elif ns_status == "BUILD":
                            detailed_status = step + "; {}".format(ns_status_info)
                        elif ns_status == "ACTIVE":
                            step = detailed_status = "Waiting for management IP address reported by the VIM"
                            try:
                                desc = await RO.show("ns", RO_nsr_id)
                                nsr_lcm["nsr_ip"] = RO.get_ns_vnf_info(desc)
                                break
                            except ROclient.ROClientException as e:
                                if e.http_code != 409:  # IP address is not ready return code is 409 CONFLICT
                                    raise e
                        else:
                            assert False, "ROclient.check_ns_status returns unknown {}".format(ns_status)
                    if detailed_status != detailed_status_old:
                        detailed_status_old = db_nslcmop_update["detailed-status"] = detailed_status
                        self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)

                    await asyncio.sleep(5, loop=self.loop)
                    deployment_timeout -= 5
                if deployment_timeout <= 0:
                    raise ROclient.ROClientException("Timeout waiting ns to be ready")

                step = "Updating VNFRs"
                self.ns_update_vnfr({db_vnfr["member-vnf-index-ref"]: db_vnfr}, desc)

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

            if db_nsr_update:
                self.update_db_2("nsrs", nsr_id, db_nsr_update)

            # execute primitive service POST-SCALING
            step = "Executing post-scale vnf-config-primitive"
            if scaling_descriptor.get("scaling-config-action"):
                for scaling_config_action in scaling_descriptor["scaling-config-action"]:
                    if scaling_config_action.get("trigger") and scaling_config_action["trigger"] == "post-scale-out" \
                            and scaling_type == "SCALE_OUT":
                        vnf_config_primitive = scaling_config_action["vnf-config-primitive-name-ref"]
                        step = db_nslcmop_update["detailed-status"] = \
                            "executing post-scale scaling-config-action '{}'".format(vnf_config_primitive)
                        # look for primitive
                        primitive_params = {}
                        for config_primitive in db_vnfd.get("vnf-configuration", {}).get("config-primitive", ()):
                            if config_primitive["name"] == vnf_config_primitive:
                                for parameter in config_primitive.get("parameter", ()):
                                    if 'default-value' in parameter and \
                                            parameter['default-value'] == "<VDU_SCALE_INFO>":
                                        primitive_params[parameter["name"]] = yaml.safe_dump(vdu_scaling_info,
                                                                                             default_flow_style=True,
                                                                                             width=256)
                                break
                        else:
                            raise LcmException("Invalid vnfd descriptor at scaling-group-descriptor[name='{}']:"
                                               "scaling-config-action[vnf-config-primitive-name-ref='{}'] does not "
                                               "match any vnf-cnfiguration:config-primitive".format(scaling_group,
                                                                                                    config_primitive))
                        result, result_detail = await self._ns_execute_primitive(nsr_lcm, vnf_index,
                                                                                 vnf_config_primitive, primitive_params)
                        self.logger.debug(logging_text + "vnf_config_primitive={} Done with result {} {}".format(
                            vnf_config_primitive, result, result_detail))
                        if result == "FAILED":
                            raise LcmException(result_detail)

            db_nslcmop_update["operationState"] = nslcmop_operation_state = "COMPLETED"
            db_nslcmop_update["statusEnteredTime"] = time()
            db_nslcmop_update["detailed-status"] = "done"
            db_nsr_update["detailed-status"] = "done"
            db_nsr_update["operational-status"] = "running"
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
                    db_nsr_update["operational-status"] = "FAILED {}: {}".format(step, exc),
                    db_nsr_update["detailed-status"] = "failed"
            if db_nslcmop_update:
                self.update_db_2("nslcmops", nslcmop_id, db_nslcmop_update)
            if db_nsr_update:
                self.update_db_2("nsrs", nsr_id, db_nsr_update)
            if nslcmop_operation_state:
                try:
                    await self.msg.aiowrite("ns", "scaled", {"nsr_id": nsr_id, "nslcmop_id": nslcmop_id,
                                                             "operationState": nslcmop_operation_state})
                    # if cooldown_time:
                    #     await asyncio.sleep(cooldown_time)
                    # await self.msg.aiowrite("ns","scaled-cooldown-time", {"nsr_id": nsr_id, "nslcmop_id": nslcmop_id})
                except Exception as e:
                    self.logger.error(logging_text + "kafka_write notification Exception {}".format(e))
            self.logger.debug(logging_text + "Exit")
            self.lcm_tasks.remove("ns", nsr_id, nslcmop_id, "ns_scale")