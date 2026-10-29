"""组合根（composition root）。

ADR-0003 规定 `core` / `runtime` / `api` / `cli` 不得反向依赖适配器层，
但总得有个地方把它们拼起来——**只有本模块允许同时 import 两侧**。
新增接线一律放这里，不要在 cli / api 里偷偷 import adapters。

当前接入：
- 内置 `WeightVerifyAdapter`（`weights.verify`，零依赖）
- 注册中心按 kind 提供启动前门禁（缺失依赖 / 不兼容在提交阶段就报错）

尚未接入（需要 ADR-0003 的 L1 可选依赖锁定后）：
- LLM 推理适配器（SGLang，WBS-2.4）
- SDXL 推理适配器（WBS-2.5）
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .adapters import ModelRegistry
from .adapters.builtin import VERIFY_KIND, WeightVerifyAdapter
from .adapters.protocol import DependencySpec, describe_capabilities
from .core import LiteInferConfig, TaskRequest
from .core.config import config_from_mapping
from .runtime import ExecutionContext, TaskService, default_executor
from .runtime.meter import build_meter

AdapterFactory = Callable[[], WeightVerifyAdapter]


def build_registry(*, device_capability: tuple[int, int] | None = None) -> ModelRegistry:
    """构造注册中心。`device_capability` 形如 `(8, 6)`；未知传 `None`。"""
    return ModelRegistry(device_capability=device_capability)


def _verify_handler(adapter: WeightVerifyAdapter) -> Callable[..., dict[str, Any]]:
    def handler(context: ExecutionContext, request: TaskRequest) -> dict[str, Any]:
        context.check_cancel()
        report = adapter.infer(request.payload)
        return {"output": report, "metrics": {"backend": VERIFY_KIND, "ok": report["ok"]}}

    return handler


def build_service(
    config: LiteInferConfig | Mapping[str, Any] | None = None,
    *,
    device_capability: tuple[int, int] | None = None,
    extra_handlers: Mapping[str, Callable[..., dict[str, Any]]] | None = None,
    configure: Callable[[ModelRegistry], None] | None = None,
) -> TaskService:
    """组装一个带适配器门禁的任务服务。

    `configure` 用于在返回前继续注册适配器（例如声明依赖 L1 的 LLM 适配器）。
    """
    registry = build_registry(device_capability=device_capability)
    verify = WeightVerifyAdapter()
    registry.register(
        verify,
        capability=describe_capabilities(
            kinds=(VERIFY_KIND,),
            modalities=("weights",),
            weight_formats=("safetensors", "bin", "gguf", "onnx"),
        ),
        dependency=DependencySpec(level="L0", note="零依赖，始终可用"),
    )

    if configure is not None:
        configure(registry)

    handlers: dict[str, Callable[..., dict[str, Any]]] = {
        VERIFY_KIND: _verify_handler(verify),
    }
    handlers.update(extra_handlers or {})
    executor = default_executor(handlers)

    if isinstance(config, LiteInferConfig):
        resolved = config
    elif config is None:
        resolved = None
    else:
        resolved = config_from_mapping(dict(config))
    # 采样器只装当前环境真能测的那些（显存采样待 WBS-3.4 的 NVML 接入）
    return TaskService(resolved, executor, adapter_lookup=registry, meter=build_meter())


def weights_root_from(path: str | Path | None) -> Path | None:
    """把配置里的权重根目录规范成 `Path`，便于传给校验适配器。"""
    return Path(path) if path else None
