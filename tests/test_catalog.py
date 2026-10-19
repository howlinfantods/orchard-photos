from io import BytesIO
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, UnidentifiedImageError

from server import catalog, photos
from server.api import App, arguments
from server.cache import connect
from server.catalog import LightroomCatalog, PhotosBundle, largest_jpeg, parse_path_map, snapshot
from server.library import Library
from server.scanner import OnlyInCloud, Scanner
from server.semantic import Encoder


def jpeg(size=(40, 30), color='red'):
    stream = BytesIO()
    Image.new('RGB', size, color).save(stream, 'JPEG')
    return stream.getvalue()


def section(payload: bytes, name=b'level_0') -> bytes:
    """A preview section the way the reader assumes Lightroom lays them out: AgHg, the header's length, the data's length
    (8 bytes), the padding's length and a version, a name, then the data."""
    header = b'AgHg' + (24 + len(name)).to_bytes(4, 'big') + len(payload).to_bytes(8, 'big') + (0).to_bytes(4, 'big') + (1).to_bytes(4, 'big') + name
    return header + payload


def tree(path: Path) -> dict:
    """Every file below a folder with its bytes and modification time, to show nothing was touched."""
    found = {}
    for here, _, names in os.walk(path):
        for name in names:
            file = Path(here) / name
            found[str(file)] = (file.read_bytes(), file.stat().st_mtime_ns)
    return found


