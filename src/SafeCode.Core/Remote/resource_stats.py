"""Read-only Linux resource sample. No dependencies, writes or background process."""
import json
import shutil
import time
from pathlib import Path


def cpu_ticks(path=Path('/proc/stat')):
    # Guest ticks are already included in user/nice; don't count them twice.
    values = [int(value) for value in path.read_text().splitlines()[0].split()[1:9]]
    return sum(values), values[3] + values[4]


def memory_bytes(path=Path('/proc/meminfo')):
    values = {key: int(value.split()[0]) * 1024
              for key, value in (line.split(':', 1) for line in path.read_text().splitlines())}
    total = values['MemTotal']
    available = values.get('MemAvailable')
    if available is None:
        available = sum(values.get(key, 0) for key in ('MemFree', 'Buffers', 'Cached', 'SReclaimable')) - values.get('Shmem', 0)
    return max(0, min(total, total - available)), total


def sample():
    total_before, idle_before = cpu_ticks()
    time.sleep(0.2)
    total_after, idle_after = cpu_ticks()
    elapsed = total_after - total_before
    cpu = 100.0 * (elapsed - (idle_after - idle_before)) / elapsed if elapsed > 0 else 0.0
    used, total = memory_bytes()
    disk = shutil.disk_usage('/')
    return dict(cpuPercent=round(max(0.0, min(100.0, cpu)), 1), memoryUsedBytes=used,
                memoryTotalBytes=total, diskUsedBytes=disk.used, diskTotalBytes=disk.total)


if __name__ == '__main__':
    print(json.dumps(sample()))
