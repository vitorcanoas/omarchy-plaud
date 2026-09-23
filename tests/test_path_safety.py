"""Descriptor operations preserve recording sentinels under substituted paths."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent))
from plaud_linux.safe_io import Directory, prune


class PathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='plaud-path-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.logs = self.root / 'logs'
        self.recs = self.root / 'recordings'
        self.logs.mkdir()
        self.recs.mkdir()
        self.audio = self.recs / 'session.opus'
        self.audio.write_bytes(b'Opus sentinel')

    def test_parent_descriptor_survives_rename(self):
        with Directory.open(self.logs) as directory:
            self.logs.rename(self.root / 'original')
            self.logs.symlink_to(self.recs)
            directory.write('test.log', b'correct directory')
        self.assertFalse((self.recs / 'test.log').exists())
        self.assertEqual((self.root / 'original/test.log').read_bytes(), b'correct directory')

    def test_atomic_write_refuses_symlink(self):
        (self.logs / 'app.log').symlink_to(self.audio)
        with Directory.open(self.logs) as directory:
            with self.assertRaises(OSError):
                directory.write('app.log', b'not audio')
        self.assertEqual(self.audio.read_bytes(), b'Opus sentinel')

    def test_atomic_write_detects_changed_target(self):
        target = self.logs / 'app.log'
        target.write_bytes(b'old')
        original = Directory.unchanged
        def swap(directory, name, previous):
            target.unlink()
            target.symlink_to(self.audio)
            return original(directory, name, previous)
        with Directory.open(self.logs) as directory, mock.patch.object(Directory, 'unchanged', swap):
            with self.assertRaises(OSError):
                directory.write('app.log', b'new')
        self.assertEqual(self.audio.read_bytes(), b'Opus sentinel')
        self.assertFalse(list(self.logs.glob('.plaud-*.tmp')))

    def test_prune_keeps_audio_and_metadata(self):
        meta = self.recs / 'session.json'
        meta.write_bytes(b'{}')
        log = self.logs / 'old.log'
        log.write_bytes(b'old')
        os.utime(log, (0, 0))
        prune(self.root, self.recs)
        self.assertFalse(log.exists())
        self.assertEqual(self.audio.read_bytes(), b'Opus sentinel')
        self.assertEqual(meta.read_bytes(), b'{}')

    def test_prune_tail_stays_same_inode(self):
        path = self.logs / 'app.log'
        path.write_bytes(b'x' * 3000 + b'recent')
        before = path.stat().st_ino
        with path.open('ab') as writer:
            prune(self.root, self.recs, max_log=1000, keep_log=100)
            writer.write(b'next')
        self.assertEqual(path.stat().st_ino, before)
        self.assertEqual(path.stat().st_size, 104)
        self.assertTrue(path.read_bytes().endswith(b'recentnext'))

    def test_prune_hardlinked_log_keeps_audio(self):
        os.link(self.audio, self.logs / 'app.log')
        prune(self.root, self.recs, max_log=1, keep_log=1)
        self.assertEqual(self.audio.read_bytes(), b'Opus sentinel')

    def test_prune_logs_alias_keeps_recording_files(self):
        self.logs.rmdir()
        self.logs.symlink_to(self.recs)
        sentinel = self.recs / 'app.log'
        sentinel.write_bytes(b'not a log')
        prune(self.root, self.recs, max_log=1, keep_log=1)
        self.assertEqual(sentinel.read_bytes(), b'not a log')

    def test_screenshot_root_alias_is_not_pruned(self):
        (self.recs / 'screenshots').symlink_to(self.recs)
        folder = self.recs / 'old'
        folder.mkdir()
        sentinel = folder / 'retain.png'
        sentinel.write_bytes(b'user image')
        os.utime(folder, (0, 0))
        prune(self.root, self.recs)
        self.assertEqual(sentinel.read_bytes(), b'user image')

    def test_old_screenshots_removed_recent_kept(self):
        folder = self.recs / 'screenshots/old'
        folder.mkdir(parents=True)
        (folder / 'shot.png').write_bytes(b'derived')
        os.utime(folder, (0, 0))
        recent = self.recs / 'screenshots/recent'
        recent.mkdir()
        (recent / 'shot.png').write_bytes(b'derived')
        prune(self.root, self.recs)
        self.assertFalse(folder.exists())
        self.assertTrue(recent.exists())

    def test_explicit_data_root_symlink_supported(self):
        alias = self.root / 'selected'
        alias.symlink_to(self.root)
        (self.logs / 'app.log').write_bytes(b'abcdef')
        prune(alias, alias / 'recordings', max_log=3, keep_log=2)
        self.assertEqual((self.logs / 'app.log').read_bytes(), b'ef')

    def test_fifo_read_rejected_without_waiting(self):
        os.mkfifo(self.logs / 'fifo')
        with Directory.open(self.logs) as directory:
            with self.assertRaises(OSError):
                directory.read('fifo', 100)

    def test_bounded_regular_read(self):
        (self.logs / 'large').write_bytes(b'x' * 200)
        with Directory.open(self.logs) as directory:
            with self.assertRaises(OSError):
                directory.read('large', 100)


class Result(unittest.TextTestResult):
    def addSuccess(self, test):
        super().addSuccess(test)
        print('PASS ' + test.id().rsplit('.', 1)[-1])
    def addFailure(self, test, err):
        super().addFailure(test, err)
        print('FAIL ' + test.id().rsplit('.', 1)[-1])
    def addError(self, test, err):
        super().addError(test, err)
        print('FAIL ' + test.id().rsplit('.', 1)[-1])


if __name__ == '__main__':
    result = unittest.TextTestRunner(resultclass=Result).run(unittest.defaultTestLoader.loadTestsFromTestCase(PathTests))
    sys.exit(not result.wasSuccessful())
