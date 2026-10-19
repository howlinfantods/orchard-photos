import hashlib
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from server import video
from server.cache import connect, frames_path, thumbnail_path
from server.library import Library
from server.scanner import Scanner
from server.semantic import MODEL, Encoder

FFMPEG = video.available()
COLOURS = {'red': (1, 0, 0), 'green': (0, 1, 0), 'blue': (0, 0, 1)}


def make_video(path: Path, scenes, gop=5, fps=10, **tags):
    """Write a tiny video of flat colour scenes, [(colour, seconds)], with a keyframe every `gop` frames."""
    command = [shutil.which('ffmpeg'), '-v', 'error', '-y']
    for colour, seconds in scenes:
        command += ['-f', 'lavfi', '-i', f'color=c={colour}:s=128x96:r={fps}:d={seconds}']
    chain = ''.join(f'[{place}:v]' for place in range(len(scenes))) + f'concat=n={len(scenes)}:v=1:a=0[v]'
    for key, value in tags.items():
        command += ['-metadata', f'{key}={value}']
    command += ['-filter_complex', chain, '-map', '[v]', '-c:v', 'mpeg4', '-g', str(gop), '-bf', '0', str(path)]
    subprocess.run(command, check=True)
    return path


def colour_vector(image):
    """A fixed vector for the dominant colour of an image, standing in for the model."""
    red, green, blue = (np.asarray(image.convert('RGB'))[24:72, 32:96].reshape(-1, 3).mean(axis=0) / 255)
    return np.array(COLOURS[max(zip((red, green, blue), COLOURS), key=lambda pair: pair[0])[1]], dtype=np.float32)


def fake_features(*, images=None, text=None):
    if images is not None:
        return np.stack([colour_vector(image) for image in images])
    return np.array([COLOURS['blue'] for _ in text], dtype=np.float32)


