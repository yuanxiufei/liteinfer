# 适配器与权重（WBS-2）

> 描述**已落地**部分（WBS-2.1 / 2.2 / 2.3 / 2.6 / 2.7），未落地部分单独标注。
> 详细契约见源码 docstring；本文只讲边界与口径。

## 分层与依赖方向

```text
bootstrap.py（组合根：唯一允许同时 import 两侧的地方）
      │
      ├── adapters/  协议 · 注册中心 · 兼容性（可依赖第三方库）
      ├── weights/   清单校验 · 引用计数生命周期
      │
      ▼
runtime/  ── 只认 ports.AdapterLookup 端口，不 import adapters（ADR-0003）
```

`core` / `runtime` / `api` / `cli` **不得反向 import `adapters`**；
`runtime` 需要的适配器信息通过 `runtime/ports.py::AdapterLookup` 注入。
组装只在 `bootstrap.py` 发生——新增接线一律放那里。

## 适配器契约（2.1）

| 协议 | 方法 | 说明 |
|---|---|---|
| `ModelAdapter` | `validate_config` / `load` / `infer` / `release` | 推理最小接口（基线 §5.1） |
| `TrainingAdapter` | `forward` / `loss` / `backward` / `optimizer_step` / `save_checkpoint` / `load_checkpoint` | **与推理协议相互独立**，不要求推理模型实现训练方法 |

版本规则（基线 §5.2）：MAJOR 不兼容 / MINOR 新增可选能力 / PATCH 兼容性修复。
适配器用范围表达式声明兼容的框架 API（如 `>=1.0.0,<2.0.0`），
注册时由 `check_compatibility()` 判定；`~1.0` 这类模糊写法直接报错，不接受。

## 注册与就绪（2.2 / 2.3）

| 情况 | 注册时 | 启动任务时 |
|---|---|---|
| adapter_id 重复 | 拒绝（`conflict`） | — |
| 不满足协议 / api_version 非法 | 拒绝（`invalid_argument`） | — |
| 框架 API 不兼容 | **拒绝注册**（`unsupported`） | — |
| 可选依赖缺失 | 允许注册，`ready=false` | 拒绝（`dependency_missing`，HTTP 503，不可重试） |
| 设备能力不足 | 允许注册，`ready=false` | 拒绝（`unsupported`） |

两条口径：

1. **依赖缺失只关闭该能力**，核心照常可用（ADR-0003 分级）。
2. **设备能力未知 ≠ 兼容**：`device_capability=None` 时，要求特定计算能力的适配器一律判为不就绪。
   例：FP8 需要 sm89+，本机 A5000 是 sm86 ⇒ 必被拦下。

`/v1/models` 如实返回 `ready` 与 `problems`，CLI `model list` 读的就是它。

## 权重清单校验（2.6）

```python
from liteinfer.core import WeightRef
from liteinfer.weights import WeightManifest, verify_manifest

report = verify_manifest(
    WeightManifest(
        model_id="demo",
        weights=(WeightRef(source="model.safetensors", format="safetensors",
                           sha256="<hex>", size_bytes=123),),
    ),
    root="/models/demo",
)
assert report.ok  # 只有全部文件都 ok 才为真
```

| 状态 | 触发条件 |
|---|---|
| `ok` | 存在、可读、大小一致、哈希一致、格式已知 |
| `missing` | 文件不存在 |
| `unreadable` | 无法 stat / 读取 |
| `size_mismatch` | 清单大小 ≠ 实际大小 |
| `hash_mismatch` | 清单 SHA-256 ≠ 实际（含"要求哈希但没给"） |
| `unknown_format` | 格式不在已知集合，默认不放行 |

口径：**没有"大概能用"**。空清单不算通过；未知格式默认不放行；
`sha256` 给了就必须验，没给则由调用方用 `require_sha=True` 决定是否强制。
哈希分块（1 MiB）流式计算，不把大文件读进内存。

## 权重 / LoRA 生命周期（2.7）

`WeightStore` 用**引用计数**：同一份权重多个任务持有，只有最后一个释放才真正卸载。
卸载器、加载器、资源采样器全部外部注入（本模块不依赖 torch / NVML）。

每次真正卸载都产出一条 `ReleaseRecord`：`released_at` / `refcount_after` / `unloaded` /
释放前后的资源采样。**没有注入卸载器时 `unloaded=false`**——不能假装卸载成功。
对应 G2 前置条件：「不能仅以 Python 对象删除作为回收成功依据」。

## 已接入与未接入

| 能力 | 状态 |
|---|---|
| `weights.verify`（内置适配器，零依赖） | **已接入**：注册 → 门禁 → 调用 → 报告全链路可跑 |
| LLM 推理适配器（SGLang） | 未接入（WBS-2.4，等 L1 依赖锁定） |
| SDXL 推理适配器 | 未接入（WBS-2.5） |
| 训练适配器（LoRA） | 未接入（WBS-5） |
| 故障注入与恢复报告 | 未接入（WBS-2.8） |

端到端证据：`tests/integration/test_adapters_flow.py`
（含"本机没装 torch ⇒ 声明 L1 依赖的适配器在提交阶段被 503 拒绝"的真实门禁验证）。
