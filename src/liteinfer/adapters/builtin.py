"""内置适配器（WBS-2.1 的参考实现）。

这里只放**零第三方依赖**就能真实干活的能力。
刻意不放 SGLang / SDXL 适配器：它们依赖 L1 可选依赖，
在未锁定的环境里写死只会产出"看起来能用、实际跑不起来"的代码（ADR-0003）。

`WeightVerifyAdapter` 是真能力：校验权重清单，损坏权重绝不判为有效。
它同时是协议的可执行样例：注册 → 启动前门禁 → 调用 → 释放，全链路可测。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..core import WeightRef, invalid_argument
from ..weights import WeightManifest, verify_manifest
from .protocol import FRAMEWORK_API_VERSION, AdapterContext

VERIFY_KIND = "weights.verify"


class WeightVerifyAdapter:
    """权重清单校验适配器（无 GPU、无第三方依赖）。"""

    adapter_id = "builtin.weights-verify"
    api_version = "1.0.0"
    framework_api_range = f">={FRAMEWORK_API_VERSION},<2.0.0"

    def __init__(self) -> None:
        self.loaded = False

    def validate_config(self, config: Mapping[str, Any]) -> None:
        unknown = sorted(set(config) - {"model_id", "weights", "root", "require_sha"})
        if unknown:
            raise invalid_argument("权重校验配置含未知字段", unknown=unknown)
        if not config.get("weights"):
            raise invalid_argument("权重清单为空：没有权重就没有模型")

    def load(self, context: AdapterContext) -> None:
        context.check_cancel()
        self.loaded = True

    def infer(self, request: Any) -> Any:
        """`request` 为 dict：{"model_id", "weights": [...], "root", "require_sha"}。"""
        if not isinstance(request, Mapping):
            raise invalid_argument("权重校验请求必须是对象")
        self.validate_config(request)

        weights = tuple(
            WeightRef(
                source=str(item["source"]),
                format=str(item.get("format", "unknown")),
                sha256=item.get("sha256"),
                size_bytes=item.get("size_bytes"),
                license=item.get("license"),
            )
            for item in request["weights"]
        )
        manifest = WeightManifest(
            model_id=str(request.get("model_id", "unknown")),
            version=str(request.get("version", "unpinned")),
            weights=weights,
            license=request.get("license"),
            source=request.get("source"),
        )
        root = request.get("root")
        report = verify_manifest(
            manifest,
            root=Path(root) if root else None,
            require_sha=bool(request.get("require_sha", False)),
        )
        return report.to_dict()

    def release(self) -> None:
        self.loaded = False
