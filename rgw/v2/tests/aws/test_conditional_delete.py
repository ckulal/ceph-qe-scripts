"""
Usage: test_conditional_delete.py -c <input_yaml>
Polarion ID : 
<input_yaml>
    Note: Following yaml can be used
    configs/test_cond_delete_etag.yaml
    configs/test_cond_delete_etag_prefix.yaml
    configs/test_cond_delete_stale_attrs.yaml
    configs/test_cond_delete_same_second.yaml
    configs/test_cond_delete_missing_object.yaml
    configs/test_cond_delete_delete_marker.yaml
    configs/test_cond_delete_wrong_version.yaml
    configs/test_cond_delete_null_version.yaml
    configs/test_cond_delete_size_visible.yaml
    configs/test_cond_delete_partial_match.yaml
    configs/test_cond_delete_multi_same_key.yaml
    configs/test_cond_delete_lmt.yaml
    configs/test_cond_delete_size.yaml
    configs/test_cond_delete_correct_etag_and_incorrcet_size.yaml
    configs/test_cond_delete_correct_size_and_incorrcet_lmt.yaml
    configs/test_cond_delete_incorrect_etag_and_correct_lmt.yaml
    configs/test_cond_delete_all_correct.yaml
    configs/test_cond_put_if_none_match.yaml
    configs/test_cond_put_if_match.yaml
    configs/test_cond_put_if_none_match_ver.yaml
    configs/test_cond_put_if_match_ver.yaml
    configs/test_cond_put_multipart_if_none_match.yaml
    configs/test_cond_put_multipart_if_match.yaml

Operation:
Conditional delete scenarios. A failed precondition is HTTP 412 in the
aws --debug log. The CLI prints "argument of type 'NoneType' is not a
container or iterable" instead of that status.
test_cond_delete_etag_prefix.yaml checks that If-Match is an exact ETag:
<etag>EXTRA and "<etag>EXTRA" return 412 on the unversioned, current-version,
and versionId delete paths.
test_cond_delete_stale_attrs.yaml reads ETag, size, and mtime, overwrites the
object, then deletes with those old values. 204 after that overwrite is a
failure on unversioned and versioned buckets. The mtime-only repeat covers
the unversioned RADOS recheck and the versioned delete-marker path.
test_cond_delete_same_second.yaml overwrites an object twice within one
second, then deletes with the first generation's LastModified. Normal
requests compare that time with nanoseconds cleared, so both generations
can match. 204, or loss of the second generation, is a failure for an
unversioned object, for the current version on a versioned bucket, and
for a delete of that second version by versionId.
test_cond_delete_missing_object.yaml deletes a key that was never written,
on an unversioned bucket and on a versioned bucket, with a wrong ETag, a
wrong size, and a wrong mtime. Unversioned RGW returns not found before it
evaluates the conditions, and the S3 response turns that into 204. Versioned
deletes with no version id still evaluate size and mtime against an empty
state, so a wrong size or time can return 412, while If-Match still becomes
204. 204 is a failure: it means both "deleted the matching object" and
"there was nothing to delete."
test_cond_delete_delete_marker.yaml deletes an object once so the current
version is a delete marker, then conditional-deletes again. If-Match * can
succeed and write another delete marker. A concrete ETag must be 412, because
a delete marker has no ETag. Size 0 can match the marker and write another
one. A wrong mtime must be 412. Stacking delete markers is a failure. A
normal GET must still return 404, and x-amz-delete-marker: true is set only
when a marker was actually written.
test_cond_delete_wrong_version.yaml writes version A, then version B. Delete
with versionId of A and with B's ETag, size, and mtime must return 412, and
B must remain current. Delete with no version id and A's ETag must return
412, and A must still exist. Deleting the other generation is the data-loss
case.
test_cond_delete_null_version.yaml enables versioning, writes an object,
suspends versioning, and overwrites it so the null version is current.
Delete with no version id, and delete with versionId=null, each with the
null version's ETag and with the older version's ETag. The no-id delete
must create a delete marker only when the current null version matches.
versionId=null must permanently remove that null version only when its own
metadata matches.
test_cond_delete_size_visible.yaml deletes an empty object with size 0, and
deletes another empty object with the size header omitted. Both must succeed:
size 0 matches the empty object, and an omitted size header requests no size
condition. The same two deletes are repeated for a multipart object and a
compressed object. When HEAD Content-Length and the raw stored size differ,
both sizes are sent. The Content-Length delete must succeed. A stored size
that is not the Content-Length must return 412 and leave the object in place.
Matching the wrong size field either blocks a valid delete or deletes an
object whose visible size did not match.
test_cond_delete_partial_match.yaml sends a correct ETag with a wrong size,
then a correct size with a wrong mtime. Any one mismatch must return 412
and leave the object in place, on an unversioned bucket and on the current
version of a versioned bucket. A delete when only part of the set matched
is a failure.
test_cond_delete_multi_same_key.yaml sends one DeleteObjects body that lists
the same key twice on a versioned bucket: a bad ETag, then the real ETag.
Earlier entries skip the OLH update, and every entry is checked against the
original current version. The bad entry must be PreconditionFailed and the
good entry must delete exactly once. Two delete markers, or a current
version that does not match the index, is a failure. With Quiet set, the
success may be hidden, but PreconditionFailed must still be returned. A
misspelled Etag element must not delete the object with no condition.
"""
import argparse
import json
import logging
import os
import random
import re
import shlex
import sys
import time
import traceback

sys.path.append(os.path.abspath(os.path.join(__file__, "../../../..")))


from v2.lib import resource_op
from v2.lib.aws import auth as aws_auth
from v2.lib.aws.resource_op import AWS
from v2.lib.exceptions import RGWBaseException, TestExecError
from v2.lib.rgw_config_opts import CephConfOp, ConfigOpts
from v2.lib.s3.write_io_info import BasicIOInfoStructure, IOInfoInitialize
from v2.tests.aws import reusable as aws_reusable
from v2.tests.s3_swift import reusable as s3_reusable
from v2.utils import utils
from v2.utils.log import configure_logging
from v2.utils.test_desc import AddTestInfo
from v2.utils.utils import RGWService

log = logging.getLogger(__name__)
TEST_DATA_PATH = None
ETAG_PREFIX_SUFFIX = "EXTRA"
SAME_SECOND_ATTEMPTS = 15


def _load_object_versions(cli_aws, bucket_name, endpoint):
    raw = aws_reusable.list_object_versions(cli_aws, bucket_name, endpoint)
    if not raw:
        raise AssertionError(f"list-object-versions failed for {bucket_name}")
    return json.loads(raw)


def _versions_for(version_list, object_name, version_id=None):
    versions = version_list.get("Versions") or []
    matched = [v for v in versions if v.get("Key") == object_name]
    if version_id is not None:
        matched = [v for v in matched if v.get("VersionId") == version_id]
    return matched


def _latest_version(version_list, object_name):
    versions = _versions_for(version_list, object_name)
    latest = [v for v in versions if v.get("IsLatest")]
    if latest:
        return latest[0]
    return versions[0] if versions else None


def _delete_markers_for(version_list, object_name):
    markers = version_list.get("DeleteMarkers") or []
    return [m for m in markers if m.get("Key") == object_name]


def _etag_from_version(version):
    etag = (version.get("ETag") or "").strip('"')
    if not etag:
        raise AssertionError(f"object version has no ETag: {version}")
    return etag


def _precondition_failed(response):
    if not isinstance(response, str) or not response:
        return False
    lowered = response.lower()
    return (
        "preconditionfailed" in lowered
        or "precondition failed" in lowered
        or "pre-condition" in lowered
    )


_STATUS_412 = re.compile(
    r"(?i)(?:http/1\.[01][\"']?\s+412\b|status(?:_code|\s+code)?[\"'\s:=]+412\b)"
)


def _debug_status_412(text):
    """True when aws --debug logged an HTTP 412 status."""
    return isinstance(text, str) and bool(_STATUS_412.search(text))


def _response_is_412(text):
    """
    The CLI delete of a failed precondition prints
    "argument of type 'NoneType' is not a container or iterable"
    instead of 412. The status is in the --debug log.
    """
    return _debug_status_412(text) or _precondition_failed(text)


def _debug_excerpt(text):
    if not isinstance(text, str) or not text:
        return repr(text)
    lowered = text.lower()
    for needle in ("412", "precondition", "nonetype"):
        index = lowered.find(needle)
        if index >= 0:
            start = max(0, index - 80)
            return text[start : start + 240]
    return text[:240]


def _delete_succeeded(response):
    if not isinstance(response, str):
        return False
    if _response_is_412(response):
        return False
    lowered = response.lower()
    if "error occurred" in lowered or "nonetype" in lowered:
        return False
    return True


def _object_not_found(response):
    if not isinstance(response, str) or not response:
        return False
    lowered = response.lower()
    return "nosuchkey" in lowered or "not found" in lowered


def _reject_etag_prefix(
    cli_aws, bucket_name, object_name, endpoint, etag, version_id, path_name
):
    """If-Match of etag+EXTRA must be 412 and must not remove the object."""
    for form, if_match in (
        ("unquoted", f"{etag}{ETAG_PREFIX_SUFFIX}"),
        ("quoted", f'"{etag}{ETAG_PREFIX_SUFFIX}"'),
    ):
        log.info(
            "%s delete If-Match %s %r version_id=%s",
            path_name,
            form,
            if_match,
            version_id,
        )
        out = _conditional_delete_debug(
            cli_aws,
            bucket_name,
            object_name,
            endpoint,
            version_id=version_id,
            etag=if_match,
        )
        version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
        if version_id:
            version_kept = bool(_versions_for(version_list, object_name, version_id))
        else:
            version_kept = bool(_versions_for(version_list, object_name))
        markers = _delete_markers_for(version_list, object_name)
        if not _response_is_412(out) or not version_kept or markers:
            raise AssertionError(
                f"{path_name} If-Match {form} {if_match!r} must return 412 "
                f"and leave the object in place. "
                f"debug_412={_response_is_412(out)} "
                f"debug_excerpt={_debug_excerpt(out)!r} "
                f"version_kept={version_kept} delete_markers={len(markers)}. "
                "If-Match is an exact ETag compare. etag+EXTRA matches only "
                "when the check uses etag.length() characters, which is the "
                "unversioned and current-version bug. Delete with versionId "
                "already rejects that value."
            )


def _delete_exact_etag(
    cli_aws, bucket_name, object_name, endpoint, etag, version_id, path_name
):
    log.info(
        "%s delete If-Match exact %s version_id=%s", path_name, etag, version_id
    )
    out = _conditional_delete_debug(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        version_id=version_id,
        etag=etag,
    )
    if not _delete_succeeded(out):
        raise AssertionError(
            f"{path_name} exact If-Match {etag!r} should delete the object, "
            f"debug_412={_response_is_412(out)} "
            f"debug_excerpt={_debug_excerpt(out)!r}"
        )
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    if version_id:
        if _versions_for(version_list, object_name, version_id):
            raise AssertionError(
                f"{path_name} exact If-Match left version {version_id} in place"
            )
        return
    if path_name == "current-version":
        if not _delete_markers_for(version_list, object_name):
            raise AssertionError(
                f"{path_name} exact If-Match did not create a delete marker: {out!r}"
            )
        if not _versions_for(version_list, object_name):
            raise AssertionError(
                f"{path_name} exact If-Match removed the object version instead "
                "of creating a delete marker"
            )
        return
    if _versions_for(version_list, object_name) or _delete_markers_for(
        version_list, object_name
    ):
        raise AssertionError(
            f"{path_name} exact If-Match left object {object_name} in the bucket"
        )


def _test_etag_prefix_if_match(cli_aws, bucket_name, object_name, endpoint):
    """
    If-Match is an exact ETag compare on every delete path.

    For an object whose ETag is <etag>, If-Match <etag>EXTRA and
    "<etag>EXTRA" must return 412 and leave the object in place:
      - unversioned delete
      - current-version delete (versioned bucket, no versionId)
      - delete with versionId
    An exact If-Match still deletes, so a server that rejects every
    If-Match does not pass.
    """
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    current = _latest_version(version_list, object_name)
    if current is None:
        raise AssertionError(f"{object_name} missing before prefix If-Match checks")
    etag = _etag_from_version(current)
    log.info(
        "unversioned object %s ETag=%s; If-Match %s%s and \"%s%s\" must be 412",
        object_name,
        etag,
        etag,
        ETAG_PREFIX_SUFFIX,
        etag,
        ETAG_PREFIX_SUFFIX,
    )
    _reject_etag_prefix(
        cli_aws, bucket_name, object_name, endpoint, etag, None, "unversioned"
    )
    _delete_exact_etag(
        cli_aws, bucket_name, object_name, endpoint, etag, None, "unversioned"
    )

    log.info("enable versioning for current-version and versionId If-Match paths")
    aws_reusable.put_get_bucket_versioning(cli_aws, bucket_name, endpoint)
    ver_object = "testobj-ver"
    aws_reusable.put_object(
        cli_aws, bucket_name, ver_object, endpoint, body=object_name
    )
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    ver = _latest_version(version_list, ver_object)
    if ver is None:
        raise AssertionError(f"{ver_object} missing after versioned put")
    ver_etag = _etag_from_version(ver)
    ver_id = ver.get("VersionId")
    if not ver_id or ver_id == "null":
        raise AssertionError(
            f"versioned put of {ver_object} did not return a version id: {ver_id}"
        )
    log.info(
        "versioned object %s ETag=%s VersionId=%s", ver_object, ver_etag, ver_id
    )
    _reject_etag_prefix(
        cli_aws,
        bucket_name,
        ver_object,
        endpoint,
        ver_etag,
        None,
        "current-version",
    )
    _reject_etag_prefix(
        cli_aws,
        bucket_name,
        ver_object,
        endpoint,
        ver_etag,
        ver_id,
        "versionId",
    )
    _delete_exact_etag(
        cli_aws,
        bucket_name,
        ver_object,
        endpoint,
        ver_etag,
        None,
        "current-version",
    )
    _delete_exact_etag(
        cli_aws,
        bucket_name,
        ver_object,
        endpoint,
        ver_etag,
        ver_id,
        "versionId",
    )


def _normalize_lmt(last_modified, ceph_major):
    text = str(last_modified)
    if float(ceph_major) >= 20:
        return text.split("+")[0]
    return text.split(".")[0]


