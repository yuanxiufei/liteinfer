# ADR 变更记录

## 用途

凡影响接口边界、依赖组合、正式运行环境、安全策略或验收口径的决策，都必须落一份 ADR。

## 编号与命名

```text
docs/adr/0001-<短横线小写英文短语>.md
```

从 `0000-adr-template.md` 复制，编号不复用、不重排。

## 首份 ADR（必写）

第一周必须提交第一份技术决策记录，至少明确：

1. 正式 GPU 环境是 Linux 容器还是 WSL2，以及 Windows 主机的职责边界。
2. 训练后端边界：LiteInfer 负责哪些，底层已验证训练路径负责哪些。
3. 锁定的版本组合（见 `env/compatibility-matrix.md`）。
4. 已知限制（例如 A5000 无 FP8 Tensor Core、扩散训练探针结果）。

## 状态流转

`proposed` → `accepted` →（`superseded by NNNN` / `deprecated`）→ 保留原文不删。
