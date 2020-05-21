import argparse
import calendar
import json
import logging
import logging.handlers
import os
import socket
import sys
import time

from vnc_api import vnc_api

__version__ = "1.0"

SPLIT_SIZE = 2
UNTAGGED_ERROR_NUM = 2

"""
NOTE: As that script is not self contained in a python package and as it
supports multiple Contrail releases, it brings its own version that needs to
bemanually updated each time it is modified.
We also maintain a change log list in that header:
* 1.0:
    - Script checks if Virtual Port Groups with TF violate any
      VN/VLAN restrictions.
"""


# Class handles Validation of VPGs with Fabric
class FabricVPGValidator(object):
    def __init__(self, args=''):
        """Pass the arguments given by command line."""
        self._args = args

        # Support Logging
        self._logger = logging.getLogger(__name__)
        log_level = 'DEBUG' if self._args.debug else 'INFO'
        self._logger.setLevel(log_level)
        logformat = logging.Formatter("%(levelname)s: %(message)s")
        stdout = logging.StreamHandler(sys.stdout)
        stdout.setFormatter(logformat)
        self._logger.addHandler(stdout)
        logfile = logging.handlers.RotatingFileHandler(
            self._args.log_file, maxBytes=10000000, backupCount=5)
        logfile.setFormatter(logformat)
        self._logger.addHandler(logfile)

        # Initiate Validation required parameters
        hostname = socket.gethostname()
        self.vnc_lib = vnc_api.VncApi(
            api_server_host=socket.gethostbyname(hostname))
        self.vpg_uuids = self.vnc_lib.virtual_port_groups_list(
            fields=['annotations'])
        self.validation_failures = {}
        self.across_fabric_errors = 0
        self.within_vpg_errors = 0
        self.untagged_vlan_errors = 0
        self.invalid_vpgs = 0

        # Data structure to keep track of where was the vmi, vn
        # combination seen in which vpg and attached to which vmi
        self.vn_to_vpg_map = {}
        self.vlan_to_vpg_map = {}

    def _get_vpg_obj_dicts(self):
        return self.vpg_uuids['virtual-port-groups']

    def _get_vnc_lib(self):
        return self.vnc_lib

    # Function returns all annotations for a given VPG as a list
    def _get_annotations_for_vpg(self, vpg_dict):
        annotations_list = []
        annotations_kv_pairs = vpg_dict['annotations']['key_value_pair']
        for annotations_kv_dict in annotations_kv_pairs:
            annotation_key = annotations_kv_dict['key']
            annotations_value = annotations_kv_dict['value']
            annotations_list.append((annotation_key, annotations_value))
        return annotations_list

    # Helper function that returns info within annotation as a dictionary
    def _extract_annotation_info(self, annotation, vmi_val):
        untagged = False
        annotation_info_dict = {}
        if 'untagged_vlan_id' in annotation:
            untagged = True
            value_split = vmi_val.split(":")
            annotation_info_dict['untagged_vlan'] = value_split[0]
        annotation_data = annotation.split('/')
        for ann_info in annotation_data:
            tag_val = ann_info.split(':')
            if len(tag_val) == SPLIT_SIZE:
                annotation_info_dict[tag_val[0]] = tag_val[1]
        return annotation_info_dict, untagged

    # Function that finds validation Errors within VPG
    def _validation_check_within_vpg(self, vpg_dict, fabric_vn_vlan_set,
                                     fabric_vn_set, fabric_vlan_set):
        local_vn_set = set()
        local_vlan_set = set()
        local_vn_vlan_set = set()
        vn_to_annotation_map = {}
        vlan_to_annotation_map = {}
        untagged_vlan = None
        untagged_vmi = None
        untagged = False
        annotations_seen_map = {}
        local_validation_failures = []
        across_fabric_failures = []
        untagged_fabric_failures = []
        vpg_fq_name = vpg_dict['fq_name'][-1]
        # Iterate over all annotations with VPG
        for annotation, vmi_uuid in self._get_annotations_for_vpg(vpg_dict):
            annotation_info_dict, untagged = \
                self._extract_annotation_info(annotation, vmi_uuid)

            # untagged vlan is used
            if untagged is True:
                if untagged_vlan is None:
                    untagged_vlan = annotation_info_dict['untagged_vlan']
                    untagged_vmi = vmi_uuid.split(':')[1]
                    if untagged_vlan not in vlan_to_annotation_map:
                        vlan_to_annotation_map[untagged_vlan] = \
                            (annotation, untagged_vmi)
                if annotation_info_dict['untagged_vlan'] != untagged_vlan:
                    untagged_fabric_failures.append((
                        vmi_uuid.split(':')[1],
                        annotation_info_dict['untagged_vlan'],
                        untagged_vmi,
                        untagged_vlan
                    ))
                    continue

            try:
                vn_uuid = annotation_info_dict['vn']
                vlan_id = annotation_info_dict['vlan_id']
            except KeyError:
                continue

            if vn_uuid not in self.vn_to_vpg_map:
                self.vn_to_vpg_map[vn_uuid] = (
                    vpg_fq_name, vpg_dict['uuid'],
                    vmi_uuid
                )

            if vlan_id not in self.vlan_to_vpg_map:
                self.vlan_to_vpg_map[vlan_id] = (
                    vpg_fq_name, vpg_dict['uuid'],
                    vmi_uuid
                )

            if vn_uuid not in vn_to_annotation_map:
                vn_to_annotation_map[vn_uuid] = (annotation, vmi_uuid)

            if vlan_id not in vlan_to_annotation_map:
                vlan_to_annotation_map[vlan_id] = (annotation, vmi_uuid)

            # Check for inconsistencies within VPG
            if annotation not in annotations_seen_map:
                annotations_seen_map[annotation] = vmi_uuid
                local_vn_vlan_set.add((vn_uuid, vlan_id))
            else:
                local_validation_failures.append((
                    vmi_uuid,
                    annotations_seen_map[annotation],
                    vn_uuid, vlan_id
                ))
                continue

            # Checks needed for only enterprise style fabrics
            if annotation_info_dict['validation'] == 'enterprise':
                if vn_uuid not in local_vn_set:
                    local_vn_set.add(vn_uuid)
                else:
                    local_vn_vlan_set.remove((vn_uuid, vlan_id))
                    self._logger.debug(
                        "VN with uuid {0} already in use by a different VMI".
                        format(vn_uuid))
                    local_validation_failures.append((
                        vmi_uuid,
                        vn_to_annotation_map[vn_uuid][1],
                        vn_uuid, vlan_id
                    ))
                    continue
                if vlan_id not in local_vlan_set:
                    local_vlan_set.add(vlan_id)
                else:
                    local_vn_vlan_set.remove((vn_uuid, vlan_id))
                    self._logger.debug(
                        "VLAN  with uuid {0} already in use by a different VMI"
                        .format(vlan_id))
                    local_validation_failures.append((
                        vmi_uuid,
                        vlan_to_annotation_map[vlan_id][1],
                        vn_uuid, vlan_id
                    ))
                    continue

                # finally check across vpgs
                if ((vn_uuid, vlan_id) in fabric_vn_vlan_set) or \
                   ((vn_uuid not in fabric_vn_set) and
                   (vlan_id not in fabric_vlan_set)):
                    continue
                else:
                    self._logger.debug("VN/VLAN combination {0} not exact in".
                                       format((vn_uuid, vlan_id)) +
                                       " different VPG")

                    vn_seen = (vn_uuid in fabric_vn_set)
                    vlan_seen = (vlan_id in fabric_vlan_set)

                    if vn_seen is True:
                        vpg_using = self.vn_to_vpg_map[vn_uuid]
                    elif vlan_seen is True:
                        vpg_using = self.vlan_to_vpg_map[vlan_id]

                    across_fabric_failures.append((
                        vmi_uuid,
                        vpg_using,
                        vn_uuid, vlan_id
                    ))

        fabric_vn_vlan_set = fabric_vn_vlan_set.union(local_vn_vlan_set)
        fabric_vn_set = fabric_vn_set.union(local_vn_set)
        fabric_vlan_set = fabric_vlan_set.union(local_vlan_set)
        return local_validation_failures, untagged_fabric_failures, \
            across_fabric_failures, fabric_vn_vlan_set, fabric_vn_set, \
            fabric_vlan_set

    # Loop over all vpgs with Fabric and check for errors
    def _validation_check_within_fabric(self):
        fabric_vn_vlan_set = set()
        fabric_vn_set = set()
        fabric_vlan_set = set()
        self._num_total_vpgs = len(self._get_vpg_obj_dicts())
        for vpg_dict in self._get_vpg_obj_dicts():
            local_validation_failures, untagged_failures, \
                across_fabric_failures, fabric_vn_vlan_set, \
                fabric_vn_set, fabric_vlan_set = \
                self._validation_check_within_vpg(
                    vpg_dict, fabric_vn_vlan_set, fabric_vn_set,
                    fabric_vlan_set
                )

            self.across_fabric_errors += len(across_fabric_failures)
            self.untagged_vlan_errors += len(untagged_failures)
            self.within_vpg_errors += len(local_validation_failures)
            vpg_uuid = vpg_dict['uuid']
            # vpg_fq_name = ':'.join(x for x in vpg_dict['fq_name'])
            vpg_fq_name = vpg_dict['fq_name'][-1]
            self.validation_failures[(vpg_uuid, vpg_fq_name)] = {
                'local_check': local_validation_failures,
                'untagged_vlan': untagged_failures,
                'across_fabric': across_fabric_failures
            }

        self.total_errors = self.across_fabric_errors + \
            self.untagged_vlan_errors + \
            self.within_vpg_errors

    def _report_within_vpg_errors(self, local_errors, vpg_key):
        def _create_error_msg(vpg_name, vpg_uuid,
                              vmi_name, vmi_uuid,
                              other_vmi_name, other_vmi_uuid,
                              vn_uuid, vlan_id):
            error_msg = "VN-VLAN REUSED IN A VPG: "
            error_msg += "{0}({1}):{2}({3})".format(vpg_name, vpg_uuid,
                                                    vmi_name, vmi_uuid)
            error_msg += "and {0}({1}):{2}({3}) ".format(vpg_name, vpg_uuid,
                                                         other_vmi_name,
                                                         other_vmi_uuid)

            error_msg += "has same VN({0}) or VLAN({1}) or both".format(
                vn_uuid, vlan_id)

            return error_msg

        if len(local_errors) == 0:
            return
        self._logger.info(
            "Validation Errors that occured in VPG due to wrong combination:")
        vnc_lib = self._get_vnc_lib()
        for existing_vmi, other_vmi, vn, vlan in local_errors:
            existing_vmi_fq_name = vnc_lib.virtual_machine_interface_read(
                id=existing_vmi).get_display_name()
            other_vmi_fq_name = vnc_lib.virtual_machine_interface_read(
                id=other_vmi).get_display_name()
            self._logger.info(
                _create_error_msg(
                    vpg_key[0], vpg_key[1],
                    existing_vmi, existing_vmi_fq_name,
                    other_vmi, other_vmi_fq_name,
                    vn, vlan
                )
            )

    def _report_across_fabric_errors(self, local_errors, vpg_key):
        def _create_error_msg(vpg_name, vpg_uuid,
                              vmi_name, vmi_uuid,
                              other_vpg_name, other_vpg_uuid,
                              other_vmi_name, other_vmi_uuid,
                              vn_uuid, vlan_id):
            error_msg = "VN-VLAN REUSED IN A FABRIC: "
            error_msg += "{0}({1}):{2}({3})".format(vpg_name, vpg_uuid,
                                                    vmi_name,
                                                    vmi_uuid)
            error_msg += " and {0}({1}):{2}({3}) ".format(other_vpg_name,
                                                          other_vpg_uuid,
                                                          other_vmi_name,
                                                          other_vmi_uuid)
            error_msg += "has same VN({0}) or VLAN({1})".format(
                vn_uuid, vlan_id
            )

            return error_msg
        if len(local_errors) == 0:
            return
        self._logger.info(
            "Validation Errors that occured due to other VPGs within fabric ")
        vnc_lib = self._get_vnc_lib()
        for vmi, other_vpg, vn, vlan in local_errors:
            vmi_fq_name = vnc_lib.virtual_machine_interface_read(
                id=vmi
            ).get_display_name()
            other_vmi_name = vnc_lib.virtual_machine_interface_read(
                id=other_vpg[2]).get_display_name()
            self._logger.info(
                _create_error_msg(
                    vpg_key[1], vpg_key[0],
                    vmi_fq_name, vmi,
                    other_vpg[0], other_vpg[1],
                    other_vmi_name, other_vpg[2],
                    vn, vlan
                )
            )

    def _report_untagged_vlan_errors(self, local_errors, vpg_key):
        def _create_error_msg(vpg_name, vpg_uuid,
                              vmi_name, vmi_uuid,
                              other_vmi_name, other_vmi_uuid,
                              first_vlan, second_vlan):
            error_msg = "MULTIPLE UNTAGGED VLANS: "
            error_msg += "{0}({1}):{2}({3}):VLAN({4}), ".format(
                vpg_name, vpg_uuid, vmi_name, vmi_uuid, first_vlan
            )

            error_msg += "{0}({1}):{2}({3}):VLAN({4}), ".format(
                vpg_name, vpg_uuid, vmi_name, vmi_uuid, second_vlan
            )

            return error_msg

        if len(local_errors) == 0:
            return
        self._logger.info(
            "Validation Errors that occured due to" +
            "multiple Untagged VLANs")
        vnc_lib = self._get_vnc_lib()
        for vmi_untag, untag_vlan, other_vmi, other_vlan in local_errors:
            other_vmi_fq_name = vnc_lib.virtual_machine_interface_read(
                id=other_vmi).get_display_name()
            untag_vmi_fq_name = vnc_lib.virtual_machine_interface_read(
                id=vmi_untag).get_display_name()

            self._logger.info(
                _create_error_msg(
                    vpg_key[0], vpg_key[1],
                    vmi_untag, untag_vmi_fq_name,
                    other_vmi, other_vmi_fq_name,
                    untag_vlan, other_vlan
                )
            )

    def _report_validation_error_in_fabric(self):
        for vpg, errors in self.validation_failures.items():
            if all(len(error) == 0 for error_type, error in errors.items()):
                self._logger.info(
                    "For vpg with uuid: {0}, there were no failures".
                    format(vpg))
            else:
                self.invalid_vpgs += 1
                self._logger.info(
                    "The following errors occured for vpg with uuid: {0}"
                    .format(vpg))
                self._report_within_vpg_errors(errors['local_check'], vpg)
                self._report_across_fabric_errors(errors['across_fabric'], vpg)
                self._report_untagged_vlan_errors(errors['untagged_vlan'], vpg)

    def _report_statistics(self):
        print("\n")
        print("Reporting Statistics:")
        if self.total_errors == 0:
            across_percent = 0
            within_vpg = 0
            untagged_vlan = 0
        else:
            across_percent =  \
                (1.0 * self.across_fabric_errors / self.total_errors) * 100
            within_vpg = \
                (1.0 * self.within_vpg_errors / self.total_errors) * 100
            untagged_vlan = \
                (1.0 * self.untagged_vlan_errors / self.total_errors) * 100

        print("Percentage of errors that occur due to Across" +
              " Fabric VN/VLAN combinations is {0}%".format(across_percent))
        print(
            "Percentage of errors that occur due to " +
            "Duplicate Untagged VLANs is {0}%".
            format(untagged_vlan))
        print("Percentage of errors that occur due to invalid" +
              "VN/VLAN combinations within vpgs is {0}%".format(within_vpg))

        invalid_percent = \
            (1.0 * self.invalid_vpgs / self._num_total_vpgs) * 100
        print("Invalid VPG percentage is {0}".format(invalid_percent))
