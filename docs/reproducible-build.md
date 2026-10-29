# 可复现构建

原则：只用锁定的版本与哈希，环境差异必须先记录再消解。

- Python 依赖：以 `uv.lock` 锁定，用 `uv sync --frozen` 安装。
- 容器基础镜像：固定标签并记录 digest；不要使用 `latest`。
- CUDA 扩展：锁定源码版本、编译参数、目标架构与构建产物哈希。
- 模型权重：把来源、哈希、格式、版本和许可信息登记进权重清单（WBS-2.6）。
- 运行时版本台账：`env/runtime-versions.json`。

## 验证流程

1. 在干净环境（新容器 / 新虚拟环境）从头构建并安装。
2. 执行 `deploy/scripts/verify_dependencies.py`，比对已安装结果与锁文件差异。
3. 执行 `pytest -m unit`（无 GPU）与 `pytest -m gpu`（正式 GPU 环境）。
4. 跑一轮基准测试，与 `env/benchmark-baseline.json` 比对软阈值。
5. 把环境差异、测试命令与日志位置归档到本次 `acceptance-report.md`。

不能用"理论上版本已足够"作为复现证据，必须实际执行一遍。