class IdentityTests(unittest.TestCase):
    def test_a_video_is_named_by_its_size_and_three_samples_and_never_like_an_image(self):
        content = bytes(range(256)) * (20 << 12)
        size = len(content)
        base = video.video_id(BytesIO(content), size)
        self.assertRegex(base, r'^[a-f0-9]{64}$')
        self.assertEqual(base, video.video_id(BytesIO(content), size))
        # An image is named by the SHA-256 of its whole content.
        self.assertNotEqual(base, hashlib.sha256(content).hexdigest())
        small = b'a small file'
        self.assertNotEqual(video.video_id(BytesIO(small), len(small)), hashlib.sha256(small).hexdigest())

        def changed(offset):
            edited = bytearray(content)
            edited[offset] ^= 1
            return video.video_id(BytesIO(bytes(edited)), size)
        # Bytes in the start, middle and end samples count, and bytes between the samples don't.
        for offset in (0, size // 2, size - 1):
            self.assertNotEqual(changed(offset), base, offset)
        self.assertEqual(changed(video.SAMPLE + 10), base)
        self.assertNotEqual(video.video_id(BytesIO(content + b'x'), size + 1), base)

    def test_probe_output_gives_the_displayed_size_and_the_details_a_library_shows(self):
        stream = {'codec_type': 'video', 'codec_name': 'hevc', 'width': 3840, 'height': 2160, 'color_transfer': 'arib-std-b67',
                  'side_data_list': [{'side_data_type': 'Display Matrix', 'rotation': -90}], 'tags': {}}
        container = {'duration': '12.5', 'tags': {'creation_time': '2026-10-12T19:58:21.000000Z', 'com.apple.quicktime.make': 'Apple',
                                                  'com.apple.quicktime.model': 'iPhone 15 Pro'}}
        info = video.describe({'streams': [{'codec_type': 'audio'}, stream], 'format': container})
        self.assertEqual((info['width'], info['height'], info['duration'], info['codec']), (2160, 3840, 12.5, 'hevc'))
        self.assertEqual((info['taken'], info['camera'], info['transfer']), ('2026-10-12T19:58:21Z', 'Apple iPhone 15 Pro', 'arib-std-b67'))
        # The old rotate tag works too, a quarter turn only swaps the sides, and an unset clock is not a date.
        old = video.describe({'streams': [{**stream, 'side_data_list': [], 'tags': {'rotate': '180'}}], 'format': {'duration': '1', 'tags': {'creation_time': '1970-01-01T00:00:00Z'}}})
        self.assertEqual((old['width'], old['height'], old['taken']), (3840, 2160, None))
        for broken in ({'streams': [{'codec_type': 'audio'}]}, {'streams': [{**stream, 'width': 0}], 'format': container},
                       {'streams': [stream], 'format': {}}):
            with self.assertRaises(video.VideoError):
                video.describe(broken)


class LogFootageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_sony_s_log3_is_found_in_the_sidecar_or_in_xml_inside_the_file(self):
        gamma = b'<NonRealTimeMeta><Acquisition><Item name="CaptureGammaEquation" value="s-log3-cine"/></Acquisition></NonRealTimeMeta>'
        plain = self.root / 'A001.MP4'
        plain.write_bytes(b'\0' * 5000)
        self.assertFalse(video.is_slog3(plain))
        (self.root / 'A001M01.XML').write_bytes(gamma)
        self.assertTrue(video.is_slog3(plain))
        (self.root / 'A001M01.XML').write_bytes(gamma.replace(b's-log3-cine', b'rec709'))
        self.assertFalse(video.is_slog3(plain))
        embedded = self.root / 'C001.MP4'
        embedded.write_bytes(b'\0' * 5000 + gamma + b'\0' * 5000)
        self.assertTrue(video.is_slog3(embedded))

    def test_log_footage_without_a_lut_is_indexed_and_marked_unnormalized(self):
        lut = self.root / 'slog3-to-709.cube'
        lut.write_text('LUT_3D_SIZE 2\n')
        with patch.dict('os.environ', {video.LOG_LUT: str(lut)}):
            self.assertEqual(video.log_lut(True), (str(lut), False))
            self.assertEqual(video.log_lut(False), (None, False))
        for value in ('', str(self.root / 'missing.cube')):
            with patch.dict('os.environ', {video.LOG_LUT: value}):
                self.assertEqual(video.log_lut(True), (None, True))
                self.assertEqual(video.log_lut(False), (None, False))

    def test_the_filter_chain_applies_the_lut_once_after_scaling(self):
        chain = video.filters(384, '/luts/a:b,c.cube', select=True)
        self.assertEqual(chain.count('lut3d'), 1)
        self.assertLess(chain.index('scale='), chain.index('lut3d'))
        self.assertIn(r'/luts/a\:b\,c.cube', chain)
        self.assertNotIn('lut3d', video.filters(384, None, select=True))


class SegmentTests(unittest.TestCase):
    def feed(self, vectors, crisp=None, times=None):
        segments = video.Segments()
        for place, vector in enumerate(vectors):
            vector = np.array(vector, dtype=np.float32)
            segments.add(times[place] if times else float(place), vector / np.linalg.norm(vector), crisp[place] if crisp else 1.0, f'frame {place}')
        return segments

    def test_consecutive_frames_at_cosine_095_or_more_merge_and_the_sharpest_member_stays(self):
        # 0.95 is the cut: the second frame is just inside it (0.951), the fourth (cosine 0.894 to the third) is out.
        edge = [0.951, np.sqrt(1 - 0.951 ** 2)]
        segments = self.feed([[1, 0], edge, [0, 1], [0.447, 0.894], [0.447, 0.894]], crisp=[1, 2, 5, 3, 4])
        found = segments.result()
        self.assertEqual([(s['t_start'], s['t_end'], s['t_frame'], s['image']) for s in found],
                         [(0.0, 1.0, 1.0, 'frame 1'), (2.0, 2.0, 2.0, 'frame 2'), (3.0, 4.0, 4.0, 'frame 4')])
        self.assertEqual(len(found), 3)

    def test_frames_apart_in_time_but_alike_do_not_rejoin_after_a_different_one(self):
        found = self.feed([[1, 0], [0, 1], [1, 0]]).result()
        self.assertEqual([(s['t_start'], s['t_end']) for s in found], [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)])

    def test_the_thumbnail_is_the_segment_frame_closest_to_the_mean_of_all_frames(self):
        segments = self.feed([[1, 0]] * 6 + [[0, 1]])
        found = segments.result()
        self.assertEqual(len(found), 2)
        self.assertEqual(video.central(found, segments.mean()), 0)

    def test_seeks_fill_in_a_video_with_too_few_keyframes(self):
        times = video.seeks(10.0, [0.0])
        self.assertEqual(times, [1.0, 3.0, 5.0, 7.0, 9.0])
        self.assertEqual(video.seeks(10.0, [1.1]), [3.0, 5.0, 7.0, 9.0])


