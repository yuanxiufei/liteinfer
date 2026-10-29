"""资源采样（WBS-3.4 的一部分：不需要 GPU 的那些）。

口径：

- **只报自己真测到的**。测不到的一律留 0 并把 `source` 标出来，绝不用估算值填空。
- 三类资源**分开记**，不相加：显存、内存、磁盘。
- **显存采样（NVML / torch）尚未接入**——那需要 GPU 与锁定的依赖（ADR-0003）。
  本模块只覆盖进程内存（Linux `/proc`）与磁盘占用（`shutil.disk_usage`，跨平台）。
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from ..core import LiteInferError, unsupported
from .lease import ResourceMeter, ResourceSample


class ProcMeter:
    """进程内存采样：读 Linux `/proc/<pid>/status` 的 `VmRSS`。

    非 Linux（或文件不存在）时 `supported=False` 且 `sample()` 明确报 `unsupported`，
    **不伪装成 0 字节的"测到了"**。
    """

    DEFAULT_PATH = "/proc/self/status"

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else Path(self.DEFAULT_PATH)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def supported(self) -> bool:
        """Linux 且 `/proc` 可读即可用；Windows / macOS 明确不支持。"""
        return sys.platform.startswith("linux") and self._path.is_file()

    def sample(self) -> ResourceSample:
        if not self.supported:
            raise unsupported(
                "当前平台不支持进程内存采样（需要 Linux /proc）",
                platform=sys.platform,
                path=str(self._path),
            )
        text = self._path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return ResourceSample(ram_bytes=int(parts[1]) * 1024, source="proc:VmRSS")
        raise unsupported(
            "进程状态里读不到 VmRSS", path=str(self._path), platform=sys.platform
        )


class DiskMeter:
    """磁盘占用采样（SSD 口径）。跨平台，测的是**整块盘**的已用字节，不是本进程占用。"""

    def __init__(self, path: str | Path = ".") -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def supported(self) -> bool:
        return self._path.exists()

    def sample(self) -> ResourceSample:
        if not self.supported:
            raise unsupported("磁盘路径不存在，无法采样", path=str(self._path))
        usage = shutil.disk_usage(os.fspath(self._path))
        return ResourceSample(ssd_bytes=usage.used, source="disk_usage")


class BestEffortMeter:
    """包一层：采样失败退回空样本，**不因计量失败拖垮业务**。

    失败次数与最后一次错误都留着可查（`failures` / `last_error`）——
    计量失败不等于"资源是 0"，所以样本里 `source` 会标成 `unavailable`。
    """

    def __init__(self, meter: ResourceMeter, *, path: str | Path | None = None) -> None:
        self._meter = meter
        self._path = Path(path) if path else None
        self.failures = 0
        self.last_error: str | None = None

    def sample(self) -> ResourceSample:
        try:
            return self._meter.sample()
        except LiteInferError as exc:
            self.failures += 1
            self.last_error = f"{exc.code.value}: {exc}"
            return ResourceSample(source="unavailable")


def build_meter(*, disk_path: str | Path = ".") -> CompositeMeter:
    """当前环境能装什么就装什么。显存采样（NVML）未接入，缺的那类留 0。"""
    return CompositeMeter([ProcMeter(), DiskMeter(disk_path)])


class CompositeMeter:
    """把多个采样器合起来。同类资源取**最大值**，并把来源如实拼出来。

    每个子采样器单独容错：一个测不到（例如 Windows 上没有 `/proc`）
    不会把其他采样一起拖垮，但失败会记进 `failures`。
    """

    def __init__(self, meters: Sequence[ResourceMeter]) -> None:
        self._meters = [
            meter if isinstance(meter, BestEffortMeter) else BestEffortMeter(meter)
            for meter in meters
        ]

    @property
    def failures(self) -> int:
        return sum(meter.failures for meter in self._meters)

    def sample(self) -> ResourceSample:
        vram = ram = ssd = 0
        sources: list[str] = []
        for meter in self._meters:
            sample = meter.sample()
            if sample.source == "unavailable":
                continue  # 这一类没测到，不许记成 0 的来源
            vram = max(vram, sample.vram_bytes)
            ram = max(ram, sample.ram_bytes)
            ssd = max(ssd, sample.ssd_bytes)
            if sample.source and sample.source not in sources:
                sources.append(sample.source)
        return ResourceSample(
            vram_bytes=vram,
            ram_bytes=ram,
            ssd_bytes=ssd,
            source="+".join(sources) or "unavailable",
        )
