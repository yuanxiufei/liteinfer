"""独立 HTTP 服务（WBS-1.6）。

传输层选择：在 WBS-0.2 锁定依赖之前，运行时依赖必须保持为空，
因此首版用标准库 `http.server` 实现，不引入 Web 框架（见 ADR-0002）。
路由只做协议转换，所有业务判断都在 `TaskService`。

背压语义（基线 §6.1）：参数非法 400 / 队列满 429 / 资源不足 503 /
未知资源 404 / 状态冲突 409。拒绝时**不会**把任务塞进后台执行。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from ..core import ErrorCode, LiteInferError, new_request_id
from ..observability import bind, get_logger, log_event
from ..runtime import TaskService
from .schemas import (
    cancel_response,
    error_response,
    health_response,
    parse_status,
    preempt_response,
    task_response,
)

CatalogFn = Callable[[], list[dict[str, Any]]]
logger = get_logger("api")


class LiteInferHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        service: TaskService,
        catalog: CatalogFn | None = None,
        version: str = "0.1.0",
    ) -> None:
        super().__init__(server_address, handler)
        self.service = service
        self.catalog = catalog
        self.version = version


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LiteInfer/0.1"

    @property
    def _srv(self) -> LiteInferHTTPServer:
        return cast(LiteInferHTTPServer, self.server)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 父类签名固定
        logger.debug("http.access %s", format % args)

    # ------------------------------------------------------------------ 入口

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        request_id = self.headers.get("X-Request-ID") or new_request_id()
        with bind(request_id=request_id):
            try:
                status, payload = self._route(method, self._read_body())
            except LiteInferError as exc:
                status, payload = exc.http_status, error_response(exc, request_id=request_id)
            except Exception as exc:  # noqa: BLE001 - 不允许把异常抛给客户端断连
                logger.exception("未处理异常")
                wrapped = LiteInferError(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}")
                status, payload = wrapped.http_status, error_response(
                    wrapped, request_id=request_id
                )
            self._send_json(status, payload, request_id)
            log_event(logger, "http.request", method=method, path=self.path, status=status)

    # ------------------------------------------------------------------ 路由

    def _route(self, method: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        service = self._srv.service

        if path == "/healthz":
            if method != "GET":
                return self._method_not_allowed(method, path)
            return 200, health_response(
                service="liteinfer", version=self._srv.version, queue_depth=service.queue_depth()
            )
        if path == "/v1/models":
            if method != "GET":
                return self._method_not_allowed(method, path)
            catalog = self._srv.catalog
            models = catalog() if catalog else [item.to_dict() for item in service.adapters()]
            return 200, {
                "models": models,
                "note": (
                    "列表为空表示尚未注册适配器；ready=false 表示已注册但依赖缺失或不兼容"
                    "（ADR-0003）。真实 LLM / SDXL 适配器待可选依赖锁定后接入。"
                ),
            }
        if path == "/v1/metrics":
            if method != "GET":
                return self._method_not_allowed(method, path)
            return 200, {"metrics": service.metrics(), "queue": service.queue_depth()}
        if path == "/v1/tasks":
            if method == "POST":
                record = service.submit_mapping(body)
                return 201, task_response(record)
            if method == "GET":
                status = parse_status(_first(query, "status"))
                records = service.list_tasks(
                    status=status,
                    tenant=_first(query, "tenant"),
                    kind=_first(query, "kind"),
                )
                return 200, {"tasks": [record.to_dict() for record in records]}
            return self._method_not_allowed(method, path)

        prefix = "/v1/tasks/"
        if path.startswith(prefix):
            rest = path[len(prefix) :]
            if "/" not in rest:
                if method == "GET":
                    return 200, task_response(service.get(rest))
                return self._method_not_allowed(method, path)
            task_id, _, action = rest.partition("/")
            if action == "cancel" and method == "POST":
                return 202, cancel_response(service.cancel(task_id))
            # WBS-3.9：抢占只在声明安全点的执行器上受理，否则由服务层报 CONFLICT
            if action == "preempt" and method == "POST":
                return 202, preempt_response(service.preempt(task_id))
            if action == "resume" and method == "POST":
                return 202, task_response(service.resume(task_id))
            return self._method_not_allowed(method, path)

        from ..core import not_found

        raise not_found("未知路由", path=path, method=method)

    def _method_not_allowed(self, method: str, path: str) -> tuple[int, dict[str, Any]]:
        from ..core import ErrorCode, LiteInferError

        raise LiteInferError(
            ErrorCode.UNSUPPORTED,
            f"该路径不支持 {method} 方法",
            details={"path": path, "method": method},
            http_status=405,
        )

    # ------------------------------------------------------------------ 传输

    def _read_body(self) -> dict[str, Any]:
        from ..core import invalid_argument

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise invalid_argument(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(decoded, dict):
            raise invalid_argument("请求体必须是 JSON 对象")
        return decoded

    def _send_json(self, status: int, payload: dict[str, Any], request_id: str) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", request_id)
        self.end_headers()
        self.wfile.write(body)


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


class HttpService:
    """可嵌入的服务包装：便于测试用 port=0 起服务并拿到真实 URL。"""

    def __init__(
        self,
        service: TaskService,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        catalog: CatalogFn | None = None,
        version: str = "0.1.0",
    ) -> None:
        self._server = LiteInferHTTPServer(
            (host, port), _Handler, service=service, catalog=catalog, version=version
        )
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{str(host)}:{port}"

    def start(self) -> str:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever, name="liteinfer-http", daemon=True
            )
            self._thread.start()
        return self.url

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def serve_forever(self) -> None:
        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            self.stop()


def run_server(
    service: TaskService,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    catalog: CatalogFn | None = None,
    version: str = "0.1.0",
) -> HttpService:
    """启动 HTTP 服务（阻塞）。供 `liteinfer serve` 使用。"""
    http = HttpService(service, host=host, port=port, catalog=catalog, version=version)
    http.serve_forever()
    return http
