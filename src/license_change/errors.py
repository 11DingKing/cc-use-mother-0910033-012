"""领域异常。"""
from __future__ import annotations


class DomainError(Exception):
    """规则冲突（非法状态迁移、存在未处置阻塞项等）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NotFoundError(DomainError):
    """聚合不存在。"""

    def __init__(self, message: str):
        super().__init__("not_found", message)


class ConflictError(DomainError):
    """并发/版本冲突或重复提交。"""
    def __init__(self, message: str):
        super().__init__("conflict", message)
