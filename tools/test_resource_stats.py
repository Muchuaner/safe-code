"""Exercise Linux accounting without connecting to a server or writing system files."""
import importlib.util
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('resource_stats', Path(__file__).resolve().parents[1] / 'src/SafeCode.Core/Remote/resource_stats.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ResourceStatsTests(unittest.TestCase):
    def test_guest_cpu_is_not_double_counted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stat'
            path.write_text('cpu 100 2 30 400 15 5 7 3 90 5\ncpu0 100 0 0 0\n')
            self.assertEqual(probe.cpu_ticks(path), (562, 415))

    def test_available_memory_includes_reclaimable_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meminfo'
            path.write_text('MemTotal: 1000 kB\nMemAvailable: 250 kB\nMemFree: 100 kB\nCached: 100 kB\n')
            self.assertEqual(probe.memory_bytes(path), (750 * 1024, 1000 * 1024))

    def test_older_kernel_memory_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meminfo'
            path.write_text('MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 50 kB\nCached: 200 kB\nSReclaimable: 50 kB\nShmem: 20 kB\n')
            self.assertEqual(probe.memory_bytes(path), (620 * 1024, 1000 * 1024))

    def test_sampling_uses_cpu_deltas_and_root_filesystem(self):
        disk = namedtuple('Disk', 'total used free')(3000, 2000, 1000)
        with patch.object(probe, 'cpu_ticks', side_effect=[(1000, 800), (1200, 900)]), patch.object(probe, 'memory_bytes', return_value=(300, 1000)), patch.object(probe.shutil, 'disk_usage', return_value=disk) as usage, patch.object(probe.time, 'sleep'):
            self.assertEqual(probe.sample(), dict(cpuPercent=50.0, memoryUsedBytes=300, memoryTotalBytes=1000, diskUsedBytes=2000, diskTotalBytes=3000))
            usage.assert_called_once_with('/')


if __name__ == '__main__':
    unittest.main()