@unittest.skipUnless(FFMPEG, 'ffmpeg is not installed')
class FrameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_every_frame_a_keyframe_is_still_capped_at_one_a_second(self):
        path = make_video(self.root / 'intra.mp4', [('red', 5)], gop=1)
        frames = list(video.keyframes(path, 5))
        times = [t for t, _ in frames]
        self.assertTrue(5 <= len(times) <= 6, times)
        self.assertTrue(all(second - first >= video.FRAME_GAP - 1e-6 for first, second in zip(times, times[1:])), times)
        self.assertEqual(times[0], 0)
        self.assertLessEqual(max(max(image.size) for _, image in frames), video.FRAME_EDGE)

    def test_only_keyframes_are_decoded_and_frames_are_fitted_to_384_pixels(self):
        path = make_video(self.root / 'sparse.mp4', [('green', 6)], gop=20)
        probed = video.probe(path)
        self.assertEqual((probed['width'], probed['height'], probed['codec']), (128, 96, 'mpeg4'))
        self.assertAlmostEqual(probed['duration'], 6, delta=0.2)
        # A keyframe every 2 seconds, so 3 of them, and the 128 pixel frames are not enlarged.
        frames = list(video.keyframes(path, 6))
        self.assertEqual([round(t) for t, _ in frames], [0, 2, 4])
        self.assertEqual(frames[0][1].size, (128, 96))
        big = make_video(self.root / 'big.mp4', [('green', 2)], gop=5)
        with patch.object(video, 'FRAME_EDGE', 64):
            self.assertEqual(next(video.keyframes(big, 2))[1].size, (64, 48))

    def test_the_lut_is_applied_once(self):
        # A LUT that inverts every colour: red once is cyan, and twice would be red again.
        lut = self.root / 'invert.cube'
        corners = [(1 - red, 1 - green, 1 - blue) for blue in (0, 1) for green in (0, 1) for red in (0, 1)]
        lut.write_text('LUT_3D_SIZE 2\n' + ''.join(f'{r} {g} {b}\n' for r, g, b in corners))
        path = make_video(self.root / 'red.mp4', [('red', 2)], gop=5)
        plain = np.asarray(next(video.keyframes(path, 2))[1]).reshape(-1, 3).mean(axis=0)
        once = np.asarray(next(video.keyframes(path, 2, str(lut)))[1]).reshape(-1, 3).mean(axis=0)
        self.assertGreater(plain[0], 200)
        self.assertLess(once[0], 60)
        self.assertGreater(once[1], 200)
        self.assertGreater(np.asarray(video.grab(path, 1, 64, str(lut)))[:, :, 1].mean(), 200)

    def test_garbage_is_not_a_video(self):
        path = self.root / 'broken.mp4'
        path.write_bytes(b'not a video' * 100)
        with self.assertRaises(video.VideoError):
            video.probe(path)


