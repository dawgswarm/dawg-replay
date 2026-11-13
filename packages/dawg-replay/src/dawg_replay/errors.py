"""Ошибки ядра. Текст готов для показа человеку, code стабилен для машин."""

from __future__ import annotations


class ReplayError(Exception):
    def __init__(self, code: str, message: str, details: object = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.details is not None:
            d["details"] = self.details
        return d
