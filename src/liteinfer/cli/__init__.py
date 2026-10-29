"""命令行入口（WBS-1.5）：doctor / model list / infer / task status / serve。

CLI 与 HTTP 服务调用同一套核心领域服务；`doctor` 不产出 GPU 验收结论。
"""

from .main import build_parser, main

__all__ = ["build_parser", "main"]
