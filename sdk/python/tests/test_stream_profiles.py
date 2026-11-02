"""Pure profile checks: measurement bytes remain opaque, including NaN bits."""
from copy import deepcopy
import math
import struct

import pytest

from r2kernel.core.stream_profiles import validate_sample_layout, validate_stream_format, validate_stream_limits


def limits():
    return {"max_codec_config_bytes": 1024, "max_encoded_unit_bytes": 4096,
            "max_decoded_unit_bytes": 4096, "max_chunk_bytes": 1024,
            "max_pending_units": 2, "max_pending_bytes": 8192,
            "max_reassembly_age_ns": 1000, "max_parts_per_unit": 8}


def visual():
    return {"representation": "raw_raster", "width": 2, "height": 2,
            "pixel_format": "gray8", "color_space": "linear", "color_range": "full",
            "bit_depth": 8, "update_model": "live_samples", "limits": limits(),
            "planes": [{"plane_index": 0, "width": 2, "height": 2,
                        "offset_bytes": 0, "row_stride_bytes": 2, "bytes_per_sample": 1,
                        "horizontal_subsampling": 1, "vertical_subsampling": 1,
                        "sample_format": "uint8", "endianness": "little"}]}


def depth():
    return {"width": 2, "height": 2, "element_type": "float32", "row_stride_bytes": 8,
            "endianness": "little", "meters_per_unit": 1., "invalid_value_rule": "nan",
            "frame_id": "depth_optical", "calibration_revision": 3,
            "loss_policy": "lossless", "limits": limits()}


def intrinsics():
    return {"width": 2, "height": 2, "fx": 1., "fy": 1., "cx": 1., "cy": 1.}


def rgbd():
    return {"visual_format": visual(), "depth_format": depth(),
            "pairing_mode": "hardware_synchronized", "spatial_relation": "registered",
            "maximum_pair_skew_ns": 10, "color_calibration_revision": 2,
            "depth_calibration_revision": 3, "color_calibration_id": "camera:color:2",
            "depth_calibration_id": "camera:depth:3", "color_frame_id": "color_optical",
            "depth_frame_id": "depth_optical", "reference_frame_id": "color_optical",
            "color_intrinsics": intrinsics(), "extrinsics_calibration_id": "rig:extrinsic:2"}


def lidar():
    return {"frame_id": "lidar", "layout_kind": "angular_scan", "angle_min_rad": -1.,
            "angle_increment_rad": .1, "channel_count": 2, "returns_per_measurement": 1,
            "record_stride_bytes": 4, "endianness": "little",
            "fields": [{"name": "range", "semantic": "range", "scalar_type": "float32",
                        "offset_bytes": 0, "unit": "m"}], "scan_duration_ns": 1000,
            "time_reference": "scan_start", "invalid_return_rule": "nan", "loss_policy": "lossless",
            "preserves_return_order": True, "limits": limits()}


def cloud():
    return {"coordinate_frame_id": "world", "endianness": "little", "record_stride_bytes": 12,
            "fields": [{"name": axis, "semantic": axis, "scalar_type": "float32", "count": 1,
                        "offset_bytes": index * 4, "unit": "m"} for index, axis in enumerate(("x", "y", "z"))],
            "loss_policy": "lossless", "preserves_point_order": True, "preserves_point_count": True,
            "invalid_point_policy": "nan_xyz", "limits": limits()}


@pytest.mark.parametrize("kind,factory", [("visual", visual), ("depth", depth), ("rgbd", rgbd),
                                         ("lidar", lidar), ("pointcloud", cloud)])
def test_complete_explicit_formats(kind, factory):
    validate_stream_format(kind, factory())


def test_raw_depth_nan_and_invalid_bits_are_not_rewritten():
    payload = bytes.fromhex("0100c07f0000803f0000000000008040")
    sample = {"payload_bytes": payload}
    validate_sample_layout("depth", depth(), sample)
    assert sample["payload_bytes"] == payload
    assert math.isnan(struct.unpack("<f", payload[:4])[0])


@pytest.mark.parametrize("field,value", [("meters_per_unit", 0), ("meters_per_unit", math.nan),
                                       ("endianness", "native"), ("row_stride_bytes", 1),
                                       ("invalid_value_rule", "")])
def test_depth_rejects_ambiguous_layout(field, value):
    selected = depth()
    selected[field] = value
    with pytest.raises(ValueError):
        validate_stream_format("depth", selected)