# End of class FabricVPGValidator


def _parse_args(args_str):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description='')

    parser.add_argument(
        '-v', '--version', action='version',
        version='%(prog)s ' + __version__
    )

    parser.add_argument(
        "--debug", help="Run in debug mode, default False",
        action='store_true', default=False
    )

    parser.add_argument(
        "--to-json", help="File to dump json to", default=None
    )

    ts = calendar.timegm(time.gmtime())
    if os.path.isdir("/var/log/contrail"):
        default_log = "/var/log/contrail/fabric_validation-{0}.log".format(ts)
    else:
        import tempfile
        default_log = '{0}/fabric_validation-{1}.log'.format(
            tempfile.gettempdir(), ts)

    parser.add_argument(
        "--log_file", help="Log file to save output, default '%(default)s'",
        default=default_log
    )

    args_obj, _ = parser.parse_known_args(args_str.split())
    _args = args_obj

    return _args
# end _parse_args


def main():
    args = _parse_args(' '.join(sys.argv[1:]))
    # Create Fabric Validator object
    fabric_validator = FabricVPGValidator(args)
    fabric_validator._validation_check_within_fabric()
    if fabric_validator._args.to_json is not None:
        # check if the backup directory exists
        default_dir = fabric_validator._args.to_json
        with open(default_dir, 'w') as f:
            json.dump(fabric_validator.validation_failures,
                      f, indent=3, sort_keys=True)
    else:
        fabric_validator._report_validation_error_in_fabric()
    fabric_validator._report_statistics()


if __name__ == "__main__":
    main()
