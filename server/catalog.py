"""Lightroom Classic catalogs and Photos library bundles as sources of images, read from a copy.

A catalog or a library is only ever read from a copy made into Orchard's cache: the database is copied, with its -wal and
-shm files, and the copy is opened read-only. The originals the catalog lists are opened for reading like any file, so the
catalog and the library themselves are never opened, written, locked or moved. Both sources work like scanner.Folder, and
record what the catalog knows about each file (rating, flag, keywords and so on) for the filters and the viewer.
"""
from datetime import UTC, datetime
from hashlib import sha1
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from urllib.parse import quote

from PIL import Image, UnidentifiedImageError

from server.cache import connect, while_open
from server.scanner import VIDEO, FolderUnavailable, Offline, OnlyInCloud

LIGHTROOM_EXTENSION = '.lrcat'
# What the viewer says when Lightroom has the catalog open, and the copy may be a little behind.
OPEN_NOTICE = 'Lightroom has this catalog open; showing a snapshot.'
# Core Data counts seconds from 2001-01-01, not 1970.
CORE_DATA_EPOCH = 978_307_200
SCHEMA = '''
    CREATE TABLE IF NOT EXISTS catalog_meta (
        path TEXT PRIMARY KEY, origin TEXT NOT NULL, source TEXT NOT NULL, from_preview INTEGER NOT NULL DEFAULT 0,
        rating INTEGER NOT NULL DEFAULT 0, pick INTEGER NOT NULL DEFAULT 0, label TEXT, captured TEXT,
        favorite INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS catalog_tags (
        path TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (path, kind, name)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS catalog_tag_name ON catalog_tags(kind, name);
'''
META = 'origin, source, from_preview, rating, pick, label, captured, favorite'
logger = logging.getLogger(__name__)


def prepare(directory: Path):
    """Make the tables that hold what a catalog says about each file. Every folder's cache has them, so the filters can
    always ask."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    with connect(directory) as db:
        db.executescript(SCHEMA)


def is_catalog(path: Path) -> bool:
    return Path(path).suffix.lower() == LIGHTROOM_EXTENSION


def parse_path_map(values) -> list[tuple[str, str]]:
    """OLD=NEW pairs, longest OLD first so the most specific one wins. Raises ValueError for a pair that is not one."""
    pairs = []
    for value in values or ():
        # Pairs already parsed pass through.
        old, separator, new = (value[0], '=', value[1]) if isinstance(value, tuple) else str(value).partition('=')
        old, new = old.replace('\\', '/').rstrip('/'), new.rstrip('/\\')
        if not separator or not old or not new:
            raise ValueError(f'Use --path-map OLD=NEW, like /Volumes/Photos=/mnt/photos. Got {value!r}.')
        pairs.append((old, new))
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def snapshot(source: Path, directory: Path) -> Path:
    """A copy of a SQLite database and its -wal and -shm files in directory, made again only when their sizes or
    modification times have changed. The source is read and nothing else; returns the copy's path. A database that
    changes while it is copied is copied again, a few times."""
    source, directory = Path(source), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    copy, state_path = directory / source.name, directory / f'{source.name}.state.json'

    def state():
        found = {}
        for suffix in ('', '-wal', '-shm'):
            try:
                status = os.stat(f'{source}{suffix}')
            except OSError:
                continue
            found[suffix] = [status.st_size, status.st_mtime_ns]
        return found

    current = state()
    if not current or '' not in current:
        raise FileNotFoundError(source)
    try:
        saved = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        saved = None
    if saved == current and copy.is_file():
        return copy
    state_path.unlink(missing_ok=True)
    for _ in range(3):
        for suffix in ('', '-wal', '-shm'):
            target = Path(f'{copy}{suffix}')
            if suffix in current:
                temporary = target.with_name(target.name + '.tmp')
                shutil.copyfile(f'{source}{suffix}', temporary)
                while_open(temporary.replace, target)
            else:
                while_open(target.unlink, missing_ok=True)
        after = state()
        if after == current:
            break
        current = after
    state_path.write_text(json.dumps(current), encoding='utf-8')
    return copy


def read_only(path: Path) -> sqlite3.Connection:
    """A read-only connection to a copy."""
    return sqlite3.connect(f'file:{quote(str(path))}?mode=ro', uri=True, timeout=30)


def columns(db, table: str) -> set:
    """The columns a table has, none if it doesn't exist, so a different version of the schema degrades."""
    return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}


def utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def safe(*parts: str) -> bool:
    """Whether names from a database stay inside the folder they are joined to."""
    return all(part and part not in ('.', '..') and '/' not in part and '\\' not in part and '\x00' not in part for part in parts)


class Source:
    """What a catalog and a Photos library have in common as a source for the scanner: it lists files with their
    metadata, and opens the one a path names. The metadata, and where each path's bytes come from, are kept in the
    cache's tables, so a restarted server can open files before it has listed them again."""

    kind = ''

    def __init__(self, path: Path, directory: Path):
        self.path = Path(path)
        self.directory = Path(directory)
        self.copies = self.directory / 'copies'
        self.exports = self.directory / 'exports'
        self.notice = None
        self.from_preview = 0
        self._sources: dict[str, tuple[str, bool]] = {}
        prepare(self.directory)

    def store(self, rows: dict):
        """Make the cache's metadata match rows: path -> (the META values, a set of (kind, name) tags)."""
        with connect(self.directory) as db:
            known = {row[0]: tuple(row[1:]) for row in db.execute(f'SELECT path, {META} FROM catalog_meta')}
            tags: dict[str, set] = {}
            for path, kind, name in db.execute('SELECT path, kind, name FROM catalog_tags'):
                tags.setdefault(path, set()).add((kind, name))
            db.executemany('DELETE FROM catalog_meta WHERE path=?', [(path,) for path in known.keys() - rows.keys()])
            db.executemany('DELETE FROM catalog_tags WHERE path=?', [(path,) for path in tags.keys() - rows.keys()])
            for path, (values, labels) in rows.items():
                if known.get(path) != values:
                    db.execute(f'INSERT OR REPLACE INTO catalog_meta (path, {META}) VALUES (?,?,?,?,?,?,?,?,?)', (path, *values))
                if tags.get(path, set()) != labels:
                    db.execute('DELETE FROM catalog_tags WHERE path=?', (path,))
                    db.executemany('INSERT INTO catalog_tags VALUES (?,?,?)', [(path, kind, name) for kind, name in labels])
        self._sources = {path: (values[1], bool(values[2])) for path, (values, _) in rows.items()}

    def where(self, path: str) -> tuple[str, bool]:
        """The file a path's bytes come from, and whether it is a preview of the original."""
        if path not in self._sources:
            with connect(self.directory, rows=True) as db:
                row = db.execute('SELECT source, from_preview FROM catalog_meta WHERE path=?', (path,)).fetchone()
            if not row:
                raise FileNotFoundError(path)
            self._sources[path] = (row['source'], bool(row['from_preview']))
        return self._sources[path]

    def missing(self, path: str) -> OSError:
        return Offline(path)

    def open(self, path: str):
        source, preview = self.where(path)
        if not source:
            raise self.missing(path)
        if preview:
            return BytesIO(largest_jpeg(Path(source).read_bytes()))
        try:
            return Path(source).open('rb')
        except FileNotFoundError:
            raise self.missing(path) from None

    def file(self, path: str) -> Path:
        source, preview = self.where(path)
        if not preview:
            return Path(source)
        # A preview is a JPEG inside a larger file, so it is written out to be viewed and downloaded.
        target = self.exports / f'{sha1(source.encode()).hexdigest()[:16]}-{Path(source).stem}' / (Path(path).stem + '.jpg')
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f'.{os.getpid()}.tmp')
            temporary.write_bytes(largest_jpeg(Path(source).read_bytes()))
            while_open(temporary.replace, target)
        return target


def regular(path: str):
    """The size and modification time in nanoseconds of the file at a path, or None if it isn't there."""
    try:
        status = os.stat(path)
    except (OSError, ValueError):
        return None
    return (status.st_size, status.st_mtime_ns) if stat.S_ISREG(status.st_mode) else None


