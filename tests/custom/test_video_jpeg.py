import time
import typing

from hume_expression_measurement.video_helpers import JpegSplitter

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


# The splitter reads only the marker structure, so the segment payloads and scan data need not
# decode to a picture. The quantization table holds an end marker's bytes, which the splitter
# must skip by the segment's length.
def _jpeg(*, segments: typing.Sequence[bytes] = (), scans: typing.Sequence[bytes] = (b"\x12\x34\x56",)) -> bytes:
    image = _SOI + _segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00") + b"".join(segments)
    image += _segment(0xDB, b"\x00\xff\xd9\x10\x20")
    image += _segment(0xC0, b"\x08\x00\x10\x00\x10\x01\x01\x11\x00")
    for index, scan in enumerate(scans):
        image += _segment(0xC4, bytes([index, 0x01, 0x02]))
        image += _segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + scan
    return image + _EOI


def _exif(thumbnail: bytes) -> bytes:
    return _segment(0xE1, b"Exif\x00\x00" + thumbnail)


def _split(*chunks: bytes) -> typing.List[bytes]:
    splitter = JpegSplitter()
    return [image for chunk in chunks for image in splitter.push(chunk)]


_WITH_EXIF = _jpeg(segments=[_exif(_jpeg())], scans=[b"\x01\xff\x00\x02\xff\xd0\x03\xff\xff"])
_PROGRESSIVE = _jpeg(scans=[b"\x0a\xff\x00", b"\x0b", b"\x0c\xff\xd7\x0d"])


def test_images_pushed_in_one_chunk_are_split() -> None:
    assert _split(_WITH_EXIF + _PROGRESSIVE) == [_WITH_EXIF, _PROGRESSIVE]


def test_the_same_images_come_out_wherever_the_stream_is_cut() -> None:
    stream = b"\x00\xff\x01" + _WITH_EXIF + b"\xff" + _PROGRESSIVE
    for cut in range(len(stream) + 1):
        assert _split(stream[:cut], stream[cut:]) == [_WITH_EXIF, _PROGRESSIVE]


def test_the_same_images_come_out_when_the_stream_arrives_one_byte_at_a_time() -> None:
    stream = _WITH_EXIF + _PROGRESSIVE
    assert _split(*(stream[at : at + 1] for at in range(len(stream)))) == [_WITH_EXIF, _PROGRESSIVE]


def test_an_image_stays_whole_when_a_thumbnail_in_its_exif_data_has_its_own_end_marker() -> None:
    assert _split(_WITH_EXIF) == [_WITH_EXIF]


def test_stuffed_bytes_restart_markers_and_fill_bytes_in_the_scan_are_read_past() -> None:
    image = _jpeg(scans=[b"\xff\x00\xff\xd0\xff\x00\xff\xff\xff\xd7\x01"])
    assert _split(image, image) == [image, image]


def test_every_scan_of_a_progressive_image_is_kept() -> None:
    assert _split(_PROGRESSIVE) == [_PROGRESSIVE]


def test_fill_bytes_before_a_marker_between_segments_are_accepted() -> None:
    image = _SOI + b"\xff\xff" + _segment(0xE0, b"\x00") + _segment(0xDA, b"\x00") + b"\x01" + b"\xff" + _EOI
    assert _split(image) == [image]


def test_bytes_before_an_image_and_between_images_are_discarded() -> None:
    stream = b"\x00\xff\xd9\xff" + _WITH_EXIF + b"\x0a\xff\x00" + _PROGRESSIVE
    assert _split(stream) == [_WITH_EXIF, _PROGRESSIVE]


# Copying everything held back on each push takes about 300 ms here, and grows with the square of
# the number of chunks. Copying only each new chunk takes a few milliseconds.
def test_an_image_near_the_2_mb_message_limit_splits_from_small_chunks_in_time_proportional_to_its_size() -> None:
    empty = _jpeg(scans=[b""])
    image = empty[: -len(_EOI)] + b"\x12" * 2_000_000 + _EOI
    chunks = [image[at : at + 256] for at in range(0, len(image), 256)]
    splitter = JpegSplitter()
    images: typing.List[bytes] = []

    start = time.monotonic()
    for chunk in chunks:
        images += splitter.push(chunk)
    elapsed = time.monotonic() - start

    assert images == [image]
    assert elapsed < 0.1


def test_a_partial_image_is_held_back_until_the_rest_arrives() -> None:
    splitter = JpegSplitter()
    cut = len(_PROGRESSIVE) - 1

    assert splitter.push(_WITH_EXIF + _PROGRESSIVE[:cut]) == [_WITH_EXIF]
    assert splitter.push(_PROGRESSIVE[cut:]) == [_PROGRESSIVE]


def test_an_image_cut_short_in_its_scan_by_the_start_of_the_next_image_is_dropped() -> None:
    truncated = _WITH_EXIF[:-4]
    assert _split(truncated + _PROGRESSIVE) == [_PROGRESSIVE]


def test_an_image_whose_structure_is_broken_is_dropped_and_the_next_is_kept() -> None:
    broken = _SOI + b"\x12\x34" + _segment(0xDA, b"\x00") + _EOI
    assert _split(broken + _PROGRESSIVE) == [_PROGRESSIVE]


def test_an_image_with_a_reserved_marker_or_an_impossible_segment_length_is_dropped() -> None:
    reserved = _SOI + b"\xff\x02" + _EOI
    # A scan header, because after any other segment a length under 2 leaves the parser on a byte
    # that is not a marker, which drops the image even without the length check.
    short_length = _SOI + b"\xff\xda\x00\x01\x12" + _EOI
    assert _split(reserved + short_length + _PROGRESSIVE) == [_PROGRESSIVE]