def _object_attrs(cli_aws, bucket_name, object_name, endpoint, ceph_major):
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    markers = _delete_markers_for(version_list, object_name)
    latest_versions = [
        version
        for version in _versions_for(version_list, object_name)
        if version.get("IsLatest")
    ]
    latest = (
        latest_versions[0]
        if latest_versions
        else _latest_version(version_list, object_name)
    )
    if latest is None:
        return None, markers
    return {
        "etag": _etag_from_version(latest),
        "size": int(latest["Size"]),
        "lmt": _normalize_lmt(latest["LastModified"], ceph_major),
        "lmt_raw": str(latest["LastModified"]),
        "version_id": latest.get("VersionId"),
    }, markers


def _put_sized_object(cli_aws, bucket_name, object_name, endpoint, size):
    body = (
        os.path.join(TEST_DATA_PATH, object_name)
        if TEST_DATA_PATH
        else object_name
    )
    quoted_body = shlex.quote(body)
    created = utils.exec_shell_cmd(
        f"rm -f {quoted_body} && fallocate -l {size} {quoted_body}"
    )
    if created is False:
        raise AssertionError(f"failed to create object body {body}")
    aws_reusable.put_object(
        cli_aws, bucket_name, object_name, endpoint, body=body
    )


def _overwrite_until_changed(
    cli_aws, bucket_name, object_name, endpoint, ceph_major, old, new_size
):
    """Overwrite until ETag, size, and mtime all differ from the values just read."""
    updated = None
    for _ in range(5):
        time.sleep(1)
        _put_sized_object(cli_aws, bucket_name, object_name, endpoint, new_size)
        updated, markers = _object_attrs(
            cli_aws, bucket_name, object_name, endpoint, ceph_major
        )
        if markers:
            raise AssertionError(
                f"overwrite of {object_name} created a delete marker"
            )
        if (
            updated
            and updated["etag"] != old["etag"]
            and updated["size"] != old["size"]
            and updated["lmt"] != old["lmt"]
        ):
            return updated
    raise AssertionError(
        f"overwrite of {object_name} did not change ETag, size, and mtime; "
        f"old={old} new={updated}"
    )


def _assert_stale_delete_keeps_object(
    cli_aws,
    bucket_name,
    object_name,
    endpoint,
    ceph_major,
    old,
    new,
    path_name,
    condition_name,
    send_etag,
    send_size,
    send_mtime,
):
    out = _conditional_delete_debug(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        etag=old["etag"] if send_etag else None,
        size=old["size"] if send_size else None,
        last_modified_time=old["lmt"] if send_mtime else None,
    )
    current, markers = _object_attrs(
        cli_aws, bucket_name, object_name, endpoint, ceph_major
    )
    kept = (
        _response_is_412(out)
        and not markers
        and current is not None
        and current["etag"] == new["etag"]
        and current["size"] == new["size"]
        and current["version_id"] == new["version_id"]
    )
    if not kept:
        raise AssertionError(
            f"{path_name} delete with stale {condition_name} must return 412 "
            f"and keep the overwritten object. "
            f"debug_412={_response_is_412(out)} "
            f"debug_excerpt={_debug_excerpt(out)!r} "
            f"current={current} delete_markers={len(markers)} "
            f"expected_etag={new['etag']} expected_size={new['size']}. "
            "Critical failure is 204 with the new data gone. ETag and size "
            "are checked in memory and are not tied to the RADOS delete; on "
            "a versioned bucket that check is also not tied to the "
            "delete-marker write. Unversioned mtime is rechecked in the "
            "RADOS operation; the versioned path does not recheck it."
        )


def _exercise_stale_delete(
    cli_aws,
    bucket_name,
    object_name,
    endpoint,
    ceph_major,
    path_name,
    send_etag,
    send_size,
    send_mtime,
):
    condition_name = "+".join(
        name
        for name, enabled in (
            ("etag", send_etag),
            ("size", send_size),
            ("mtime", send_mtime),
        )
        if enabled
    )
    _put_sized_object(cli_aws, bucket_name, object_name, endpoint, 1024)
    old, markers = _object_attrs(
        cli_aws, bucket_name, object_name, endpoint, ceph_major
    )
    if old is None or markers:
        raise AssertionError(f"{object_name} missing after the initial put")
    log.info(
        "%s read %s ETag=%s size=%s mtime=%s before overwrite",
        path_name,
        object_name,
        old["etag"],
        old["size"],
        old["lmt"],
    )
    new = _overwrite_until_changed(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        ceph_major,
        old,
        2048,
    )
    log.info(
        "%s overwrite %s ETag=%s size=%s mtime=%s; delete immediately with stale %s",
        path_name,
        object_name,
        new["etag"],
        new["size"],
        new["lmt"],
        condition_name,
    )
    _assert_stale_delete_keeps_object(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        ceph_major,
        old,
        new,
        path_name,
        condition_name,
        send_etag,
        send_size,
        send_mtime,
    )


def _purge_object(cli_aws, bucket_name, object_name, endpoint):
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    version_ids = []
    for entry in _versions_for(version_list, object_name) + _delete_markers_for(
        version_list, object_name
    ):
        version_id = entry.get("VersionId")
        if version_id and version_id != "null":
            version_ids.append(version_id)
    if version_ids:
        for version_id in version_ids:
            aws_reusable.delete_object(
                cli_aws, bucket_name, object_name, endpoint, versionid=version_id
            )
        return
    if _versions_for(version_list, object_name):
        aws_reusable.delete_object(cli_aws, bucket_name, object_name, endpoint)


def _test_stale_conditional_delete(
    cli_aws, bucket_name, seeded_object, endpoint, ceph_major
):
    """
    Read ETag, size, and mtime, overwrite the object, then conditional-delete
    with the values from before the overwrite.

    The delete is sent as soon as the overwrite returns. 204 means the new
    object was removed. ETag and size are an in-memory check on both the
    unversioned delete and the versioned delete-marker write. The mtime-only
    repeat expects the same 412: unversioned rechecks mtime in RADOS, and the
    versioned path does not.
    """
    cases = (
        ("stale-etag-size", True, True, False),
        ("stale-all", True, True, True),
        ("stale-mtime", False, False, True),
    )
    for key, send_etag, send_size, send_mtime in cases:
        _exercise_stale_delete(
            cli_aws,
            bucket_name,
            key,
            endpoint,
            ceph_major,
            "unversioned",
            send_etag,
            send_size,
            send_mtime,
        )
    for key, _, _, _ in cases:
        _purge_object(cli_aws, bucket_name, key, endpoint)
    _purge_object(cli_aws, bucket_name, seeded_object, endpoint)

    log.info("enable versioning for stale delete-marker checks")
    aws_reusable.put_get_bucket_versioning(cli_aws, bucket_name, endpoint)
    for key, send_etag, send_size, send_mtime in cases:
        _exercise_stale_delete(
            cli_aws,
            bucket_name,
            key + "-ver",
            endpoint,
            ceph_major,
            "versioned",
            send_etag,
            send_size,
            send_mtime,
        )
    for key, _, _, _ in cases:
        _purge_object(cli_aws, bucket_name, key + "-ver", endpoint)


def _clear_lmt_nanoseconds(last_modified):
    """Normal requests compare LastModified with fractional seconds removed."""
    text = str(last_modified).strip()
    if "." in text:
        text = text.split(".", 1)[0]
    if text.endswith("Z"):
        text = text[:-1]
    plus = text.find("+")
    if plus != -1:
        text = text[:plus]
    return text


def _lmt_seconds(last_modified):
    if hasattr(last_modified, "strftime"):
        return last_modified.strftime("%Y-%m-%dT%H:%M:%S")
    return _clear_lmt_nanoseconds(last_modified)


def _s3_client(endpoint, access_key, secret_key):
    """Short-lived client so two puts can land in the same second."""
    import warnings

    import boto3
    from botocore.config import Config as BotoConfig

    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    cfg_kwargs = {
        "signature_version": "s3v4",
        "s3": {"addressing_style": "path"},
        "connect_timeout": 10,
        "read_timeout": 60,
        "retries": {"max_attempts": 1},
        "request_checksum_calculation": "when_required",
        "response_checksum_validation": "when_required",
    }
    try:
        cfg = BotoConfig(**cfg_kwargs)
    except TypeError:
        cfg_kwargs.pop("request_checksum_calculation", None)
        cfg_kwargs.pop("response_checksum_validation", None)
        cfg = BotoConfig(**cfg_kwargs)
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        verify=False,
        config=cfg,
    )


def _attrs_from_head(response):
    etag = str(response.get("ETag") or "").strip('"')
    if (
        not etag
        or "LastModified" not in response
        or response.get("ContentLength") is None
    ):
        raise AssertionError(
            f"head-object missing ETag, size, or LastModified: {response!r}"
        )
    last_modified = response["LastModified"]
    raw = (
        last_modified.isoformat()
        if hasattr(last_modified, "isoformat")
        else str(last_modified)
    )
    return {
        "etag": etag,
        "size": int(response["ContentLength"]),
        "lmt_raw": raw,
        "lmt": _lmt_seconds(last_modified),
        "version_id": response.get("VersionId"),
    }


def _put_and_head(client, bucket_name, object_name, body):
    from botocore.exceptions import ClientError

    try:
        client.put_object(Bucket=bucket_name, Key=object_name, Body=body)
        response = client.head_object(Bucket=bucket_name, Key=object_name)
    except ClientError as exc:
        raise AssertionError(f"overwrite of {object_name} failed: {exc}") from exc
    return _attrs_from_head(response)


def _overwrite_twice_same_second(client, bucket_name, object_name, path_name):
    """
    Write two generations back to back until their LastModified values fall
    in the same second. ETag and size must differ so the generations are
    distinct after nanoseconds are cleared.
    """
    last = None
    for attempt in range(1, SAME_SECOND_ATTEMPTS + 1):
        first = _put_and_head(client, bucket_name, object_name, b"a" * 1024)
        second = _put_and_head(client, bucket_name, object_name, b"b" * 2048)
        last = (first, second)
        distinct = first["etag"] != second["etag"] and first["size"] != second["size"]
        if path_name == "versioned":
            first_vid = first.get("version_id")
            second_vid = second.get("version_id")
            if (
                not first_vid
                or not second_vid
                or first_vid == "null"
                or second_vid == "null"
                or first_vid == second_vid
            ):
                raise AssertionError(
                    f"versioned overwrite of {object_name} did not create two "
                    f"version ids: first={first} second={second}"
                )
        if first["lmt"] and first["lmt"] == second["lmt"] and distinct:
            log.info(
                "%s overwrote %s twice in %s; first LastModified %s, second %s",
                path_name,
                object_name,
                first["lmt"],
                first["lmt_raw"],
                second["lmt_raw"],
            )
            return first, second
        log.info(
            "%s attempt %s/%s did not land two generations in one second: %s -> %s",
            path_name,
            attempt,
            SAME_SECOND_ATTEMPTS,
            first["lmt_raw"],
            second["lmt_raw"],
        )
    raise AssertionError(
        f"{path_name} could not overwrite {object_name} twice within one second "
        f"in {SAME_SECOND_ATTEMPTS} attempts; last={last}"
    )


def _assert_same_second_delete_keeps_second(
    cli_aws,
    bucket_name,
    object_name,
    endpoint,
    ceph_major,
    first,
    second,
    path_name,
    version_id=None,
):
    out = _conditional_delete_debug(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        version_id=version_id,
        last_modified_time=first["lmt"],
    )
    current, markers = _object_attrs(
        cli_aws, bucket_name, object_name, endpoint, ceph_major
    )
    second_vid = second.get("version_id")
    version_ok = True
    if second_vid and second_vid != "null":
        version_ok = current is not None and current.get("version_id") == second_vid
    versions_kept = True
    if version_id:
        version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
        versions_kept = bool(_versions_for(version_list, object_name, second_vid))
        first_vid = first.get("version_id")
        if first_vid and first_vid != "null":
            versions_kept = versions_kept and bool(
                _versions_for(version_list, object_name, first_vid)
            )
    kept = (
        _response_is_412(out)
        and not markers
        and current is not None
        and current["etag"] == second["etag"]
        and current["size"] == second["size"]
        and version_ok
        and versions_kept
    )
    if not kept:
        raise AssertionError(
            f"{path_name} delete with the first generation LastModified "
            f"{first['lmt']!r} version_id={version_id!r} must return 412 and "
            f"keep the second generation. debug_412={_response_is_412(out)} "
            f"debug_excerpt={_debug_excerpt(out)!r} current={current} "
            f"delete_markers={len(markers)} first={first} second={second}. "
            "Normal requests compare time with nanoseconds cleared, so both "
            "generations can match. Critical failure is the second generation "
            "being deleted."
        )


def _exercise_same_second_delete(
    cli_aws,
    client,
    bucket_name,
    object_name,
    endpoint,
    ceph_major,
    path_name,
):
    first, second = _overwrite_twice_same_second(
        client, bucket_name, object_name, path_name
    )
    listed, markers = _object_attrs(
        cli_aws, bucket_name, object_name, endpoint, ceph_major
    )
    second_vid = second.get("version_id")
    version_ok = True
    if second_vid and second_vid != "null":
        version_ok = listed is not None and listed.get("version_id") == second_vid
    if (
        markers
        or listed is None
        or listed["etag"] != second["etag"]
        or listed["size"] != second["size"]
        or _clear_lmt_nanoseconds(listed["lmt_raw"]) != second["lmt"]
        or not version_ok
    ):
        raise AssertionError(
            f"{path_name} latest {object_name} is not the second generation "
            f"before the conditional delete: listed={listed} markers={len(markers)} "
            f"second={second}"
        )
    if path_name == "unversioned":
        _assert_same_second_delete_keeps_second(
            cli_aws,
            bucket_name,
            object_name,
            endpoint,
            ceph_major,
            first,
            second,
            path_name,
        )
        return
    _assert_same_second_delete_keeps_second(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        ceph_major,
        first,
        second,
        "current-version",
    )
    _assert_same_second_delete_keeps_second(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        ceph_major,
        first,
        second,
        "versionId",
        version_id=second["version_id"],
    )


