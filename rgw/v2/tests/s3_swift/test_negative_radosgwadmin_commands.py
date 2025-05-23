"""

Test multisite negative scenarios in Ceph RGW

Usage:

test_negative_radosgwadmin_commands.py -c <input_yaml> --rgw-node <rgw_ip>
<input_yaml>:

multisite_configs/test_negative_radosgw_admin_primary.yaml
multisite_configs/test_negative_radosgw_admin_secondary.yaml
Operation:

Create realms, zonegroups, and zones with invalid or boundary values in a multisite environment.
Test commands like realm creation, zonegroup creation, zone creation, and user creation with missing or incorrect parameters.
Execute period pull commands with invalid credentials or URL.
Ensure that errors are returned for each invalid scenario.
Logs will capture all errors and results, and the script expects failures for each of the test commands.

"""
import argparse
import os
import subprocess
import sys

import yaml

sys.path.append(os.path.abspath(os.path.join(__file__, "../../../..")))
import v1.utils.log as log
from v1.utils.test_desc import AddTestInfo


def execute_command(command):
    """Executes a command and returns stdout, stderr, and return code."""
    process = subprocess.Popen(
        command, shell=True, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    stdout, stderr = process.communicate()
    return_code = process.returncode
    return return_code, stdout, stderr


def test_exec_primary(config, rgw_node):
    test_info = AddTestInfo("test multisite negative primary")
    try:
        test_info.started_info()
        commands = [
            "radosgw-admin realm create --rgw-realm india --default",
            "radosgw-admin zonegroup create --rgw-realm india --rgw-zonegroup shared --endpoints http://node_ip:node5:80 --master --default",
            "radosgw-admin zone create --rgw-realm india --rgw-zonegroup shared --rgw-zone primary --endpoints http://node_ip:node5:80 --master --default",
            "radosgw-admin zone create --rgw-realm india --rgw-zonegroup shared --rgw-zone primary --endpoints http://node_ip:node5:80 --master --default",
            "radosgw-admin user create --uid=repuser --display_name='Replication user' --access-key 21e86bce636c3aa0 --secret cf764951f1fdde5d --rgw-realm india --system",
            "radosgw-admin zonegroup default --rgw-zonegroup non_existent_zonegroup",
            "radosgw-admin realm default --rgw-realm non_existent_realm",
            "radosgw-admin zone default --rgw-zone non_existent_zone",
            "radosgw-admin realm create --rgw-realm '' --default"
            "radosgw-admin zonegroup add --rgw-zonegroup us --rgw-zone non_existent_zone",
            "radosgw-admin zonegroup create --rgw-realm india --rgw-zonegroup ''  --endpoints=http://node_ip:node5:80 --master --default",
            "radosgw-admin zone create --rgw-realm india --rgw-zonegroup shared --rgw-zone ''  --endpoints=http://node_ip:node5:80 --master --default",
            "radosgw-admin zone create --rgw-realm india --rgw-zonegroup '' --endpoints=http://node_ip:node5:80 --master --defaultt",
            "radosgw-admin user create --uid=zone --display-name=''",
            "radosgw-admin user create --uid= --display-name='Zone User1'",
        ]
        for command in commands:
            log.info(f"Executing command: {command} ")
            return_code, stdout, stderr = execute_command(command)
            if return_code == 0:
                test_info.failed_status(
                    f"Command '{command}' succeeded unexpectedly. Stdout: {stdout}, Stderr: {stderr}, Return Code: {return_code}"
                )
                sys.exit(1)
            else:
                log.info(f"{stderr} failed as expected.")

        test_info.success_status("Negative tests on ceph-pri completed")
        sys.exit(0)
    except Exception as e:
        log.error(f"An error occurred: {e}")
        test_info.failed_status(f"An error occurred: {e}")
        sys.exit(1)


def test_exec_secondary(config, rgw_node):
    test_info = AddTestInfo("test multisite negative secondary")
    try:
        test_info.started_info()
        commands = [
            "radosgw-admin period pull --url http://node_ip:ceph-pri#node5:80 --access-key invalid_key --secret invalid_secret",
            "radosgw-admin period pull --url http://node_ip:ceph-pri#node5:80 --access-key 21e86bce636c3aa0 --secret ''",
            "radosgw-admin period pull --url http://node_ip:ceph-pri#node5:80 --access-key '' --secret ''",
            "radosgw-admin period pull --url http://node_ip:ceph-pri#node5:80 --access-key '' --secret '' --rgw-realm india --rgw-zonegroup shared --rgw-zone secondary",
            "radosgw-admin realm pull --url=http://node_ip:ceph-pri#node5:80  --access-key=invalid_key --secret=invalid_secret",
            "radosgw-admin realm pull --url=http://node_ip:ceph-pri#node5:80  --access-key= --secret=",
            "radosgw-admin realm default --rgw-realm=non_existent_realm",
            "radosgw-admin zonegroup default --rgw-zonegroup=non_existent_zonegroup",
            "radosgw-admin zone create --rgw-zonegroup=us --rgw-zone=us-2 --access-key=invalid_key --secret=invalid_secret --endpoints=http://node_ip:ceph-sec#node5:80",
            "radosgw-admin zone create --rgw-zonegroup=us --rgw-zone=us-2 --access-key= --secret= --endpoints=http://node_ip:ceph-sec#node5:80",
            "radosgw-admin zone create --rgw-zonegroup=us --rgw-zone=us-2 --access-key=21e86bce636c3aa0 --secret=cf764951f1fdde5d --endpoints=invalid_endpoint",
        ]
        for command in commands:
            log.info(f"Executing command: {command} ")
            return_code, stdout, stderr = execute_command(command)
            if return_code == 0:
                test_info.failed_status(
                    f"Command '{command}' succeeded unexpectedly. Stdout: {stdout}, Stderr: {stderr}, Return Code: {return_code}"
                )
                sys.exit(1)
            else:
                log.info(f"{stderr} failed as expected.")

        test_info.success_status("Negative tests on ceph-sec completed")
        sys.exit(0)
    except Exception as e:
        log.error(f"An error occurred: {e}")
        test_info.failed_status(f"An error occurred: {e}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RGW Multisite Negative Tests")
    parser.add_argument("-c", dest="config", help="Test yaml configuration")
    parser.add_argument("--rgw-node", dest="rgw_node", help="rgw node ip")
    args = parser.parse_args()

    yaml_file = args.config
    config = {}
    if yaml_file:
        with open(yaml_file, "r") as f:
            config = yaml.safe_load(f)

    rgw_node = args.rgw_node

    if config.get("is_primary", True):  # Default to primary if not specified
        test_exec_primary(config, rgw_node)
    else:
        test_exec_secondary(config, rgw_node)
