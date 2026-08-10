"""Ошибки работы с панелью.

Деление на transient/permanent определяет, ретраить задачу или сдаваться.
"""

from __future__ import annotations


class PanelError(Exception):
    pass


class PanelUnavailable(PanelError):
    """Сеть, таймаут, 5xx, пустой ответ. Повторим позже."""


class PanelAuthError(PanelUnavailable):
    """Сессия протухла. Особый случай: сначала re-login, потом повтор."""


class PanelRejected(PanelError):
    """Панель ответила success=false с внятной причиной. Ретрай не поможет."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    @property
    def is_duplicate(self) -> bool:
        """Клиент с таким email/uuid уже есть — значит, надо не add, а update."""
        low = self.message.lower()
        return "duplicate" in low or "exist" in low or "уже" in low
