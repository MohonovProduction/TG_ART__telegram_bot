import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from app.archive_delivery import create_zip_parts
from app.jobs import HeavyJobQueue, JobCancelled, PreviewCache, cleanup_expired_job_directories


class JobInfrastructureTest(unittest.IsolatedAsyncioTestCase):
    async def test_heavy_queue_limits_concurrency_and_reports_waiting_position(self):
        queue = HeavyJobQueue(1)
        started = asyncio.Event()
        release = asyncio.Event()
        queued_positions = []

        async def first():
            started.set()
            await release.wait()
            return "first"

        async def second():
            return "second"

        first_task = asyncio.create_task(queue.run(1, lambda _: asyncio.sleep(0), first))
        await started.wait()
        second_task = asyncio.create_task(queue.run(2, lambda position: _record(queued_positions, position), second))
        await asyncio.sleep(0)
        self.assertFalse(second_task.done())
        release.set()
        self.assertEqual(await first_task, "first")
        self.assertEqual(await second_task, "second")
        self.assertEqual(queued_positions, [2])

    async def test_queued_job_can_be_cancelled_before_rendering(self):
        queue = HeavyJobQueue(1)
        release = asyncio.Event()
        first = asyncio.create_task(queue.run(1, lambda _: asyncio.sleep(0), lambda: release.wait()))
        await asyncio.sleep(0)
        second = asyncio.create_task(queue.run(2, lambda _: asyncio.sleep(0), lambda: asyncio.sleep(0)))
        await asyncio.sleep(0)
        self.assertTrue(queue.cancel(2))
        with self.assertRaises(JobCancelled):
            await second
        release.set()
        await first

    async def test_active_job_can_be_cancelled(self):
        queue = HeavyJobQueue(1)
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(queue.run(1, lambda _: asyncio.sleep(0), work))
        await started.wait()
        self.assertTrue(queue.cancel(1))
        with self.assertRaises(asyncio.CancelledError):
            await task


async def _record(target, value):
    target.append(value)


class TemporaryDataTest(unittest.TestCase):
    def test_preview_cache_expires_and_is_bounded(self):
        cache = PreviewCache(ttl_seconds=60, max_items=1)
        cache.put("old", {"value": 1})
        cache.put("new", {"value": 2})
        self.assertIsNone(cache.get("old"))
        self.assertEqual(cache.get("new"), {"value": 2})
        cache.discard("new")
        self.assertIsNone(cache.get("new"))

    def test_expired_renderer_job_directories_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "stickers_old"
            old.mkdir()
            os.utime(old, (time.time() - 7200, time.time() - 7200))
            source = root / "123_source.webm"
            source.write_bytes(b"source")
            self.assertEqual(cleanup_expired_job_directories(root, 3600), 1)
            self.assertFalse(old.exists())
            self.assertTrue(source.exists())

    def test_zip_parts_are_valid_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = []
            for name in ("one.png", "two.png"):
                path = root / name
                path.write_bytes(b"data")
                files.append(path)
            archive_dir, parts = create_zip_parts(files, "art", root)
            self.assertEqual(len(parts), 1)
            self.assertTrue(parts[0].is_file())
            self.assertTrue(archive_dir.name.startswith("archive_"))