class LightroomFixture(unittest.TestCase):
    """A synthetic Lightroom catalog beside its previews, and a folder of originals the catalog knows by another name."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.originals = self.root / 'disk' / 'Photos'
        (self.originals / '2024').mkdir(parents=True)
        for name, color in (('a.jpg', 'red'), ('b.jpg', 'green'), ('c.jpg', 'blue')):
            (self.originals / '2024' / name).write_bytes(jpeg(color=color))
        self.lightroom = self.root / 'Lightroom'
        self.lightroom.mkdir()
        self.catalog = self.lightroom / 'Wedding.lrcat'
        self.previews = self.lightroom / 'Wedding Previews.lrdata'
        self.cache = self.root / 'cache'
        self.build()

    def build(self, journal='DELETE'):
        db = sqlite3.connect(self.catalog)
        db.execute(f'PRAGMA journal_mode={journal}')
        db.executescript('''
            CREATE TABLE AgLibraryRootFolder (id_local INTEGER PRIMARY KEY, absolutePath TEXT, name TEXT);
            CREATE TABLE AgLibraryFolder (id_local INTEGER PRIMARY KEY, pathFromRoot TEXT, rootFolder INTEGER);
            CREATE TABLE AgLibraryFile (id_local INTEGER PRIMARY KEY, baseName TEXT, extension TEXT, folder INTEGER);
            CREATE TABLE Adobe_images (id_local INTEGER PRIMARY KEY, rootFile INTEGER, masterImage INTEGER, rating REAL, pick REAL,
                                       colorLabels TEXT, captureTime TEXT, fileFormat TEXT);
            CREATE TABLE AgLibraryKeyword (id_local INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE AgLibraryKeywordImage (id_local INTEGER PRIMARY KEY, image INTEGER, tag INTEGER);
            CREATE TABLE AgLibraryCollection (id_local INTEGER PRIMARY KEY, name TEXT, creationId TEXT);
            CREATE TABLE AgLibraryCollectionImage (id_local INTEGER PRIMARY KEY, collection INTEGER, image INTEGER);
            INSERT INTO AgLibraryRootFolder VALUES (1, '/Volumes/Old/Photos/', 'Photos');
            INSERT INTO AgLibraryFolder VALUES (1, '', 1), (2, '2024/', 1);
            INSERT INTO AgLibraryFile VALUES (1, 'a', 'jpg', 2), (2, 'b', 'jpg', 2), (3, 'c', 'jpg', 2), (4, 'gone', 'jpg', 2), (5, 'clip', 'mov', 2);
            INSERT INTO Adobe_images VALUES
                (10, 1, NULL, 5, 1, 'Red', '2024-05-01T10:30:00', 'JPG'),
                (11, 2, NULL, 3, 0, '', '2024-05-02T11:00:00', 'JPG'),
                (12, 3, NULL, NULL, -1, NULL, '2024-05-03T12:00:00', 'JPG'),
                (13, 1, 10, 1, 0, NULL, '2024-05-01T10:30:00', 'JPG'),
                (14, 4, NULL, 2, 0, NULL, '2024-05-04T09:00:00', 'JPG'),
                (15, 5, NULL, 0, 0, NULL, '2024-05-05T09:00:00', 'VIDEO');
            INSERT INTO AgLibraryKeyword VALUES (1, 'bride'), (2, 'venue');
            INSERT INTO AgLibraryKeywordImage VALUES (1, 10, 1), (2, 10, 2), (3, 11, 2);
            INSERT INTO AgLibraryCollection VALUES (1, 'Selects', 'com.adobe.ag.library.collection'), (2, 'Smart', 'com.adobe.ag.library.smart_collection');
            INSERT INTO AgLibraryCollectionImage VALUES (1, 1, 10), (2, 1, 12), (3, 2, 11);
        ''')
        db.commit()
        db.close()

    def preview(self, image_id=14, uuid='ABCD1234-0000-0000-0000-000000000000', digest='f00d'):
        """A preview of the image whose original is not on disk: a small level, a header-only section, and a larger level."""
        folder = self.previews / uuid[0] / uuid[:4]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'{uuid}-{digest}.lrprev').write_bytes(
            section(b'name = "meta"', b'info') + section(jpeg((20, 10), 'white')) + section(jpeg((80, 60), 'orange'), b'level_3'))
        db = sqlite3.connect(self.previews / 'previews.db')
        db.execute('CREATE TABLE IF NOT EXISTS ImageCacheEntry (imageId INTEGER, uuid TEXT, digest TEXT)')
        db.execute('INSERT INTO ImageCacheEntry VALUES (?,?,?)', (image_id, uuid, digest))
        db.commit()
        db.close()

    def source(self):
        return LightroomCatalog(self.catalog, self.cache, [f'/Volumes/Old/Photos={self.originals}'])

    def scan(self, source=None):
        source = source or self.source()
        scanner = Scanner(self.catalog, self.cache, source)
        return scanner, scanner.scan()

    def files(self):
        with connect(self.cache, rows=True) as db:
            return {row['path']: dict(row) for row in db.execute('SELECT * FROM files')}


class LightroomTests(LightroomFixture):
    def test_files_are_listed_through_the_path_map_without_virtual_copies_or_videos(self):
        entries, unreadable, libraries, skipped = self.source().list()
        self.assertEqual([entry[0] for entry in entries], ['Photos/2024/a.jpg', 'Photos/2024/b.jpg', 'Photos/2024/c.jpg', 'Photos/2024/gone.jpg'])
        self.assertEqual({entry[1] for entry in entries}, {'Photos/2024'})
        self.assertEqual((entries[0][2], entries[0][3]), ((self.originals / '2024/a.jpg').stat().st_size, (self.originals / '2024/a.jpg').stat().st_mtime_ns))
        self.assertEqual((unreadable, libraries, skipped), ([], [], {'raw': 0, 'video': 1}))

    def test_a_scan_indexes_the_originals_and_keeps_what_the_catalog_says(self):
        scanner, stats = self.scan()
        self.assertEqual((stats['files'], stats['unreadable']), (3, 1))
        with connect(self.cache, rows=True) as db:
            meta = {row['path']: dict(row) for row in db.execute('SELECT * FROM catalog_meta')}
            tags = {(row['path'], row['kind'], row['name']) for row in db.execute('SELECT * FROM catalog_tags')}
        a = meta['Photos/2024/a.jpg']
        self.assertEqual((a['rating'], a['pick'], a['label'], a['captured'], a['from_preview']), (5, 1, 'Red', '2024-05-01T10:30:00', 0))
        self.assertEqual(a['source'], str(self.originals / '2024/a.jpg'))
        self.assertEqual((meta['Photos/2024/b.jpg']['rating'], meta['Photos/2024/b.jpg']['label']), (3, None))
        self.assertEqual((meta['Photos/2024/c.jpg']['rating'], meta['Photos/2024/c.jpg']['pick']), (0, -1))
        self.assertEqual(len(meta), 4, 'The virtual copy and the video have none')
        self.assertEqual({tag[1:] for tag in tags if tag[0] == 'Photos/2024/a.jpg'}, {('keyword', 'bride'), ('keyword', 'venue'), ('collection', 'Selects')})
        self.assertIn(('Photos/2024/b.jpg', 'keyword', 'venue'), tags)
        self.assertNotIn(('Photos/2024/b.jpg', 'collection', 'Smart'), tags)
        self.assertEqual(self.files()['Photos/2024/gone.jpg']['error'], 'Original is offline')

    def test_without_a_path_map_the_originals_are_not_found(self):
        _, stats = self.scan(LightroomCatalog(self.catalog, self.cache))
        self.assertEqual((stats['files'], stats['unreadable']), (0, 4))

    def test_a_scan_changes_nothing_in_the_catalog_or_beside_it(self):
        self.preview()
        before = tree(self.lightroom)
        listing = sorted(os.listdir(self.lightroom))
        self.scan()
        self.scan()
        self.assertEqual(tree(self.lightroom), before)
        self.assertEqual(sorted(os.listdir(self.lightroom)), listing)
        self.assertTrue((self.cache / 'copies' / 'Wedding.lrcat').is_file())

    def test_a_catalog_in_wal_mode_is_copied_with_its_log_and_left_alone(self):
        self.catalog.unlink()
        self.build('WAL')
        writer = sqlite3.connect(self.catalog)
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("INSERT INTO AgLibraryKeyword VALUES (3, 'only in the log')")
        writer.execute('INSERT INTO AgLibraryKeywordImage VALUES (4, 12, 3)')
        writer.commit()
        self.addCleanup(writer.close)
        self.assertTrue(Path(f'{self.catalog}-wal').stat().st_size > 0)
        before = tree(self.lightroom)
        self.scan()
        self.assertEqual(tree(self.lightroom), before)
        with connect(self.cache) as db:
            self.assertEqual(db.execute("SELECT path FROM catalog_tags WHERE name='only in the log'").fetchall(), [('Photos/2024/c.jpg',)])

    def test_the_catalog_is_copied_again_only_when_it_changed(self):
        self.scan()
        copy = self.cache / 'copies' / 'Wedding.lrcat'
        marker = copy.stat().st_mtime_ns
        self.scan()
        self.assertEqual(copy.stat().st_mtime_ns, marker)
        db = sqlite3.connect(self.catalog)
        db.execute('UPDATE Adobe_images SET rating=4 WHERE id_local=11')
        db.commit()
        db.close()
        self.scan()
        with connect(self.cache) as db:
            self.assertEqual(db.execute("SELECT rating FROM catalog_meta WHERE path='Photos/2024/b.jpg'").fetchone(), (4,))

    def test_a_lock_file_is_noticed_and_the_snapshot_still_shows(self):
        source = self.source()
        source.list()
        self.assertIsNone(source.notice)
        Path(f'{self.catalog}.lock').write_text('')
        scanner, stats = self.scan()
        self.assertEqual(stats['notice'], 'Lightroom has this catalog open; showing a snapshot.')
        self.assertEqual(stats['files'], 3)

    def test_a_missing_original_is_shown_from_the_largest_level_of_its_preview(self):
        self.preview()
        scanner, stats = self.scan()
        self.assertEqual((stats['files'], stats['unreadable'], stats['from_preview']), (4, 0, 1))
        with connect(self.cache, rows=True) as db:
            meta = db.execute("SELECT * FROM catalog_meta WHERE path='Photos/2024/gone.jpg'").fetchone()
            image = db.execute('SELECT i.width, i.height, i.mime FROM files f JOIN images i ON i.id=f.image_id WHERE f.path=?', ('Photos/2024/gone.jpg',)).fetchone()
        self.assertEqual((meta['from_preview'], meta['rating']), (1, 2))
        self.assertTrue(meta['source'].endswith('ABCD1234-0000-0000-0000-000000000000-f00d.lrprev'))
        self.assertEqual(meta['origin'], str(self.originals / '2024/gone.jpg'))
        self.assertEqual((image['width'], image['height'], image['mime']), (80, 60, 'image/jpeg'))
        shown = scanner.source.file('Photos/2024/gone.jpg')
        with Image.open(shown) as exported:
            self.assertEqual(exported.size, (80, 60))

    def test_the_original_is_used_again_once_it_is_back(self):
        self.preview()
        self.scan()
        (self.originals / '2024' / 'gone.jpg').write_bytes(jpeg((50, 50), 'purple'))
        _, stats = self.scan()
        self.assertEqual(stats['from_preview'], 0)
        with connect(self.cache) as db:
            self.assertEqual(db.execute("SELECT from_preview FROM catalog_meta WHERE path='Photos/2024/gone.jpg'").fetchone(), (0,))
            self.assertEqual(db.execute('SELECT i.width FROM files f JOIN images i ON i.id=f.image_id WHERE f.path=?', ('Photos/2024/gone.jpg',)).fetchone(), (50,))

    def test_columns_a_version_lacks_are_left_out_but_the_files_are_still_listed(self):
        db = sqlite3.connect(self.catalog)
        db.executescript('ALTER TABLE Adobe_images DROP COLUMN pick; ALTER TABLE Adobe_images DROP COLUMN colorLabels; DROP TABLE AgLibraryCollection; DROP TABLE AgLibraryKeyword;')
        db.close()
        _, stats = self.scan()
        self.assertEqual(stats['files'], 3)
        with connect(self.cache) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM catalog_tags').fetchone(), (0,))
            self.assertEqual(db.execute("SELECT pick, label FROM catalog_meta WHERE path='Photos/2024/a.jpg'").fetchone(), (0, None))

    def test_something_that_is_not_a_catalog_says_so(self):
        self.catalog.write_bytes(b'not a database' * 50)
        with self.assertRaisesRegex(Exception, 'could not be read as a Lightroom'):
            self.source().list()
        self.catalog.unlink()
        sqlite3.connect(self.catalog).close()
        with self.assertRaisesRegex(Exception, 'not a Lightroom Classic catalog'):
            self.source().list()


class PreviewTests(unittest.TestCase):
    def test_the_largest_jpeg_is_chosen_whatever_order_the_levels_come_in(self):
        small, large = jpeg((10, 10)), jpeg((90, 70))
        data = b'junk' + section(large) + section(b'text') + section(small)
        self.assertEqual(largest_jpeg(data), large)

    def test_a_file_without_headers_is_searched_for_a_jpeg(self):
        data = b'\x00\x01' + jpeg((30, 30)) + b'tail'
        with Image.open(BytesIO(largest_jpeg(data))) as image:
            self.assertEqual(image.size, (30, 30))

    def test_no_jpeg_is_an_error(self):
        with self.assertRaises(UnidentifiedImageError):
            largest_jpeg(section(b'text only'))

    def test_path_maps_pair_up_and_the_longest_comes_first(self):
        self.assertEqual(parse_path_map(['/Volumes/A=/mnt/a/', '/Volumes/A/Deep=/x']), [('/Volumes/A/Deep', '/x'), ('/Volumes/A', '/mnt/a')])
        with self.assertRaises(ValueError):
            parse_path_map(['nothing'])

    def test_a_path_is_mapped_only_at_a_folder_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            mapper = LightroomCatalog(Path(temp) / 'c.lrcat', Path(temp) / 'cache', ['/Volumes/Photos=/mnt/p'])
            self.assertEqual(mapper.mapped('/Volumes/Photos/2024/a.jpg'), '/mnt/p/2024/a.jpg')
            self.assertEqual(mapper.mapped('/Volumes/Photos2/a.jpg'), '/Volumes/Photos2/a.jpg')


class SnapshotTests(unittest.TestCase):
    def test_a_missing_database_is_an_error_and_nothing_is_made(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(FileNotFoundError):
            snapshot(Path(temp) / 'none.db', Path(temp) / 'copies')


class PhotosFixture(unittest.TestCase):
    """A synthetic Photos library bundle: only the tables the reader uses."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bundle = self.root / 'Family.photoslibrary'
        (self.bundle / 'database').mkdir(parents=True)
        (self.bundle / 'originals' / '0').mkdir(parents=True)
        (self.bundle / 'originals' / 'A').mkdir(parents=True)
        (self.bundle / 'originals' / '0' / 'U1.jpeg').write_bytes(jpeg(color='red'))
        (self.bundle / 'originals' / 'A' / 'U2.jpeg').write_bytes(jpeg(color='green'))
        self.cache = self.root / 'cache'
        db = sqlite3.connect(self.bundle / 'database' / 'Photos.sqlite')
        db.executescript('''
            CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZDIRECTORY TEXT, ZFILENAME TEXT, ZTRASHEDSTATE INTEGER, ZDATECREATED REAL,
                                 ZFAVORITE INTEGER, ZKIND INTEGER, ZWIDTH INTEGER, ZHEIGHT INTEGER, ZHIDDEN INTEGER);
            CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, ZASSET INTEGER, ZORIGINALFILENAME TEXT);
            CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZTRASHEDSTATE INTEGER);
            CREATE TABLE Z_26ASSETS (Z_26ALBUMS INTEGER, Z_3ASSETS INTEGER);
            INSERT INTO ZASSET VALUES
                (1, 'U1', '0', 'U1.jpeg', 0, 0, 1, 0, 40, 30, 0),
                (2, 'U2', 'A', 'U2.jpeg', 0, 700000000, 0, 0, 40, 30, 0),
                (3, 'U3', NULL, NULL, 0, 700000001, 0, 0, 40, 30, 0),
                (4, 'U4', '0', 'U4.jpeg', 1, 700000002, 0, 0, 40, 30, 0),
                (5, 'U5', '0', 'U5.mov', 0, 700000003, 0, 1, 1920, 1080, 0),
                (6, 'U6', '../..', 'etc', 0, 700000004, 0, 0, 1, 1, 0),
                (7, 'U7', '0', 'U7.jpeg', 0, 700000005, 0, 0, 1, 1, 1);
            INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES (1, 1, 'IMG_0001.JPG'), (2, 2, 'IMG_0002.JPG'), (3, 3, 'IMG_0003.HEIC');
            INSERT INTO ZGENERICALBUM VALUES (1, 'Holiday', 0), (2, 'Deleted album', 1), (3, NULL, 0);
            INSERT INTO Z_26ASSETS VALUES (1, 1), (1, 2), (2, 1), (3, 2);
        ''')
        db.commit()
        db.close()

    def source(self):
        return PhotosBundle(self.bundle, self.cache)

    def files(self):
        with connect(self.cache, rows=True) as db:
            return {row['path']: dict(row) for row in db.execute('SELECT * FROM files')}


