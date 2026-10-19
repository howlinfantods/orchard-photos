"""Videos as searchable items: an id from a few samples of the file, ffprobe's description of it, keyframes through
ffmpeg, and the segments that consecutive alike frames merge into.

Originals are only ever read. ffmpeg and ffprobe are looked up on the PATH and never bundled.
"""
from datetime import UTC, datetime
import hashlib
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading

from PIL import Image

from server.cache import while_open

logger = logging.getLogger(__name__)

# The videos ffmpeg is asked to read. Listed once, so the scanner and these functions agree.
EXTENSIONS = {'.3gp', '.avi', '.flv', '.m2ts', '.m4v', '.mkv', '.mov', '.mp4', '.mpeg', '.mpg', '.mts', '.webm', '.wmv'}
NO_FFMPEG = 'Install ffmpeg to index videos.'
NOT_A_VIDEO = 'Not a readable video'
# The id hashes this first, so it can't equal the SHA-256 of any file, which is how an image is identified.
ID_PREFIX = b'orchard-video-v1\0'
# A video's id is its size and three samples of this many bytes: the start, middle and end. Reading whole files of
# several GB just to name them would make a scan of a phone's footage take hours, and two different recordings agreeing
# in size and in all three samples is not a thing a camera produces.
SAMPLE = 4 << 20
# Candidate frames fit within this many pixels each way, and are at least this many seconds apart.
FRAME_EDGE = 384
FRAME_GAP = 1.0
# A video that gives fewer keyframes than this also gets evenly spaced frames, up to SEEK_FRAMES in all.
MIN_FRAMES = 3
SEEK_FRAMES = 5
# Consecutive frames whose vectors have at least this cosine similarity are one segment.
SEGMENT_SIMILARITY = 0.95
# ffmpeg is stopped after this many seconds, or twice the video's length if that is longer.
LIMIT = 300
# Times are within this of an existing frame's, so a seek doesn't add the frame again.
CLOSE = 0.25
LOG_LUT = 'ORCHARD_LOG_LUT'
# How much of each end of a file is searched for embedded Sony XML.
EMBEDDED = 1 << 20
SLOG3 = re.compile(rb'CaptureGammaEquation.{0,80}?s-log3', re.I | re.S)
SCHEMA = '''
    CREATE TABLE IF NOT EXISTS videos (
        image_id TEXT PRIMARY KEY, duration REAL NOT NULL, codec TEXT, transfer TEXT, log_slog3 INTEGER NOT NULL DEFAULT 0,
        log_unnormalized INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS video_segments (
        image_id TEXT NOT NULL, model TEXT NOT NULL, idx INTEGER NOT NULL, t_start REAL NOT NULL, t_end REAL NOT NULL,
        t_frame REAL NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (image_id, model, idx)
    );
'''


class VideoError(Exception):
    """A video that could not be read, with a message to show."""


def save(image: Image.Image, target: Path, quality: int = 82):
    """Write an image as WebP, replacing the file only once it is whole."""
    temporary = target.with_suffix(f'.{os.getpid()}-{threading.get_ident()}-{id(image)}.tmp')
    image.save(temporary, 'WEBP', quality=quality)
    while_open(temporary.replace, target)


def which(name: str) -> str | None:
    return shutil.which(name)


def available() -> bool:
    return bool(which('ffmpeg') and which('ffprobe'))


def mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or 'video/mp4'


