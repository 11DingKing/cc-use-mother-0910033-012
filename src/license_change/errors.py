"""领域错误。"""
from __future__ import annotations

import enum


class ErrorCode(enum.Enum):
    """错误码，HTTP 层据此映射状态码。"""

    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    VALIDATION = "VALIDATION"
    BLOCKING_ITEMS = "BLOCKING_ITEMS"
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"


HTTP_STATUS = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.VALIDATION: 400,
    ErrorCode.BLOCKING_ITEMS: 409,
    ErrorCode.ILLEGAL_TRANSITION: 409,
}


class DomainError(Exception):
    """携带错误码与可读依据的领域异常。"""

    def __init__(self, code: ErrorCode, message: str, *, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reasons = reasons or []

    def to_dict(self) -> dict:
        return {
            "error": self.code.value,
            "message": self.message,
            "reasons": self.reasons,
        }