def test_lossy_depth_requires_explicit_finite_metric_error():
    selected = depth()
    selected["loss_policy"] = "bounded_error"
    with pytest.raises(ValueError):
        validate_stream_format("depth", selected)
    selected["maximum_absolute_error_m"] = .001
    validate_stream_format("depth", selected)
    selected["maximum_absolute_error_m"] = math.inf
    with pytest.raises(ValueError):
        validate_stream_format("depth", selected)


def test_visual_planes_overlap_and_bad_stride_rejected():
    selected = visual()
    second = deepcopy(selected["planes"][0])
    second["plane_index"] = 1
    selected["planes"].append(second)
    with pytest.raises(ValueError, match="overlap"):
        validate_stream_format("visual", selected)
    selected = visual()
    selected["planes"][0]["row_stride_bytes"] = 1
    with pytest.raises(ValueError, match="stride"):
        validate_stream_format("visual", selected)


def test_codec_requires_explicit_support_and_configuration_cap():
    selected = visual()
    selected["representation"] = "video_access_unit"
    selected["codec"] = {"codec_id": "test.codec.v1", "codec_version": "1", "profile": "test",
                         "bitstream_framing": "access-unit", "configuration_format": "test.v1",
                         "configuration_bytes": b"config", "decoded_layout": "gray8"}
    with pytest.raises(ValueError, match="unsupported codec"):
        validate_stream_format("visual", selected)
    validate_stream_format("visual", selected, supported_codecs={"test.codec.v1"})
    selected["codec"]["configuration_bytes"] = bytes(1025)
    with pytest.raises(ValueError, match="configuration"):
        validate_stream_format("visual", selected, supported_codecs={"test.codec.v1"})


def test_decoded_allocation_cap_is_checked_before_payload_allocation():
    selected = depth()
    selected["height"] = 1000000000
    with pytest.raises(ValueError, match="decoded geometry"):
        validate_stream_format("depth", selected)


def test_rgbd_pair_preserves_each_acquisition_time_and_skew():
    sample = {"capture_set_id": 1, "capture_reference_time_sync_ns": 100,
              "color_time_offset_ns": 0, "depth_time_offset_ns": 5,
              "visual_sample": {"header": {"acquired_at_sync_ns": 100}, "payload_bytes": b"abcd"},
              "depth_sample": {"header": {"acquired_at_sync_ns": 105}, "payload_bytes": bytes(16)}}
    validate_sample_layout("rgbd", rgbd(), sample)
    sample["depth_time_offset_ns"] = 20
    with pytest.raises(ValueError, match="offset"):
        validate_sample_layout("rgbd", rgbd(), sample)
    sample["depth_sample"]["header"]["acquired_at_sync_ns"] = 120
    with pytest.raises(ValueError, match="skew"):
        validate_sample_layout("rgbd", rgbd(), sample)


def test_registered_rgbd_requires_real_calibration_reference():
    selected = rgbd()
    selected.pop("extrinsics_calibration_id")
    with pytest.raises(ValueError, match="extrinsics"):
        validate_stream_format("rgbd", selected)


def test_native_lidar_record_count_and_endianness():
    validate_sample_layout("lidar", lidar(), {"measurement_count": 3, "payload_bytes": bytes(24)})
    with pytest.raises(ValueError, match="raw payload size"):
        validate_sample_layout("lidar", lidar(), {"measurement_count": 3, "payload_bytes": bytes(12)})


def test_pointcloud_fields_and_organization():
    selected = cloud()
    validate_sample_layout("pointcloud", selected,
                           {"width": 2, "height": 1, "point_count": 2, "is_dense": True, "payload_bytes": bytes(24)})
    selected["fields"][2]["offset_bytes"] = 6
    with pytest.raises(ValueError, match="overlap"):
        validate_stream_format("pointcloud", selected)


def test_empty_pointcloud_is_a_sample_not_a_missing_sample():
    validate_sample_layout("pointcloud", cloud(),
                           {"width": 0, "height": 1, "point_count": 0, "is_dense": True, "payload_bytes": b""})


def test_lossy_point_count_change_requires_explicit_bound():
    selected = cloud()
    selected.update(loss_policy="bounded_error", maximum_position_error_m=.01, preserves_point_count=False)
    with pytest.raises(ValueError, match="maximum_point_count_delta"):
        validate_stream_format("pointcloud", selected)
    selected["maximum_point_count_delta"] = 10
    validate_stream_format("pointcloud", selected)


@pytest.mark.parametrize("field,value", [("max_parts_per_unit", 0), ("max_pending_units", True),
                                       ("max_pending_bytes", 1000), ("max_parts_per_unit", 1)])
def test_bad_memory_and_fragment_limits_rejected(field, value):
    selected = limits()
    selected[field] = value
    with pytest.raises(ValueError):
        validate_stream_limits(selected)