@unittest.skipUnless(FFMPEG, 'ffmpeg is not installed')
class LibraryVideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.folder = base / 'footage'
        self.folder.mkdir()
        self.encoder = Encoder(base / 'models')
        self.library = Library(self.folder, base / 'cache', self.encoder)
        for target, replacement in (('load', None), ('features', fake_features)):
            patcher = patch.object(self.encoder, target, replacement) if replacement else patch.object(self.encoder, target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def scan_and_index(self):
        stats = self.library.scanner.scan()
        self.library.semantic.index()
        self.library.groups.update()
        return stats

    def segments(self):
        with connect(self.library.directory, rows=True) as db:
            return [dict(row) for row in db.execute('SELECT * FROM video_segments ORDER BY idx')]

    def test_a_video_is_scanned_segmented_and_found_with_the_time_it_matches(self):
        make_video(self.folder / 'colours.mp4', [('red', 3), ('green', 3), ('blue', 3)], creation_time='2026-01-02T03:04:05Z')
        Image.new('RGB', (64, 48), (0, 0, 255)).save(self.folder / 'sky.jpg')
        Image.new('RGB', (64, 48), (255, 0, 0)).save(self.folder / 'rose.jpg')
        stats = self.scan_and_index()
        self.assertEqual((stats['images'], stats['unreadable'], stats['skipped']['video']), (3, 0, 0))
        items = {Path(item['path']).name: item for item in self.library.pictures({})['items']}
        clip = items['colours.mp4']
        self.assertEqual((clip['kind'], items['sky.jpg']['kind'], items['sky.jpg']['duration']), ('video', 'image', None))
        self.assertAlmostEqual(clip['duration'], 9, delta=0.3)
        self.assertEqual((clip['width'], clip['height'], clip['mime'], clip['dated'], clip['date']),
                         (128, 96, 'video/mp4', 'taken', '2026-01-02T03:04:05Z'))
        # Nine seconds of three colours: every candidate frame is embedded, and each colour is one segment.
        found = self.segments()
        self.assertEqual(len(found), 3)
        self.assertEqual({row['model'] for row in found}, {MODEL})
        self.assertTrue(all(second['t_start'] - first['t_end'] <= 1.5 for first, second in zip(found, found[1:])))
        self.assertTrue(found[2]['t_start'] >= 5.5 and found[2]['t_end'] <= 9)
        folder = frames_path(self.library.directory, clip['id'])
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ['0.webp', '1.webp', '2.webp'])
        with Image.open(thumbnail_path(self.library.directory, clip['id'])) as thumbnail:
            self.assertLessEqual(max(thumbnail.size), 720)
        # Searching for blue finds the video at its blue part, and the photo of the sky.
        result = self.library.pictures({'q': 'blue', 'mode': 'visual'})
        by_name = {Path(item['path']).name: item for item in result['items']}
        self.assertEqual(set(by_name), {'colours.mp4', 'sky.jpg'})
        self.assertGreaterEqual(by_name['colours.mp4']['t'], 5.5)
        self.assertLessEqual(by_name['colours.mp4']['t'], 9)
        self.assertNotIn('t', by_name['sky.jpg'])
        # Search by example works the same: a red photo finds the video at its red part.
        rose = items['rose.jpg']['id']
        red = {Path(item['path']).name: item for item in self.library.pictures({'similar': rose})['items']}
        self.assertEqual(set(red), {'colours.mp4', 'rose.jpg'})
        self.assertLessEqual(red['colours.mp4']['t'], 3)
        details = self.library.details(clip['id'])
        self.assertEqual((details['kind'], details['preview'], len(details['segments']), details['logUnnormalized']), ('video', False, 3, False))

    def test_a_video_scores_as_its_best_segment_and_hits_carry_its_time(self):
        make_video(self.folder / 'two.mp4', [('red', 4), ('blue', 4)])
        self.scan_and_index()
        hits = self.library.semantic.search(text='blue')
        self.assertEqual(len(hits), 1)
        image_id, score = hits[0]
        self.assertAlmostEqual(score, 1.0, places=4)
        self.assertGreaterEqual(hits[0].t, 3.5)

    def test_a_video_with_one_keyframe_gets_evenly_spaced_frames_too(self):
        make_video(self.folder / 'still.mp4', [('blue', 3)], gop=1000)
        embedded = []
        with patch.object(self.encoder, 'features', lambda images: embedded.append(len(images)) or fake_features(images=images)):
            self.scan_and_index()
        self.assertEqual(sum(embedded), 1 + 5)  # The keyframe at 0, and 5 seeks.
        self.assertEqual(len(self.segments()), 1)

    def test_removing_a_video_clears_its_rows_frames_and_thumbnail(self):
        path = make_video(self.folder / 'gone.mp4', [('red', 3)])
        self.scan_and_index()
        image_id = self.library.pictures({})['items'][0]['id']
        path.unlink()
        stats = self.library.scanner.scan()
        self.assertEqual(stats['removed'], 1)
        self.assertEqual(self.segments(), [])
        with connect(self.library.directory) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM videos').fetchone()[0], 0)
        self.assertFalse(frames_path(self.library.directory, image_id).exists())
        self.assertFalse(thumbnail_path(self.library.directory, image_id).exists())

    def test_a_model_change_clears_segments_with_the_vectors(self):
        make_video(self.folder / 'a.mp4', [('red', 3)])
        self.scan_and_index()
        with connect(self.library.directory) as db:
            db.execute("UPDATE video_segments SET model='another/model'")
            db.execute("UPDATE semantic_embeddings SET model='another/model'")
        reopened = Library(self.folder, self.library.directory, self.encoder)
        self.assertEqual(self.segments(), [])
        self.assertFalse(reopened.semantic.has_other_models())

    def test_footage_that_cannot_be_read_is_reported_not_indexed(self):
        (self.folder / 'broken.mov').write_bytes(b'not a video' * 1000)
        stats = self.scan_and_index()
        self.assertEqual((stats['images'], stats['unreadable']), (0, 1))
        with connect(self.library.directory, rows=True) as db:
            self.assertEqual(db.execute('SELECT error FROM files').fetchone()[0], video.NOT_A_VIDEO)

    def test_the_first_thumbnail_is_not_kept_by_browsers_but_the_final_one_is(self):
        make_video(self.folder / 'a.mp4', [('red', 3)])
        self.library.scanner.scan()
        image_id = self.library.pictures({})['items'][0]['id']
        self.assertTrue(self.library.provisional(image_id))
        self.library.semantic.index()
        self.assertFalse(self.library.provisional(image_id))

    def test_a_video_without_ffmpeg_is_counted_not_listed(self):
        make_video(self.folder / 'a.mp4', [('red', 3)])
        with patch('server.video.which', return_value=None):
            stats = Scanner(self.folder, self.library.directory).scan()
        self.assertEqual((stats['images'], stats['skipped']['video']), (0, 1))


if __name__ == '__main__':
    unittest.main()
