"""
test_multipart_partnumber_storage_class.py

Usage:
    test_multipart_partnumber_storage_class.py -c configs/test_multipart_partnumber_storage_class.yaml

Tracker: https://tracker.ceph.com/issues/81780

A multipart upload created with a non-default storage class that has its own
data pool must support part reads after completion. Before the fix,
get_part_obj_state() looks up the part head in the bucket's default pool, so
GET/HEAD ?partNumber returns 400 InvalidPart. GetObjectAttributes ObjectParts
goes through the same lookup and reports compressed part sizes when that
storage class uses compression.

Runs on a single-site cluster with no realm. Zone and zonegroup placement
are stored locally, and RGW loads them when the daemons restart.

Operation:
    1. Add STANDARD_IA (own data pool) and a zlib storage class to the local
       default placement, then restart RGW.
    2. Complete a multipart upload in the default storage class, in STANDARD_IA,
       and in the compressed class.
    3. GET the whole object. HEAD/GET each ?partNumber and require the uploaded
       bytes, content length, and part count.
    4. GetObjectAttributes ObjectParts sizes must match the uploaded part sizes.
    5. A partNumber past the last part must still return InvalidPart.
    6. Remove the objects, storage classes, and pools.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import traceback

sys.path.append(os.path.abspath(os.path.join(__file__, "../../../..")))

import v2.lib.resource_op as s3lib
import v2.utils.utils as utils
from botocore.exceptions import BotoCoreError, ClientError
from v2.lib.exceptions import RGWBaseException, TestExecError
from v2.lib.resource_op import Config
from v2.lib.rgw_config_opts import CephConfOp
from v2.lib.s3.auth import Auth
from v2.lib.s3.write_io_info import BasicIOInfoStructure, IOInfoInitialize
from v2.tests.s3_swift import reusable
from v2.utils.log import configure_logging
from v2.utils.test_desc import AddTestInfo

log = logging.getLogger()

TRACKER = "https://tracker.ceph.com/issues/81780"
MIB = 1024 * 1024
MIN_NON_FINAL_PART = 5 * MIB
# Pools that belong to the default RGW layout. A leftover storage class must
# not take one of these with it.
_PROTECTED_POOLS = {
    ".rgw.root",
    "default.rgw.control",
    "default.rgw.meta",
    "default.rgw.log",
    "default.rgw.buckets.index",
    "default.rgw.buckets.data",
    "default.rgw.buckets.non-ec",
}


def _loads_doc(text, cmd):
    """Parse the first JSON value in command output.

    ``ceph orch ls`` prints a list. Starting at the first ``{`` would parse the
    service object inside that list, and iterating it yields key strings.
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return parsed
    raise TestExecError(f"no JSON in output of {cmd}: {text[:400]}")


def _run(cmd):
    out = utils.exec_shell_cmd(cmd)
    if out is False:
        raise TestExecError(f"command failed: {cmd}")
    return out


def _json_cmd(cmd):
    return _loads_doc(_run(cmd), cmd)


def _placement_id(zone_doc, override):
    if override:
        return override
    raw = zone_doc.get("default_placement") or "default-placement"
    if isinstance(raw, dict):
        return raw.get("placement_id") or raw.get("name") or "default-placement"
    return raw or "default-placement"


def _zone_context(placement_override=None):
    zone_doc = _json_cmd("radosgw-admin zone get")
    zonegroup_doc = _json_cmd("radosgw-admin zonegroup get")
    placement = _placement_id(zone_doc, placement_override)
    return {
        "zone": zone_doc["name"],
        "zonegroup": zonegroup_doc["name"],
        "placement": placement,
        "zone_doc": zone_doc,
        "zonegroup_doc": zonegroup_doc,
    }


def _zone_storage_class(zone_doc, placement, storage_class):
    for entry in zone_doc.get("placement_pools", []):
        if entry.get("key") != placement:
            continue
        classes = entry.get("val", {}).get("storage_classes", {})
        return classes.get(storage_class)
    return None


def _zonegroup_has_class(zonegroup_doc, placement, storage_class):
    for target in zonegroup_doc.get("placement_targets", []):
        if target.get("name") != placement:
            continue
        classes = target.get("storage_classes") or []
        if isinstance(classes, dict):
            return storage_class in classes
        return storage_class in classes
    return False


def _storage_class_specs(config):
    specs = [
        {
            "name": config.storage_class or "STANDARD_IA",
            "pool": config.pool_name or "standard.ia",
            "compression": "none",
        }
    ]
    compression = (config.test_ops.get("compression") or "zlib").strip()
    if compression.lower() != "none":
        specs.append(
            {
                "name": config.second_storage_class or "STANDARD_IA_ZLIB",
                "pool": config.second_pool_name or "standard.ia.zlib",
                "compression": compression,
            }
        )
    names = [spec["name"] for spec in specs]
    pools = [spec["pool"] for spec in specs]
    if len(set(names)) != len(names) or len(set(pools)) != len(pools):
        raise TestExecError(f"storage class names and pools must be unique: {specs}")
    return specs


def _part_sizes(config):
    raw = config.test_ops.get("part_sizes_mb", [5, 5, 1])
    try:
        sizes = [int(item) * MIB for item in raw]
    except (TypeError, ValueError) as exc:
        raise TestExecError(f"part_sizes_mb must be a list of integers: {raw}") from exc
    if len(sizes) < 2:
        raise TestExecError("a multipart object needs at least 2 parts")
    too_small = [size for size in sizes[:-1] if size < MIN_NON_FINAL_PART]
    if too_small or sizes[-1] <= 0:
        raise TestExecError(
            "each non-final part must be at least 5MiB and the final part "
            f"must be positive; got {sizes}"
        )
    return sizes


def _require_single_site_without_realm(ctx):
    """Placement edits on a realm-less cluster are local and need an RGW restart."""
    realm_doc = _json_cmd("radosgw-admin realm list")
    realms = realm_doc.get("realms") or []
    default_info = realm_doc.get("default_info") or ""
    realm_id = ctx["zonegroup_doc"].get("realm_id") or ""
    zones = ctx["zonegroup_doc"].get("zones") or []
    zone_names = []
    for zone in zones:
        if isinstance(zone, dict):
            zone_names.append(zone.get("name", ""))
        else:
            zone_names.append(zone)
    if realms or default_info or realm_id:
        raise TestExecError(
            "This test runs on a single-site cluster with no realm. "
            "Placement is written to the local zone and zonegroup, then RGW "
            "is restarted so the daemons reload that config. "
            f"Found realms={realms}, default_info={default_info!r}, "
            f"zonegroup realm_id={realm_id!r}."
        )
    if len(zone_names) != 1:
        raise TestExecError(
            "This test runs on a single-site cluster with one zone. "
            f"Zonegroup {ctx['zonegroup']} has zones {zone_names}."
        )
    log.info(
        "single-site cluster with no realm (zone=%s zonegroup=%s placement=%s)",
        ctx["zone"],
        ctx["zonegroup"],
        ctx["placement"],
    )


def _restart_rgw_daemons():
    log.info("restarting RGW so it reloads the local zone and zonegroup")
    listed = _json_cmd("ceph orch ls --service-type rgw -f json")
    if isinstance(listed, dict):
        listed = [listed]
    if not listed:
        raise TestExecError("no RGW services found to restart")
    for service in listed:
        name = service.get("service_name")
        if not name:
            raise TestExecError(f"RGW service is missing service_name: {service}")
        restarted = utils.exec_shell_cmd(f"ceph orch restart {name}")
        if restarted is False:
            raise TestExecError(f"failed to restart {name}")
    utils.rgw_daemons_status()


def _pool_names():
    return set(_run("ceph osd pool ls").split())


def _delete_pool(pool_name):
    if pool_name not in _pool_names():
        return
    log.info("removing pool %s", pool_name)
    if utils.exec_shell_cmd("ceph config set mon mon_allow_pool_delete true") is False:
        raise TestExecError("failed to allow pool deletion")
    try:
        _run(
            f"ceph osd pool rm {pool_name} {pool_name} "
            "--yes-i-really-really-mean-it"
        )
    finally:
        utils.exec_shell_cmd("ceph config set mon mon_allow_pool_delete false")


def _remove_storage_class_if_present(spec, ctx):
    """Drop a leftover storage class so this run can create it again."""
    existing = _zone_storage_class(ctx["zone_doc"], ctx["placement"], spec["name"])
    on_zonegroup = _zonegroup_has_class(
        ctx["zonegroup_doc"], ctx["placement"], spec["name"]
    )
    pools_to_remove = set()
    if spec["pool"] in _pool_names():
        pools_to_remove.add(spec["pool"])
    if not existing and not on_zonegroup:
        for pool_name in pools_to_remove:
            _delete_pool(pool_name)
        return

    log.info(
        "storage class %s already exists on placement %s; removing it before this run",
        spec["name"],
        ctx["placement"],
    )
    if existing:
        data_pool = existing.get("data_pool") if isinstance(existing, dict) else None
        if data_pool in _PROTECTED_POOLS:
            log.info("leaving built-in pool %s in place", data_pool)
        elif data_pool:
            pools_to_remove.add(data_pool)
        _run(
            "radosgw-admin zone placement rm "
            f"--rgw-zone {ctx['zone']} "
            f"--placement-id {ctx['placement']} "
            f"--storage-class {spec['name']}"
        )
    if on_zonegroup:
        _run(
            "radosgw-admin zonegroup placement rm "
            f"--rgw-zonegroup {ctx['zonegroup']} "
            f"--placement-id {ctx['placement']} "
            f"--storage-class {spec['name']}"
        )
    for pool_name in pools_to_remove:
        _delete_pool(pool_name)


def _setup_storage_classes(specs, placement_override, ready):
    ctx = _zone_context(placement_override)
    _require_single_site_without_realm(ctx)
    log.info(
        "placement context zone=%s zonegroup=%s placement=%s",
        ctx["zone"],
        ctx["zonegroup"],
        ctx["placement"],
    )
    for spec in specs:
        _remove_storage_class_if_present(spec, ctx)
        ctx = _zone_context(placement_override)
    for spec in specs:
        _run(f"ceph osd pool create {spec['pool']}")
        ready.append(spec)
        _run(f"ceph osd pool application enable {spec['pool']} rgw")
        _run(
            "radosgw-admin zonegroup placement add "
            f"--rgw-zonegroup {ctx['zonegroup']} "
            f"--placement-id {ctx['placement']} "
            f"--storage-class {spec['name']}"
        )
        _run(
            "radosgw-admin zone placement add "
            f"--rgw-zone {ctx['zone']} "
            f"--placement-id {ctx['placement']} "
            f"--storage-class {spec['name']} "
            f"--data-pool {spec['pool']} "
            f"--compression {spec['compression']}"
        )
        ctx = _zone_context(placement_override)
    _restart_rgw_daemons()
    ctx = _zone_context(placement_override)
    for spec in ready:
        info = _zone_storage_class(ctx["zone_doc"], ctx["placement"], spec["name"])
        if not isinstance(info, dict):
            raise TestExecError(
                f"storage class {spec['name']} is missing after RGW restart"
            )
        data_pool = info.get("data_pool")
        if data_pool != spec["pool"]:
            raise TestExecError(
                f"storage class {spec['name']} data pool is {data_pool}, "
                f"expected {spec['pool']}"
            )
        actual = info.get("compression_type") or "none"
        expected = spec["compression"] or "none"
        if actual != expected:
            raise TestExecError(
                f"storage class {spec['name']} compression is {actual}, "
                f"expected {expected}"
            )
        log.info(
            "storage class %s is ready (pool=%s compression=%s)",
            spec["name"],
            spec["pool"],
            actual,
        )


def _teardown_storage_classes(specs, placement_override):
    if not specs:
        return None
    problems = []
    try:
        ctx = _zone_context(placement_override)
    except TestExecError as exc:
        log.error("teardown could not read zone info: %s", exc)
        ctx = None
        problems.append(str(exc))
    if ctx:
        for spec in specs:
            for cmd in (
                "radosgw-admin zone placement rm "
                f"--rgw-zone {ctx['zone']} "
                f"--placement-id {ctx['placement']} "
                f"--storage-class {spec['name']}",
                "radosgw-admin zonegroup placement rm "
                f"--rgw-zonegroup {ctx['zonegroup']} "
                f"--placement-id {ctx['placement']} "
                f"--storage-class {spec['name']}",
            ):
                if utils.exec_shell_cmd(cmd) is False:
                    problems.append(f"command failed: {cmd}")
    if utils.exec_shell_cmd("ceph config set mon mon_allow_pool_delete true") is False:
        problems.append("failed to allow pool deletion")
    try:
        for spec in specs:
            cmd = (
                f"ceph osd pool rm {spec['pool']} {spec['pool']} "
                "--yes-i-really-really-mean-it"
            )
            if utils.exec_shell_cmd(cmd) is False:
                problems.append(f"command failed: {cmd}")
    finally:
        utils.exec_shell_cmd("ceph config set mon mon_allow_pool_delete false")
    try:
        _restart_rgw_daemons()
    except TestExecError as exc:
        problems.append(f"RGW restart after teardown failed: {exc}")
    if problems:
        log.error("storage class teardown problems: %s", problems)
        return "; ".join(problems)
    log.info("removed storage classes and data pools")
    return None


def _client_error(exc):
    error = exc.response.get("Error", {})
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status, error.get("Code", ""), error.get("Message", str(exc))


def _upload_multipart(client, bucket, key, part_sizes, storage_class):
    kwargs = {"Bucket": bucket, "Key": key}
    if storage_class:
        kwargs["StorageClass"] = storage_class
    created = client.create_multipart_upload(**kwargs)
    upload_id = created["UploadId"]
    log.info("upload %s id=%s storage_class=%s", key, upload_id, storage_class)
    parts = []
    bodies = []
    try:
        for number, size in enumerate(part_sizes, 1):
            data = bytes([number]) * size
            response = client.upload_part(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=number,
                Body=data,
            )
            parts.append({"PartNumber": number, "ETag": response["ETag"]})
            bodies.append(data)
            log.info("uploaded %s part %s (%s bytes)", key, number, size)
        client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        try:
            client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        except ClientError as exc:
            log.warning("abort of %s failed: %s", upload_id, _client_error(exc))
        raise
    return bodies


def _object_stat(bucket, key):
    cmd = f"radosgw-admin object stat --bucket={bucket} --object={key}"
    return _json_cmd(cmd)


def _json_has_value(node, needle):
    if isinstance(node, dict):
        return any(_json_has_value(value, needle) for value in node.values())
    if isinstance(node, list):
        return any(_json_has_value(value, needle) for value in node)
    return node == needle


def _compressed_byte_count(compression):
    total = 0
    found = False
    for block in compression.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for field in ("len", "length", "compressed_size"):
            if field in block:
                total += int(block[field])
                found = True
                break
    if not found:
        return None
    return total


def _check_placement(stat, label, storage_class, pool, compression):
    failures = []
    if storage_class and not _json_has_value(stat, storage_class):
        failures.append(
            f"{label}: object stat does not record storage class {storage_class}"
        )
    if pool and not _json_has_value(stat, pool):
        failures.append(
            f"{label}: object stat does not record data pool {pool}. "
            "Part heads for this upload must live in the storage class pool."
        )
    recorded = (stat.get("compression") or {}).get("compression_type") or ""
    if compression:
        if recorded != compression:
            failures.append(
                f"{label}: object compression_type is {recorded or 'unset'}, "
                f"expected {compression}"
            )
        logical = stat.get("size")
        compressed = _compressed_byte_count(stat.get("compression") or {})
        log.info(
            "%s object stat size=%s compressed_blocks=%s compression=%s",
            label,
            logical,
            compressed,
            recorded,
        )
        if compressed is None:
            failures.append(
                f"{label}: object stat has no compression block lengths, so "
                "compressed ObjectParts sizes cannot be distinguished"
            )
        elif logical is not None and compressed >= int(logical):
            failures.append(
                f"{label}: compressed size {compressed} is not smaller than "
                f"logical size {logical}"
            )
    elif recorded not in ("", "none"):
        failures.append(f"{label}: expected an uncompressed object, found {recorded}")
    return failures


def _check_part_reads(client, bucket, key, bodies, label):
    failures = []
    part_count = len(bodies)
    for number, data in enumerate(bodies, 1):
        try:
            head = client.head_object(Bucket=bucket, Key=key, PartNumber=number)
        except ClientError as exc:
            status, code, message = _client_error(exc)
            failures.append(
                f"{label}: HEAD partNumber={number} failed "
                f"({status} {code}: {message}). {TRACKER} requires this read to "
                "succeed for a multipart object in a non-default storage class."
            )
            continue
        if head.get("ContentLength") != len(data):
            failures.append(
                f"{label}: HEAD part {number} ContentLength "
                f"{head.get('ContentLength')} != {len(data)}"
            )
        if head.get("PartsCount") != part_count:
            failures.append(
                f"{label}: HEAD part {number} PartsCount {head.get('PartsCount')} "
                f"!= {part_count}"
            )
        try:
            got = client.get_object(Bucket=bucket, Key=key, PartNumber=number)
        except ClientError as exc:
            status, code, message = _client_error(exc)
            failures.append(
                f"{label}: GET partNumber={number} failed "
                f"({status} {code}: {message}). {TRACKER} requires this read to "
                "succeed for a multipart object in a non-default storage class."
            )
            continue
        payload = got["Body"].read()
        if got.get("ContentLength") != len(data) or payload != data:
            failures.append(
                f"{label}: GET part {number} returned {len(payload)} bytes, "
                f"expected {len(data)}"
            )
        if got.get("PartsCount") != part_count:
            failures.append(
                f"{label}: GET part {number} PartsCount {got.get('PartsCount')} "
                f"!= {part_count}"
            )
    try:
        client.head_object(Bucket=bucket, Key=key, PartNumber=part_count + 1)
        failures.append(
            f"{label}: HEAD partNumber={part_count + 1} succeeded; a missing "
            "part must return InvalidPart"
        )
    except ClientError as exc:
        status, code, message = _client_error(exc)
        if code != "InvalidPart":
            failures.append(
                f"{label}: HEAD partNumber={part_count + 1} returned "
                f"{status} {code}: {message}; expected InvalidPart"
            )
        else:
            log.info("%s missing partNumber returned InvalidPart", label)
    return failures


def _check_object_attributes(client, bucket, key, bodies, label, storage_class):
    failures = []
    logical = sum(len(data) for data in bodies)
    try:
        attributes = client.get_object_attributes(
            Bucket=bucket,
            Key=key,
            ObjectAttributes=["ETag", "ObjectSize", "ObjectParts", "StorageClass"],
        )
    except ClientError as exc:
        status, code, message = _client_error(exc)
        return [
            f"{label}: GetObjectAttributes failed ({status} {code}: {message}). "
            f"{TRACKER} reads part info through the same part-head lookup."
        ]
    log.info("%s GetObjectAttributes: %s", label, attributes)
    if attributes.get("ObjectSize") != logical:
        failures.append(
            f"{label}: GetObjectAttributes ObjectSize {attributes.get('ObjectSize')} "
            f"!= logical size {logical}"
        )
    if storage_class and attributes.get("StorageClass") != storage_class:
        failures.append(
            f"{label}: GetObjectAttributes StorageClass "
            f"{attributes.get('StorageClass')} != {storage_class}"
        )
    object_parts = attributes.get("ObjectParts") or {}
    total = object_parts.get("TotalPartsCount", object_parts.get("PartsCount"))
    if total != len(bodies):
        failures.append(
            f"{label}: ObjectParts TotalPartsCount {total} != {len(bodies)}"
        )
    by_number = {}
    for part in object_parts.get("Parts") or []:
        by_number[int(part["PartNumber"])] = part
    for number, data in enumerate(bodies, 1):
        part = by_number.get(number)
        if part is None:
            failures.append(f"{label}: ObjectParts is missing part {number}")
            continue
        size = int(part.get("Size", -1))
        if size != len(data):
            failures.append(
                f"{label}: ObjectParts part {number} Size {size} != uploaded "
                f"size {len(data)}. {TRACKER}: ObjectParts must report the "
                "logical part size, including uploads in a compressed storage class."
            )
    return failures


def _run_case(client, bucket, key, part_sizes, storage_class, pool, compression):
    label = storage_class or "STANDARD"
    log.info("CASE %s (%s)", label, TRACKER)
    try:
        bodies = _upload_multipart(client, bucket, key, part_sizes, storage_class)
    except ClientError as exc:
        status, code, message = _client_error(exc)
        return [f"{label}: multipart upload failed ({status} {code}: {message})"]

    failures = []
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        status, code, message = _client_error(exc)
        failures.append(f"{label}: HEAD object failed ({status} {code}: {message})")
        head = {}
    if storage_class and head.get("StorageClass") != storage_class:
        failures.append(
            f"{label}: HEAD StorageClass {head.get('StorageClass')} != {storage_class}"
        )
    logical = b"".join(bodies)
    try:
        whole = client.get_object(Bucket=bucket, Key=key)
        payload = whole["Body"].read()
        if payload != logical or whole.get("ContentLength") != len(logical):
            failures.append(
                f"{label}: whole-object GET returned {len(payload)} bytes, "
                f"expected {len(logical)}"
            )
    except ClientError as exc:
        status, code, message = _client_error(exc)
        failures.append(
            f"{label}: whole-object GET failed ({status} {code}: {message})"
        )

    try:
        stat = _object_stat(bucket, key)
    except TestExecError as exc:
        failures.append(f"{label}: object stat failed: {exc}")
    else:
        failures.extend(
            _check_placement(stat, label, storage_class, pool, compression)
        )
    failures.extend(_check_part_reads(client, bucket, key, bodies, label))
    failures.extend(
        _check_object_attributes(
            client, bucket, key, bodies, label, storage_class
        )
    )
    if failures:
        log.error("CASE %s failed: %s", label, failures)
    else:
        log.info("CASE %s passed", label)
    return failures


def _bucket_region():
    """Location constraint RGW will accept on this single-site zonegroup."""
    zonegroup = _json_cmd("radosgw-admin zonegroup get")
    return zonegroup.get("api_name") or zonegroup.get("name") or "default"


def _safe_bucket_name(user_id):
    raw = utils.gen_bucket_name_from_userid(user_id, rand_no=0).lower()
    cleaned = "".join(char if char.isalnum() or char in ".-" else "-" for char in raw)
    cleaned = cleaned.strip(".-")
    if len(cleaned) < 3:
        cleaned = f"mpu-sc-{cleaned}"
    return cleaned[:63]


def _endpoint_is_up(endpoint_url):
    try:
        completed = subprocess.run(
            [
                "curl",
                "-k",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "5",
                endpoint_url,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info("endpoint probe failed: %s", exc)
        return False
    code = (completed.stdout or "").strip()
    log.info("endpoint %s probe http=%s", endpoint_url, code or "none")
    return code not in ("", "000")


def _wait_for_endpoint(endpoint_url, attempts=12, delay=5):
    for attempt in range(1, attempts + 1):
        if _endpoint_is_up(endpoint_url):
            return
        log.info(
            "RGW endpoint %s is not accepting connections (attempt %s/%s)",
            endpoint_url,
            attempt,
            attempts,
        )
        time.sleep(delay)
    raise TestExecError(
        f"RGW endpoint {endpoint_url} did not accept connections after restart"
    )


def _create_bucket(auth, bucket_name, zonegroup_region):
    """Create a bucket and return the client that succeeded.

    ``reusable.create_bucket`` hides the boto error. After an RGW restart the
    gateway can also refuse the first calls, and a location constraint of
    ``default`` is rejected when it does not match the zonegroup.
    """
    # us-east-1 sends no location constraint, which single-site RGW accepts.
    # The zonegroup name is the second try when a constraint is required.
    regions = ["us-east-1"]
    if zonegroup_region and zonegroup_region not in regions:
        regions.append(zonegroup_region)
    last_error = "no response"
    for region in regions:
        client = auth.do_auth_using_client(region_name=region)
        for attempt in range(1, 7):
            try:
                if region == "us-east-1":
                    client.create_bucket(Bucket=bucket_name)
                else:
                    client.create_bucket(
                        Bucket=bucket_name,
                        CreateBucketConfiguration={"LocationConstraint": region},
                    )
                log.info("created bucket %s with region %s", bucket_name, region)
                return client
            except ClientError as exc:
                status, code, message = _client_error(exc)
                last_error = f"{status} {code}: {message}"
                if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    log.info("bucket %s already exists", bucket_name)
                    return client
                log.warning(
                    "create bucket %s attempt %s (%s): %s",
                    bucket_name,
                    attempt,
                    region,
                    last_error,
                )
                if code in (
                    "InvalidLocationConstraint",
                    "IllegalLocationConstraintException",
                ):
                    break
            except BotoCoreError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "create bucket %s attempt %s (%s): %s",
                    bucket_name,
                    attempt,
                    region,
                    last_error,
                )
            time.sleep(5)
    raise TestExecError(f"bucket creation failed for {bucket_name}: {last_error}")


def _delete_bucket(client, bucket):
    try:
        uploads = client.list_multipart_uploads(Bucket=bucket)
        for upload in uploads.get("Uploads") or []:
            client.abort_multipart_upload(
                Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"]
            )
        listed = client.list_objects_v2(Bucket=bucket)
        for obj in listed.get("Contents") or []:
            client.delete_object(Bucket=bucket, Key=obj["Key"])
        client.delete_bucket(Bucket=bucket)
        log.info("deleted bucket %s", bucket)
    except ClientError as exc:
        log.warning("cleanup of bucket %s failed: %s", bucket, _client_error(exc))


def _exercise(config, ssh_con, part_sizes, specs):
    users = s3lib.create_users(config.user_count or 1)
    region = _bucket_region()
    failures = []
    for each_user in users:
        client = None
        bucket_name = None
        try:
            auth = Auth(
                each_user, ssh_con, ssl=config.ssl, haproxy=config.haproxy
            )
            _wait_for_endpoint(auth.endpoint_url)
            bucket_name = _safe_bucket_name(each_user["user_id"])
            client = _create_bucket(auth, bucket_name, region)
            if config.test_ops.get("verify_default_storage_class", True):
                failures.extend(
                    _run_case(
                        client,
                        bucket_name,
                        "mpu-default",
                        part_sizes,
                        None,
                        None,
                        None,
                    )
                )
            for spec in specs:
                key = "mpu-" + spec["name"].lower().replace("_", "-")
                compression = spec["compression"]
                if compression == "none":
                    compression = None
                failures.extend(
                    _run_case(
                        client,
                        bucket_name,
                        key,
                        part_sizes,
                        spec["name"],
                        spec["pool"],
                        compression,
                    )
                )
        finally:
            if client and bucket_name:
                _delete_bucket(client, bucket_name)
            if config.user_remove:
                reusable.remove_user(each_user)
    if failures:
        detail = "\n".join(failures)
        raise TestExecError(
            f"multipart partNumber storage-class validation failed ({TRACKER}):\n"
            f"{detail}"
        )


def test_exec(config, ssh_con):
    io_info_initialize = IOInfoInitialize()
    basic_io_structure = BasicIOInfoStructure()
    io_info_initialize.initialize(basic_io_structure.initial())

    specs = _storage_class_specs(config)
    part_sizes = _part_sizes(config)
    placement = config.test_ops.get("placement_id")
    ready = []
    try:
        _setup_storage_classes(specs, placement, ready)
        _exercise(config, ssh_con, part_sizes, ready)
    finally:
        teardown_error = _teardown_storage_classes(ready, placement)
    if teardown_error:
        raise TestExecError(f"storage class teardown failed: {teardown_error}")


if __name__ == "__main__":
    test_info = AddTestInfo(
        "multipart partNumber reads on a non-default storage class",
        id="81780",
    )
    test_info.started_info()
    try:
        parser = argparse.ArgumentParser(description="RGW multipart storage class")
        parser.add_argument("-c", dest="config", help="RGW Test yaml configuration")
        parser.add_argument(
            "-log_level",
            dest="log_level",
            help="Set Log Level [DEBUG, INFO, WARNING, ERROR, CRITICAL]",
            default="info",
        )
        parser.add_argument(
            "--rgw-node", dest="rgw_node", help="RGW Node", default="127.0.0.1"
        )
        args = parser.parse_args()
        yaml_file = args.config
        ssh_con = None
        if args.rgw_node != "127.0.0.1":
            ssh_con = utils.connect_remote(args.rgw_node)
        log_f_name = os.path.basename(os.path.splitext(yaml_file)[0])
        configure_logging(f_name=log_f_name, set_level=args.log_level.upper())
        config = Config(yaml_file)
        CephConfOp(ssh_con)
        config.read(ssh_con)
        test_exec(config, ssh_con)
        test_info.success_status("test passed")
        sys.exit(0)
    except (RGWBaseException, Exception) as exc:
        log.error(exc)
        log.error(traceback.format_exc())
        test_info.failed_status("test failed")
        sys.exit(1)
