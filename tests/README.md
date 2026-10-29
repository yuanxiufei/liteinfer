# 测试体系

分层与 WBS 对应关系：

| 目录 | 标记 | 用途 | 是否需要 GPU |
|---|---|---|---|
| `tests/unit/` | `unit` | 状态机、配置校验、版本匹配、错误码 | 否 |
| `tests/integration/` | `integration` | SDK / HTTP / CLI 进程级流程 | 否 |
| `tests/gpu/` | `gpu` | 真实加载、生成、卸载、恢复 | 是 |
| `tests/regression/` | `regression` | 数值、显存、性能对照基线 | 是（基线） |
| `tests/chaos/` | `chaos` | 故障注入：队列溢出、磁盘满、权重损坏、进程退出、CUDA 错误 | 是 |

```bash
pytest -q                       # 全部（当前 unit + integration）
pytest -m unit                  # 无 GPU 场景，CI 默认
pytest -m integration           # 进程 / 服务级（HTTP 真实起服务，不用 mock）
pytest -m gpu                   # 需要真实 GPU
pytest -m "regression or chaos" # 需要基线数据与显存预算
```

`tests/conftest.py` 会把 `src/` 加进 `sys.path`，未执行 `pip install -e .` 也能跑。
当前 `gpu/` / `regression/` / `chaos/` 为空：需要真实 GPU 与基线数据后才能写。

规则：

1. 涉及 GPU 的测试必须在报告里记录 GPU 型号、驱动、软件版本、模型版本与运行配置。
2. 开发机上成功运行一次不等于通过回归验收。
3. 释放验证不能只看 Python 对象删除，必须看框架可观测资源回落。
4. 数值容差按任务类型配置，不能要求所有模型输出逐元素相等。
