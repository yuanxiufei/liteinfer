"""权重与 LoRA 生命周期（WBS-2.6 / 2.7 / WBS-5）。

- `manifest`：权重清单校验，损坏权重绝不判为有效。
- `store`：引用计数式加载/释放，每次真正释放都留可观测证据。

本模块不依赖 torch / NVML：加载器、卸载器与资源采样器都由外部注入。
"""

from __future__ import annotations

from .manifest import (
    CHUNK_BYTES,
    KNOWN_FORMATS,
    VerificationReport,
    WeightCheck,
    WeightManifest,
    WeightStatus,
    check_weight,
    sha256_of,
    verify_manifest,
)
from .store import (
    LoadFn,
    ReleaseRecord,
    SamplerFn,
    UnloadFn,
    WeightHandle,
    WeightStore,
)

__all__ = [
    "CHUNK_BYTES",
    "KNOWN_FORMATS",
    "LoadFn",
    "ReleaseRecord",
    "SamplerFn",
    "UnloadFn",
    "VerificationReport",
    "WeightCheck",
    "WeightHandle",
    "WeightManifest",
    "WeightStatus",
    "WeightStore",
    "check_weight",
    "sha256_of",
    "verify_manifest",
]