def _test_same_second_conditional_delete(
    cli_aws, bucket_name, seeded_object, endpoint, ceph_major, access_key, secret_key
):
    """
    Overwrite an object twice within one second, then conditional-delete with
    the first generation's LastModified.

    Normal requests compare that time with nanoseconds cleared, so both
    generations can match. The delete is sent with the cleared first
    LastModified. 204 means the second generation was removed.

    On a versioned bucket the same delete is sent twice: once against the
    current version, and once with the second version's versionId.
    """
    client = _s3_client(endpoint, access_key, secret_key)
    _exercise_same_second_delete(
        cli_aws,
        client,
        bucket_name,
        "same-sec",
        endpoint,
        ceph_major,
        "unversioned",
    )
    _purge_object(cli_aws, bucket_name, "same-sec", endpoint)
    _purge_object(cli_aws, bucket_name, seeded_object, endpoint)

    log.info(
        "enable versioning for same-second current-version and versionId checks"
    )
    aws_reusable.put_get_bucket_versioning(cli_aws, bucket_name, endpoint)
    _exercise_same_second_delete(
        cli_aws,
        client,
        bucket_name,
        "same-sec-ver",
        endpoint,
        ceph_major,
        "versioned",
    )
    _purge_object(cli_aws, bucket_name, "same-sec-ver", endpoint)


def _key_presence(cli_aws, bucket_name, object_name, endpoint):
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    versions = _versions_for(version_list, object_name)
    markers = _delete_markers_for(version_list, object_name)
    return versions, markers


def _assert_missing_key_delete(
    cli_aws,
    bucket_name,
    object_name,
    endpoint,
    path_name,
    condition_name,
    etag=None,
    size=None,
    last_modified_time=None,
):
    """
    Return a failure description when a wrong condition on a never-written key
    looks like a successful delete. 412 and NoSuchKey leave the key absent.
    """
    versions, markers = _key_presence(
        cli_aws, bucket_name, object_name, endpoint
    )
    if versions or markers:
        raise AssertionError(
            f"{path_name} {object_name} exists before the wrong-{condition_name} "
            f"delete: versions={len(versions)} delete_markers={len(markers)}"
        )
    log.info(
        "%s delete never-written %s with wrong %s and no version id",
        path_name,
        object_name,
        condition_name,
    )
    out = _conditional_delete_debug(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        etag=etag,
        size=size,
        last_modified_time=last_modified_time,
    )
    versions, markers = _key_presence(
        cli_aws, bucket_name, object_name, endpoint
    )
    distinguished = _response_is_412(out) or _object_not_found(out)
    if distinguished and not versions and not markers:
        return None
    return (
        f"{path_name} wrong {condition_name} must return 412 or NoSuchKey. "
        f"debug_412={_response_is_412(out)} "
        f"debug_excerpt={_debug_excerpt(out)!r} "
        f"versions={len(versions)} delete_markers={len(markers)}"
    )


def _exercise_missing_key(cli_aws, bucket_name, object_name, endpoint, path_name):
    cases = (
        ("etag", "wrong-etag", None, None),
        ("size", None, 1024, None),
        ("mtime", None, None, "2000-01-01T00:00:00"),
    )
    failures = []
    for condition_name, etag, size, last_modified_time in cases:
        failure = _assert_missing_key_delete(
            cli_aws,
            bucket_name,
            object_name,
            endpoint,
            path_name,
            condition_name,
            etag=etag,
            size=size,
            last_modified_time=last_modified_time,
        )
        if failure:
            log.error(failure)
            failures.append(failure)
    return failures


def _test_missing_conditional_delete(cli_aws, bucket_name, seeded_object, endpoint):
    """
    Delete a key that was never written, once on an unversioned bucket and
    once on a versioned bucket, using a wrong ETag, a wrong size, and a wrong
    mtime. No version id is sent.

    Unversioned RGW returns not found before it evaluates the conditions, and
    the S3 response turns that into 204. A wrong condition still looks
    successful. Versioned size and mtime are evaluated against an empty state,
    so a wrong size or time can return 412, while If-Match still becomes 204.
    """
    failures = _exercise_missing_key(
        cli_aws, bucket_name, "missing-unver", endpoint, "unversioned"
    )
    _purge_object(cli_aws, bucket_name, seeded_object, endpoint)

    log.info("enable versioning for missing-object deletes with no version id")
    aws_reusable.put_get_bucket_versioning(cli_aws, bucket_name, endpoint)
    failures.extend(
        _exercise_missing_key(
            cli_aws, bucket_name, "missing-ver", endpoint, "versioned"
        )
    )
    if failures:
        raise AssertionError(
            "Delete of a never-written key with a wrong ETag, size, or mtime "
            "must not return 204. "
            + " ".join(failures)
            + " Unversioned RGW returns not found before it evaluates the "
            "conditions, and the S3 response turns that into 204, so a wrong "
            "condition still looks successful. Versioned deletes with no "
            "version id still evaluate size and mtime against an empty state, "
            "so a wrong size or time can return 412, while If-Match still "
            "becomes 204. Critical hole: 204 means both \"deleted the "
            "matching object\" and \"there was nothing to delete.\""
        )


def _aws_debug_text(cli_aws, operation, param_string):
    """Run one s3api call with --debug. Return combined output and success."""
    command = cli_aws.command(operation, [param_string + " --debug"])
    result = utils.exec_shell_cmd(command, debug_info=True, return_err=True)
    if result is False:
        raise AssertionError(f"{operation} did not run")
    if isinstance(result, tuple):
        stdout, stderr = result
        return f"{stdout or ''}\n{stderr or ''}", True
    return str(result), False


def _delete_marker_header_true(text):
    """True when the response sets x-amz-delete-marker: true."""
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    needle = "x-amz-delete-marker"
    start = 0
    while True:
        index = lowered.find(needle, start)
        if index < 0:
            return False
        tail = lowered[index + len(needle) : index + len(needle) + 24]
        tail = tail.lstrip(" '\"=: \t")
        if tail.startswith("true"):
            return True
        start = index + len(needle)


def _marker_version_ids(cli_aws, bucket_name, object_name, endpoint):
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    return [
        marker.get("VersionId")
        for marker in _delete_markers_for(version_list, object_name)
        if marker.get("VersionId")
    ]


def _latest_marker_problem(cli_aws, bucket_name, object_name, endpoint):
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    markers = _delete_markers_for(version_list, object_name)
    latest = [marker for marker in markers if marker.get("IsLatest")]
    if len(markers) == 1 and len(latest) == 1:
        return None
    return (
        f"{object_name} current version must be one delete marker: "
        f"markers={len(markers)} latest_markers={len(latest)}"
    )


def _get_delete_marker_problem(cli_aws, bucket_name, object_name, endpoint):
    outfile = (
        os.path.join(TEST_DATA_PATH, f"{object_name}.get")
        if TEST_DATA_PATH
        else f"{object_name}.get"
    )
    text, ok = _aws_debug_text(
        cli_aws,
        "get-object",
        "--bucket {bucket} --key {key} {outfile} --endpoint-url {endpoint}".format(
            bucket=bucket_name,
            key=object_name,
            outfile=shlex.quote(outfile),
            endpoint=endpoint,
        ),
    )
    try:
        os.remove(outfile)
    except OSError:
        pass
    not_found = (not ok) and (
        _object_not_found(text) or "404" in text.lower()
    )
    header = _delete_marker_header_true(text)
    if not_found and header:
        return None
    return (
        f"GET {object_name} must return 404 with x-amz-delete-marker: true. "
        f"success={ok} not_found={not_found} header={header}"
    )


def _delete_condition_params(
    bucket_name, object_name, endpoint, etag=None, size=None, last_modified_time=None
):
    params = (
        f"--bucket {bucket_name} --key {object_name} --endpoint-url {endpoint}"
    )
    if etag is not None:
        params += f" --if-match {shlex.quote(str(etag))}"
    if size is not None:
        params += f" --if-match-size {int(size)}"
    if last_modified_time is not None:
        params += (
            " --if-match-last-modified-time "
            f"{shlex.quote(str(last_modified_time))}"
        )
    return params


def _conditional_delete_debug(
    cli_aws,
    bucket_name,
    object_name,
    endpoint,
    etag=None,
    size=None,
    last_modified_time=None,
    version_id=None,
):
    """Delete with --debug so a 412 can be read when the CLI hides it."""
    params = _delete_condition_params(
        bucket_name,
        object_name,
        endpoint,
        etag=etag,
        size=size,
        last_modified_time=last_modified_time,
    )
    if version_id is not None:
        params += f" --version-id {shlex.quote(str(version_id))}"
    text, ok = _aws_debug_text(cli_aws, "delete-object", params)
    log.info(
        "conditional delete debug for %s version_id=%s http_412=%s cli_ok=%s",
        object_name,
        version_id,
        _response_is_412(text),
        ok,
    )
    return text


def _conditional_put_debug(
    cli_aws, bucket_name, object_name, endpoint, etag=None
):
    """Put with --debug so a 412 can be read when the CLI hides it."""
    params = f"--bucket {bucket_name} --key {object_name} --endpoint-url {endpoint}"
    if etag:
        params += f" --if-match {etag}"
    else:
        params += ' --if-none-match "*"'
    text, ok = _aws_debug_text(cli_aws, "put-object", params)
    log.info(
        "conditional put debug for %s http_412=%s cli_ok=%s",
        object_name,
        _response_is_412(text),
        ok,
    )
    return text


def _exercise_delete_marker_case(
    cli_aws,
    bucket_name,
    object_name,
    endpoint,
    condition_name,
    require_412,
    etag=None,
    size=None,
    last_modified_time=None,
):
    """
    Put an object, delete it once, then conditional-delete the delete marker.
    """
    _put_sized_object(cli_aws, bucket_name, object_name, endpoint, 1024)
    created, created_ok = _aws_debug_text(
        cli_aws,
        "delete-object",
        f"--bucket {bucket_name} --key {object_name} --endpoint-url {endpoint}",
    )
    failures = []
    if not created_ok or not _delete_marker_header_true(created):
        failures.append(
            f"{condition_name} initial delete of {object_name} must write a "
            f"delete marker and set x-amz-delete-marker: true. "
            f"success={created_ok} header={_delete_marker_header_true(created)}"
        )
    marker_problem = _latest_marker_problem(
        cli_aws, bucket_name, object_name, endpoint
    )
    if marker_problem:
        failures.append(f"{condition_name} {marker_problem}")
    get_problem = _get_delete_marker_problem(
        cli_aws, bucket_name, object_name, endpoint
    )
    if get_problem:
        failures.append(f"{condition_name} {get_problem}")
    if (
        not created_ok
        or not _delete_marker_header_true(created)
        or marker_problem
    ):
        return failures

    before = _marker_version_ids(cli_aws, bucket_name, object_name, endpoint)
    text, ok = _aws_debug_text(
        cli_aws,
        "delete-object",
        _delete_condition_params(
            bucket_name,
            object_name,
            endpoint,
            etag=etag,
            size=size,
            last_modified_time=last_modified_time,
        ),
    )
    after = _marker_version_ids(cli_aws, bucket_name, object_name, endpoint)
    wrote_marker = len(after) > len(before) or set(after) != set(before)
    header = _delete_marker_header_true(text)
    log.info(
        "%s conditional delete success=%s http_412=%s header=%s "
        "markers %s -> %s",
        condition_name,
        ok,
        _response_is_412(text),
        header,
        before,
        after,
    )
    if require_412 and not _response_is_412(text):
        why = (
            "a delete marker has no ETag"
            if condition_name == "concrete-etag"
            else "the marker mtime does not match"
        )
        failures.append(
            f"{condition_name} must return 412 ({why}). success={ok} "
            f"debug_412={_response_is_412(text)} "
            f"debug_excerpt={_debug_excerpt(text)!r}"
        )
    if wrote_marker:
        failures.append(
            f"{condition_name} stacked delete markers: {before} -> {after}. "
            "If-Match * can succeed and write another delete marker. "
            "Size 0 can match the marker and write another one."
        )
    if header != wrote_marker:
        if header:
            failures.append(
                f"{condition_name} set x-amz-delete-marker: true but did not "
                "write a delete marker"
            )
        else:
            failures.append(
                f"{condition_name} wrote a delete marker but did not set "
                "x-amz-delete-marker: true"
            )
    get_after = _get_delete_marker_problem(
        cli_aws, bucket_name, object_name, endpoint
    )
    if get_after:
        failures.append(f"{condition_name} after conditional delete, {get_after}")
    return failures


def _test_delete_marker_conditional_delete(
    cli_aws, bucket_name, seeded_object, endpoint
):
    """
    Delete an object once so the current version is a delete marker, then
    conditional-delete again with no version id.

    If-Match * can succeed and write another delete marker. A concrete ETag
    must be 412, because a delete marker has no ETag. Size 0 can match the
    marker and write another one. A wrong mtime must be 412. Stacking delete
    markers is a failure. GET must stay 404, and x-amz-delete-marker: true is
    set only when a marker was actually written.
    """
    _purge_object(cli_aws, bucket_name, seeded_object, endpoint)
    log.info("enable versioning so the current version can be a delete marker")
    aws_reusable.put_get_bucket_versioning(cli_aws, bucket_name, endpoint)
    cases = (
        ("if-match-star", False, "*", None, None),
        ("concrete-etag", True, "concrete-etag", None, None),
        ("size-zero", False, None, 0, None),
        ("wrong-mtime", True, None, None, "2000-01-01T00:00:00"),
    )
    failures = []
    keys = []
    for condition_name, require_412, etag, size, last_modified_time in cases:
        object_name = f"marker-{condition_name}"
        keys.append(object_name)
        log.info(
            "current version of %s will be a delete marker, then %s",
            object_name,
            condition_name,
        )
        failures.extend(
            _exercise_delete_marker_case(
                cli_aws,
                bucket_name,
                object_name,
                endpoint,
                condition_name,
                require_412,
                etag=etag,
                size=size,
                last_modified_time=last_modified_time,
            )
        )
    for object_name in keys:
        _purge_object(cli_aws, bucket_name, object_name, endpoint)
    if failures:
        raise AssertionError(
            "Conditional delete of a delete marker must not stack another "
            "marker. "
            + " ".join(failures)
            + " If-Match * can succeed and write another delete marker. "
            "A concrete ETag must be 412 because a delete marker has no "
            "ETag. Size 0 can match the marker and write another one. A "
            "wrong mtime must be 412. Stacking delete markers is the "
            "failure. A normal GET must still return 404, and "
            "x-amz-delete-marker: true is set only when a marker was "
            "actually written."
        )


def _version_facts(version, ceph_major):
    version_id = version.get("VersionId")
    if not version_id or version_id == "null":
        raise AssertionError(f"versioned object has no version id: {version}")
    return {
        "etag": _etag_from_version(version),
        "size": int(version["Size"]),
        "lmt": _normalize_lmt(version["LastModified"], ceph_major),
        "version_id": version_id,
    }