class LightroomCatalog(Source):
    """A Lightroom Classic catalog, listed from a copy. A file's path is its root folder's name and its place in it, and
    the folders are the catalog's own. A file whose original is missing is read from Lightroom's preview of it."""

    kind = 'catalog'

    def __init__(self, path: Path, directory: Path, path_map=()):
        super().__init__(path, directory)
        self.path_map = parse_path_map(path_map)
        self.previews = self.path.with_name(f'{self.path.stem} Previews.lrdata')

    def check(self):
        if not self.path.is_file():
            raise FolderUnavailable(f'{self.path} is not available. Reconnect its drive, or choose another catalog.')
        if not os.access(self.path, os.R_OK):
            raise FolderUnavailable(f'{self.path} cannot be read. Check its permissions, or choose another catalog.')

    def mapped(self, text: str) -> str:
        """A path in the catalog as this computer names it, through --path-map."""
        for old, new in self.path_map:
            if text == old or text.startswith(old + '/'):
                return new + text[len(old):]
        return text

    def list(self, found=None):
        """Every image as (path, folder, size, mtime_ns), with no unreadable folders or libraries, and the videos
        counted as skipped. The catalog's virtual copies are not files, so they are left out."""
        self.check()
        copy = snapshot(self.path, self.copies)
        self.notice = OPEN_NOTICE if Path(f'{self.path}.lock').exists() else None
        try:
            with read_only(copy) as db:
                records = self.records(db)
                keywords, collections = self.labels(db)
        except sqlite3.DatabaseError as error:
            logger.info('Could not read %s: %s', self.path, error)
            raise FolderUnavailable(f'{self.path.name} could not be read as a Lightroom Classic catalog. Choose a catalog saved by '
                                    'Lightroom Classic, or rescan if Lightroom was writing to it.') from None
        previews = None
        entries, rows, videos = [], {}, 0
        self.from_preview = 0
        for record in records:
            if record['video']:
                videos += 1
                continue
            origin = self.mapped(record['origin'])
            facts = regular(origin)
            source, preview = origin, False
            if facts is None:
                if previews is None:
                    previews = self.preview_files()
                level = previews.get(record['id'])
                facts = level and regular(str(level))
                if facts:
                    source, preview = str(level), True
                    self.from_preview += 1
            path = record['path']
            entries.append((path, path.rpartition('/')[0], *(facts or (1, 0))))
            labels = {('keyword', name) for name in keywords.get(record['id'], ())} | {('collection', name) for name in collections.get(record['id'], ())}
            rows[path] = ((origin, source, int(preview), record['rating'], record['pick'], record['label'], record['captured'], 0), labels)
            if found and len(entries) % 500 == 0:
                found(len(entries))
        self.store(rows)
        self.clean_exports()
        return entries, [], [], {'raw': 0, 'video': videos}

    def records(self, db) -> "list[dict]":
        """One record per real image: its place, rating, flag, label and capture time. Names that this version of the
        schema lacks are left out, as long as the tables that say where the file is are there."""
        tables = {'Adobe_images': {'id_local', 'rootFile'}, 'AgLibraryFile': {'id_local', 'baseName', 'extension', 'folder'},
                  'AgLibraryFolder': {'id_local', 'pathFromRoot', 'rootFolder'}, 'AgLibraryRootFolder': {'id_local', 'absolutePath'}}
        present = {table: columns(db, table) for table in tables}
        if any(not needed <= present[table] for table, needed in tables.items()):
            raise FolderUnavailable(f'{self.path.name} is not a Lightroom Classic catalog Orchard can read.')

        alias = {'Adobe_images': 'i', 'AgLibraryFile': 'f', 'AgLibraryFolder': 'fo', 'AgLibraryRootFolder': 'r'}

        def pick(table, name):
            return f'{alias[table]}.{name}' if name in present[table] else 'NULL'
        query = f'''SELECT i.id_local, {pick('Adobe_images', 'rating')}, {pick('Adobe_images', 'pick')}, {pick('Adobe_images', 'colorLabels')},
                           {pick('Adobe_images', 'captureTime')}, {pick('Adobe_images', 'fileFormat')},
                           r.id_local, r.absolutePath, {pick('AgLibraryRootFolder', 'name')}, fo.pathFromRoot, f.baseName, f.extension
                    FROM Adobe_images i
                    JOIN AgLibraryFile f ON f.id_local=i.rootFile
                    JOIN AgLibraryFolder fo ON fo.id_local=f.folder
                    JOIN AgLibraryRootFolder r ON r.id_local=fo.rootFolder
                    {'WHERE i.masterImage IS NULL' if 'masterImage' in present['Adobe_images'] else ''}
                    ORDER BY i.id_local'''
        raw = db.execute(query).fetchall()
        # Two root folders may share a name, so the later ones are told apart by their number.
        names: dict[int, str] = {}
        for row in raw:
            if row[6] not in names:
                base = row[8] or Path(str(row[7]).rstrip('/')).name or 'Catalog'
                names[row[6]] = base if base not in names.values() else f'{base} ({row[6]})'
        records = []
        for image, rating, flag, label, captured, kind, root, absolute, _, relative, base, extension in raw:
            extension = str(extension or '')
            name = f'{base}.{extension}' if extension else str(base)
            below = str(relative or '')
            records.append({
                'id': image, 'rating': max(0, min(5, int(rating or 0))), 'pick': int(flag or 0), 'label': str(label or '') or None,
                'captured': str(captured or '') or None, 'path': f'{names[root]}/{below}{name}',
                'origin': f'{absolute}{below}{name}', 'video': str(kind or '').upper() == 'VIDEO' or f'.{extension.lower()}' in VIDEO})
        return records

    @staticmethod
    def labels(db) -> tuple[dict, dict]:
        """Each image's keywords and collections, by the image's id in the catalog. Smart collections hold rules, not
        images, so they have none."""
        keywords: dict[int, list] = {}
        collections: dict[int, list] = {}
        if {'image', 'tag'} <= columns(db, 'AgLibraryKeywordImage') and {'id_local', 'name'} <= columns(db, 'AgLibraryKeyword'):
            for image, name in db.execute('SELECT ki.image, k.name FROM AgLibraryKeywordImage ki JOIN AgLibraryKeyword k ON k.id_local=ki.tag'):
                if name:
                    keywords.setdefault(image, []).append(name)
        collection_columns = columns(db, 'AgLibraryCollection')
        if {'image', 'collection'} <= columns(db, 'AgLibraryCollectionImage') and {'id_local', 'name'} <= collection_columns:
            smart = " WHERE c.creationId IS NULL OR c.creationId NOT LIKE '%smart%'" if 'creationId' in collection_columns else ''
            for image, name in db.execute(f'SELECT ci.image, c.name FROM AgLibraryCollectionImage ci JOIN AgLibraryCollection c ON c.id_local=ci.collection{smart}'):
                if name:
                    collections.setdefault(image, []).append(name)
        return keywords, collections

    def preview_files(self) -> dict:
        """Lightroom's preview file for each image id that has one on disk, from a copy of its previews database."""
        found = {}
        source = self.previews / 'previews.db'
        if not source.is_file():
            return found
        try:
            with read_only(snapshot(source, self.copies)) as db:
                if not {'imageId', 'uuid', 'digest'} <= columns(db, 'ImageCacheEntry'):
                    return found
                for image, uuid, digest in db.execute('SELECT imageId, uuid, digest FROM ImageCacheEntry ORDER BY rowid'):
                    uuid = str(uuid or '')
                    if len(uuid) >= 4 and digest and safe(uuid, str(digest)):
                        candidate = self.previews / uuid[0] / uuid[:4] / f'{uuid}-{digest}.lrprev'
                        if candidate.is_file():
                            found[image] = candidate
        except (sqlite3.DatabaseError, OSError) as error:
            logger.info('Could not read the previews of %s: %s', self.path, error)
        return found

    def clean_exports(self):
        """Forget previews written out for viewing that no file uses now."""
        if not self.exports.is_dir():
            return
        current = {sha1(source.encode()).hexdigest()[:16] + '-' + Path(source).stem for source, preview in self._sources.values() if preview}
        for folder in self.exports.iterdir():
            if folder.name not in current:
                shutil.rmtree(folder, ignore_errors=True)


