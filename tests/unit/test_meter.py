"""资源采样（WBS-3.4 的非 GPU 部分）：只读真实数据，测不到就明确报 unsupported。"""

from __future__ import annotations

import sys

import pytest

from liteinfer.core import ErrorCode, LiteInferError
from liteinfer.runtime import CompositeMeter, DiskMeter, ProcMeter

pytestmark = pytest.mark.unit

_PROC_CONTENT = """Name:\tpython
VmPeak:\t  123456 kB
VmRSS:\t   40960 kB
Threads:\t3
"""


def test_proc_meter_reads_rss(tmp_path):
    fake = tmp_path / "status"
    fake.write_text(_PROC_CONTENT, encoding="utf-8")
    meter = ProcMeter(fake)
    if not sys.platform.startswith("linux"):
        # 非 Linux 上 supported=False：这是诚实行为，不是缺陷
        assert meter.supported is False
        return
    sample = meter.sample()
    assert sample.ram_bytes == 40960 * 1024
    assert sample.vram_bytes == 0  # 显存测不到就留 0，不许拿内存冒充
    assert sample.source == "proc:VmRSS"


def test_proc_meter_is_unsupported_when_file_missing(tmp_path):
    meter = ProcMeter(tmp_path / "not-there")
    assert meter.supported is False
    with pytest.raises(LiteInferError) as excinfo:
        meter.sample()
    assert excinfo.value.code is ErrorCode.UNSUPPORTED


def test_proc_meter_reports_unsupported_on_windows_by_default():
    if sys.platform.startswith("linux"):
        pytest.skip("Linux 上默认路径可用")
    meter = ProcMeter()
    assert meter.supported is False
    with pytest.raises(LiteInferError) as excinfo:
        meter.sample()
    assert excinfo.value.code is ErrorCode.UNSUPPORTED
    assert excinfo.value.details["platform"] == sys.platform


def test_proc_meter_rejects_status_without_rss(tmp_path):
    fake = tmp_path / "status"
    fake.write_text("Name:\tpython\nThreads:\t1\n", encoding="utf-8")
    meter = ProcMeter(fake)
    if not meter.supported:
        pytest.skip("非 Linux：走到 unsupported 分支，已在别的用例覆盖")
    with pytest.raises(LiteInferError) as excinfo:
        meter.sample()
    assert excinfo.value.code is ErrorCode.UNSUPPORTED


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


def test_composite_meter_merges_kinds_without_adding_them(tmp_path):
    """三类资源分开记，合并时也不许相加成一个总数。"""
    disk = DiskMeter(tmp_path)
    composite = CompositeMeter([disk])
    sample = composite.sample()
    assert sample.ssd_bytes == disk.sample().ssd_bytes
    assert sample.vram_bytes == 0
    assert sample.source == "disk_usage"
