import os
import sys
from random import randint

sys.path.append(os.path.abspath(os.path.join(__file__, "../../../..")))
import argparse
import json
import logging
import time
import traceback

import v2.lib.resource_op as s3lib
import v2.utils.utils as utils
from v2.lib.exceptions import RGWBaseException, TestExecError
from v2.lib.resource_op import Config
from v2.lib.rgw_config_opts import CephConfOp
from v2.lib.s3.auth import Auth
from v2.lib.s3.write_io_info import BasicIOInfoStructure, IOInfoInitialize
from v2.tests.s3_swift import reusable
from v2.utils.log import configure_logging
from v2.utils.test_desc import AddTestInfo
from v2.utils.utils import RGWService

log = logging.getLogger()
TEST_DATA_PATH = None


def test_exec(config, ssh_con):
    io_info_initialize = IOInfoInitialize()
    basic_io_structure = BasicIOInfoStructure()
    io_info_initialize.initialize(basic_io_structure.initial())

    # create user
    all_users_info = s3lib.create_users(config.user_count)
    for each_user in all_users_info:
        # authenticate
        auth = Auth(each_user, ssh_con, ssl=config.ssl)
        if config.use_aws4 is True:
            rgw_conn = auth.do_auth(**{"signature_version": "s3v4"})
        else:
            rgw_conn = auth.do_auth()

        period_details = json.loads(utils.exec_shell_cmd("radosgw-admin period get"))
        zone_list = json.loads(utils.exec_shell_cmd("radosgw-admin zone list"))
        for zone in period_details["period_map"]["zonegroups"][0]["zones"]:
            if zone["name"] not in zone_list["zones"]:
                rgw_nodes = zone["endpoints"][0].split(":")
                node_rgw = rgw_nodes[1].split("//")[-1]
                log.info(f"Another site is: {zone['name']} and ip {node_rgw}")
                break
        rgw_ssh_con = utils.connect_remote(node_rgw)

        # create buckets
        if config.test_ops.get("create_bucket", False):
            log.info(f"no of buckets to create: {config.bucket_count}")
            buckets = []
            for bc in range(config.bucket_count):
                bucket_name_to_create = utils.gen_bucket_name_from_userid(
                    each_user["user_id"], rand_no=bc
                )
                log.info(f"creating bucket with name: {bucket_name_to_create}")
                bucket = reusable.create_bucket(
                    bucket_name_to_create, rgw_conn, each_user
                )
                reusable.verify_bucket_sync_on_other_site(rgw_ssh_con, bucket)
                buckets.append(bucket)

            for bkt in buckets:
                if utils.is_cluster_multisite():
                    if config.test_ops.get("create_object", False):
                        # uploading data
                        log.info(
                            f"s3 objects to create: {config.objects_count}"
                        )
                        for oc, size in list(config.mapped_sizes.items()):
                            config.obj_size = size
                            s3_object_name = utils.gen_s3_object_name(
                                bkt.name, oc
                            )
                            log.info(f"s3 object name: {s3_object_name}")
                            s3_object_path = os.path.join(
                                TEST_DATA_PATH, s3_object_name
                            )
                            log.info(f"s3 object path: {s3_object_path}")
                            if config.test_ops.get("enable_version", False):
                                reusable.upload_version_object(
                                    config,
                                    each_user,
                                    rgw_conn,
                                    s3_object_name,
                                    config.obj_size,
                                    bkt,
                                    TEST_DATA_PATH,
                                )
                            else:
                                log.info("upload type: normal")
                                reusable.upload_object(
                                    s3_object_name,
                                    bkt,
                                    TEST_DATA_PATH,
                                    config,
                                    each_user,
                                )

                    # reusable.verify_object_sync_on_other_site(
                    #     rgw_ssh_con, bkt, config
                    # )
                    reusable.check_sync_status(return_while_sync_inprogress=True)
                    reusable.reboot_rgw_nodes()
                    reusable.check_sync_status()

                    

                        