def _is_same_generation(version, expected):
    if version is None:
        return False
    return (
        version.get("VersionId") == expected["version_id"]
        and _etag_from_version(version) == expected["etag"]
        and int(version["Size"]) == expected["size"]
    )


def _test_wrong_version_conditional_delete(
    cli_aws, bucket_name, seeded_object, endpoint, ceph_major
):
    """
    On a versioned bucket, write version A then version B.

    Delete with versionId of A and with B's ETag, size, and mtime. That must
    return 412, and B must remain current. Then delete with no version id and
    A's ETag. That must return 412, and A must still exist. Deleting the
    other generation is the data-loss case.
    """
    object_name = "wrong-version"
    _purge_object(cli_aws, bucket_name, seeded_object, endpoint)
    log.info("enable versioning before writing versions A and B")
    aws_reusable.put_get_bucket_versioning(cli_aws, bucket_name, endpoint)
    _put_sized_object(cli_aws, bucket_name, object_name, endpoint, 1024)
    _put_sized_object(cli_aws, bucket_name, object_name, endpoint, 2048)
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    versions = _versions_for(version_list, object_name)
    latest_versions = [version for version in versions if version.get("IsLatest")]
    if len(versions) != 2 or len(latest_versions) != 1:
        raise AssertionError(
            f"{object_name} must have version A and current version B: "
            f"versions={len(versions)} latest={len(latest_versions)}"
        )
    version_b = _version_facts(latest_versions[0], ceph_major)
    older = [
        version
        for version in versions
        if version.get("VersionId") != version_b["version_id"]
    ]
    version_a = _version_facts(older[0], ceph_major)
    if (
        version_a["etag"] == version_b["etag"]
        or version_a["size"] == version_b["size"]
    ):
        raise AssertionError(
            f"versions A and B must differ in ETag and size: "
            f"A={version_a} B={version_b}"
        )
    log.info(
        "version A id=%s ETag=%s size=%s mtime=%s; "
        "version B id=%s ETag=%s size=%s mtime=%s",
        version_a["version_id"],
        version_a["etag"],
        version_a["size"],
        version_a["lmt"],
        version_b["version_id"],
        version_b["etag"],
        version_b["size"],
        version_b["lmt"],
    )

    log.info(
        "delete versionId %s with B's ETag, size, and mtime",
        version_a["version_id"],
    )
    by_version = _conditional_delete_debug(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        version_id=version_a["version_id"],
        etag=version_b["etag"],
        size=version_b["size"],
        last_modified_time=version_b["lmt"],
    )
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    latest = _latest_version(version_list, object_name)
    a_exists = bool(
        _versions_for(version_list, object_name, version_a["version_id"])
    )
    markers = _delete_markers_for(version_list, object_name)
    b_current = _is_same_generation(latest, version_b) and not markers
    if not (_response_is_412(by_version) and b_current and a_exists):
        raise AssertionError(
            f"delete with versionId {version_a['version_id']} and B's ETag, "
            f"size, and mtime must return 412 and leave B current. "
            f"debug_412={_response_is_412(by_version)} "
            f"debug_excerpt={_debug_excerpt(by_version)!r} latest={latest} "
            f"A_exists={a_exists} delete_markers={len(markers)}. "
            "The CLI prints \"argument of type 'NoneType' is not a "
            "container or iterable\" instead of 412; the status is read "
            "from --debug. Deleting the other generation is the data-loss "
            "case."
        )

    log.info("delete with no version id and A's ETag %s", version_a["etag"])
    by_etag = _conditional_delete_debug(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        etag=version_a["etag"],
    )
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    latest = _latest_version(version_list, object_name)
    a_exists = bool(
        _versions_for(version_list, object_name, version_a["version_id"])
    )
    markers = _delete_markers_for(version_list, object_name)
    b_current = _is_same_generation(latest, version_b) and not markers
    if not (_response_is_412(by_etag) and a_exists and b_current):
        raise AssertionError(
            f"delete with no version id and A's ETag {version_a['etag']!r} "
            f"must return 412 and leave A in place. "
            f"debug_412={_response_is_412(by_etag)} "
            f"debug_excerpt={_debug_excerpt(by_etag)!r} latest={latest} "
            f"A_exists={a_exists} delete_markers={len(markers)}. "
            "The CLI prints \"argument of type 'NoneType' is not a "
            "container or iterable\" instead of 412; the status is read "
            "from --debug. Deleting the other generation is the data-loss "
            "case."
        )
    _purge_object(cli_aws, bucket_name, object_name, endpoint)


def _version_brief(version):
    version_id = version.get("VersionId")
    if not version_id:
        raise AssertionError(f"object version has no version id: {version}")
    return {
        "etag": _etag_from_version(version),
        "size": int(version["Size"]),
        "version_id": version_id,
    }


def _key_listing(cli_aws, bucket_name, object_name, endpoint):
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    versions = _versions_for(version_list, object_name)
    return versions, _delete_markers_for(version_list, object_name)


def _version_with_id(versions, version_id):
    matched = [
        version for version in versions if version.get("VersionId") == version_id
    ]
    return matched[0] if matched else None


def _purge_versions_and_null(cli_aws, bucket_name, object_name, endpoint):
    versions, markers = _key_listing(cli_aws, bucket_name, object_name, endpoint)
    version_ids = []
    for entry in versions + markers:
        version_id = entry.get("VersionId")
        if version_id and version_id not in version_ids:
            version_ids.append(version_id)
    for version_id in version_ids:
        aws_reusable.delete_object(
            cli_aws, bucket_name, object_name, endpoint, versionid=version_id
        )


def _prepare_null_current(cli_aws, bucket_name, object_name, endpoint):
    """Write the older version while versioning is enabled. Return its facts."""
    _put_sized_object(cli_aws, bucket_name, object_name, endpoint, 1024)
    versions, markers = _key_listing(cli_aws, bucket_name, object_name, endpoint)
    latest = _latest_version(
        {"Versions": versions, "DeleteMarkers": markers}, object_name
    )
    if latest is None or markers:
        raise AssertionError(f"{object_name} missing after the versioned put")
    older = _version_brief(latest)
    if older["version_id"] == "null":
        raise AssertionError(
            f"{object_name} versioned put stored a null version: {older}"
        )
    return older


def _overwrite_null_current(cli_aws, bucket_name, object_name, endpoint, older):
    """Overwrite after versioning is suspended. The null version becomes current."""
    _put_sized_object(cli_aws, bucket_name, object_name, endpoint, 2048)
    versions, markers = _key_listing(cli_aws, bucket_name, object_name, endpoint)
    latest = _latest_version(
        {"Versions": versions, "DeleteMarkers": markers}, object_name
    )
    if latest is None or markers:
        raise AssertionError(
            f"{object_name} missing after the suspended overwrite"
        )
    null_version = _version_brief(latest)
    older_still = _version_with_id(versions, older["version_id"])
    if (
        null_version["version_id"] != "null"
        or not latest.get("IsLatest")
        or null_version["etag"] == older["etag"]
        or older_still is None
    ):
        raise AssertionError(
            f"{object_name} must have an older version and a current null "
            f"version with a different ETag. older={older} null={null_version} "
            f"older_kept={older_still is not None}"
        )
    return null_version


def _test_null_version_conditional_delete(
    cli_aws, bucket_name, seeded_object, endpoint
):
    """
    Enable versioning, write an object, suspend versioning, and overwrite it
    so the null version is current.

    Delete with no version id, and delete with versionId=null, each with the
    null version's ETag and with the older version's ETag. The no-id delete
    must create a delete marker only when the current null version matches.
    versionId=null must permanently remove that null version only when its
    own metadata matches.
    """
    keys = {
        "noid-match": "null-noid-match",
        "noid-older": "null-noid-older",
        "vid-match": "null-vid-match",
        "vid-older": "null-vid-older",
    }
    _purge_object(cli_aws, bucket_name, seeded_object, endpoint)
    log.info("enable versioning, then write the older version of each object")
    aws_reusable.put_get_bucket_versioning(cli_aws, bucket_name, endpoint)
    older_by_key = {
        key: _prepare_null_current(cli_aws, bucket_name, key, endpoint)
        for key in keys.values()
    }
    log.info("suspend versioning and overwrite so the null version is current")
    aws_reusable.put_get_bucket_versioning(
        cli_aws, bucket_name, endpoint, status="Suspended"
    )
    null_by_key = {
        key: _overwrite_null_current(
            cli_aws, bucket_name, key, endpoint, older_by_key[key]
        )
        for key in keys.values()
    }
    failures = []
    failures.extend(
        _delete_null_without_version_id(
            cli_aws,
            bucket_name,
            keys["noid-match"],
            endpoint,
            null_by_key[keys["noid-match"]],
            older_by_key[keys["noid-match"]],
            use_null_etag=True,
        )
    )
    failures.extend(
        _delete_null_without_version_id(
            cli_aws,
            bucket_name,
            keys["noid-older"],
            endpoint,
            null_by_key[keys["noid-older"]],
            older_by_key[keys["noid-older"]],
            use_null_etag=False,
        )
    )
    failures.extend(
        _delete_null_version_id(
            cli_aws,
            bucket_name,
            keys["vid-match"],
            endpoint,
            null_by_key[keys["vid-match"]],
            older_by_key[keys["vid-match"]],
            use_null_etag=True,
        )
    )
    failures.extend(
        _delete_null_version_id(
            cli_aws,
            bucket_name,
            keys["vid-older"],
            endpoint,
            null_by_key[keys["vid-older"]],
            older_by_key[keys["vid-older"]],
            use_null_etag=False,
        )
    )
    for key in keys.values():
        _purge_versions_and_null(cli_aws, bucket_name, key, endpoint)
    if failures:
        raise AssertionError(
            "Suspended versioning leaves a current null version. "
            + " ".join(failures)
            + " The no-id delete must create a delete marker only when the "
            "current null version matches. versionId=null must permanently "
            "remove that null version only when its own metadata matches."
        )


def _delete_null_without_version_id(
    cli_aws,
    bucket_name,
    object_name,
    endpoint,
    null_version,
    older,
    use_null_etag,
):
    etag = null_version["etag"] if use_null_etag else older["etag"]
    which = "null version" if use_null_etag else "older version"
    log.info(
        "no version id delete of %s with the %s ETag %s",
        object_name,
        which,
        etag,
    )
    out = _conditional_delete_debug(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        etag=etag,
    )
    versions, markers = _key_listing(cli_aws, bucket_name, object_name, endpoint)
    latest_markers = [marker for marker in markers if marker.get("IsLatest")]
    null_now = _version_with_id(versions, "null")
    older_kept = _version_with_id(versions, older["version_id"]) is not None
    if use_null_etag:
        if _delete_succeeded(out) and latest_markers and older_kept:
            return []
        return [
            f"no version id delete of {object_name} with the null version "
            f"ETag must create a delete marker and keep the older version. "
            f"debug_412={_response_is_412(out)} "
            f"debug_excerpt={_debug_excerpt(out)!r} "
            f"delete_markers={len(markers)} "
            f"latest_markers={len(latest_markers)} older_kept={older_kept}"
        ]
    null_current = (
        null_now is not None
        and null_now.get("IsLatest")
        and _etag_from_version(null_now) == null_version["etag"]
    )
    if _response_is_412(out) and not markers and null_current and older_kept:
        return []
    return [
        f"no version id delete of {object_name} with the older version ETag "
        f"must return 412 and must not create a delete marker. "
        f"debug_412={_response_is_412(out)} "
        f"debug_excerpt={_debug_excerpt(out)!r} "
        f"delete_markers={len(markers)} "
        f"null_current={null_current} older_kept={older_kept}"
    ]


def _delete_null_version_id(
    cli_aws,
    bucket_name,
    object_name,
    endpoint,
    null_version,
    older,
    use_null_etag,
):
    etag = null_version["etag"] if use_null_etag else older["etag"]
    which = "null version" if use_null_etag else "older version"
    log.info(
        "versionId=null delete of %s with the %s ETag %s",
        object_name,
        which,
        etag,
    )
    out = _conditional_delete_debug(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        version_id="null",
        etag=etag,
    )
    versions, markers = _key_listing(cli_aws, bucket_name, object_name, endpoint)
    null_now = _version_with_id(versions, "null")
    older_kept = _version_with_id(versions, older["version_id"]) is not None
    if use_null_etag:
        if _delete_succeeded(out) and null_now is None and older_kept and not markers:
            return []
        return [
            f"versionId=null delete of {object_name} with the null version "
            f"ETag must permanently remove that null version and leave the "
            f"older version. debug_412={_response_is_412(out)} "
            f"debug_excerpt={_debug_excerpt(out)!r} "
            f"null_removed={null_now is None} "
            f"older_kept={older_kept} delete_markers={len(markers)}"
        ]
    null_current = (
        null_now is not None
        and null_now.get("IsLatest")
        and _etag_from_version(null_now) == null_version["etag"]
    )
    if _response_is_412(out) and null_current and older_kept and not markers:
        return []
    return [
        f"versionId=null delete of {object_name} with the older version ETag "
        f"must return 412 and must leave the null version in place. "
        f"debug_412={_response_is_412(out)} "
        f"debug_excerpt={_debug_excerpt(out)!r} "
        f"null_current={null_current} older_kept={older_kept} "
        f"delete_markers={len(markers)}"
    ]


