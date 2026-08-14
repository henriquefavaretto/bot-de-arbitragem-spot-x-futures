"""
Handler de logging em memória (ring buffer) para expor os logs internos do
bot (bot_engine, mexc_futures_ws_private, etc.) via API, sem precisar
instrumentar cada ponto de decisão manualmente - captura tudo que já é
logado via `logger.info/warning/error/critical` nesses módulos.
"""
import logging
from collections import deque
from typing import Optional

MAX_LOG_LINES = 2000

# Módulos cujos logs interessam na aba "Logs do bot" (evita poluir com logs
# do FastAPI/uvicorn/httpx, que não são relevantes para o usuário final).
BOT_LOGGER_NAMES = (
    "bot_engine",
    "mexc_futures_ws_private",
    "mexc_spot_client",
    "mexc_futures_client",
)


class InMemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = MAX_LOG_LINES):
        super().__init__()
        self.buffer: deque = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord):
        try:
            self.buffer.append({
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            })
        except Exception:
            pass  # nunca deixar o logging quebrar o app

    def get_logs(self, level: Optional[str] = None, limit: int = 500) -> list:
        items = list(self.buffer)
        if level:
            items = [i for i in items if i["level"] == level.upper()]
        items = items[-limit:]
        items.reverse()  # mais recente primeiro
        return items

    def clear(self) -> int:
        count = len(self.buffer)
        self.buffer.clear()
        return count


_handler_instance: Optional[InMemoryLogHandler] = None


def install_in_memory_log_handler() -> InMemoryLogHandler:
    """Instala o handler nos loggers relevantes do bot. Chamar uma vez na inicialização."""
    global _handler_instance
    if _handler_instance is not None:
        return _handler_instance

    handler = InMemoryLogHandler()
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)

    for name in BOT_LOGGER_NAMES:
        logging.getLogger(name).addHandler(handler)

    _handler_instance = handler
    return handler
