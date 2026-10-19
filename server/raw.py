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
# LibRaw's flip, as the EXIF orientation that undoes it.
ORIENTATION = {0: 1, 3: 3, 5: 8, 6: 6}


def available() -> bool:
    return rawpy is not None


def is_raw(name: str) -> bool:
    return name[name.rfind('.'):].lower() in EXTENSIONS


def exif(stream) -> Image.Exif:
    """The EXIF from a RAW file's TIFF header, empty when it has none."""
    stream.seek(0)
    data = Image.Exif()
    try:
        data.load(stream.read(HEADER_BYTES))
    except Exception:
        pass
    return data


def open_raw(stream) -> Image.Image:
    """The image a RAW file holds, as Pillow opens a JPEG: the embedded preview, or else a half-size development.
    Its EXIF orientation is set from the RAW file, so callers turn it upright as they would a JPEG, and
    `info['raw_size']` is the developed photo's size, which the preview may be smaller than."""
    if rawpy is None:
        raise OSError('RAW support needs rawpy. Install it with: pip install rawpy')
    stream.seek(0)
    with rawpy.imread(stream) as raw:
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
