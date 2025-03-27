import argparse
import datetime
import json
import logging
import os
import socket
import sys
import time
import traceback

sys.path.append(os.path.abspath(os.path.join(__file__, "../../../..")))


from v2.lib import resource_op
from v2.lib.admin import UserMgmt
from v2.lib.exceptions import RGWBaseException, TestExecError
from v2.lib.rgw_config_opts import CephConfOp, ConfigOpts
from v2.lib.s3.auth import Auth
from v2.lib.s3.write_io_info import BasicIOInfoStructure, BucketIoInfo, IOInfoInitialize
from v2.lib.s3cmd import auth as s3_auth
from v2.tests.s3_swift import reusable
from v2.tests.s3cmd import reusable as s3cmd_reusable
from v2.utils import utils
from v2.utils.log import configure_logging
from v2.utils.test_desc import AddTestInfo
from v2.utils.utils import RGWService

log = logging.getLogger()


def test_exec(config, ssh_con):
    """
    Executes test based on configuration passed
    Args:
        config(object): Test configuration
    """
    io_info_initialize = IOInfoInitialize()
    basic_io_structure = BasicIOInfoStructure()
    io_info_initialize.initialize(basic_io_structure.initial())
    write_bucket_io_info = BucketIoInfo()
    umgmt = UserMgmt()
    ceph_conf = CephConfOp()
    rgw_service = RGWService()

    ip_and_port = s3cmd_reusable.get_rgw_ip_and_port(ssh_con)
    if config.haproxy:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        port = 5000
        ip_and_port = f"{ip}:{port}"
    
        ceph_conf = CephConfOp(ssh_con)
    rgw_service = RGWService()

    if config.d3n_feature is True:
        log.info("Enabling D3n feature on the cluster")
        data_path_cmd = f"sudo ls {config.datacache_path}"
        host_ips = utils.exec_shell_cmd("cut -f 1 /etc/hosts | cut -d ' ' -f 3")
        host_ips = host_ips.splitlines()
        log.info(f"hosts_ips: {host_ips}")
        for ip in host_ips:
            if ip.startswith("10."):
                log.info(f"ip is {ip}")
                ssh_con = utils.connect_remote(ip)
                stdin, stdout, stderr = ssh_con.exec_command(
                    "sudo netstat -nltp | grep radosgw"
                )
                netstst_op = stdout.readline().strip()
                log.info(f"netstat op on node {ip} is:{netstst_op}")
                if netstst_op:
                    log.info("Entering RGW node")
                    _, stdout, stderr = ssh_con.exec_command(data_path_cmd)
                    stderr = stderr.readline().strip()
                    if stderr:
                        # creating mount of nvme_drive
                        nvme_drive_path = config.test_ops.get("nvme_drive_path", None)
                        if nvme_drive_path:
                            log.info("Baremetal setup with nvme drive")
                            log.info(f"creating mount for nvme drive {nvme_drive_path} on a RGW node: {ip}")
                            _, stdout, stderr = ssh_con.exec_command(f"mkfs.ext4 {nvme_drive_path}")
                            stderr = stderr.readline().strip()
                            if stderr:
                                raise AssertionError("nvme mount creation failed!")


                        d3n_cache_directory_name = config.test_ops.get("d3n_cache_directory_name", None)
                        if  nvme_drive_path and d3n_cache_directory_name:
                            datacache_path = f"{nvme_drive_path}/{d3n_cache_directory_name}/"
                        else:
                            datacache_path = config.datacache_path
                        log.info(f"creating datacache path")
                        create_cmd = f"sudo mkdir {datacache_path}"
                        log.info(f"executing command:{create_cmd}")
                        _, stdout, stderr = ssh_con.exec_command(create_cmd)
                        stderr = stderr.readline().strip()
                        if stderr:
                            raise AssertionError("datacache path creation failed!")
                        
                        # Set permission for datacache path created
                        cmd = f" chmod a+rwx {nvme_drive_path} ; chmod a+rwx {datacache_path}"
                        _, stdout, stderr = ssh_con.exec_command(cmd)
                        stderr = stderr.readline().strip()
                        if stderr:
                            raise AssertionError("Set permission for datacache path failed!")
    
        rgw_service_name = utils.exec_shell_cmd("ceph orch ls | grep rgw").split(" ")[0]
        log.info(f"rgw service name is {rgw_service_name}")
        file_name = "/home/rgw_spec.yml"
        utils.exec_shell_cmd(
            f"ceph orch ls --service-name {rgw_service_name} --export > {file_name}"
        )
        op = utils.exec_shell_cmd(f"cat {file_name}")
        log.info(f"rgw spec is \n {op}")
        indent = " "
        new_content = f'extra_container_args:\n{indent} - "-v"\n{indent} - "{datacache_path}:{datacache_path}"'
        with open(file_name, "a") as f:
            f.write(new_content)
        op = utils.exec_shell_cmd(f"cat /home/rgw_spec.yml")
        log.info(f"Final rgw spec content is {op}")
        cmd = f"ceph orch apply -i {file_name}"
        utils.exec_shell_cmd(cmd)
        time.sleep(50)
        ceph_status = utils.exec_shell_cmd(cmd="sudo ceph status")
        if "HEALTH_ERR" in ceph_status:
            raise AssertionError("cluster is in HEALTH_ERR state")
        ceph_conf.set_to_ceph_conf(
            "global",
            ConfigOpts.rgw_d3n_l1_local_datacache_enabled,
            "true",
            ssh_con,
            set_to_all=True,
        )
        ceph_conf.set_to_ceph_conf(
            "global",
            ConfigOpts.rgw_d3n_l1_datacache_persistent_path,
            str(datacache_path),
            ssh_con,
            set_to_all=True,
        )
        ceph_conf.set_to_ceph_conf(
            "global",
            ConfigOpts.rgw_d3n_l1_datacache_size,
            str(config.datacache_size),
            ssh_con,
            set_to_all=True,
        )
        srv_restarted = rgw_service.restart(ssh_con)
        time.sleep(30)
        if srv_restarted is False:
            raise TestExecError("RGW service restart failed")
        else:
            log.info("RGW service restarted")

        user_info = resource_op.create_users(no_of_users_to_create=config.user_count)

        user_info = resource_op.create_users(no_of_users_to_create=config.user_count)
        s3_auth.do_auth(user_info[0], ip_and_port)
        auth = Auth(user_info[0], ssh_con, ssl=config.ssl, haproxy=config.haproxy)
        rgw_conn = auth.do_auth()
        for bc in range(config.bucket_count):
            bucket_name = utils.gen_bucket_name_from_userid(
                user_info[0]["user_id"], rand_no=bc
            )
            s3cmd_reusable.create_bucket(bucket_name)
            log.info(f"Bucket {bucket_name} created")
            s3cmd_path = "/home/cephuser/venv/bin/s3cmd"

            log.info(f"uploading objects greater than 4MB to bucket {bucket_name}")
            utils.exec_shell_cmd(f"fallocate -l 10M obj10m")
            cmd = f"{s3cmd_path} put obj10m s3://{bucket_name}/object1"
            utils.exec_shell_cmd(cmd)

            cmd = f"{s3cmd_path} get s3://{bucket_name}/object1 /dev/shm/object1_get.dat"
            utils.exec_shell_cmd(cmd)

            log.info(f"Check cache created")
            cmd = f"ls -lh {datacache_path}"
            out = utils.exec_shell_cmd(cmd)

            cmd = f"{s3cmd_path} get s3://{bucket_name}/object1 /dev/shm/object1_get2.dat"
            utils.exec_shell_cmd(cmd)