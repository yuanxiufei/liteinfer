"""资源采样（WBS-3.4）：显存 / 内存 / 磁盘三类都要有真实通路，测不到就明说。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from liteinfer.core import ErrorCode, LiteInferError
from liteinfer.runtime import (
    BestEffortMeter,
    CompositeMeter,
    DiskMeter,
    NvmlMeter,
    ProcMeter,
    build_meter,
    parse_smi_memory,
)

pytestmark = pytest.mark.unit

_PROC_CONTENT = """Name:\tpython
VmPeak:\t  123456 kB
VmRSS:\t   40960 kB
Threads:\t3
"""


# ---------------------------------------------------------------------- 内存

def test_proc_meter_reads_rss_on_linux_parser(tmp_path):
    """Linux 解析逻辑在任意平台都能验证（显式给 platform）。"""
    fake = tmp_path / "status"
    fake.write_text(_PROC_CONTENT, encoding="utf-8")
    meter = ProcMeter(fake, platform="linux")
    assert meter.supported is True
    sample = meter.sample()
    assert sample.ram_bytes == 40960 * 1024
    assert sample.vram_bytes == 0  # 显存测不到就留 0，不许拿内存冒充
    assert sample.source == "proc:VmRSS"


def test_proc_meter_rejects_status_without_rss(tmp_path):
    fake = tmp_path / "status"
    fake.write_text("Name:\tpython\nThreads:\t1\n", encoding="utf-8")
    meter = ProcMeter(fake, platform="linux")
    with pytest.raises(LiteInferError) as excinfo:
        meter.sample()
    assert excinfo.value.code is ErrorCode.UNSUPPORTED


def test_proc_meter_is_unsupported_when_file_missing(tmp_path):
    meter = ProcMeter(tmp_path / "not-there", platform="linux")
    assert meter.supported is False
    with pytest.raises(LiteInferError) as excinfo:
        meter.sample()
    assert excinfo.value.code is ErrorCode.UNSUPPORTED


def test_proc_meter_unsupported_on_unknown_platform():
    meter = ProcMeter(platform="plan9")
    assert meter.supported is False
    with pytest.raises(LiteInferError) as excinfo:
        meter.sample()
    assert excinfo.value.code is ErrorCode.UNSUPPORTED
    assert excinfo.value.details["platform"] == "plan9"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="只在 Windows 上验证 psapi 通路")
def test_proc_meter_reads_working_set_on_windows():
    """CPU 这一侧在 Windows 上也要真跑得通，不能只支持 Linux。"""
    meter = ProcMeter()
    assert meter.supported is True
    sample = meter.sample()
    assert sample.ram_bytes > 0
    assert sample.source == "win:WorkingSetSize"


# ---------------------------------------------------------------------- 显存

def test_parse_smi_memory_accepts_units():
    assert parse_smi_memory("1057 MiB\n") == 1057 * 1024 * 1024
    # 只认第一列（used），同行的 total 不参与
    assert parse_smi_memory("1057 MiB, 23028 MiB\n") == 1057 * 1024 * 1024
    assert parse_smi_memory("1057\n") == 1057 * 1024 * 1024  # 裸数字按 MiB
    assert parse_smi_memory("2 GiB\n") == 2 * 1024**3
    # 多卡取最大值
    assert parse_smi_memory("512 MiB\n2048 MiB\n") == 2048 * 1024 * 1024


def test_parse_smi_memory_refuses_empty_output():
    """读不出数字就报错，不许当成 0 字节的"显存空闲"。"""
    with pytest.raises(LiteInferError) as excinfo:
        parse_smi_memory("[N/A]\n")
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT

    with pytest.raises(LiteInferError):
        parse_smi_memory("")


def test_nvml_meter_unsupported_without_any_backend(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    meter = NvmlMeter()
    assert meter.supported is False
    assert meter.backend == "none"
    with pytest.raises(LiteInferError) as excinfo:
        meter.sample()
    assert excinfo.value.code is ErrorCode.UNSUPPORTED


def test_nvml_meter_reads_smi_output(monkeypatch):
    """有 nvidia-smi 就真解析，来源标清楚是命令行（不是 pynvml）。"""
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/nvidia-smi")
    meter = NvmlMeter(
        runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="1057 MiB\n", stderr=""
        )
    )
    assert meter.backend == "nvidia-smi"
    sample = meter.sample()
    assert sample.vram_bytes == 1057 * 1024 * 1024
    assert sample.source == "nvml:nvidia-smi"


def test_nvml_meter_reports_failed_smi_call(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/nvidia-smi")
    meter = NvmlMeter(
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom")
    )
    with pytest.raises(LiteInferError) as excinfo:
        meter.sample()
    assert excinfo.value.code is ErrorCode.UNSUPPORTED
    assert excinfo.value.details["returncode"] == 1


@pytest.mark.skipif(shutil.which("nvidia-smi") is None, reason="本机没有 nvidia-smi")
def test_nvml_meter_reads_real_gpu():
    """本机有 NVIDIA 卡时真采一次：整卡已用显存必须大于 0。"""
    sample = NvmlMeter().sample()
    assert sample.vram_bytes > 0
    assert sample.source.startswith("nvml:")


# ---------------------------------------------------------------------- 磁盘

def test_disk_meter_reports_real_usage(tmp_path):
    meter = DiskMeter(tmp_path)
    sample = meter.sample()
    assert sample.ssd_bytes > 0  # 整块盘的已用字节，不是本进程占用
    assert sample.ram_bytes == 0
    assert sample.source == "disk_usage"


def test_disk_meter_unsupported_when_path_missing(tmp_path):
    meter = DiskMeter(tmp_path / "missing")
    assert meter.supported is False
    with pytest.raises(LiteInferError) as excinfo:
        meter.sample()
    assert excinfo.value.code is ErrorCode.UNSUPPORTED


# ---------------------------------------------------------------------- 组合

def test_composite_meter_merges_kinds_without_adding_them(tmp_path):
    """三类资源分开记，合并时也不许相加成一个总数。"""
    disk = DiskMeter(tmp_path)
    composite = CompositeMeter([disk])
    sample = composite.sample()
    assert sample.ssd_bytes == disk.sample().ssd_bytes
    assert sample.vram_bytes == 0
    assert sample.source == "disk_usage"


def test_composite_meter_report_says_unmeasured_instead_of_zero():
    """「没有数据」与「测得 0」必须能区分。"""

    class Dead:
        kind = "vram"

        def sample(self):
            raise LiteInferError(ErrorCode.UNSUPPORTED, "没有 GPU")

    composite = CompositeMeter([Dead()])
    report = composite.report()
    assert report["vram"] == {"bytes": 0, "source": "unmeasured", "measured": False}
    assert composite.failures == 1


def test_composite_meter_keeps_other_kinds_when_one_fails(tmp_path):
    """GPU 测不到，不许把内存和磁盘也一起拖垮。"""

    class Dead:
        kind = "vram"

        def sample(self):
            raise LiteInferError(ErrorCode.UNSUPPORTED, "没有 GPU")

    composite = CompositeMeter([Dead(), DiskMeter(tmp_path)])
    sample = composite.sample()
    assert sample.ssd_bytes > 0
    assert sample.vram_bytes == 0
    assert sample.source == "disk_usage"


def test_best_effort_meter_records_failure_not_zero():
    class Dead:
        def sample(self):
            raise LiteInferError(ErrorCode.UNSUPPORTED, "测不到")

    wrapped = BestEffortMeter(Dead())
    sample = wrapped.sample()
    assert sample.source == "unavailable"
    assert wrapped.failures == 1
    assert wrapped.last_error is not None and wrapped.last_error.startswith("unsupported")


def test_build_meter_installs_three_kinds():
    meter = build_meter()
    kinds = {type(item.meter).__name__ for item in meter._meters}
    assert kinds == {"NvmlMeter", "ProcMeter", "DiskMeter"}


def test_build_meter_can_skip_gpu():
    meter = build_meter(include_gpu=False)
    assert "NvmlMeter" not in {type(item.meter).__name__ for item in meter._meters}


def test_real_subprocess_path_is_used_for_smi_query():
    """QUERY 形状要能被真实 nvidia-smi 接受（`nounit` 在部分驱动上不被识别）。"""
    assert NvmlMeter.QUERY == ["--query-gpu=memory.used", "--format=csv,noheader"]
    if shutil.which("nvidia-smi") is None:
        pytest.skip("本机没有 nvidia-smi")
    output = subprocess.run(
        ["nvidia-smi", *NvmlMeter.QUERY],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert output.returncode == 0
    assert parse_smi_memory(output.stdout) > 0