class PhotosBundleTests(PhotosFixture):
    def test_assets_are_listed_by_uuid_and_original_name_without_trashed_hidden_videos_or_escapes(self):
        entries, unreadable, libraries, skipped = self.source().list()
        self.assertEqual([entry[0] for entry in entries], ['U1/IMG_0001.JPG', 'U2/IMG_0002.JPG', 'U3/IMG_0003.HEIC', 'U6/etc'])
        self.assertEqual({entry[1] for entry in entries}, {''})
        self.assertEqual(entries[0][2], (self.bundle / 'originals/0/U1.jpeg').stat().st_size)
        self.assertEqual((unreadable, libraries, skipped), ([], [], {'raw': 0, 'video': 1}))

    def test_metadata_has_favorites_albums_and_the_date_created(self):
        scanner = Scanner(self.bundle, self.cache, self.source())
        stats = scanner.scan()
        self.assertEqual(stats['files'], 2)
        with connect(self.cache, rows=True) as db:
            meta = {row['path']: dict(row) for row in db.execute('SELECT * FROM catalog_meta')}
            tags = {(row['path'], row['kind'], row['name']) for row in db.execute('SELECT * FROM catalog_tags')}
        self.assertEqual((meta['U1/IMG_0001.JPG']['favorite'], meta['U1/IMG_0001.JPG']['captured']), (1, '2001-01-01T00:00:00Z'))
        self.assertEqual((meta['U2/IMG_0002.JPG']['favorite'], meta['U2/IMG_0002.JPG']['captured']), (0, '2023-03-08T20:26:40Z'))
        self.assertEqual(tags, {('U1/IMG_0001.JPG', 'collection', 'Holiday'), ('U2/IMG_0002.JPG', 'collection', 'Holiday')})

    def test_an_original_that_is_only_in_icloud_is_unreadable_for_that_reason(self):
        Scanner(self.bundle, self.cache, self.source()).scan()
        files = self.files()
        self.assertEqual(files['U3/IMG_0003.HEIC']['error'], 'Only in iCloud')
        self.assertEqual(files['U6/etc']['error'], 'Only in iCloud', 'A directory that climbs out of the bundle is not followed')
        self.assertIsNone(files['U1/IMG_0001.JPG']['error'])
        with self.assertRaises(OnlyInCloud):
            self.source().open('U3/IMG_0003.HEIC')

    def test_a_scan_changes_nothing_in_the_library(self):
        before = tree(self.bundle)
        listing = sorted(str(path) for path in self.bundle.rglob('*'))
        Scanner(self.bundle, self.cache, self.source()).scan()
        Scanner(self.bundle, self.cache, self.source()).scan()
        self.assertEqual(tree(self.bundle), before)
        self.assertEqual(sorted(str(path) for path in self.bundle.rglob('*')), listing)

    def test_a_library_in_wal_mode_is_read_with_its_log(self):
        path = self.bundle / 'database' / 'Photos.sqlite'
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("INSERT INTO ZGENERICALBUM VALUES (4, 'Only in the log', 0)")
        writer.execute('INSERT INTO Z_26ASSETS VALUES (4, 1)')
        writer.commit()
        before = tree(self.bundle)
        Scanner(self.bundle, self.cache, self.source()).scan()
        self.assertEqual(tree(self.bundle), before)
        with connect(self.cache) as db:
            self.assertEqual(db.execute("SELECT path FROM catalog_tags WHERE name='Only in the log'").fetchall(), [('U1/IMG_0001.JPG',)])

    def test_a_folder_that_is_not_a_library_is_unavailable(self):
        (self.bundle / 'database' / 'Photos.sqlite').unlink()
        with self.assertRaisesRegex(Exception, 'no database/Photos.sqlite'):
            self.source().check()