def _int_or_none(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _add_size(found, value, visible):
    number = _int_or_none(value)
    if number is None or number == visible or number in found:
        return
    found.append(number)


def _compression_block_total(compression):
    blocks = compression.get("blocks") or []
    if not isinstance(blocks, list) or not blocks:
        return None
    total = 0
    for block in blocks:
        if not isinstance(block, dict):
            return None
        length = _int_or_none(block.get("len"))
        if length is None:
            length = _int_or_none(block.get("compressed_size"))
        if length is None:
            return None
        total += length
    return total


def _stored_sizes_differing_from(stat, visible):
    """
    Sizes stored for the object other than the Content-Length HEAD returns.

    Compression block bytes are the raw compressed size. accounted_size is
    the size charged for the object. The index size and the multipart head
    size are included when they are not the visible size.
    """
    found = []
    if not isinstance(stat, dict):
        return found
    compression = stat.get("compression")
    if isinstance(compression, dict):
        _add_size(found, _compression_block_total(compression), visible)
        _add_size(found, compression.get("compressed_size"), visible)
        _add_size(found, compression.get("orig_size"), visible)
    _add_size(found, stat.get("accounted_size"), visible)
    _add_size(found, stat.get("size"), visible)
    manifest = stat.get("manifest")
    if isinstance(manifest, dict):
        _add_size(found, manifest.get("head_size"), visible)
        _add_size(found, manifest.get("obj_size"), visible)
        objs = manifest.get("objs") or []
        if isinstance(objs, list) and objs:
            total = 0
            complete = True
            for obj in objs:
                part = _int_or_none(obj.get("size")) if isinstance(obj, dict) else None
                if part is None:
                    complete = False
                    break
                total += part
            if complete:
                _add_size(found, total, visible)
    return found


def _object_is_compressed(stat):
    if not isinstance(stat, dict):
        return False
    compression = stat.get("compression")
    if isinstance(compression, dict):
        comp_type = _normalize_compression(compression.get("compression_type"))
        if comp_type != "none" or compression.get("blocks"):
            return True
    elif isinstance(compression, str) and _normalize_compression(compression) != "none":
        return True
    attrs = stat.get("attrs") or {}
    if isinstance(attrs, dict) and any(
        "rgw.compression" in str(key) for key in attrs
    ):
        return True
    return False


def _stat_size_summary(stat):
    if not isinstance(stat, dict):
        return "object stat unavailable"
    manifest = stat.get("manifest") if isinstance(stat.get("manifest"), dict) else {}
    compression = stat.get("compression")
    if isinstance(compression, dict):
        blocks = compression.get("blocks") or []
        compression_text = (
            f"type={compression.get('compression_type')} "
            f"orig_size={compression.get('orig_size')} blocks={len(blocks)} "
            f"stored={_compression_block_total(compression)}"
        )
    else:
        compression_text = repr(compression)
    return (
        f"size={stat.get('size')} accounted_size={stat.get('accounted_size')} "
        f"head_size={manifest.get('head_size')} compression={compression_text}"
    )


def _normalize_compression(value):
    text = "" if value is None else str(value).strip()
    if text.lower() in ("", "none"):
        return "none"
    return text


def _run_case(failures, label, fn):
    try:
        result = fn()
        if result:
            failures.extend(result)
    except Exception as exc:
        log.error("%s failed: %s", label, exc)
        failures.append(f"{label} failed: {exc}")


def _setup_then(setup, action):
    setup()
    return action()


def _size_header_value(params):
    parts = params.split()
    if "--if-match-size" not in parts:
        return None
    index = parts.index("--if-match-size")
    if index + 1 >= len(parts):
        return ""
    return parts[index + 1]


def _head_result(cli_aws, bucket_name, object_name, endpoint):
    command = cli_aws.command(
        operation="head-object",
        params=[
            f"--bucket {bucket_name} --key {object_name} "
            f"--endpoint-url {endpoint}"
        ],
    )
    out = utils.exec_shell_cmd(command, return_err=True)
    if not isinstance(out, str) or not out.strip():
        return None, False
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and "ContentLength" in data:
        return int(data["ContentLength"]), False
    not_found = _object_not_found(out) or "404" in out.lower()
    return None, not_found


def _object_stat(bucket_name, object_name):
    command = (
        "radosgw-admin object stat "
        f"--bucket {shlex.quote(bucket_name)} "
        f"--object {shlex.quote(object_name)}"
    )
    out = utils.exec_shell_cmd(command)
    if not isinstance(out, str) or not out.strip():
        log.info("object stat failed for %s/%s", bucket_name, object_name)
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        log.info("object stat for %s was not JSON", object_name)
        return None


def _listed_size(cli_aws, bucket_name, object_name, endpoint):
    version_list = _load_object_versions(cli_aws, bucket_name, endpoint)
    versions = _versions_for(version_list, object_name)
    if not versions:
        return None
    latest = [version for version in versions if version.get("IsLatest")]
    chosen = latest[0] if latest else versions[0]
    return int(chosen["Size"])


def _delete_with_size(cli_aws, bucket_name, object_name, endpoint, size):
    """size is None when the size header is omitted. size 0 is sent."""
    params = _delete_condition_params(
        bucket_name, object_name, endpoint, size=size
    )
    sent = _size_header_value(params)
    if size is None:
        if sent is not None:
            raise AssertionError(
                f"size header was included for {object_name} when no size "
                "condition was requested"
            )
    elif sent != str(int(size)):
        raise AssertionError(
            f"--if-match-size {int(size)} was not included for {object_name}; "
            f"sent={sent!r}"
        )
    text, _ok = _aws_debug_text(cli_aws, "delete-object", params)
    log.info(
        "size conditional delete for %s http_412=%s",
        object_name,
        _response_is_412(text),
    )
    return text


def _removed_by_size(
    cli_aws, bucket_name, object_name, endpoint, size, reason
):
    out = _delete_with_size(
        cli_aws, bucket_name, object_name, endpoint, size
    )
    remaining, not_found = _head_result(
        cli_aws, bucket_name, object_name, endpoint
    )
    if _delete_succeeded(out) and not_found:
        return []
    blocked = " A 412 blocks a valid delete." if _response_is_412(out) else ""
    return [
        f"delete of {object_name} {reason} must succeed and remove the "
        f"object.{blocked} debug_412={_response_is_412(out)} "
        f"debug_excerpt={_debug_excerpt(out)!r} content_length={remaining} "
        f"not_found={not_found}"
    ]


def _kept_when_size_differs(
    cli_aws, bucket_name, object_name, endpoint, raw, visible
):
    out = _delete_with_size(
        cli_aws, bucket_name, object_name, endpoint, raw
    )
    remaining, not_found = _head_result(
        cli_aws, bucket_name, object_name, endpoint
    )
    if not_found or _delete_succeeded(out):
        return [
            f"delete of {object_name} with stored size {raw} removed an "
            f"object whose visible Content-Length is {visible}. "
            f"debug_412={_response_is_412(out)} "
            f"debug_excerpt={_debug_excerpt(out)!r}"
        ]
    if _response_is_412(out) and remaining == visible:
        return []
    return [
        f"delete of {object_name} with stored size {raw} must return 412 "
        f"and leave the object at Content-Length {visible}. "
        f"debug_412={_response_is_412(out)} "
        f"debug_excerpt={_debug_excerpt(out)!r} content_length={remaining}"
    ]


def _put_bytes(cli_aws, bucket_name, object_name, endpoint, size):
    if size != 0:
        _put_sized_object(cli_aws, bucket_name, object_name, endpoint, size)
        return
    body = (
        os.path.join(TEST_DATA_PATH, object_name)
        if TEST_DATA_PATH
        else object_name
    )
    with open(body, "wb"):
        pass
    aws_reusable.put_object(
        cli_aws, bucket_name, object_name, endpoint, body=body
    )


def _put_multipart_object(cli_aws, bucket_name, object_name, endpoint):
    if not TEST_DATA_PATH:
        raise AssertionError("TEST_DATA_PATH is not set")
    multipart_config = argparse.Namespace(
        obj_size=6 * 1024 * 1024,
        split_size=5,
        local_file_delete=True,
    )
    result = aws_reusable.upload_multipart_aws(
        cli_aws,
        bucket_name,
        object_name,
        TEST_DATA_PATH,
        endpoint,
        multipart_config,
    )
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict):
        raise AssertionError(
            f"multipart upload failed for {object_name}: {result!r}"
        )
    etag = str(result.get("ETag", "")).strip('"')
    if "-" not in etag:
        raise AssertionError(
            f"multipart complete ETag {etag!r} for {object_name} is not a "
            "multipart ETag"
        )
    log.info("multipart %s ETag=%s", object_name, etag)


def _exercise_size_fields(
    cli_aws,
    bucket_name,
    object_name,
    endpoint,
    kind,
    expected_visible=None,
    require_compressed=False,
):
    failures = []
    visible, not_found = _head_result(
        cli_aws, bucket_name, object_name, endpoint
    )
    if visible is None:
        return [
            f"{kind} {object_name} has no Content-Length before the size "
            f"delete. not_found={not_found}"
        ]
    if expected_visible is not None and visible != expected_visible:
        return [
            f"{kind} {object_name} Content-Length is {visible}, expected "
            f"{expected_visible}"
        ]
    stat = _object_stat(bucket_name, object_name)
    if stat is None:
        failures.append(
            f"{kind} {object_name} object stat failed, so the raw stored "
            f"size was not compared with Content-Length {visible}"
        )
        stored = []
    else:
        log.info("%s %s stat %s", kind, object_name, _stat_size_summary(stat))
        if require_compressed and not _object_is_compressed(stat):
            failures.append(
                f"{kind} {object_name} is not stored compressed"
            )
        stored = _stored_sizes_differing_from(stat, visible)
    try:
        listed = _listed_size(cli_aws, bucket_name, object_name, endpoint)
    except AssertionError as exc:
        failures.append(
            f"{kind} {object_name} list-object-versions failed: {exc}"
        )
        listed = None
    if listed is not None and listed != visible and listed not in stored:
        stored.append(listed)
    if stored:
        log.info(
            "%s %s Content-Length=%s stored sizes that differ=%s",
            kind,
            object_name,
            visible,
            stored,
        )
    else:
        log.info(
            "%s %s Content-Length=%s matches the stored size fields",
            kind,
            object_name,
            visible,
        )
    for raw in stored:
        still_there, gone = _head_result(
            cli_aws, bucket_name, object_name, endpoint
        )
        if gone or still_there is None:
            break
        failures.extend(
            _kept_when_size_differs(
                cli_aws,
                bucket_name,
                object_name,
                endpoint,
                raw,
                visible,
            )
        )
    remaining, gone = _head_result(cli_aws, bucket_name, object_name, endpoint)
    if gone or remaining is None:
        if not failures:
            failures.append(
                f"{kind} {object_name} was gone before the delete with "
                f"Content-Length {visible}"
            )
        return failures
    reason = (
        "with size 0"
        if visible == 0
        else f"with Content-Length {visible}, the size the client sees"
    )
    failures.extend(
        _removed_by_size(
            cli_aws,
            bucket_name,
            object_name,
            endpoint,
            visible,
            reason,
        )
    )
    return failures


def _exercise_size_omitted(
    cli_aws, bucket_name, object_name, endpoint, kind, expected_visible=None
):
    visible, not_found = _head_result(
        cli_aws, bucket_name, object_name, endpoint
    )
    if visible is None:
        return [
            f"{kind} {object_name} is missing before the delete with no "
            f"size header. not_found={not_found}"
        ]
    if expected_visible is not None and visible != expected_visible:
        return [
            f"{kind} {object_name} Content-Length is {visible}, expected "
            f"{expected_visible}"
        ]
    return _removed_by_size(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        None,
        "with the size header omitted, because no size condition was "
        "requested",
    )


def _zone_placement_compression():
    raw = utils.exec_shell_cmd("radosgw-admin zone get")
    if not isinstance(raw, str) or not raw.strip():
        raise AssertionError("radosgw-admin zone get failed")
    zone = json.loads(raw)
    zone_name = zone.get("name")
    if not zone_name:
        raise AssertionError("radosgw-admin zone get returned no zone name")
    for pool in zone.get("placement_pools") or []:
        if not isinstance(pool, dict) or pool.get("key") != "default-placement":
            continue
        val = pool.get("val") or {}
        classes = val.get("storage_classes") or {}
        standard = classes.get("STANDARD") if isinstance(classes, dict) else None
        if isinstance(standard, dict) and standard.get("compression_type"):
            compression = standard.get("compression_type")
        else:
            compression = val.get("compression")
        return zone_name, _normalize_compression(compression)
    raise AssertionError("default-placement was not found in the zone")


def _set_placement_compression(zone_name, compression_type):
    command = (
        "radosgw-admin zone placement modify "
        f"--rgw-zone={shlex.quote(zone_name)} "
        "--placement-id=default-placement "
        "--storage-class=STANDARD "
        f"--compression={shlex.quote(compression_type)}"
    )
    log.info("set placement compression: %s", command)
    out = utils.exec_shell_cmd(command)
    if out is False:
        raise AssertionError(
            f"failed to set placement compression to {compression_type}"
        )
    commit = utils.exec_shell_cmd("radosgw-admin period update --commit")
    if commit is False:
        log.info(
            "period update --commit did not succeed; the RGW restart applies "
            "the placement compression change"
        )


def _compressed_size_cases(cli_aws, bucket_name, endpoint, ssh_con, keys):
    failures = []
    compression_changed = False
    zone_name = None
    original = "none"
    ready = True
    try:
        zone_name, original = _zone_placement_compression()
        log.info(
            "placement compression is %s on zone %s", original, zone_name
        )
        if original == "none":
            _set_placement_compression(zone_name, "zlib")
            compression_changed = True
            if not RGWService().restart(ssh_con):
                failures.append(
                    "RGW restart failed after enabling zlib compression"
                )
                ready = False
            else:
                time.sleep(5)
        if ready:
            _run_case(
                failures,
                "compressed size",
                lambda: _setup_then(
                    lambda: _put_bytes(
                        cli_aws,
                        bucket_name,
                        keys["compressed"],
                        endpoint,
                        4 * 1024 * 1024,
                    ),
                    lambda: _exercise_size_fields(
                        cli_aws,
                        bucket_name,
                        keys["compressed"],
                        endpoint,
                        "compressed",
                        require_compressed=True,
                    ),
                ),
            )
            _run_case(
                failures,
                "compressed omit",
                lambda: _setup_then(
                    lambda: _put_bytes(
                        cli_aws,
                        bucket_name,
                        keys["compressed_omit"],
                        endpoint,
                        4 * 1024 * 1024,
                    ),
                    lambda: _exercise_size_omitted(
                        cli_aws,
                        bucket_name,
                        keys["compressed_omit"],
                        endpoint,
                        "compressed",
                    ),
                ),
            )
    except Exception as exc:
        log.error("compressed object setup failed: %s", exc)
        failures.append(f"compressed object setup failed: {exc}")
    finally:
        if compression_changed and zone_name:
            try:
                _set_placement_compression(zone_name, original)
                if not RGWService().restart(ssh_con):
                    failures.append(
                        "RGW restart failed while restoring placement "
                        f"compression to {original}"
                    )
                else:
                    time.sleep(5)
            except Exception as exc:
                failures.append(
                    f"failed to restore placement compression to {original}: "
                    f"{exc}"
                )
    return failures


