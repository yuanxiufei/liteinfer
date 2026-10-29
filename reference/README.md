# 参考源码（reference/）

本目录放**上游参考项目的源码快照**，用来读实现、对齐术语与确认版本约束。
它不是本项目的代码，也不是本项目的依赖。

## 铁律

1. **不是依赖**：不得写进 `pyproject.toml`，不得 `import`，不得把源码复制进 `src/`。
   确需照抄某个实现细节时，必须另开 ADR 说明来源与取舍，并按其许可证处理。
2. **只读**：不要在这里改代码；改动会在下次 `--update` 时丢失。
3. **版本以 commit 为准**：`SOURCES.lock.json` 记录每个仓库的 commit 与抓取日期，
   任何结论引用都必须能落到某个 commit，不能只写「参考 SGLang」。
4. **不进主仓**：目录内容已被 `.gitignore` 排除，只有本文件与 `SOURCES.lock.json` 入库；
   换机器时用脚本重新拉取，不靠手抄。
   另外 `pyproject.toml` 已把 `reference` 排除出 ruff——上游自带 ruff 配置（含 `required-version`）
   会顶掉本仓版本，不排除会让 `ruff check .` 直接失败。

## 复现

```bash
python deploy/scripts/fetch_references.py            # 检查现状（不动盘）
python deploy/scripts/fetch_references.py --apply    # 补齐缺失的仓库
python deploy/scripts/fetch_references.py --update   # 跟进上游最新 commit（会改 lock）
python deploy/scripts/fetch_references.py --all      # 含 optional（triton，WBS-7）
```

## 已拉取（2026-10-29，浅克隆 `--depth 1`）

| 项目 | WBS | 用途 | commit | 体积 |
|---|---|---|---|---|
| [sglang](https://github.com/sgl-project/sglang) | WBS-0.2 / WBS-2 | **推理后端真源**：调度、KV 缓存、RadixAttention、取消与抢占语义 | `f2d31889258d` | 0.19 GiB |
| [vllm](https://github.com/vllm-project/vllm) | WBS-3 | 对照实现：分页 KV、连续批处理、背压与抢占（**只读，不采用为依赖**） | `f5aa164b5148` | 0.18 GiB |
| [peft](https://github.com/huggingface/peft) | WBS-5 | LoRA 训练参考：adapter 注入、权重合并与保存格式 | `89570547b9c2` | 0.04 GiB |
| [diffusers](https://github.com/huggingface/diffusers) | WBS-6 | 扩散训练参考：噪声/时间步采样、训练循环与显存优化 | `40e551a4380c` | 0.06 GiB |
| triton | WBS-7 | 算子管理参考（optional，默认不拉，`--all` 拉取） | — | — |

## 上游线索（WBS-0.2 锁版本的输入，非结论）

以下来自所拉 commit 的 `pyproject.toml`，是**候选约束**，必须在 WSL2 实测通过后才写进
`env/compatibility-matrix.md` 与 `env/runtime-versions.json`，并在 `install.py` 里校验。

| 项 | sglang（`f2d31889258d`） | vllm（`f5aa164b5148`） |
|---|---|---|
| Python | `>=3.11` | `>=3.11,<3.15` |
| torch | `==2.14.1` | `==2.14.0` |
| transformers | `==5.19.0` | 动态依赖，未在此文件钉死 |
| CUDA 相关 | `cuda-python>=13.0`、`flashinfer_python[cu13]==0.7.2`、`nvidia-cutlass-dsl[cu13]==4.8.0` | 未见同等 pin |

**冲突提示**：两个后端钉死的 torch 版本不同（2.14.1 vs 2.14.0）。
这就是 vllm 在本项目里只作参考、不作依赖的理由之一——同时装会直接撞车。
本项目选 **sglang 作为唯一推理后端依赖**，vllm 仅用于对照设计。

**与本机的一致性**：宿主驱动 610.88 支持 CUDA 13；sglang 的 `cu13` extra 与其匹配（ADR-0001）。
A5000 为 sm86（Ampere），无 FP8 Tensor Core，sglang 的 FP8 路径不可用于本机验收。
