from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageOps

from server import raw
from server.scanner import exif_details


class FakeRaw:
    """What rawpy.imread hands back, for a camera that embeds `thumb` and turns its sensor by `flip`."""

    def __init__(self, thumb, flip=0, size=(6000, 4000)):
        self.thumb, self.sizes = thumb, SimpleNamespace(flip=flip, width=size[0], height=size[1])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def extract_thumb(self):
        if self.thumb is None:
            raise FakeRawpy.LibRawNoThumbnailError
        return self.thumb

    def postprocess(self, **_):
        import numpy
        return numpy.zeros((20, 30, 3), dtype=numpy.uint8)


class FakeRawpy:
    class LibRawNoThumbnailError(Exception):
        pass

    class LibRawUnsupportedThumbnailError(Exception):
        pass

    ThumbFormat = SimpleNamespace(JPEG='jpeg', BITMAP='bitmap')

    def __init__(self, raw_file):
        self.raw_file = raw_file

    def imread(self, _stream):
        return self.raw_file


def jpeg(size=(60, 40)):
    data = BytesIO()
    Image.new('RGB', size, 'blue').save(data, 'JPEG')
    return data.getvalue()


def tiff_header(make='SONY', model='ILCE-7M4', taken='2026:10:01 12:00:00'):
    exif = Image.Exif()
    exif[271], exif[272] = make, model
    exif.get_ifd(0x8769)[36867] = taken
    # What a TIFF-based RAW file starts with: a TIFF header and its IFDs.
    return BytesIO(exif.tobytes().removeprefix(b'Exif\x00\x00'))


class RawTests(unittest.TestCase):
    def test_names(self):
        self.assertTrue(raw.is_raw('a/DSC00012.ARW'))
        self.assertTrue(raw.is_raw('x.dng'))
        self.assertFalse(raw.is_raw('photo.jpg'))

    def test_embedded_jpeg_is_used_and_turned_by_the_sensor_flip(self):
        fake = FakeRawpy(FakeRaw(SimpleNamespace(format='jpeg', data=jpeg()), flip=6))
        with patch.object(raw, 'rawpy', fake):
            image = raw.open_raw(BytesIO(b'raw bytes'))
        self.assertEqual(image.size, (60, 40))
        self.assertEqual(image.info['raw_size'], (6000, 4000))
        self.assertEqual(ImageOps.exif_transpose(image).size, (40, 60))

    def test_without_a_preview_the_raw_is_developed(self):
        fake = FakeRawpy(FakeRaw(None, flip=3))
        with patch.object(raw, 'rawpy', fake):
            image = raw.open_raw(BytesIO(b'raw bytes'))
        self.assertEqual(image.size, (30, 20))
        self.assertEqual(image.getexif()[274], 3)

    def test_missing_rawpy_is_an_os_error(self):
        with patch.object(raw, 'rawpy', None), self.assertRaises(OSError):
            raw.open_raw(BytesIO(b'raw bytes'))

    def test_exif_comes_from_the_tiff_header(self):
        taken, camera = exif_details(None, raw.exif(tiff_header()))
        self.assertEqual(camera, 'SONY ILCE-7M4')
        self.assertTrue(taken.startswith('2026-10-01'))

    def test_a_non_tiff_raw_has_empty_exif(self):
        self.assertEqual(dict(raw.exif(BytesIO(b'ftypcrx not a tiff'))), {})


if __name__ == '__main__':
    unittest.main()
