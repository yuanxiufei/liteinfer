"""权重清单校验：损坏权重绝不能被判为有效（WBS-2.6 / G2 前置）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from liteinfer.core import WeightRef
from liteinfer.weights import (
    WeightManifest,
    WeightStatus,
    check_weight,
    sha256_of,
    verify_manifest,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def weight_dir(tmp_path: Path) -> Path:
    return tmp_path


def _write(path: Path, content: bytes) -> str:
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_ok_when_size_and_hash_match(weight_dir: Path):
    digest = _write(weight_dir / "model.safetensors", b"weights-bytes")
    ref = WeightRef(source="model.safetensors", format="safetensors", sha256=digest, size_bytes=13)
    result = check_weight(ref, root=weight_dir)
    assert result.ok, result.detail
    assert result.status == WeightStatus.OK


def test_missing_file_is_not_ok(weight_dir: Path):
    ref = WeightRef(source="nope.safetensors", format="safetensors")
    result = check_weight(ref, root=weight_dir)
    assert result.status == WeightStatus.MISSING
    assert not result.ok


def test_size_mismatch_is_detected(weight_dir: Path):
    _write(weight_dir / "m.bin", b"12345")
    ref = WeightRef(source="m.bin", format="bin", size_bytes=999)
    result = check_weight(ref, root=weight_dir)
    assert result.status == WeightStatus.SIZE_MISMATCH
    assert "999" in result.detail


def test_corrupted_content_is_caught_by_hash(weight_dir: Path):
    """核心口径：内容被改但大小没变，也必须抓出来。"""
    digest = _write(weight_dir / "m.bin", b"original-bytes")
    (weight_dir / "m.bin").write_bytes(b"corrupted-byte")  # 长度相同（14），内容已变
    ref = WeightRef(source="m.bin", format="bin", sha256=digest, size_bytes=14)
    result = check_weight(ref, root=weight_dir)
    assert result.status == WeightStatus.HASH_MISMATCH
    assert "损坏" in result.detail


def test_unknown_format_is_not_silently_accepted(weight_dir: Path):
    _write(weight_dir / "m.weird", b"x")
    ref = WeightRef(source="m.weird", format="weird")
    assert check_weight(ref, root=weight_dir).status == WeightStatus.UNKNOWN_FORMAT
    assert check_weight(ref, root=weight_dir, allow_unknown_format=True).ok


def test_require_sha_flags_manifest_without_hash(weight_dir: Path):
    _write(weight_dir / "m.bin", b"x")
    ref = WeightRef(source="m.bin", format="bin")
    assert check_weight(ref, root=weight_dir).ok
    assert check_weight(ref, root=weight_dir, require_sha=True).status == WeightStatus.HASH_MISMATCH


def test_sha256_of_large_file_is_chunked(tmp_path: Path):
    """分块算哈希：结果必须与一次性算的一致（大文件不能靠全量读入内存）。"""
    path = tmp_path / "big.bin"
    content = b"a" * (3 * 1024 * 1024 + 7)
    path.write_bytes(content)
    assert sha256_of(path, chunk_bytes=1024) == hashlib.sha256(content).hexdigest()


def test_manifest_ok_only_when_every_file_ok(weight_dir: Path):
    good = _write(weight_dir / "a.safetensors", b"aaa")
    _write(weight_dir / "b.safetensors", b"bbb")
    manifest = WeightManifest(
        model_id="demo",
        weights=(
            WeightRef(source="a.safetensors", format="safetensors", sha256=good, size_bytes=3),
            WeightRef(source="b.safetensors", format="safetensors", sha256="deadbeef"),
        ),
    )
    report = verify_manifest(manifest, root=weight_dir)
    assert not report.ok
    assert len(report.problems) == 1
    assert report.problems[0].status == WeightStatus.HASH_MISMATCH


def test_empty_manifest_is_not_ok():
    """没有权重就没有模型：空清单绝不能算通过。"""
    report = verify_manifest(WeightManifest(model_id="empty"))
    assert not report.ok