def _test_size_visible_conditional_delete(
    cli_aws, bucket_name, seeded_object, endpoint, ssh_con
):
    """
    Delete an empty object with size 0, and delete another empty object with
    the size header omitted. Both must succeed. Repeat for a multipart object
    and a compressed object. When HEAD Content-Length and the raw stored size
    differ, send both. The Content-Length delete must remove the object. A
    stored size that is not that Content-Length must return 412.
    """
    keys = {
        "empty": "size-empty-zero",
        "empty_omit": "size-empty-omit",
        "multipart": "size-mp-length",
        "multipart_omit": "size-mp-omit",
        "compressed": "size-comp-length",
        "compressed_omit": "size-comp-omit",
    }
    failures = []
    try:
        _run_case(
            failures,
            "seeded object",
            lambda: _purge_object(cli_aws, bucket_name, seeded_object, endpoint)
            or [],
        )
        _run_case(
            failures,
            "empty size 0",
            lambda: _setup_then(
                lambda: _put_bytes(
                    cli_aws, bucket_name, keys["empty"], endpoint, 0
                ),
                lambda: _exercise_size_fields(
                    cli_aws,
                    bucket_name,
                    keys["empty"],
                    endpoint,
                    "empty",
                    expected_visible=0,
                ),
            ),
        )
        _run_case(
            failures,
            "empty size omitted",
            lambda: _setup_then(
                lambda: _put_bytes(
                    cli_aws, bucket_name, keys["empty_omit"], endpoint, 0
                ),
                lambda: _exercise_size_omitted(
                    cli_aws,
                    bucket_name,
                    keys["empty_omit"],
                    endpoint,
                    "empty",
                    expected_visible=0,
                ),
            ),
        )
        _run_case(
            failures,
            "multipart size",
            lambda: _setup_then(
                lambda: _put_multipart_object(
                    cli_aws, bucket_name, keys["multipart"], endpoint
                ),
                lambda: _exercise_size_fields(
                    cli_aws,
                    bucket_name,
                    keys["multipart"],
                    endpoint,
                    "multipart",
                ),
            ),
        )
        _run_case(
            failures,
            "multipart size omitted",
            lambda: _setup_then(
                lambda: _put_multipart_object(
                    cli_aws, bucket_name, keys["multipart_omit"], endpoint
                ),
                lambda: _exercise_size_omitted(
                    cli_aws,
                    bucket_name,
                    keys["multipart_omit"],
                    endpoint,
                    "multipart",
                ),
            ),
        )
        failures.extend(
            _compressed_size_cases(
                cli_aws, bucket_name, endpoint, ssh_con, keys
            )
        )
    finally:
        for key in keys.values():
            try:
                _purge_object(cli_aws, bucket_name, key, endpoint)
            except Exception as exc:
                failures.append(f"cleanup of {key} failed: {exc}")
    if failures:
        for failure in failures:
            log.error(failure)
        raise AssertionError(
            "An empty object deleted with size 0 must succeed, and the same "
            "delete with the size header omitted must succeed because no "
            "size condition was requested. A multipart object and a "
            "compressed object repeat that delete. When HEAD Content-Length "
            "and the raw stored size differ, both are sent. "
            + " ".join(failures)
            + " Matching the wrong size field either blocks a valid delete "
            "or deletes an object whose visible size did not match."
        )


def _partial_match_case(
    cli_aws, bucket_name, object_name, endpoint, ceph_major, path_name, mode
):
    """
    mode etag-size sends the object ETag and a different size.
    mode size-mtime sends the object size and a different mtime.
    """
    _put_sized_object(cli_aws, bucket_name, object_name, endpoint, 1024)
    before, markers = _object_attrs(
        cli_aws, bucket_name, object_name, endpoint, ceph_major
    )
    if before is None or markers:
        return [
            f"{path_name} {object_name} is missing after the put. "
            f"attrs={before} delete_markers={len(markers)}"
        ]
    if mode == "etag-size":
        etag = before["etag"]
        size = before["size"] + 1
        last_modified_time = None
        matched = "ETag"
        mismatched = f"size {size}"
    else:
        etag = None
        size = before["size"]
        last_modified_time = "2000-01-01T00:00:00"
        if before["lmt"].startswith(last_modified_time):
            last_modified_time = "1999-01-01T00:00:00"
        matched = "size"
        mismatched = f"mtime {last_modified_time}"
    log.info(
        "%s delete %s with correct %s and wrong %s",
        path_name,
        object_name,
        matched,
        mismatched,
    )
    out = _conditional_delete_debug(
        cli_aws,
        bucket_name,
        object_name,
        endpoint,
        etag=etag,
        size=size,
        last_modified_time=last_modified_time,
    )
    after, after_markers = _object_attrs(
        cli_aws, bucket_name, object_name, endpoint, ceph_major
    )
    kept = (
        _response_is_412(out)
        and not after_markers
        and after is not None
        and after["etag"] == before["etag"]
        and after["size"] == before["size"]
        and after["version_id"] == before["version_id"]
    )
    if kept:
        return []
    return [
        f"{path_name} delete of {object_name} with correct {matched} and "
        f"wrong {mismatched} must return 412 and leave the object in place. "
        f"debug_412={_response_is_412(out)} "
        f"debug_excerpt={_debug_excerpt(out)!r} current={after} "
        f"delete_markers={len(after_markers)} "
        f"expected_etag={before['etag']} expected_size={before['size']}. "
        "A delete when only part of the set matched is the failure."
    ]


def _test_partial_match_conditional_delete(
    cli_aws, bucket_name, seeded_object, endpoint, ceph_major
):
    """
    Send a correct ETag with a wrong size, then a correct size with a wrong
    mtime. Each mismatch must return 412 and leave the object in place.
    """
    cases = (
        ("etag-size", "multi-etag-size"),
        ("size-mtime", "multi-size-mtime"),
    )
    failures = []
    for mode, key in cases:
        _run_case(
            failures,
            f"unversioned {mode}",
            lambda mode=mode, key=key: _partial_match_case(
                cli_aws,
                bucket_name,
                key,
                endpoint,
                ceph_major,
                "unversioned",
                mode,
            ),
        )
    for _, key in cases:
        _run_case(
            failures,
            f"cleanup {key}",
            lambda key=key: _purge_object(cli_aws, bucket_name, key, endpoint)
            or [],
        )
    _run_case(
        failures,
        "seeded object",
        lambda: _purge_object(cli_aws, bucket_name, seeded_object, endpoint)
        or [],
    )

    log.info("enable versioning for partial-match deletes with no version id")
    try:
        aws_reusable.put_get_bucket_versioning(cli_aws, bucket_name, endpoint)
        for mode, key in cases:
            versioned_key = key + "-ver"
            _run_case(
                failures,
                f"versioned {mode}",
                lambda mode=mode, key=versioned_key: _partial_match_case(
                    cli_aws,
                    bucket_name,
                    key,
                    endpoint,
                    ceph_major,
                    "versioned",
                    mode,
                ),
            )
    except Exception as exc:
        log.error("versioned partial-match setup failed: %s", exc)
        failures.append(f"versioned partial-match setup failed: {exc}")
    for _, key in cases:
        _run_case(
            failures,
            f"cleanup {key}-ver",
            lambda key=key: _purge_object(
                cli_aws, bucket_name, key + "-ver", endpoint
            )
            or [],
        )
    if failures:
        for failure in failures:
            log.error(failure)
        raise AssertionError(
            "A correct ETag with a wrong size, and a correct size with a "
            "wrong mtime, must each return 412 and leave the object in "
            "place. "
            + " ".join(failures)
            + " A delete when only part of the set matched is the failure."
        )


def _local_xml_name(tag):
    return str(tag).rsplit("}", 1)[-1]


def _is_precondition_error(item):
    if not isinstance(item, dict):
        return False
    blob = f"{item.get('Code', '')} {item.get('Message', '')}"
    compact = blob.lower().replace("_", "").replace("-", "").replace(" ", "")
    return "preconditionfailed" in compact


def _raw_has_precondition(raw):
    if not isinstance(raw, str):
        return False
    compact = raw.lower().replace("_", "").replace("-", "").replace(" ", "")
    return "preconditionfailed" in compact


def _entries_for_key(items, object_name):
    if not isinstance(items, list):
        return []
    matched = [
        item
        for item in items
        if isinstance(item, dict) and item.get("Key") == object_name
    ]
    if matched:
        return matched
    return [
        item for item in items if isinstance(item, dict) and not item.get("Key")
    ]