MAGIC = b'AgHg'
SOI, EOI = b'\xff\xd8\xff', b'\xff\xd9'


def jpeg_in(section: bytes) -> bytes | None:
    """The JPEG a preview section holds. A section starts with AgHg, the length of its header and the length of its
    data, both big-endian, and its data follows the header. If that isn't where a JPEG starts, the section is searched."""
    if len(section) >= 16:
        header, length = int.from_bytes(section[4:8], 'big'), int.from_bytes(section[8:16], 'big')
        if 16 <= header < len(section) and section[header:header + 3] == SOI:
            payload = section[header:header + length] if 0 < length <= len(section) - header else section[header:]
            end = payload.rfind(EOI)
            return payload[:end + 2] if end > 0 else payload
    start = section.find(SOI)
    if start < 0:
        return None
    end = section.rfind(EOI)
    return section[start:end + 2] if end > start else section[start:]


def largest_jpeg(data: bytes) -> bytes:
    """The JPEG with the most pixels in a Lightroom preview file, a sequence of sections that each start with AgHg. The
    levels run from small to large, and which are in the file depends on the sizes Lightroom has drawn."""
    starts = [match.start() for match in re.finditer(MAGIC, data)]
    sections = [data[start:end] for start, end in zip(starts, starts[1:] + [len(data)])] or [data]
    best, area = None, 0
    for section in sections:
        jpeg = jpeg_in(section)
        if not jpeg:
            continue
        try:
            with Image.open(BytesIO(jpeg)) as image:
                size = image.width * image.height
        except (OSError, UnidentifiedImageError):
            continue
        if size > area:
            best, area = jpeg, size
    if best is None:
        raise UnidentifiedImageError('No JPEG in the preview')
    return best


