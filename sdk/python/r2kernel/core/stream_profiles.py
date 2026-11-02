"""Explicit structural checks for public stream format/sample contracts.

No codec, calibration lookup, unit conversion, quantization, or format fallback
is performed here. Encoded formats require the receiver's explicit supported
codec IDs; that receiver must additionally check the exact version/profile and
enforce decoded allocation limits inside its decoder.
"""
from __future__ import annotations

import math
from collections.abc import Mapping


_SCALAR_BYTES = {"int8": 1, "uint8": 1, "int16": 2, "uint16": 2,
                 "int32": 4, "uint32": 4, "float32": 4,
                 "int64": 8, "uint64": 8, "float64": 8}
_LIMITS = ("max_codec_config_bytes", "max_encoded_unit_bytes", "max_decoded_unit_bytes",
           "max_chunk_bytes", "max_pending_units", "max_pending_bytes",
           "max_reassembly_age_ns", "max_parts_per_unit")


def _mapping(value, name):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _integer(value, name, *, minimum=0, maximum=(1 << 63) - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _positive(value, name):
    return _integer(value, name, minimum=1)


def _finite(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be explicit non-empty text")
    return value


def _choice(value, name, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(sorted(choices))}")
    return value


def validate_stream_limits(value):
    limits = _mapping(value, "limits")
    for name in _LIMITS:
        _positive(limits.get(name), name)
    for name in ("max_codec_config_bytes", "max_chunk_bytes", "max_pending_units", "max_parts_per_unit"):
        _integer(limits[name], name, minimum=1, maximum=(1 << 32) - 1)
    if limits["max_chunk_bytes"] > limits["max_encoded_unit_bytes"]:
        raise ValueError("max_chunk_bytes exceeds max_encoded_unit_bytes")
    if limits["max_encoded_unit_bytes"] > limits["max_pending_bytes"]:
        raise ValueError("max_pending_bytes cannot hold one maximum encoded unit")
    if limits["max_parts_per_unit"] * limits["max_chunk_bytes"] < limits["max_encoded_unit_bytes"]:
        raise ValueError("part count and chunk cap cannot carry one maximum encoded unit")


def _codec(format_value, supported_codecs):
    codec = format_value.get("codec")
    if codec is None:
        return
    codec = _mapping(codec, "codec")
    for name in ("codec_id", "codec_version", "profile", "bitstream_framing",
                 "configuration_format", "decoded_layout"):
        _text(codec.get(name), "codec." + name)
    if codec["codec_id"] not in supported_codecs:
        raise ValueError(f"unsupported codec: {codec['codec_id']}")
    configuration = codec.get("configuration_bytes")
    if configuration is not None:
        if not isinstance(configuration, (bytes, bytearray, memoryview)):
            raise ValueError("codec.configuration_bytes must contain bytes")
        if len(configuration) > format_value["limits"]["max_codec_config_bytes"]:
            raise ValueError("codec configuration exceeds max_codec_config_bytes")


def _loss(format_value, error_field):
    loss = _choice(format_value.get("loss_policy"), "loss_policy", {"lossless", "bounded_error"})
    error = format_value.get(error_field)
    if loss == "bounded_error":
        _finite(error, error_field, positive=True)
    elif error is not None and _finite(error, error_field) != 0:
        raise ValueError("lossless format cannot declare nonzero loss allowance")


def _within_decoded_limit(size, format_value):
    if size > format_value["limits"]["max_decoded_unit_bytes"]:
        raise ValueError("decoded geometry exceeds max_decoded_unit_bytes")
    return size


def _visual(format_value):
    representation = _choice(format_value.get("representation"), "representation",
                             {"raw_raster", "encoded_image", "video_access_unit"})
    width = _positive(format_value.get("width"), "width")
    height = _positive(format_value.get("height"), "height")
    for name in ("pixel_format", "color_space", "color_range"):
        _text(format_value.get(name), name)
    _integer(format_value.get("bit_depth"), "bit_depth", minimum=1, maximum=64)
    _choice(format_value.get("update_model"), "update_model", {"static_image", "live_samples"})
    if (representation == "raw_raster") != (format_value.get("codec") is None):
        raise ValueError("raw_raster has no codec; encoded visual representations require a codec")
    planes = format_value.get("planes")
    if not isinstance(planes, list) or not planes or len(planes) > 16:
        raise ValueError("visual planes must contain 1..16 explicitly described planes")
    occupied = []
    indices = set()
    for plane in planes:
        plane = _mapping(plane, "plane")
        index = _integer(plane.get("plane_index"), "plane_index")
        if index in indices:
            raise ValueError("duplicate plane_index")
        indices.add(index)
        horizontal = _integer(plane.get("horizontal_subsampling"), "horizontal_subsampling", minimum=1, maximum=255)
        vertical = _integer(plane.get("vertical_subsampling"), "vertical_subsampling", minimum=1, maximum=255)
        plane_width = _positive(plane.get("width"), "plane.width")
        plane_height = _positive(plane.get("height"), "plane.height")
        if plane_width != (width + horizontal - 1) // horizontal or plane_height != (height + vertical - 1) // vertical:
            raise ValueError("plane geometry does not match declared subsampling")
        sample_bytes = _integer(plane.get("bytes_per_sample"), "bytes_per_sample", minimum=1, maximum=255)
        row_stride = _positive(plane.get("row_stride_bytes"), "row_stride_bytes")
        if row_stride < plane_width * sample_bytes:
            raise ValueError("plane row_stride_bytes is too small")
        _text(plane.get("sample_format"), "sample_format")
        _choice(plane.get("endianness"), "plane.endianness", {"little", "big"})
        offset = _integer(plane.get("offset_bytes"), "offset_bytes")
        end = offset + row_stride * plane_height
        if any(offset < other_end and other_start < end for other_start, other_end in occupied):
            raise ValueError("visual planes overlap")
        occupied.append((offset, end))
    return _within_decoded_limit(max(end for _, end in occupied), format_value)


def _depth(format_value):
    width = _positive(format_value.get("width"), "width")
    height = _positive(format_value.get("height"), "height")
    element = _choice(format_value.get("element_type"), "element_type", {"uint16", "uint32", "float32"})
    _choice(format_value.get("endianness"), "endianness", {"little", "big"})
    stride = _positive(format_value.get("row_stride_bytes"), "row_stride_bytes")
    if stride < width * _SCALAR_BYTES[element]:
        raise ValueError("depth row_stride_bytes is too small")
    _finite(format_value.get("meters_per_unit"), "meters_per_unit", positive=True)
    _text(format_value.get("invalid_value_rule"), "invalid_value_rule")
    _text(format_value.get("frame_id"), "frame_id")
    _integer(format_value.get("calibration_revision"), "calibration_revision")
    _loss(format_value, "maximum_absolute_error_m")
    return _within_decoded_limit(stride * height, format_value)


def _fields(format_value, *, counted):
    stride = _positive(format_value.get("record_stride_bytes"), "record_stride_bytes")
    _choice(format_value.get("endianness"), "endianness", {"little", "big"})
    fields = format_value.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > 256:
        raise ValueError("fields must contain 1..256 explicit field descriptors")
    names, occupied = set(), []
    for field in fields:
        field = _mapping(field, "field")
        name = _text(field.get("name"), "field.name")
        if name in names:
            raise ValueError("duplicate packed field name")
        names.add(name)
        for field_name in ("semantic", "unit"):
            _text(field.get(field_name), "field." + field_name)
        scalar = _choice(field.get("scalar_type"), "scalar_type", set(_SCALAR_BYTES))
        count = _positive(field.get("count"), "field.count") if counted else 1
        offset = _integer(field.get("offset_bytes"), "field.offset_bytes")
        end = offset + _SCALAR_BYTES[scalar] * count
        if end > stride or any(offset < other_end and other_start < end for other_start, other_end in occupied):
            raise ValueError("packed fields overlap or exceed record_stride_bytes")
        occupied.append((offset, end))
    return stride


def validate_stream_format(kind: str, format_value: Mapping, *, supported_codecs=frozenset()) -> None:
    """Validate a selected format before allocating buffers or admitting data.

    The finite field/plane inventory limits below are this reference helper's
    supported bounds, not an allocation request accepted from a sender.
    """
    value = _mapping(format_value, "format")
    if kind in {"visual", "depth", "lidar", "pointcloud"}:
        validate_stream_limits(value.get("limits"))
        _codec(value, supported_codecs)
    if kind == "visual":
        _visual(value)
    elif kind == "depth":
        _depth(value)
    elif kind == "camera":
        _text(value.get("camera_frame_id"), "camera_frame_id")
        _integer(value.get("calibration_revision"), "calibration_revision")
        visual = _mapping(value.get("visual_format"), "visual_format")
        validate_stream_format("visual", visual, supported_codecs=supported_codecs)
        intrinsics = _mapping(value.get("intrinsics"), "intrinsics")
        for dimension in ("width", "height"):
            if _positive(intrinsics.get(dimension), "intrinsics." + dimension) != visual[dimension]:
                raise ValueError("camera intrinsics dimensions do not match visual format")
        for name in ("fx", "fy", "cx", "cy"):
            _finite(intrinsics.get(name), "intrinsics." + name, positive=name in {"fx", "fy"})
        coefficients = intrinsics.get("distortion_coefficients")
        if coefficients is not None:
            _text(intrinsics.get("distortion_model"), "distortion_model")
            if not isinstance(coefficients, list) or len(coefficients) > 32:
                raise ValueError("distortion_coefficients must contain at most 32 coefficients")
            for coefficient in coefficients:
                _finite(coefficient, "distortion coefficient")
    elif kind == "rgbd":
        validate_stream_format("visual", value.get("visual_format"), supported_codecs=supported_codecs)
        validate_stream_format("depth", value.get("depth_format"), supported_codecs=supported_codecs)
        _choice(value.get("pairing_mode"), "pairing_mode", {"hardware_synchronized", "software_joined"})
        _choice(value.get("spatial_relation"), "spatial_relation", {"registered", "unregistered"})
        _integer(value.get("maximum_pair_skew_ns"), "maximum_pair_skew_ns")
        for name in ("color_calibration_revision", "depth_calibration_revision"):
            _integer(value.get(name), name)
        for name in ("color_calibration_id", "depth_calibration_id", "color_frame_id",
                     "depth_frame_id", "reference_frame_id"):
            _text(value.get(name), name)
        if value["depth_frame_id"] != value["depth_format"]["frame_id"]:
            raise ValueError("RGB-D depth frame does not match depth format")
        validate_stream_format("camera", {"camera_frame_id": value["color_frame_id"],
            "calibration_revision": value["color_calibration_revision"],
            "visual_format": value["visual_format"], "intrinsics": value.get("color_intrinsics")},
            supported_codecs=supported_codecs)
        if value["spatial_relation"] == "registered":
            _text(value.get("extrinsics_calibration_id"), "extrinsics_calibration_id")
        if value["depth_calibration_revision"] != value["depth_format"]["calibration_revision"]:
            raise ValueError("RGB-D depth calibration revision mismatch")
    elif kind == "lidar":
        _fields(value, counted=False)
        _text(value.get("frame_id"), "frame_id")
        layout = _choice(value.get("layout_kind"), "layout_kind", {"angular_scan", "native_records"})
        if layout == "angular_scan":
            _finite(value.get("angle_min_rad"), "angle_min_rad")
            if _finite(value.get("angle_increment_rad"), "angle_increment_rad") == 0:
                raise ValueError("angular scan angle_increment_rad cannot be zero")
        _positive(value.get("channel_count"), "channel_count")
        _positive(value.get("returns_per_measurement"), "returns_per_measurement")
        _integer(value.get("scan_duration_ns"), "scan_duration_ns")
        _choice(value.get("time_reference"), "time_reference", {"scan_start", "scan_end"})
        _text(value.get("invalid_return_rule"), "invalid_return_rule")
        _loss(value, "maximum_range_error_m")
        intensity_error = value.get("maximum_intensity_error")
        if intensity_error is not None:
            if _finite(intensity_error, "maximum_intensity_error") < 0:
                raise ValueError("maximum_intensity_error cannot be negative")
            if value["loss_policy"] == "lossless" and intensity_error != 0:
                raise ValueError("lossless LiDAR cannot declare intensity loss")
        if not isinstance(value.get("preserves_return_order"), bool):
            raise ValueError("preserves_return_order must be explicit bool")
        if value["loss_policy"] == "lossless" and not value["preserves_return_order"]:
            raise ValueError("lossless native LiDAR must preserve return order")
    elif kind == "pointcloud":
        _fields(value, counted=True)
        _text(value.get("coordinate_frame_id"), "coordinate_frame_id")
        _loss(value, "maximum_position_error_m")
        if not isinstance(value.get("preserves_point_order"), bool):
            raise ValueError("preserves_point_order must be explicit bool")
        if not isinstance(value.get("preserves_point_count"), bool):
            raise ValueError("preserves_point_count must be explicit bool")
        _text(value.get("invalid_point_policy"), "invalid_point_policy")
        if not value["preserves_point_count"]:
            _positive(value.get("maximum_point_count_delta"), "maximum_point_count_delta")
        elif value.get("maximum_point_count_delta") not in (None, 0):
            raise ValueError("preserved point count cannot declare a nonzero count delta")
        if value["loss_policy"] == "lossless" and (not value["preserves_point_count"] or not value["preserves_point_order"]):
            raise ValueError("lossless point cloud must preserve point count and order")
    else:
        raise ValueError(f"unsupported stream kind: {kind}")


def validate_sample_layout(kind: str, format_value: Mapping, sample: Mapping, *, supported_codecs=frozenset()) -> None:
    """Check complete raw/encoded unit size; this never decodes compressed bytes."""
    validate_stream_format(kind, format_value, supported_codecs=supported_codecs)
    sample = _mapping(sample, "sample")
    if kind == "rgbd":
        color = _mapping(sample.get("visual_sample"), "visual_sample")
        depth = _mapping(sample.get("depth_sample"), "depth_sample")
        validate_sample_layout("visual", format_value["visual_format"], color, supported_codecs=supported_codecs)
        validate_sample_layout("depth", format_value["depth_format"], depth, supported_codecs=supported_codecs)
        reference = _integer(sample.get("capture_reference_time_sync_ns"), "capture_reference_time_sync_ns")
        _integer(sample.get("capture_set_id"), "capture_set_id")
        times = []
        for name, plane in (("color", color), ("depth", depth)):
            offset = _integer(sample.get(name + "_time_offset_ns"), name + "_time_offset_ns", minimum=-(1 << 63))
            acquired = plane["header"]["acquired_at_sync_ns"]
            if acquired != reference + offset:
                raise ValueError("RGB-D capture offset does not preserve plane acquisition time")
            times.append(acquired)
        if abs(times[0] - times[1]) > format_value["maximum_pair_skew_ns"]:
            raise ValueError("RGB-D sample exceeds maximum_pair_skew_ns")
        return
    if kind == "camera":
        return validate_sample_layout("visual", format_value["visual_format"], sample, supported_codecs=supported_codecs)
    payload = sample.get("payload_bytes")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ValueError("payload_bytes must contain bytes")
    if len(payload) > format_value["limits"]["max_encoded_unit_bytes"]:
        raise ValueError("payload size exceeds encoded unit bounds")
    if kind == "visual":
        expected = _visual(format_value)
    elif kind == "depth":
        expected = _depth(format_value)
    elif kind == "lidar":
        expected = (_integer(sample.get("measurement_count"), "measurement_count")
                    * format_value["channel_count"] * format_value["returns_per_measurement"]
                    * format_value["record_stride_bytes"])
    else:
        count = _integer(sample.get("point_count"), "point_count")
        if _integer(sample.get("width"), "width") * _positive(sample.get("height"), "height") != count:
            raise ValueError("point cloud organization does not match point_count")
        if not isinstance(sample.get("is_dense"), bool):
            raise ValueError("is_dense must be explicit bool")
        expected = count * format_value["record_stride_bytes"]
    _within_decoded_limit(expected, format_value)
    if expected > 0 and len(payload) == 0:
        raise ValueError("nonempty sample cannot have an empty payload")
    if format_value.get("codec") is None and len(payload) != expected:
        raise ValueError("raw payload size does not match declared decoded geometry")