def _delete_result_json(raw):
    """Find the DeleteObjects result inside a plain body or an aws --debug log."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    decoder = json.JSONDecoder()
    start = 0
    while True:
        index = raw.find("{", start)
        if index < 0:
            return None
        try:
            data, _end = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            start = index + 1
            continue
        if isinstance(data, dict) and (
            "Deleted" in data or "Errors" in data or "Error" in data
        ):
            return data
        start = index + 1


def _split_delete_objects_body(raw):
    """Return deleted entries, error entries, and whether a result was found."""
    data = _delete_result_json(raw)
    if not isinstance(data, dict):
        return [], [], False
    deleted = data.get("Deleted") or []
    errors = data.get("Errors")
    if errors is None:
        errors = data.get("Error") or []
    if isinstance(deleted, dict):
        deleted = [deleted]
    if isinstance(errors, dict):
        errors = [errors]
    if not isinstance(deleted, list):
        deleted = []
    if not isinstance(errors, list):
        errors = []
    return deleted, errors, True


def _parse_delete_result_xml(text):
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [], []
    if _local_xml_name(root.tag) == "Error":
        fields = {_local_xml_name(child.tag): (child.text or "") for child in root}
        return [], [fields]
    deleted = []
    errors = []
    for node in list(root):
        kind = _local_xml_name(node.tag)
        fields = {_local_xml_name(child.tag): (child.text or "") for child in node}
        if kind == "Deleted":
            deleted.append(fields)
        elif kind == "Error":
            errors.append(fields)
    return deleted, errors


def _instance_from_olh(data):
    if not isinstance(data, dict):
        return None
    target = data.get("target")
    if isinstance(target, dict) and target.get("instance"):
        return str(target["instance"])
    for key in ("olh", "entry", "olh_meta"):
        found = _instance_from_olh(data.get(key))
        if found:
            return found
    key_info = data.get("key")
    if isinstance(key_info, dict) and key_info.get("instance"):
        return str(key_info["instance"])
    return None


def _misspelled_etag_body(object_name, bad_etag):
    """XML Delete body. The element is Etag, not ETag."""
    import xml.sax.saxutils

    safe_key = xml.sax.saxutils.escape(str(object_name))
    safe_etag = xml.sax.saxutils.escape(str(bad_etag))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Delete><Object>"
        f"<Key>{safe_key}</Key><Etag>{safe_etag}</Etag>"
        "</Object><Quiet>false</Quiet></Delete>"
    )


def _request_not_parsed(status, text):
    if status in (401, 403):
        return True
    lowered = (text or "").lower()
    return any(
        token in lowered
        for token in (
            "signaturedoesnotmatch",
            "missingcontentmd5",
            "invalidaccesskeyid",
            "accessdenied",
            "requesttimetooskewed",
        )
    )


def _olh_current_instance(bucket_name, object_name):
    command = (
        "radosgw-admin olh get "
        f"--bucket {shlex.quote(bucket_name)} "
        f"--object {shlex.quote(object_name)}"
    )
    out = utils.exec_shell_cmd(command, return_err=True)
    if not isinstance(out, str) or not out.strip():
        return None, "olh get returned nothing"
    start = out.find("{")
    end = out.rfind("}")
    if start == -1 or end <= start:
        return None, f"olh get was not JSON: {out[:400]!r}"
    try:
        data = json.loads(out[start : end + 1])
    except json.JSONDecodeError:
        return None, f"olh get was not JSON: {out[:400]!r}"
    instance = _instance_from_olh(data)
    if not instance:
        return None, f"olh get has no target instance: {data!r}"
    log.info("olh instance for %s is %s", object_name, instance)
    return instance, None


def _one_delete_marker_problems(
    cli_aws, bucket_name, object_name, endpoint, original_version_id, label
):
    versions, markers = _key_listing(cli_aws, bucket_name, object_name, endpoint)
    latest_markers = [marker for marker in markers if marker.get("IsLatest")]
    original = _version_with_id(versions, original_version_id)
    problems = []
    if len(markers) > 1:
        problems.append(
            f"{label} wrote {len(markers)} delete markers for {object_name}"
        )
    elif len(markers) != 1 or len(latest_markers) != 1:
        problems.append(
            f"{label} must delete {object_name} exactly once. "
            f"delete_markers={len(markers)} latest_markers={len(latest_markers)}"
        )
    if original is None:
        problems.append(
            f"{label} removed version {original_version_id} instead of "
            "adding one delete marker"
        )
    elif original.get("IsLatest"):
        problems.append(
            f"{label} left version {original_version_id} current"
        )
    if len(latest_markers) == 1:
        marker_id = latest_markers[0].get("VersionId")
        instance, olh_problem = _olh_current_instance(bucket_name, object_name)
        if olh_problem:
            problems.append(f"{label} {olh_problem}")
        elif instance != marker_id:
            problems.append(
                f"{label} current version {marker_id} does not match the "
                f"index OLH instance {instance}"
            )
    else:
        instance, olh_problem = _olh_current_instance(bucket_name, object_name)
        if instance and instance != original_version_id:
            problems.append(
                f"{label} index OLH instance {instance} does not match a "
                "current delete marker"
            )
        elif olh_problem and len(markers) == 1:
            problems.append(f"{label} {olh_problem}")
    return problems


def _current_version_fact(cli_aws, bucket_name, object_name, endpoint):
    versions, markers = _key_listing(cli_aws, bucket_name, object_name, endpoint)
    latest = [
        version for version in versions if version.get("IsLatest")
    ]
    if markers or len(latest) != 1:
        return None
    version_id = latest[0].get("VersionId")
    if not version_id or version_id == "null":
        return None
    return {
        "etag": _etag_from_version(latest[0]),
        "version_id": version_id,
    }


def _same_key_multi_delete(
    cli_aws, bucket_name, object_name, endpoint, quiet
):
    """
    One DeleteObjects body: the same key, bad ETag first, then the real ETag.
    Earlier entries skip the OLH update. Every entry is checked against the
    original current version.
    """
    label = "quiet" if quiet else "verbose"
    _put_sized_object(cli_aws, bucket_name, object_name, endpoint, 1024)
    current = _current_version_fact(cli_aws, bucket_name, object_name, endpoint)
    if current is None:
        return [
            f"{label} {object_name} did not have one current version before "
            "DeleteObjects"
        ]
    objects = [
        {"Key": object_name, "ETag": "wrong-etag"},
        {"Key": object_name, "ETag": current["etag"]},
    ]
    log.info(
        "%s DeleteObjects %s bad ETag then real ETag %s quiet=%s",
        label,
        object_name,
        current["etag"],
        quiet,
    )
    raw = aws_reusable.delete_objects(
        cli_aws,
        bucket_name,
        objects,
        endpoint,
        quiet=quiet,
        return_err=True,
        debug=True,
    )
    log.info(
        "%s DeleteObjects http_412=%s excerpt=%s",
        label,
        _response_is_412(raw),
        _debug_excerpt(raw),
    )
    deleted, errors, parsed = _split_delete_objects_body(raw)
    key_deleted = _entries_for_key(deleted, object_name)
    key_errors = _entries_for_key(errors, object_name)
    has_precondition = any(_is_precondition_error(item) for item in key_errors)
    if not has_precondition:
        has_precondition = _response_is_412(raw)
    problems = []
    if not has_precondition:
        problems.append(
            f"{label} bad ETag entry for {object_name} must be "
            f"PreconditionFailed. debug_412={_response_is_412(raw)} "
            f"debug_excerpt={_debug_excerpt(raw)!r}"
        )
    if not parsed:
        problems.append(
            f"{label} DeleteObjects response for {object_name} was not JSON: "
            f"debug_excerpt={_debug_excerpt(raw)!r}"
        )
    if not quiet and len(key_deleted) != 1:
        problems.append(
            f"{label} good ETag entry for {object_name} must delete exactly "
            f"once. deleted={len(key_deleted)} "
            f"debug_excerpt={_debug_excerpt(raw)!r}"
        )
    elif quiet and len(key_deleted) > 1:
        problems.append(
            f"{label} DeleteObjects reported {len(key_deleted)} deletes for "
            f"{object_name}"
        )
    problems.extend(
        _one_delete_marker_problems(
            cli_aws,
            bucket_name,
            object_name,
            endpoint,
            current["version_id"],
            label,
        )
    )
    return problems


def _post_delete_objects_xml(
    endpoint, bucket_name, access_key, secret_key, xml_body
):
    """POST a DeleteObjects body the AWS CLI would not send unchanged."""
    import base64
    import hashlib
    import ssl
    import urllib.error
    import urllib.request

    try:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
    except ImportError as exc:
        return None, f"botocore is required to send a misspelled Etag element: {exc}"
    payload = xml_body.encode("utf-8")
    content_md5 = base64.b64encode(hashlib.md5(payload).digest()).decode("ascii")
    url = endpoint.rstrip("/") + "/" + bucket_name + "?delete"
    headers = {
        "Content-Type": "application/xml",
        "Content-MD5": content_md5,
    }
    request = AWSRequest(method="POST", url=url, data=payload, headers=headers)
    SigV4Auth(Credentials(access_key, secret_key), "s3", "us-east-1").add_auth(
        request
    )
    header_map = {str(key): str(value) for key, value in request.headers.items()}
    body = request.body if isinstance(request.body, bytes) else payload
    req = urllib.request.Request(
        request.url, data=body, headers=header_map, method="POST"
    )
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, context=context, timeout=60) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _misspelled_etag_delete(
    cli_aws, bucket_name, object_name, endpoint, access_key, secret_key
):
    """
    Send Etag, not ETag, with a value that does not match. If the parser
    ignores the element, the object is deleted with no condition.
    """
    _put_sized_object(cli_aws, bucket_name, object_name, endpoint, 1024)
    current = _current_version_fact(cli_aws, bucket_name, object_name, endpoint)
    if current is None:
        return [
            f"{object_name} did not have one current version before the "
            "misspelled Etag delete"
        ]
    xml_body = _misspelled_etag_body(object_name, "wrong-etag")
    if "<Etag>" not in xml_body or "<ETag>" in xml_body:
        return [f"misspelled delete body did not contain an Etag element: {xml_body}"]
    log.info("DeleteObjects %s with misspelled Etag element", object_name)
    status, text = _post_delete_objects_xml(
        endpoint, bucket_name, access_key, secret_key, xml_body
    )
    log.info("misspelled Etag response status=%s body=%s", status, text)
    versions, markers = _key_listing(cli_aws, bucket_name, object_name, endpoint)
    original = _version_with_id(versions, current["version_id"])
    still_current = (
        not markers
        and original is not None
        and original.get("IsLatest")
        and _etag_from_version(original) == current["etag"]
    )
    if not still_current:
        return [
            f"misspelled Etag delete of {object_name} removed the object "
            f"with no condition. status={status} delete_markers={len(markers)} "
            f"version_kept={original is not None} response={text!r}"
        ]
    if status is None or _request_not_parsed(status, text):
        return [
            f"misspelled Etag request for {object_name} did not reach the "
            f"DeleteObjects parser. status={status} response={text!r}"
        ]
    if status != 412 and not _precondition_failed(text):
        return [
            f"misspelled Etag delete of {object_name} must return 412 when "
            f"the non-matching value is applied. status={status} "
            f"response={text[:400]!r}"
        ]
    instance, olh_problem = _olh_current_instance(bucket_name, object_name)
    if olh_problem:
        return [f"misspelled Etag {object_name} {olh_problem}"]
    if instance != current["version_id"]:
        return [
            f"misspelled Etag delete left current version "
            f"{current['version_id']} while the index OLH instance is "
            f"{instance}"
        ]
    return []


def _test_multi_delete_same_key(
    cli_aws, bucket_name, seeded_object, endpoint, access_key, secret_key
):
    """
    On a versioned bucket, one DeleteObjects request lists the same key
    twice. The first entry has a bad ETag and the second has the real ETag.
    """
    keys = {
        "verbose": "multi-same-verbose",
        "quiet": "multi-same-quiet",
        "misspell": "multi-etag-spell",
    }
    failures = []
    versioned = True
    try:
        _run_case(
            failures,
            "seeded object",
            lambda: _purge_object(cli_aws, bucket_name, seeded_object, endpoint)
            or [],
        )
        log.info(
            "enable versioning for DeleteObjects of the same key; earlier "
            "entries skip the OLH update"
        )
        try:
            aws_reusable.put_get_bucket_versioning(cli_aws, bucket_name, endpoint)
        except Exception as exc:
            versioned = False
            log.error("versioned multi-delete setup failed: %s", exc)
            failures.append(f"versioned multi-delete setup failed: {exc}")
        if versioned:
            _run_case(
                failures,
                "verbose same-key delete",
                lambda: _same_key_multi_delete(
                    cli_aws,
                    bucket_name,
                    keys["verbose"],
                    endpoint,
                    quiet=False,
                ),
            )
            _run_case(
                failures,
                "quiet same-key delete",
                lambda: _same_key_multi_delete(
                    cli_aws,
                    bucket_name,
                    keys["quiet"],
                    endpoint,
                    quiet=True,
                ),
            )
            _run_case(
                failures,
                "misspelled Etag",
                lambda: _misspelled_etag_delete(
                    cli_aws,
                    bucket_name,
                    keys["misspell"],
                    endpoint,
                    access_key,
                    secret_key,
                ),
            )
    finally:
        for key in keys.values():
            _run_case(
                failures,
                f"cleanup {key}",
                lambda key=key: _purge_object(cli_aws, bucket_name, key, endpoint)
                or [],
            )
    if failures:
        for failure in failures:
            log.error(failure)
        raise AssertionError(
            "One DeleteObjects body lists the same key twice, first with a "
            "bad ETag and then with the real ETag. The bad entry must be "
            "PreconditionFailed and the good entry must delete exactly once. "
            + " ".join(failures)
            + " Two delete markers, or a current version that does not match "
            "the index, is a failure. With Quiet set, the success may be "
            "hidden, but PreconditionFailed must still be returned. A "
            "misspelled Etag element must not delete the object with no "
            "condition."
        )


def test_exec(config, ssh_con):
    """
    Executes test based on configuration passed
    Args:
        config(object): Test configuration
    """
    io_info_initialize = IOInfoInitialize()
    basic_io_structure = BasicIOInfoStructure()
    io_info_initialize.initialize(basic_io_structure.initial())
    ceph_conf = CephConfOp()
    rgw_service = RGWService()

    user_name = (config.test_ops.get("user_name"), None)
    user_names = [user_name] if type(user_name) != list else user_name

    endpoint = aws_reusable.get_endpoint(
        ssh_con, ssl=config.ssl, haproxy=config.haproxy
    )

    ceph_version_id, _ = utils.get_ceph_version()
    ceph_version_id = ceph_version_id.split("-")
    ceph_version_id = ceph_version_id[0].split(".")

    if config.test_ops.get("user_name", False):
        op = json.loads(utils.exec_shell_cmd("radosgw-admin user list"))
        log.info(f"user {config.test_ops['user_name']} exist in cluster {op}")
        if config.test_ops["user_name"] not in op:
            log.info(f"number of user to create is {config.user_count}")
            user_info = resource_op.create_users(
                no_of_users_to_create=config.user_count,
                user_names=user_names,
            )
        elif config.user_count == 1:
            out = json.loads(
                utils.exec_shell_cmd(
                    f"radosgw-admin user info --uid={config.test_ops['user_name']}"
                )
            )
            user_info = [
                {
                    "user_id": out["user_id"],
                    "display_name": out["display_name"],
                    "access_key": out["keys"][0]["access_key"],
                    "secret_key": out["keys"][0]["secret_key"],
                }
            ]
    else:
        user_info = resource_op.create_users(no_of_users_to_create=config.user_count)

    for user in user_info:
        user_name = user["user_id"]
        log.info(user_name)
        cli_aws = AWS(ssl=config.ssl)
        aws_auth.do_auth_aws(user)
        incorrect = False
        gc_verify = True

        try:
            for bc in range(config.bucket_count):
                if config.test_ops.get("regular_and_version", False):
                    bkt_suffix = bc + 1
                    reg_bucket_name = config.test_ops["reg_bucket_name"] + f"{bkt_suffix}"
                    ver_bucket_name = config.test_ops["ver_bucket_name"] + f"{bkt_suffix}"
                    aws_reusable.create_bucket(cli_aws, reg_bucket_name, endpoint)
                    aws_reusable.create_bucket(cli_aws, ver_bucket_name, endpoint)
                    log.info(f"Bucket {reg_bucket_name} and {ver_bucket_name} created")
                    log.info(f"bucket versioning enabled on bucket: {ver_bucket_name}")
                    aws_reusable.put_get_bucket_versioning(
                        cli_aws, ver_bucket_name, endpoint
                    )

                else:
                    if config.test_ops.get("bucket_name", False):
                        bkt_suffix = bc + 1
                        bucket_name = config.test_ops["bucket_name"] + f"{bkt_suffix}"
                    else:
                        bucket_name = utils.gen_bucket_name_from_userid(
                            user_name, rand_no=bc
                        )
                    aws_reusable.create_bucket(cli_aws, bucket_name, endpoint)
                    log.info(f"Bucket {bucket_name} created")

                    if config.test_ops.get("enable_version", False):
                        log.info(f"bucket versioning test on bucket: {bucket_name}")
                        aws_reusable.put_get_bucket_versioning(
                            cli_aws, bucket_name, endpoint
                        )
                    object_name = "testobj"
                    utils.exec_shell_cmd(f"fallocate -l 6M {object_name}")
                    aws_reusable.put_object(cli_aws, bucket_name, object_name, endpoint)
                if config.test_ops.get("conditional_put_if_none_match", False):
                    if config.test_ops.get("enable_version", False):
                        gc_verify = False
                    log.info("Validate conditional Put operations with if-none-match")
                    object_name = "testobj-new"
                    utils.exec_shell_cmd(f"fallocate -l 6M {object_name}")
                    log.info("Validate conditional put with if-none-match * for 1st upload")
                    first_put = _conditional_put_debug(
                        cli_aws, bucket_name, object_name, endpoint
                    )
                    if not _delete_succeeded(first_put):
                        raise AssertionError(
                            "first upload with if-none-match must succeed. "
                            f"debug_412={_response_is_412(first_put)} "
                            f"debug_excerpt={_debug_excerpt(first_put)!r}"
                        )
                    log.info("Validate conditional put with if-none-match * for 2nd upload")
                    err = _conditional_put_debug(
                        cli_aws, bucket_name, object_name, endpoint
                    )
                    if not _response_is_412(err):
                        raise AssertionError(
                            "2nd upload of the same object with if-none-match "
                            "must return 412. "
                            f"debug_excerpt={_debug_excerpt(err)!r}"
                        )
                if config.test_ops.get("conditional_put_if_match", False):
                    if config.test_ops.get("enable_version", False):
                        gc_verify = False
                    log.info("Validate conditional Put operations with if-match")
                    version_list = json.loads(
                        aws_reusable.list_object_versions(cli_aws, bucket_name, endpoint)
                    )
                    etag = version_list["Versions"][0]["ETag"].split('"')[1]
                    incorrect_etag = etag[:-1]
                    log.info("Validate conditional put with if-match with incorret etag")
                    err = _conditional_put_debug(
                        cli_aws,
                        bucket_name,
                        object_name,
                        endpoint,
                        etag=incorrect_etag,
                    )
                    if not _response_is_412(err):
                        raise AssertionError(
                            "conditional put with if-match and an incorrect ETag "
                            "must return 412. "
                            f"debug_excerpt={_debug_excerpt(err)!r}"
                        )
                    log.info("Validate conditional put with if-match with correct Etag")
                    matched_put = _conditional_put_debug(
                        cli_aws, bucket_name, object_name, endpoint, etag=etag
                    )
                    if not _delete_succeeded(matched_put):
                        raise AssertionError(
                            "conditional put with the matching ETag must succeed. "
                            f"debug_412={_response_is_412(matched_put)} "
                            f"debug_excerpt={_debug_excerpt(matched_put)!r}"
                        )

                if config.test_ops.get("conditional_put_multipart_if_none_match", False):
                    if config.test_ops.get("enable_version", False):
                        gc_verify = False
                    log.info("Validate conditional Put (multipart) with if-none-match")
                    multipart_key = "testobj-mp"
                    if not hasattr(config, "obj_size") or config.obj_size is None:
                        config.obj_size = 15 * 1024 * 1024
                    if not hasattr(config, "split_size") or config.split_size is None:
                        config.split_size = 5
                    if not hasattr(config, "local_file_delete"):
                        config.local_file_delete = True
                    log.info(
                        "Validate conditional put multipart with if-none-match * for 1st upload"
                    )
                    aws_reusable.conditional_put_multipart_upload(
                        cli_aws,
                        bucket_name,
                        multipart_key,
                        TEST_DATA_PATH,
                        endpoint,
                        config,
                    )
                    log.info(
                        "Validate conditional put multipart with if-none-match * for 2nd upload"
                    )
                    err = aws_reusable.conditional_put_multipart_upload(
                        cli_aws,
                        bucket_name,
                        multipart_key,
                        TEST_DATA_PATH,
                        endpoint,
                        config,
                        return_err=True,
                    )
                    if not isinstance(err, str) or not _response_is_412(err):
                        raise AssertionError(
                            "2nd multipart upload of the same object with "
                            "if-none-match must return 412. "
                            f"debug_excerpt={_debug_excerpt(err)!r}"
                        )

                if config.test_ops.get("conditional_put_multipart_if_match", False):
                    if config.test_ops.get("enable_version", False):
                        gc_verify = False
                    log.info("Validate conditional Put (multipart) with if-match")
                    version_list = json.loads(
                        aws_reusable.list_object_versions(cli_aws, bucket_name, endpoint)
                    )
                    etag = version_list["Versions"][0]["ETag"].split('"')[1]
                    incorrect_etag = etag[:-1]
                    if not hasattr(config, "obj_size") or config.obj_size is None:
                        config.obj_size = 15 * 1024 * 1024
                    if not hasattr(config, "split_size") or config.split_size is None:
                        config.split_size = 5
                    if not hasattr(config, "local_file_delete"):
                        config.local_file_delete = True
                    log.info(
                        "Validate conditional put multipart with if-match with incorrect etag"
                    )
                    err = aws_reusable.conditional_put_multipart_upload(
                        cli_aws,
                        bucket_name,
                        object_name,
                        TEST_DATA_PATH,
                        endpoint,
                        config,
                        etag=incorrect_etag,
                        return_err=True,
                    )
                    if not isinstance(err, str) or not _response_is_412(err):
                        raise AssertionError(
                            "conditional multipart complete with if-match and an "
                            "incorrect ETag must return 412. "
                            f"debug_excerpt={_debug_excerpt(err)!r}"
                        )
                    log.info(
                        "Validate conditional put multipart with if-match with correct Etag"
                    )
                    aws_reusable.conditional_put_multipart_upload(
                        cli_aws,
                        bucket_name,
                        object_name,
                        TEST_DATA_PATH,
                        endpoint,
                        config,
                        etag=etag,
                    )

                if config.test_ops.get("conditional_delete_with_etag", False):
                    log.info("delete object using matching ETAG")
                    version_list = json.loads(
                        aws_reusable.list_object_versions(cli_aws, bucket_name, endpoint)
                    )
                    etag = version_list["Versions"][0]["ETag"].split('"')[1]
                    log.info("Conditional delete object with incorrect Etag")
                    incorrect_etag = etag[:-1]
                    out = _conditional_delete_debug(
                        cli_aws,
                        bucket_name,
                        object_name,
                        endpoint,
                        etag=incorrect_etag,
                    )
                    if not _response_is_412(out):
                        raise AssertionError(
                            "Conditional delete with an incorrect ETag must "
                            "return 412. "
                            f"debug_excerpt={_debug_excerpt(out)!r}"
                        )

                    log.info("Conditional delete object with correct Etag")
                    out = _conditional_delete_debug(
                        cli_aws, bucket_name, object_name, endpoint, etag=etag
                    )
                    if not _delete_succeeded(out):
                        raise AssertionError(
                            "Conditional delete with the matching ETag must "
                            "remove the object. "
                            f"debug_412={_response_is_412(out)} "
                            f"debug_excerpt={_debug_excerpt(out)!r}"
                        )
                if config.test_ops.get("conditional_delete_with_lastmodifiedtime", False):
                    log.info("delete object using matching Last Modified time")
                    version_list = json.loads(
                        aws_reusable.list_object_versions(cli_aws, bucket_name, endpoint)
                    )
                    lmt = version_list["Versions"][0]["LastModified"].split(".")[0]
                    if float(ceph_version_id[0]) >= 20:
                        lmt = version_list["Versions"][0]["LastModified"].split("+")[0]
                    log.info("Conditional delete object with incorrect Last modified time")
                    incorrect_lmt = lmt[:-1]
                    out = _conditional_delete_debug(
                        cli_aws,
                        bucket_name,
                        object_name,
                        endpoint,
                        last_modified_time=incorrect_lmt,
                    )
                    if not _response_is_412(out):
                        raise AssertionError(
                            "Conditional delete with an incorrect LastModified "
                            "time must return 412. "
                            f"debug_excerpt={_debug_excerpt(out)!r}"
                        )

                    log.info("Conditional delete object with correct Last modified time")
                    out = _conditional_delete_debug(
                        cli_aws,
                        bucket_name,
                        object_name,
                        endpoint,
                        last_modified_time=lmt,
                    )
                    if not _delete_succeeded(out):
                        raise AssertionError(
                            "Conditional delete with the matching LastModified "
                            "time must remove the object. "
                            f"debug_412={_response_is_412(out)} "
                            f"debug_excerpt={_debug_excerpt(out)!r}"
                        )
                if config.test_ops.get("conditional_delete_with_size", False):
                    log.info("delete object using matching size")
                    version_list = json.loads(
                        aws_reusable.list_object_versions(cli_aws, bucket_name, endpoint)
                    )
                    size = version_list["Versions"][0]["Size"]
                    log.info("Conditional delete object with incorrect Size")
                    incorrect_size = str(size)[:-1]
                    out = _conditional_delete_debug(
                        cli_aws,
                        bucket_name,
                        object_name,
                        endpoint,
                        size=incorrect_size,
                    )
                    if not _response_is_412(out):
                        raise AssertionError(
                            "Conditional delete with an incorrect size must "
                            "return 412. "
                            f"debug_excerpt={_debug_excerpt(out)!r}"
                        )

                    log.info("Conditional delete object with correct size")
                    out = _conditional_delete_debug(
                        cli_aws, bucket_name, object_name, endpoint, size=size
                    )
                    if not _delete_succeeded(out):
                        raise AssertionError(
                            "Conditional delete with the matching size must "
                            "remove the object. "
                            f"debug_412={_response_is_412(out)} "
                            f"debug_excerpt={_debug_excerpt(out)!r}"
                        )
                if config.test_ops.get("conditional_delete_with_multi_condition", False):
                    log.info("delete object using combination of conditions")
                    version_list = json.loads(
                        aws_reusable.list_object_versions(cli_aws, bucket_name, endpoint)
                    )
                    etag = None
                    lmt = None
                    size = None
                    if config.test_ops.get("size", False):
                        size = version_list["Versions"][0]["Size"]
                        if config.test_ops.get("size")[1] == "correct":
                            size = size
                        else:
                            incorrect = True
                            size = str(size)[:-1]
                    if config.test_ops.get("etag", False):
                        etag = version_list["Versions"][0]["ETag"].split('"')[1]
                        if config.test_ops.get("etag")[1] == "correct":
                            etag = etag
                        else:
                            incorrect = True
                            etag = etag[:-1]
                    if config.test_ops.get("last_modified_time", False):
                        lmt = version_list["Versions"][0]["LastModified"].split(".")[0]
                        if float(ceph_version_id[0]) >= 20:
                            lmt = version_list["Versions"][0]["LastModified"].split("+")[0]
                        if config.test_ops.get("last_modified_time")[1] == "correct":
                            lmt = lmt
                        else:
                            incorrect = True
                            lmt = lmt[:-1]

                    err = _conditional_delete_debug(
                        cli_aws,
                        bucket_name,
                        object_name,
                        endpoint,
                        etag=etag,
                        last_modified_time=lmt,
                        size=size,
                    )
                    if incorrect and not _response_is_412(err):
                        raise AssertionError(
                            "Conditional delete with a mismatched condition must "
                            "return 412. "
                            f"debug_excerpt={_debug_excerpt(err)!r}"
                        )
                    if not incorrect and not _delete_succeeded(err):
                        raise AssertionError(
                            "Conditional delete with matching conditions must "
                            "remove the object. "
                            f"debug_412={_response_is_412(err)} "
                            f"debug_excerpt={_debug_excerpt(err)!r}"
                        )
                if config.test_ops.get("conditional_delete_etag_prefix", False):
                    log.info(
                        "If-Match prefix (etag+EXTRA) must return 412 on unversioned, "
                        "current-version, and versionId deletes"
                    )
                    _test_etag_prefix_if_match(
                        cli_aws, bucket_name, object_name, endpoint
                    )
                if config.test_ops.get("conditional_delete_stale_attrs", False):
                    log.info(
                        "Stale ETag, size, and mtime must not delete an object "
                        "overwritten before the conditional delete"
                    )
                    _test_stale_conditional_delete(
                        cli_aws,
                        bucket_name,
                        object_name,
                        endpoint,
                        float(ceph_version_id[0]),
                    )
                if config.test_ops.get("conditional_delete_same_second", False):
                    log.info(
                        "Two overwrites in the same second must not delete the "
                        "second generation when the conditional delete uses the "
                        "first generation LastModified"
                    )
                    _test_same_second_conditional_delete(
                        cli_aws,
                        bucket_name,
                        object_name,
                        endpoint,
                        float(ceph_version_id[0]),
                        user["access_key"],
                        user["secret_key"],
                    )
                if config.test_ops.get("conditional_delete_missing_object", False):
                    log.info(
                        "A never-written key deleted with a wrong ETag, size, or "
                        "mtime must not return 204 on an unversioned or versioned "
                        "bucket"
                    )
                    _test_missing_conditional_delete(
                        cli_aws, bucket_name, object_name, endpoint
                    )
                if config.test_ops.get("conditional_delete_delete_marker", False):
                    log.info(
                        "Conditional delete of a delete marker must not stack "
                        "another marker; a concrete ETag and a wrong mtime must "
                        "return 412"
                    )
                    _test_delete_marker_conditional_delete(
                        cli_aws, bucket_name, object_name, endpoint
                    )
                if config.test_ops.get("conditional_delete_wrong_version", False):
                    log.info(
                        "A condition for one version must not delete the other "
                        "version"
                    )
                    _test_wrong_version_conditional_delete(
                        cli_aws,
                        bucket_name,
                        object_name,
                        endpoint,
                        float(ceph_version_id[0]),
                    )
                if config.test_ops.get("conditional_delete_null_version", False):
                    log.info(
                        "With versioning suspended, a conditional delete must "
                        "follow the current null version"
                    )
                    _test_null_version_conditional_delete(
                        cli_aws, bucket_name, object_name, endpoint
                    )
                if config.test_ops.get("conditional_delete_size_visible", False):
                    log.info(
                        "An empty object deleted with size 0 must succeed, and "
                        "omitting the size header must succeed. A multipart "
                        "object and a compressed object must be deleted with the "
                        "Content-Length the client sees"
                    )
                    _test_size_visible_conditional_delete(
                        cli_aws, bucket_name, object_name, endpoint, ssh_con
                    )
                if config.test_ops.get("conditional_delete_partial_match", False):
                    log.info(
                        "A correct ETag with a wrong size, and a correct size "
                        "with a wrong mtime, must each return 412 and leave the "
                        "object in place"
                    )
                    _test_partial_match_conditional_delete(
                        cli_aws,
                        bucket_name,
                        object_name,
                        endpoint,
                        float(ceph_version_id[0]),
                    )
                if config.test_ops.get("conditional_delete_multi_same_key", False):
                    log.info(
                        "DeleteObjects of the same key, bad ETag then real ETag, "
                        "must fail the bad entry and delete once"
                    )
                    _test_multi_delete_same_key(
                        cli_aws,
                        bucket_name,
                        object_name,
                        endpoint,
                        user["access_key"],
                        user["secret_key"],
                    )
                if not incorrect and gc_verify:
                    aws_reusable.validate_gc()
        finally:
            if config.user_remove is True:
                removal_error = None
                try:
                    removed = s3_reusable.remove_user(user)
                    if removed is False:
                        removal_error = (
                            "radosgw-admin user rm failed for "
                            f"{user.get('user_id')}"
                        )
                except Exception as exc:
                    removal_error = (
                        "radosgw-admin user rm failed for "
                        f"{user.get('user_id')}: {exc}"
                    )
                if removal_error and sys.exc_info()[0] is None:
                    raise TestExecError(removal_error)
                if removal_error:
                    log.error(removal_error)

    # check for any crashes during the execution
    crash_info = s3_reusable.check_for_crash()
    if crash_info:
        raise TestExecError("ceph daemon crash found!")


if __name__ == "__main__":

    test_info = AddTestInfo("Test Conditional Put and Delete using AWS")

    try:
        project_dir = os.path.abspath(os.path.join(__file__, "../../.."))
        test_data_dir = "test_data"
        TEST_DATA_PATH = os.path.join(project_dir, test_data_dir)
        log.info(f"TEST_DATA_PATH: {TEST_DATA_PATH}")
        if not os.path.exists(TEST_DATA_PATH):
            log.info("test data dir not exists, creating.. ")
            os.makedirs(TEST_DATA_PATH)
        parser = argparse.ArgumentParser(
            description="Conditional Put and Delete using AWS"
        )
        parser.add_argument(
            "-c", dest="config", help="RGW Conditional Put and Delete using using AWS"
        )
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
        rgw_node = args.rgw_node
        ssh_con = None
        if rgw_node != "127.0.0.1":
            ssh_con = utils.connect_remote(rgw_node)
        log_f_name = os.path.basename(os.path.splitext(yaml_file)[0])
        configure_logging(f_name=log_f_name, set_level=args.log_level.upper())
        config = resource_op.Config(yaml_file)
        config.read()
        test_exec(config, ssh_con)
        test_info.success_status("test passed")
        sys.exit(0)

    except (RGWBaseException, Exception) as e:
        log.error(e)
        log.error(traceback.format_exc())
        test_info.failed_status("test failed")
        sys.exit(1)

    finally:
        utils.cleanup_test_data_path(TEST_DATA_PATH)
