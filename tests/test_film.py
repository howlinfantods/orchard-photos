import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from server import film


class LookTests(unittest.TestCase):
    def test_controls_at_neutral_are_left_out(self):
        chosen = film.look({'stock': 'portra400', 'format': '120', 'exposure': '0.5', 'grain': '1', 'push': ''})
        self.assertEqual(chosen, {'stock': 'portra400', 'format': '120', 'controls': {'exposure': 0.5}})

    def test_bad_looks_are_refused(self):
        for params in ({}, {'stock': '../x'}, {'stock': 'gold200', 'format': '70mm'},
                       {'stock': 'gold200', 'exposure': '9'}, {'stock': 'gold200', 'grain': 'lots'}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                film.look(params)

    def test_keys_differ_by_look_size_and_engine(self):
        chosen = film.look({'stock': 'gold200'})
        key = film.look_key(chosen, 1024, 'a')
        self.assertNotEqual(key, film.look_key(chosen, 2048, 'a'))
        self.assertNotEqual(key, film.look_key(chosen, 1024, 'b'))
        self.assertNotEqual(key, film.look_key(film.look({'stock': 'gold200', 'push': '1'}), 1024, 'a'))

    def test_view_edges_round_up(self):
        self.assertEqual([film.view_edge(v) for v in ('300', '1024', '1500', '9000', None)], [512, 1024, 2048, 4096, 2048])


class LocateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.root))

    def test_library_is_found_in_an_extracted_appimage_with_its_stocks(self):
        library = self.root / 'squashfs-root/usr/lib/fotufilm/lib' / film.LIBRARY_NAME
        library.parent.mkdir(parents=True)
        library.write_bytes(b'')
        (self.root / 'squashfs-root/usr/lib/fotufilm/resources/Stocks').mkdir(parents=True)
        self.assertEqual(film.find_library(str(self.root / 'squashfs-root')), library)
        self.assertEqual(film.stocks_folder(library), self.root / 'squashfs-root/usr/lib/fotufilm/resources/Stocks')

    def test_nothing_installed_is_unavailable(self):
        with patch.dict(os.environ, {'ORCHARD_FOTUFILM': str(self.root / 'missing')}):
            status = film.Film().status()
        self.assertFalse(status['available'])
        self.assertIn('fotufilm.com', status['reason'])


@unittest.skipUnless(os.environ.get('ORCHARD_FOTUFILM'), 'Set ORCHARD_FOTUFILM to develop through a real Fotufilm')
class EngineTests(unittest.TestCase):
    """Runs against an installed Fotufilm. On a machine without a free GPU, point Vulkan at a software device."""

    def test_develops_a_photo_in_display_p3(self):
        engine = film.Film()
        status = engine.status()
        self.assertTrue(status['available'], status.get('reason'))
        self.assertIn('portra400', engine.stocks())
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'grey.jpg'
            Image.new('RGB', (300, 200), (120, 120, 120)).save(source)
            developed = engine.develop(source, film.look({'stock': 'portra400'}), 256)
            self.assertEqual(developed.size, (256, 171))
            self.assertEqual(developed.info['icc_profile'], film.ICC)
            target = Path(folder) / 'out.jpg'
            film.save(developed, target)
            with Image.open(target) as saved:
                self.assertEqual(saved.info['icc_profile'], film.ICC)


if __name__ == '__main__':
    unittest.main()