class PhotosBundle(Source):
    """A Photos library bundle read as files, from a copy of its database. A photo's path is its uuid and its original
    file name, like the Mac's own Photos source, and it has no folders."""

    kind = 'bundle'

    def __init__(self, path: Path, directory: Path):
        super().__init__(path, directory)
        self.database = self.path / 'database' / 'Photos.sqlite'

    def check(self):
        if not self.path.is_dir():
            raise FolderUnavailable(f'{self.path} is not available. Reconnect its drive, or choose another library.')
        if not self.database.is_file():
            raise FolderUnavailable(f'{self.path.name} has no database/Photos.sqlite, so Orchard can’t read it as a Photos library.')

    def missing(self, path: str) -> OSError:
        return OnlyInCloud(path)

    def list(self, found=None):
        """Every photo as (path, folder, size, mtime_ns). A photo whose original isn't in the bundle, because iCloud has
        it, is listed with a size of 1 and fails as only in iCloud. Videos, hidden and trashed photos are left out."""
        self.check()
        copy = snapshot(self.database, self.copies)
        try:
            with read_only(copy) as db:
                assets = self.assets(db)
                albums = self.albums(db)
        except sqlite3.DatabaseError as error:
            logger.info('Could not read %s: %s', self.database, error)
            raise FolderUnavailable(f'{self.path.name} could not be read as a Photos library. Rescan if Photos was writing to it.') from None
        entries, rows, videos = [], {}, 0
        for asset in assets:
            if asset['video']:
                videos += 1
                continue
            directory, filename = asset['directory'], asset['filename']
            origin = str(self.path / 'originals' / directory / filename) if directory and filename and safe(directory, filename) else ''
            facts = regular(origin) if origin else None
            name = asset['name'] if safe(asset['name'] or '') else filename or 'Photo'
            path = f'{asset["uuid"]}/{name}'
            entries.append((path, '', *(facts or (1, 0))))
            labels = {('collection', title) for title in albums.get(asset['pk'], ())}
            rows[path] = ((origin, origin, 0, 0, 0, None, asset['captured'], asset['favorite']), labels)
            if found and len(entries) % 500 == 0:
                found(len(entries))
        self.store(rows)
        return entries, [], [], {'raw': 0, 'video': videos}

    @staticmethod
    def assets(db) -> "list[dict]":
        wanted = {'Z_PK', 'ZUUID', 'ZDIRECTORY', 'ZFILENAME'}
        present = columns(db, 'ZASSET')
        if not wanted <= present:
            raise FolderUnavailable('This Photos library’s database is not one Orchard can read.')
        extra = columns(db, 'ZADDITIONALASSETATTRIBUTES')
        if {'ZASSET', 'ZORIGINALFILENAME'} <= extra:
            join, name = 'LEFT JOIN ZADDITIONALASSETATTRIBUTES x ON x.ZASSET=a.Z_PK', 'x.ZORIGINALFILENAME'
        else:
            join, name = '', 'NULL'

        def optional(column):
            return f'a.{column}' if column in present else 'NULL'
        conditions = [f'a.{column}=0' for column in ('ZTRASHEDSTATE', 'ZHIDDEN') if column in present]
        query = f'''SELECT a.Z_PK, a.ZUUID, a.ZDIRECTORY, a.ZFILENAME, {name}, {optional('ZKIND')}, {optional('ZFAVORITE')}, {optional('ZDATECREATED')}
                    FROM ZASSET a {join} {'WHERE ' + ' AND '.join(conditions) if conditions else ''} ORDER BY a.Z_PK'''
        records = []
        for pk, uuid, directory, filename, original, kind, favorite, created in db.execute(query):
            if not uuid or not safe(str(uuid)):
                continue
            captured = None
            if isinstance(created, int | float):
                try:
                    captured = utc(datetime.fromtimestamp(CORE_DATA_EPOCH + created, UTC))
                except (OverflowError, OSError, ValueError):
                    pass
            records.append({'pk': pk, 'uuid': str(uuid), 'directory': directory and str(directory), 'filename': filename and str(filename),
                            'name': original and str(original), 'video': kind == 1, 'favorite': int(favorite or 0), 'captured': captured})
        return records

    @staticmethod
    def albums(db) -> dict:
        """Each asset's album titles, by the asset's Z_PK. The table that joins albums to assets is called Z_<n>ASSETS,
        and n depends on the version of Photos, so it is found by its columns."""
        found: dict[int, list] = {}
        if not {'Z_PK', 'ZTITLE'} <= columns(db, 'ZGENERICALBUM'):
            return found
        trashed = ' AND g.ZTRASHEDSTATE=0' if 'ZTRASHEDSTATE' in columns(db, 'ZGENERICALBUM') else ''
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Z\\_%ASSETS' ESCAPE '\\'").fetchall():
            names = columns(db, table)
            album = next((name for name in names if re.fullmatch(r'Z_\d+ALBUMS', name)), None)
            asset = next((name for name in names if re.fullmatch(r'Z_\d+ASSETS', name)), None)
            if not (album and asset):
                continue
            for pk, title in db.execute(f'SELECT j."{asset}", g.ZTITLE FROM "{table}" j JOIN ZGENERICALBUM g ON g.Z_PK=j."{album}" WHERE g.ZTITLE IS NOT NULL{trashed}'):
                found.setdefault(pk, []).append(str(title))
        return found


