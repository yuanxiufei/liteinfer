"""权重清单与校验（WBS-2.6）。

核心纪律（G2 前置验收条件）：**损坏权重不能被误判为有效模型**。
因此这里只有两种结果：`ok` 或明确的问题；不存在"大概能用"。

校验项：
1. 文件存在且可读
2. 字节大小与清单一致（`size_bytes` 给了就必须一致）
3. SHA-256 与清单一致（给了就必须一致；分块流式算，避免大文件占内存）
4. 格式已知（`format` 缺省 `unknown`，默认不放行）

设计取舍：SHA-256 默认**不强制**——4TB 盘上的大模型全量哈希很慢。
是否强制由调用方决定（`require_sha=True`），清单里写了哈希就必须验。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import WeightRef

CHUNK_BYTES = 1 << 20  # 1 MiB

#: 已知权重格式。未知格式默认不放行，避免"扩展名对了内容不对"。
KNOWN_FORMATS = frozenset(
    {"safetensors", "bin", "pt", "pth", "gguf", "onnx", "npz", "ckpt"}
)


class WeightStatus:
    """校验状态。故意用字符串常量而不是枚举，方便日志与 JSON 直接落盘。"""

    OK = "ok"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    SIZE_MISMATCH = "size_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    UNKNOWN_FORMAT = "unknown_format"


@dataclass(frozen=True)
class WeightCheck:
    """单个权重文件的校验结果。"""

    source: str
    status: str
    detail: str = ""
    size_bytes: int | None = None
    sha256: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == WeightStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "detail": self.detail,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass
class VerificationReport:
    """清单校验汇总。只有全部 `ok` 才算通过。"""

    model_id: str
    checks: list[WeightCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(item.ok for item in self.checks)

    @property
    def problems(self) -> list[WeightCheck]:
        return [item for item in self.checks if not item.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "ok": self.ok,
            "checks": [item.to_dict() for item in self.checks],
        }


@dataclass(frozen=True)
class WeightManifest:
    """权重清单：一组文件的来源、格式、哈希与许可信息（基线 2.6）。"""

    model_id: str
    version: str = "unpinned"
    weights: tuple[WeightRef, ...] = ()
    license: str | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "weights": [item.to_dict() for item in self.weights],
            "license": self.license,
            "source": self.source,
        }


def sha256_of(path: Path, *, chunk_bytes: int = CHUNK_BYTES) -> str:
    """分块流式计算 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve(source: str, root: Path | None) -> Path:
    path = Path(source)
    if root is not None and not path.is_absolute():
        return root / path
    return path


def check_weight(
    ref: WeightRef,
    *,
    root: Path | None = None,
    require_sha: bool = False,
    allow_unknown_format: bool = False,
) -> WeightCheck:
    """校验单个权重文件。任何异常都转成明确状态，绝不返回"大概 ok"。"""
    path = _resolve(ref.source, root)

    if ref.format not in KNOWN_FORMATS and not allow_unknown_format:
        return WeightCheck(
            source=ref.source,
            status=WeightStatus.UNKNOWN_FORMAT,
            detail=f"未知权重格式 {ref.format!r}；已知格式：{sorted(KNOWN_FORMATS)}",
        )

    if not path.exists():
        return WeightCheck(source=ref.source, status=WeightStatus.MISSING, detail=str(path))

    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        return WeightCheck(
            source=ref.source, status=WeightStatus.UNREADABLE, detail=f"无法读取：{exc}"
        )

    if ref.size_bytes is not None and actual_size != ref.size_bytes:
        return WeightCheck(
            source=ref.source,
            status=WeightStatus.SIZE_MISMATCH,
            detail=f"清单 {ref.size_bytes} 字节，实际 {actual_size} 字节",
            size_bytes=actual_size,
        )

    if ref.sha256 is None and require_sha:
        return WeightCheck(
            source=ref.source,
            status=WeightStatus.HASH_MISMATCH,
            detail="要求提供 SHA-256 但清单未给出",
            size_bytes=actual_size,
        )

    actual_sha: str | None = None
    if ref.sha256 is not None:
        try:
            actual_sha = sha256_of(path)
        except OSError as exc:
            return WeightCheck(
                source=ref.source,
                status=WeightStatus.UNREADABLE,
                detail=f"读取失败：{exc}",
                size_bytes=actual_size,
            )
        if actual_sha.lower() != ref.sha256.lower():
            return WeightCheck(
                source=ref.source,
                status=WeightStatus.HASH_MISMATCH,
                detail=f"清单 {ref.sha256[:12]}… 实际 {actual_sha[:12]}…（权重已损坏或被替换）",
                size_bytes=actual_size,
                sha256=actual_sha,
            )

    return WeightCheck(
        source=ref.source,
        status=WeightStatus.OK,
        detail=str(path),
        size_bytes=actual_size,
        sha256=actual_sha,
    )


def verify_manifest(
    manifest: WeightManifest,
    *,
    root: Path | None = None,
    require_sha: bool = False,
    allow_unknown_format: bool = False,
) -> VerificationReport:
    """校验整份清单。空清单不算通过——没有权重就没有模型。"""
    report = VerificationReport(model_id=manifest.model_id)
    for ref in manifest.weights:
        report.checks.append(
            check_weight(
                ref,
                root=root,
                require_sha=require_sha,
                allow_unknown_format=allow_unknown_format,
            )
        )
    return report