class LibraryFilterTests(LightroomFixture):
    def setUp(self):
        super().setUp()
        self.preview()
        self.library = Library(self.catalog, self.cache, Encoder(self.root / 'models'), [f'/Volumes/Old/Photos={self.originals}'])
        self.library.scan()

    def names(self, **params):
        return sorted(item['path'].rpartition('/')[2] for item in self.library.pictures(params)['items'])

    def test_a_catalog_opens_like_a_folder(self):
        self.assertEqual((self.library.kind, self.library.photos), ('catalog', False))
        self.assertEqual(self.names(), ['a.jpg', 'b.jpg', 'c.jpg', 'gone.jpg'])
        self.assertEqual(self.library.status()['fromPreview'], 1)
        self.assertEqual((self.cache / 'folder.txt').read_text().strip(), str(self.catalog))

    def test_rating_picks_keyword_and_collection_narrow_the_images(self):
        self.assertEqual(self.names(rating='3'), ['a.jpg', 'b.jpg'])
        self.assertEqual(self.names(rating='5'), ['a.jpg'])
        self.assertEqual(self.names(rating='0'), ['a.jpg', 'b.jpg', 'c.jpg', 'gone.jpg'])
        self.assertEqual(self.names(picks='1'), ['a.jpg'])
        self.assertEqual(self.names(keyword='venue'), ['a.jpg', 'b.jpg'])
        self.assertEqual(self.names(collection='Selects'), ['a.jpg', 'c.jpg'])
        self.assertEqual(self.names(keyword='venue', collection='Selects', rating='4'), ['a.jpg'])
        self.assertEqual(self.names(collection='Smart'), [])
        for bad in ('6', '-1', 'many'):
            with self.subTest(rating=bad), self.assertRaisesRegex(ValueError, 'rating'):
                self.library.pictures({'rating': bad})

    def test_facets_count_what_each_filter_would_keep(self):
        facets = self.library.facets()['catalog']
        self.assertEqual(facets['kind'], 'catalog')
        self.assertEqual({r['rating']: r['count'] for r in facets['ratings']}, {1: 3, 2: 3, 3: 2, 4: 1, 5: 1})
        self.assertEqual(facets['picks'], 1)
        self.assertEqual(facets['keywords'], [{'name': 'bride', 'count': 1}, {'name': 'venue', 'count': 2}])
        self.assertEqual(facets['collections'], [{'name': 'Selects', 'count': 2}])

    def test_a_folder_has_no_catalog_facets(self):
        folder = self.root / 'plain'
        folder.mkdir()
        (folder / 'x.jpg').write_bytes(jpeg())
        plain = Library(folder, self.root / 'plain-cache', Encoder(self.root / 'models'))
        plain.scan()
        self.assertIsNone(plain.facets()['catalog'])
        self.assertEqual(plain.details(plain.pictures({})['items'][0]['id'])['source'], 'folder')

    def test_details_carry_the_catalog_metadata_and_say_when_a_preview_is_shown(self):
        by_name = {item['path'].rpartition('/')[2]: item['id'] for item in self.library.pictures({})['items']}
        a = self.library.details(by_name['a.jpg'])
        self.assertEqual(a['source'], 'catalog')
        self.assertEqual(a['files'][0]['catalog'], {
            'rating': 5, 'pick': 1, 'favorite': False, 'label': 'Red', 'captured': '2024-05-01T10:30:00', 'fromPreview': False,
            'origin': str(self.originals / '2024/a.jpg'), 'keywords': ['bride', 'venue'], 'collections': ['Selects']})
        self.assertTrue(self.library.details(by_name['gone.jpg'])['files'][0]['catalog']['fromPreview'])
        self.assertTrue(self.library.original(by_name['gone.jpg']).is_file())

    def test_the_viewer_gets_the_preview_when_the_original_is_missing(self):
        gone = next(item['id'] for item in self.library.pictures({})['items'] if item['path'].endswith('gone.jpg'))
        with Image.open(self.library.display(gone)) as shown:
            self.assertEqual(shown.size, (80, 60))


