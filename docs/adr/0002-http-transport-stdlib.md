# ADR-0002: HTTP 传输层先用标准库，依赖锁定前不引入 Web 框架

- **状态：** accepted
- **日期：** 2026-10-29
- **关联 WBS：** WBS-1.6（HTTP 服务）、WBS-0.2（依赖兼容矩阵）
- **决策者：** 项目 owner（单人主开发）

## 背景

WBS-1.6 要求交付「独立进程的 HTTP 服务，提供任务提交、查询、取消和健康检查」。
同时 `pyproject.toml` 的运行时依赖**故意为空**——基线规定：Python / PyTorch / CUDA Runtime / SGLang / Triton
的版本必须在 `env/compatibility-matrix.md` 实测锁定后才写入，禁止「先装最新版再逐个修冲突」。
在 WBS-0.2 完成之前引入 FastAPI / uvicorn，等于在锁矩阵之前先把矩阵写死。

## 决策

1. 首版 HTTP 服务用**标准库 `http.server`** 实现：`src/liteinfer/api/http_server.py`（`ThreadingHTTPServer` + 手写路由）。
2. 业务判断一律留在 `TaskService`；HTTP 层只做「错误码 → HTTP 状态码」的协议转换：
   400 / 404 / 405 / 409 / 429 / 503 的映射集中在 `core/errors.py::DEFAULT_HTTP_STATUS` 与 `api/http_server.py`。
3. 路由表固定为：`GET /healthz`、`GET /v1/models`、`GET|POST /v1/tasks`、`GET /v1/tasks/{id}`、
   `POST /v1/tasks/{id}/cancel`、`GET /v1/metrics`；未知路由 404，方法不匹配 405。
4. Web 框架作为**后续可选替换**：待 WBS-0.2 锁定后以新 ADR 评估，届时本 ADR 标记 `superseded by`。

## 备选方案

| 方案 | 优点 | 缺点 | 未选原因 |
|---|---|---|---|
| FastAPI + uvicorn | 生态成熟、自动生成 OpenAPI | 在依赖锁定前写死版本；与 WBS-0.2 纪律冲突 | 违反「先锁矩阵再装依赖」 |
| Flask / Starlette 等 | 同上 | 同上 | 同上 |
| gRPC | 强契约、流式友好 | 引入 protobuf 工具链与代码生成，依赖更重 | 首版只需最简 HTTP 契约 |
| 先不做 HTTP，只留 SDK | 工作量最小 | 违反 WBS-1.6 与「独立进程服务」的交付形态 | 验收项不允许裁剪 |

## 影响

- 受影响模块：`src/liteinfer/api/`、`src/liteinfer/cli/`（CLI 走 HTTP 调同一套领域服务）。
- 需要的适配：框架替换时必须保证错误码映射与路由语义不变（集成测试 `tests/integration/test_http_api.py` 是护栏）。
- 验收方式：`pytest -m integration`（HTTP 契约测试真实起服务，不用 mock）；
  `liteinfer serve` + `liteinfer infer --kind echo` 手工冒烟。

## 已知限制

1. 标准库服务**不提供**生产级能力：无 TLS、无连接池治理、无 HTTP/2、无自动 OpenAPI 文档。
   它只覆盖「独立进程 + 任务提交查询取消 + 健康检查」的验收口径。
2. 并发模型为每请求一线程（`ThreadingHTTPServer`）；高并发压测结论不能外推到生产。
3. 未做鉴权与租户隔离：当前 `--tenant` 只用于排队配额统计，不是安全边界。
4. 若 WBS-0.2 锁定后出现「必须用 ASGI 才能复用的组件」（例如需要挂载中间件做指标导出），本决策需重审。
