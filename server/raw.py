"""Camera RAW files, read through the JPEG the camera embeds in each one.

Nearly every camera stores a full-size or large JPEG preview inside its RAW file, rendered with the camera's own
picture settings. Extracting it takes a few milliseconds, where developing the sensor data takes a second or more, so
thumbnails, search and the viewer use it. A file without one is developed at half size instead. The EXIF is read from
the RAW file's own TIFF header, since the embedded JPEG often has none.
"""
from io import BytesIO

from PIL import Image

try:
    import rawpy
except ImportError:
    rawpy = None

EXTENSIONS = {'.3fr', '.arw', '.cr2', '.cr3', '.crw', '.dcr', '.dng', '.erf', '.fff', '.iiq', '.kdc', '.mef', '.mos',
              '.mrw', '.nef', '.nrw', '.orf', '.pef', '.raf', '.raw', '.rw2', '.rwl', '.sr2', '.srf', '.srw', '.x3f'}
MIME = 'image/x-raw'
# EXIF in a TIFF-based RAW file sits near its start. CR3 and RAF aren't TIFF, and get no EXIF this way.
HEADER_BYTES = 1 << 20
# Where embedded JPEGs are looked for when LibRaw can't read a file: cameras newer than LibRaw, like the Sony A7 V,
# still put their previews near the start of the file.
SEARCH_BYTES = 16 << 20
SOI = b'\xff\xd8\xff'
# LibRaw's flip, as the EXIF orientation that undoes it.
ORIENTATION = {0: 1, 3: 3, 5: 8, 6: 6}


def available() -> bool:
    return rawpy is not None


def is_raw(name: str) -> bool:
    return name[name.rfind('.'):].lower() in EXTENSIONS


def holds_raw(stream, name: str) -> bool:
    """Whether a stream named like a RAW file really holds one. A catalog can hand over the JPEG preview of an
    offline RAW original under the original's name."""
    if not is_raw(name):
        return False
    stream.seek(0)
    start = stream.read(2)
    stream.seek(0)
    return start != b'\xff\xd8'


def exif(stream) -> Image.Exif:
    """The EXIF from a RAW file's TIFF header, empty when it has none."""
    stream.seek(0)
    data = Image.Exif()
    try:
        data.load(stream.read(HEADER_BYTES))
    except Exception:
        pass
    return data


def largest_embedded(stream) -> Image.Image:
    """The largest JPEG stored in the first SEARCH_BYTES of a RAW file, for files LibRaw doesn't know. Orientation
    comes from the RAW file's EXIF."""
    stream.seek(0)
    data = stream.read(SEARCH_BYTES)
    best, area, start = None, 0, data.find(SOI)
    while start >= 0:
        try:
            with Image.open(BytesIO(data[start:])) as candidate:
                if candidate.format == 'JPEG' and candidate.width * candidate.height > area:
                    best, area = start, candidate.width * candidate.height
        except Exception:
            pass
        start = data.find(SOI, start + 3)
    if best is None:
        raise OSError('No preview in this RAW file')
    image = Image.open(BytesIO(data[best:]))
    if image.getexif().get(274, 1) == 1:
        image.getexif()[274] = exif(stream).get(274, 1)
    return image


def open_raw(stream) -> Image.Image:
    """The image a RAW file holds, as Pillow opens a JPEG: the embedded preview, or else a half-size development.
    Its EXIF orientation is set from the RAW file, so callers turn it upright as they would a JPEG, and
    `info['raw_size']` is the developed photo's size, which the preview may be smaller than."""
    if rawpy is None:
        return largest_embedded(stream)
    stream.seek(0)
    try:
        raw_file = rawpy.imread(stream)
    except rawpy.LibRawError:
        return largest_embedded(stream)
    with raw_file as raw:
        orientation = ORIENTATION.get(raw.sizes.flip, 1)
        try:
            thumb = raw.extract_thumb()
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            thumb = None
        size = (raw.sizes.width, raw.sizes.height)
        if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
            image = Image.open(BytesIO(thumb.data))
            image.info['raw_size'] = size
            # A preview that already says how it's turned is trusted; LibRaw's flip applies to the sensor data.
            if image.getexif().get(274, 1) == 1:
                image.getexif()[274] = orientation
            return image
        if thumb is not None and thumb.format == rawpy.ThumbFormat.BITMAP:
            image = Image.fromarray(thumb.data)
        else:
            image = Image.fromarray(raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8))
        image.getexif()[274] = orientation
        image.info['raw_size'] = size
        return image