def conditions(params) -> tuple[list, list]:
    """The WHERE clauses over files (f) for the catalog filters: `rating` is a least number of stars, `picks` keeps flagged
    files (favorites, in Photos), and `keyword` and `collection` name one each."""
    where, values = [], []
    if params.get('rating'):
        try:
            rating = int(params['rating'])
        except ValueError:
            raise ValueError('Use a rating from 1 to 5.') from None
        if not 0 <= rating <= 5:
            raise ValueError('Use a rating from 1 to 5.')
        if rating:
            where.append('EXISTS (SELECT 1 FROM catalog_meta m WHERE m.path=f.path AND m.rating>=?)')
            values.append(rating)
    if params.get('picks') in ('1', 'true'):
        where.append('EXISTS (SELECT 1 FROM catalog_meta m WHERE m.path=f.path AND (m.pick=1 OR m.favorite=1))')
    for key, kind in (('keyword', 'keyword'), ('collection', 'collection')):
        if params.get(key):
            where.append('EXISTS (SELECT 1 FROM catalog_tags t WHERE t.path=f.path AND t.kind=? AND t.name=?)')
            values += [kind, params[key]]
    return where, values


def facets(directory: Path) -> dict:
    """What the catalog filters can choose from, each with how many distinct images it holds."""
    with connect(directory, rows=True) as db:
        def count(condition, table='catalog_meta'):
            return db.execute(f'SELECT count(DISTINCT f.image_id) FROM files f JOIN {table} m ON m.path=f.path WHERE f.image_id IS NOT NULL AND {condition}').fetchone()[0]
        ratings = [{'rating': stars, 'count': count(f'm.rating>={stars}')} for stars in range(1, 6)]
        names = {kind: [dict(row) for row in db.execute(
            '''SELECT t.name, count(DISTINCT f.image_id) AS count FROM files f JOIN catalog_tags t ON t.path=f.path
               WHERE f.image_id IS NOT NULL AND t.kind=? GROUP BY t.name ORDER BY lower(t.name), t.name''', (kind,))] for kind in ('keyword', 'collection')}
        return {'ratings': ratings, 'picks': count('(m.pick=1 OR m.favorite=1)'), 'keywords': names['keyword'], 'collections': names['collection']}


def details(directory: Path, paths: list) -> dict:
    """What the catalog says about each of these files, by path."""
    with connect(directory, rows=True) as db:
        found = {}
        for path in paths:
            row = db.execute(f'SELECT {META} FROM catalog_meta WHERE path=?', (path,)).fetchone()
            if not row:
                continue
            tags = db.execute('SELECT kind, name FROM catalog_tags WHERE path=? ORDER BY lower(name), name', (path,)).fetchall()
            found[path] = {'rating': row['rating'], 'pick': row['pick'], 'favorite': bool(row['favorite']), 'label': row['label'],
                           'captured': row['captured'], 'fromPreview': bool(row['from_preview']), 'origin': row['origin'],
                           'keywords': [tag['name'] for tag in tags if tag['kind'] == 'keyword'],
                           'collections': [tag['name'] for tag in tags if tag['kind'] == 'collection']}
        return found
