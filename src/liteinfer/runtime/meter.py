"""资源采样（WBS-3.4）：显存 / 内存 / 磁盘三类都能跑，测不到就明说测不到。

口径：

- **只报自己真测到的**。测不到的一律把 `source` 标成 `unavailable`，绝不用 0 冒充"测过了"。
- 三类资源**分开记、分开报**，不相加：显存（GPU）、内存（CPU 侧）、磁盘（SSD）。
- 每一类都有真实通路，缺硬件或缺依赖时如实退化：
  - 显存：`pynvml`（可选依赖，ADR-0003 尚未锁定）→ 否则 `nvidia-smi`（NVML 官方命令行）；
    两条都没有（无 NVIDIA 卡 / 没装驱动）就 `supported=False`。
  - 内存：Linux 读 `/proc`，Windows 走 `psapi.GetProcessMemoryInfo`，macOS 走 `resource`；
    其余平台不支持。
  - 磁盘：`shutil.disk_usage`，跨平台，测的是**整块盘**已用字节。
- 没做的：本进程显存占用（要 `nvmlDeviceGetComputeRunningProcesses`，依赖锁定后再接）、
  GPU 利用率、按卡拆分的显存—现在多卡只取**最大值**并如实标注口径。
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..core import LiteInferError, ResourceBudget, invalid_argument, unsupported
from .lease import ResourceMeter, ResourceSample

_KIND_FIELDS = {"vram": "vram_bytes", "ram": "ram_bytes", "ssd": "ssd_bytes"}


class ProcMeter:
    """进程内存采样（CPU 一侧）。按平台走各自的真实接口：

    - Linux：读 `/proc/<pid>/status` 的 `VmRSS`（`source="proc:VmRSS"`）
    - Windows：`psapi.GetProcessMemoryInfo` 的 `WorkingSetSize`（`source="win:WorkingSetSize"`）
    - macOS：`resource.getrusage` 的 `ru_maxrss`（`source="resource:ru_maxrss"`，是**峰值**）

    其余平台（或文件读不到）：`supported=False`，`sample()` 抛 `unsupported`。
    """

    kind = "ram"
    DEFAULT_PATH = "/proc/self/status"

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        platform: str | None = None,
    ) -> None:
        self._path = Path(path) if path else Path(self.DEFAULT_PATH)
        # platform 可显式指定：用于在没有 /proc 的机器上验证 Linux 解析逻辑
        self._platform = platform or sys.platform

    @property
    def path(self) -> Path:
        return self._path

    @property
    def platform(self) -> str:
        return self._platform

    @property
    def supported(self) -> bool:
        if self._platform.startswith("linux"):
            return self._path.is_file()
        if self._platform.startswith("win"):
            return True  # psapi 属于系统自带，失败会在 sample() 里如实报出
        return self._platform.startswith("darwin")

    def sample(self) -> ResourceSample:
        if not self.supported:
            raise unsupported(
                "当前平台不支持进程内存采样",
                platform=self._platform,
                path=str(self._path),
            )
        if self._platform.startswith("linux"):
            return ResourceSample(ram_bytes=self._read_proc(), source="proc:VmRSS")
        if self._platform.startswith("win"):
            return ResourceSample(ram_bytes=_windows_rss_bytes(), source="win:WorkingSetSize")
        return ResourceSample(ram_bytes=_darwin_rss_bytes(), source="resource:ru_maxrss")

    def _read_proc(self) -> int:
        try:
            text = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise unsupported(
                "进程状态文件读不到", path=str(self._path), error=f"{type(exc).__name__}: {exc}"
            ) from exc
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1]) * 1024
        raise unsupported("进程状态里读不到 VmRSS", path=str(self._path), platform=self._platform)


def _windows_rss_bytes() -> int:
    """Windows 进程内存：psapi 的 `PROCESS_MEMORY_COUNTERS.WorkingSetSize`。零第三方依赖。"""

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    ok = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    )
    if not ok:
        raise unsupported(
            "Windows 进程内存采样失败",
            source="psapi:GetProcessMemoryInfo",
            error=ctypes.get_last_error(),
        )
    if counters.WorkingSetSize <= 0:
        raise unsupported("Windows 进程内存采样读到 0，视为没测到", source="psapi")
    return int(counters.WorkingSetSize)


def _darwin_rss_bytes() -> int:
    """macOS 进程内存：`ru_maxrss`（字节）。它是**峰值**，不是当前值，来源里已标出。"""
    resource = importlib.import_module("resource")

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if value <= 0:
        raise unsupported("macOS 进程内存采样读到 0，视为没测到", source="resource:ru_maxrss")
    return int(value)


def parse_smi_memory(text: str) -> int:
    """解析 `nvidia-smi --query-gpu=memory.used` 的输出，返回**字节**（多卡取最大值）。

    接受 `1057 MiB` 与裸数字（裸数字按 MiB 计，这是该命令 `nounit` 缺省单位）。
    解析不出任何数字（空输出、`[N/A]`）就抛 `invalid_argument`——
    **不许把"读不出来"当成 0 字节**。
    """
    best = 0
    seen = False
    for line in text.splitlines():
        # 只认第一列：QUERY 只查 memory.used，同行的 total 之类不参与
        cell = line.split(",")[0].strip()
        parts = cell.split()
        if not parts or not parts[0].isdigit():
            continue
        amount = int(parts[0])
        unit = parts[1].lower() if len(parts) > 1 else "mib"
        if unit not in _UNIT_BYTES:
            raise invalid_argument(f"显存采样出现未知单位：{unit}", unit=unit, line=line)
        best = max(best, amount * _UNIT_BYTES[unit])
        seen = True
    if not seen:
        raise invalid_argument("显存采样输出里没有可用数字", output=text[:200])
    return best


_UNIT_BYTES = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3}


class NvmlMeter:
    """显存采样（GPU 一侧）。按序尝试两条真实通路：

    1. `pynvml`（可选依赖，ADR-0003 尚未锁定）— 直接调 NVML；
    2. `nvidia-smi --query-gpu=memory.used` — NVML 的官方命令行；

    两条都没有（无 NVIDIA 卡 / 无驱动 / 没装依赖）就 `supported=False`，
    `sample()` 抛 `unsupported`。**绝不返回 0 冒充"显存空闲"**。

    口径：报的是**整卡已用显存**（多卡取最大值），不是本进程占用。
    """

    kind = "vram"
    QUERY = ["--query-gpu=memory.used", "--format=csv,noheader"]

    def __init__(
        self,
        *,
        command: str = "nvidia-smi",
        runner: Callable[..., object] | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._command = command
        self._runner = runner or subprocess.run
        self._timeout_s = timeout_s

    @property
    def supported(self) -> bool:
        return self._pynvml() is not None or shutil.which(self._command) is not None

    @property
    def backend(self) -> str:
        if self._pynvml() is not None:
            return "pynvml"
        if shutil.which(self._command) is not None:
            return "nvidia-smi"
        return "none"

    def sample(self) -> ResourceSample:
        pynvml = self._pynvml()
        if pynvml is not None:
            return self._sample_pynvml(pynvml)
        if shutil.which(self._command) is None:
            raise unsupported(
                "没有可用的显存采样通路（既无 pynvml，也找不到 nvidia-smi）",
                command=self._command,
            )
        output = self._run_smi()
        return ResourceSample(vram_bytes=parse_smi_memory(output), source="nvml:nvidia-smi")

    def _run_smi(self) -> str:
        completed = self._runner(
            [self._command, *self.QUERY],
            capture_output=True,
            text=True,
            check=False,
            timeout=self._timeout_s,
        )
        if getattr(completed, "returncode", 0) != 0:
            raise unsupported(
                "nvidia-smi 调用失败",
                command=self._command,
                returncode=getattr(completed, "returncode", None),
                stderr=str(getattr(completed, "stderr", ""))[:200],
            )
        return str(getattr(completed, "stdout", ""))

    @staticmethod
    def _pynvml() -> object | None:
        try:
            return importlib.import_module("pynvml")
        except ImportError:
            return None

    @staticmethod
    def _sample_pynvml(pynvml: Any) -> ResourceSample:
        pynvml.nvmlInit()
        try:
            used = 0
            for index in range(int(pynvml.nvmlDeviceGetCount())):
                handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                used = max(used, int(pynvml.nvmlDeviceGetMemoryInfo(handle).used))
            if used <= 0:
                raise unsupported("NVML 没读到任何已用显存，视为没测到", backend="pynvml")
            return ResourceSample(vram_bytes=used, source="nvml:pynvml")
        except LiteInferError:
            raise
        except Exception as exc:  # noqa: BLE001 - 驱动/设备异常也要如实报，不许当 0
            raise unsupported(
                "NVML 采样失败", backend="pynvml", error=f"{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            with contextlib.suppress(Exception):  # 收尾失败不影响已拿到的结论
                pynvml.nvmlShutdown()


class DiskMeter:
    """磁盘占用采样（SSD 口径）。跨平台，测的是**整块盘**的已用字节，不是本进程占用。"""

    kind = "ssd"

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

    def __init__(self, meter: ResourceMeter) -> None:
        self._meter = meter
        self.failures = 0
        self.last_error: str | None = None
        self.last_source: str | None = None

    @property
    def meter(self) -> ResourceMeter:
        return self._meter

    def sample(self) -> ResourceSample:
        try:
            sample = self._meter.sample()
        except LiteInferError as exc:
            self.failures += 1
            self.last_error = f"{exc.code.value}: {exc}"
            self.last_source = "unavailable"
            return ResourceSample(source="unavailable")
        self.last_source = sample.source
        return sample


class CompositeMeter:
    """把多个采样器合起来：同类取**最大值**，来源如实拼出来。

    每个子采样器单独容错：GPU 或某一类测不到，不会把其他类一起拖垮，
    但失败会记进 `failures`，并在 `report()` 里逐类说明「测到了 / 没测到」。
    """

    def __init__(self, meters: Sequence[ResourceMeter]) -> None:
        raw = list(meters)
        self._meters = [
            meter if isinstance(meter, BestEffortMeter) else BestEffortMeter(meter)
            for meter in raw
        ]
        self._kinds = [str(getattr(meter, "kind", "unknown")) for meter in raw]
        self._last: dict[str, ResourceSample] = {}

    @property
    def failures(self) -> int:
        return sum(meter.failures for meter in self._meters)

    def sample(self) -> ResourceSample:
        vram = ram = ssd = 0
        sources: list[str] = []
        for meter, kind in zip(self._meters, self._kinds, strict=True):
            sample = meter.sample()
            self._last[kind] = sample
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

    def report(self) -> dict[str, dict[str, object]]:
        """逐类报告：每类给 `bytes` / `source` / `measured`。

        没测到的类 `measured=False`、`source="unmeasured"`——
        「没有数据」和「测得 0」是两回事，这里必须能区分。
        """
        sample = self.sample()
        report: dict[str, dict[str, object]] = {}
        for kind in ("vram", "ram", "ssd"):
            field = _KIND_FIELDS[kind]
            last = self._last.get(kind)
            measured = (
                last is not None
                and last.source != "unavailable"
                and int(getattr(last, field)) > 0
            )
            report[kind] = {
                "bytes": int(getattr(sample, field)),
                "source": last.source if measured and last is not None else "unmeasured",
                "measured": measured,
            }
        return report

    def as_budget(self) -> ResourceBudget:
        """把最近一次采样折成 `ResourceBudget`（三类分开，不相加）。仅供对照，不是硬隔离。"""
        sample = self.sample()
        return ResourceBudget(
            vram_bytes=sample.vram_bytes,
            ram_bytes=sample.ram_bytes,
            ssd_bytes=sample.ssd_bytes,
        )


def build_meter(*, disk_path: str | Path = ".", include_gpu: bool = True) -> CompositeMeter:
    """当前环境能测什么就装什么：显存（GPU）+ 内存（CPU）+ 磁盘（SSD）。

    缺哪类就缺哪类，报告里会写清楚 `measured=False`，不会拿 0 填数。
    """
    meters: list[ResourceMeter] = []
    if include_gpu:
        meters.append(NvmlMeter())
    meters.append(ProcMeter())
    meters.append(DiskMeter(disk_path))
    return CompositeMeter(meters)