def video_id(stream, size: int) -> str:
    """The id of a video: SHA-256 over a prefix, its size and three samples, read from an open binary file."""
    digest = hashlib.sha256(ID_PREFIX)
    digest.update(size.to_bytes(8, 'little'))
    for offset in (0, max(0, (size - SAMPLE) // 2), max(0, size - SAMPLE)):
        stream.seek(offset)
        digest.update(stream.read(SAMPLE))
    return digest.hexdigest()


def moment(text) -> str | None:
    """An ffprobe time like 2026-10-12T19:58:21.000000Z as the scanner stores times. None when it is missing or is a
    camera's unset clock."""
    try:
        when = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None
    if when.year < 1990:
        return None
    return when.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def number(value, default=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result and result not in (float('inf'), float('-inf')) else default


def describe(data: dict) -> dict:
    """What ffprobe's JSON says about a video: duration, display size, codec, taken, camera and colour transfer."""
    streams = [stream for stream in data.get('streams', []) if stream.get('codec_type') == 'video'
               and not stream.get('disposition', {}).get('attached_pic')]
    if not streams:
        raise VideoError(NOT_A_VIDEO)
    stream, container = streams[0], data.get('format', {})
    width, height = int(stream.get('width') or 0), int(stream.get('height') or 0)
    if width <= 0 or height <= 0:
        raise VideoError(NOT_A_VIDEO)
    rotation = number(next((side.get('rotation') for side in stream.get('side_data_list', []) if 'rotation' in side), None))
    if rotation is None:
        rotation = number(stream.get('tags', {}).get('rotate'), 0)
    if round(rotation) % 180 == 90:
        width, height = height, width
    duration = number(container.get('duration')) or number(stream.get('duration'))
    if not duration or duration <= 0:
        raise VideoError(NOT_A_VIDEO)
    tags = {key.lower(): str(value) for source in (stream.get('tags', {}), container.get('tags', {})) for key, value in source.items()}
    make = next((tags[key] for key in ('com.apple.quicktime.make', 'make') if tags.get(key)), '').strip()
    model = next((tags[key] for key in ('com.apple.quicktime.model', 'model') if tags.get(key)), '').strip()
    camera = model if model.lower().startswith(make.lower()) else f'{make} {model}'.strip()
    return {'duration': duration, 'width': width, 'height': height, 'codec': stream.get('codec_name'),
            'taken': moment(tags.get('creation_time')), 'camera': camera or None, 'transfer': stream.get('color_transfer')}


def probe(path: Path) -> dict:
    """describe() of the file at `path`, read with ffprobe. Raises VideoError."""
    ffprobe = which('ffprobe')
    if not ffprobe:
        raise VideoError(NO_FFMPEG)
    try:
        result = subprocess.run([ffprobe, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(path)],
                                capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise VideoError(NOT_A_VIDEO) from None
    if result.returncode != 0:
        raise VideoError(NOT_A_VIDEO)
    try:
        return describe(json.loads(result.stdout))
    except ValueError:
        raise VideoError(NOT_A_VIDEO) from None


def is_slog3(path: Path) -> bool:
    """Whether a file is Sony S-Log3 footage: its sidecar `<name>M01.XML` or XML embedded near either end of the file
    holds a CaptureGammaEquation of s-log3."""
    for sidecar in (path.with_name(f'{path.stem}M01.XML'), path.with_name(f'{path.stem}M01.xml')):
        try:
            if SLOG3.search(sidecar.read_bytes()[:EMBEDDED]):
                return True
        except OSError:
            continue
    try:
        size = path.stat().st_size
        with path.open('rb') as stream:
            head = stream.read(EMBEDDED)
            stream.seek(max(0, size - EMBEDDED))
            tail = stream.read(EMBEDDED)
    except OSError:
        return False
    return bool(SLOG3.search(head) or SLOG3.search(tail))


def log_lut(slog3: bool) -> tuple[str | None, bool]:
    """The .cube file to apply to a log video, and whether the frames will stay unnormalized. Footage that isn't log
    needs neither. The LUT is the file named by the environment variable ORCHARD_LOG_LUT, if it exists."""
    if not slog3:
        return None, False
    lut = os.environ.get(LOG_LUT, '').strip()
    if lut and Path(lut).is_file():
        return lut, False
    return None, True


def filter_path(path: str) -> str:
    """A file name as ffmpeg's filter syntax wants it, with its special characters escaped."""
    return re.sub(r"([\\:',\[\]])", r'\\\1', path)


def filters(edge: int, lut: str | None, select: bool = False) -> str:
    """The video filters for frames of at most `edge` pixels each way: the keyframe cap if `select`, then scaling, then
    the LUT. The LUT appears here once, and nothing else adds it, so no frame gets it twice."""
    chain = []
    if select:
        chain.append(f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{FRAME_GAP:g})',showinfo")
    chain.append(f"scale='min({edge},iw)':'min({edge},ih)':force_original_aspect_ratio=decrease")
    if lut:
        chain.append(f'lut3d=file={filter_path(lut)}')
    return ','.join(chain)


_fps_option = None


def fps_option() -> list[str]:
    """ffmpeg's variable-rate option, which has been -fps_mode since 5.1 and -vsync before that."""
    global _fps_option
    if _fps_option is None:
        try:
            text = subprocess.run([which('ffmpeg'), '-hide_banner', '-h', 'full'], capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            text = ''
        _fps_option = ['-fps_mode', 'vfr'] if '-fps_mode' in text else ['-vsync', 'vfr']
    return _fps_option


def bitmaps(stream):
    """The images in a stream of BMP files, as ffmpeg's image2pipe writes them. Each file says its own length."""
    while header := stream.read(6):
        if len(header) < 6 or header[:2] != b'BM':
            return
        length = int.from_bytes(header[2:6], 'little')
        body = stream.read(length - 6)
        if len(body) < length - 6:
            return
        yield header + body


def grab(path: Path, t: float, edge: int, lut: str | None = None, threads: int = 0) -> Image.Image:
    """The frame at about `t` seconds, fitted to `edge` pixels. Raises VideoError."""
    command = [which('ffmpeg') or '', '-v', 'error', '-nostdin', '-threads', str(threads), '-ss', f'{max(t, 0):.3f}', '-i', str(path),
               '-map', '0:v:0', '-an', '-sn', '-dn', '-frames:v', '1', '-vf', filters(edge, lut), '-f', 'image2pipe', '-c:v', 'bmp', '-']
    if not command[0]:
        raise VideoError(NO_FFMPEG)
    try:
        result = subprocess.run(command, capture_output=True, timeout=LIMIT)
    except subprocess.TimeoutExpired:
        raise VideoError(NOT_A_VIDEO) from None
    frames = list(bitmaps(io.BytesIO(result.stdout)))
    if not frames:
        raise VideoError(NOT_A_VIDEO)
    image = Image.open(io.BytesIO(frames[0]))
    image.load()
    return image.convert('RGB')


def keyframes(path: Path, duration: float, lut: str | None = None, threads: int = 0):
    """Yield (pts seconds, image) for the video's keyframes, at least FRAME_GAP seconds apart, fitted to FRAME_EDGE.
    One ffmpeg process decodes only keyframes and pipes the frames out; showinfo reports each one's time. Raises
    VideoError if ffmpeg can't start or fails before giving any frame."""
    ffmpeg = which('ffmpeg')
    if not ffmpeg:
        raise VideoError(NO_FFMPEG)
    command = [ffmpeg, '-hide_banner', '-loglevel', 'info', '-nostats', '-nostdin', '-threads', str(threads), '-skip_frame', 'nokey', '-i', str(path),
               '-map', '0:v:0', '-an', '-sn', '-dn', *fps_option(), '-vf', filters(FRAME_EDGE, lut, select=True), '-f', 'image2pipe', '-c:v', 'bmp', '-']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    times: queue.Queue = queue.Queue()
    errors: list[str] = []

    def read_log():
        for line in process.stderr:
            text = line.decode('utf-8', 'replace')
            found = re.search(r'showinfo.*? pts_time:(-?[\d.]+)', text) if 'pts_time' in text else None
            if found:
                times.put(float(found.group(1)))
            elif len(errors) < 20 and not text.startswith(('[Parsed_showinfo', ' ')) and text.strip():
                errors.append(text.strip())
        times.put(None)
    reader = threading.Thread(target=read_log, daemon=True)
    reader.start()
    timer = threading.Timer(max(LIMIT, 2 * duration), process.kill)
    timer.start()
    count = 0
    try:
        for data in bitmaps(process.stdout):
            try:
                t = times.get(timeout=60)
            except queue.Empty:
                t = None
            if t is None:
                raise VideoError(NOT_A_VIDEO)
            image = Image.open(io.BytesIO(data))
            image.load()
            count += 1
            yield t, image.convert('RGB')
    finally:
        timer.cancel()
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        process.wait()
        reader.join(timeout=5)
        process.stderr.close()
    if process.returncode not in (0, None) and not count and process.returncode > 0:
        logger.info('ffmpeg could not decode %s: %s', path, ' | '.join(errors[-3:]))
        raise VideoError(NOT_A_VIDEO)


def seeks(duration: float, have: list[float]) -> list[float]:
    """Evenly spaced times to add when a video gave fewer than MIN_FRAMES frames: the middles of SEEK_FRAMES equal parts
    of it, leaving out any within CLOSE of a time already had."""
    spread = [duration * (place + .5) / SEEK_FRAMES for place in range(SEEK_FRAMES)]
    return [t for t in spread if all(abs(t - taken) > CLOSE for taken in have)]


class Segments:
    """Builds segments from frames fed in time order. A frame whose vector has a cosine similarity of at least
    SEGMENT_SIMILARITY to the one before it joins its segment; any other starts a new one. A segment keeps its sharpest
    member, so only one image per segment is held, however long the video."""

    def __init__(self, threshold: float = SEGMENT_SIMILARITY, pack=None):
        self.threshold = threshold
        # Turns a closed segment's image into something smaller to keep, such as WebP bytes.
        self.pack = pack
        self.done: list[dict] = []
        self._open: dict | None = None
        self._previous = None
        self.total = None

    def add(self, t: float, vector, crisp: float, image=None):
        self.total = vector.astype('float64') if self.total is None else self.total + vector
        if self._open is not None and float(vector @ self._previous) >= self.threshold:
            self._open['t_end'] = t
            self._open['frames'] += 1
            if crisp > self._open['crisp']:
                self._open.update(crisp=crisp, t_frame=t, vector=vector, image=image)
        else:
            self.finish_segment()
            self._open = {'t_start': t, 't_end': t, 't_frame': t, 'vector': vector, 'crisp': crisp, 'image': image, 'frames': 1}
        self._previous = vector

    def finish_segment(self):
        if self._open is not None:
            if self.pack and self._open['image'] is not None:
                self._open['image'] = self.pack(self._open['image'])
            self.done.append(self._open)
            self._open = None

    def result(self) -> list[dict]:
        """The segments so far, closing the open one. Add no more frames afterwards."""
        self.finish_segment()
        return self.done

    def mean(self):
        """The unit mean of every frame's vector."""
        import numpy

        return self.total / max(float(numpy.linalg.norm(self.total)), 1e-12)


def central(segments: list[dict], mean) -> int:
    """The index of the segment whose frame's vector is closest to `mean`."""
    return max(range(len(segments)), key=lambda place: float(segments[place]['vector'] @ mean))


def thumbnail_time(duration: float) -> float:
    """Where the first thumbnail of a new video is taken, before its frames are embedded."""
    return duration / 2


def clock(seconds: float) -> str:
    seconds = int(seconds)
    return f'{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}' if seconds >= 3600 else f'{seconds // 60}:{seconds % 60:02d}'