class PhotosLibraryFilterTests(PhotosFixture):
    def test_favorites_and_albums_filter_a_bundle(self):
        library = Library(self.bundle, self.cache, Encoder(self.root / 'models'))
        library.scan()
        self.assertEqual((library.kind, library.photos), ('bundle', False))
        self.assertEqual([item['path'] for item in library.pictures({'picks': '1'})['items']], ['U1/IMG_0001.JPG'])
        self.assertEqual(len(library.pictures({'collection': 'Holiday'})['items']), 2)
        self.assertEqual(library.facets()['catalog']['collections'], [{'name': 'Holiday', 'count': 2}])
        self.assertEqual(library.facets()['catalog']['kind'], 'bundle')


class OpeningTests(LightroomFixture):
    def setUp(self):
        super().setUp()
        self.app = App(self.root / 'data', [f'/Volumes/Old/Photos={self.originals}'])
        patcher = patch('server.api.threading.Thread')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_catalog_file_opens_as_a_folder_and_is_remembered_by_its_path(self):
        library = self.app.open(str(self.catalog))
        self.assertEqual(library.kind, 'catalog')
        status = self.app.status()
        self.assertEqual((status['folder']['kind'], status['folder']['name'], status['folder']['photos']), ('catalog', 'Wedding', False))
        self.assertEqual(library.scanner.source.path_map, [('/Volumes/Old/Photos', str(self.originals))])
        self.app.open(str(self.originals))
        self.assertEqual(self.app.status()['recent'][0]['path'], str(self.catalog))
        self.assertTrue(self.app.status()['recent'][0]['available'])

    def test_a_missing_catalog_and_a_plain_file_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            self.app.open(str(self.lightroom / 'None.lrcat'))
        with self.assertRaisesRegex(ValueError, 'is a file'):
            self.app.open(str(self.originals / '2024' / 'a.jpg'))

    def test_any_photos_bundle_with_a_database_opens_on_any_computer(self):
        bundle = self.root / 'Other.photoslibrary'
        (bundle / 'database').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'Photos.sqlite'):
            self.app.open(str(bundle))
        sqlite3.connect(bundle / 'database' / 'Photos.sqlite').close()
        library = self.app.open(str(bundle))
        self.assertEqual(library.kind, 'bundle')
        self.assertEqual(self.app.status()['folder']['label'], str(bundle))

    def test_the_system_photo_library_keeps_the_photokit_route_on_a_mac(self):
        with patch('server.api.photos.available', return_value=True), patch('server.library.photos.available', return_value=True):
            library = self.app.open(str(photos.LIBRARY))
        self.assertEqual((library.kind, library.photos), ('photos', True))

    def test_the_path_map_option_repeats(self):
        args = arguments(['--path-map', '/a=/b', '--path-map', '/c=/d'])
        self.assertEqual(args.path_map, ['/a=/b', '/c=/d'])
        with self.assertRaises(ValueError):
            App(self.root / 'data2', ['bad'])


class SchemaTests(unittest.TestCase):
    def test_every_cache_has_the_tables_so_filters_can_always_ask(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'f'
            folder.mkdir()
            library = Library(folder, Path(temp) / 'cache', Encoder(Path(temp) / 'models'))
            self.assertEqual(library.pictures({'rating': '3', 'keyword': 'x'})['items'], [])
            with connect(library.directory) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('catalog_meta', 'catalog_tags')").fetchone(), (2,))
        self.assertTrue(catalog.is_catalog(Path('X.LRCAT')))


if __name__ == '__main__':
    unittest.main()
