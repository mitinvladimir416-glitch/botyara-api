import logging
import os

import redis.asyncio as redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL")

valkey_client: redis.Redis | None = None


async def connect_valkey() -> bool:
    global valkey_client

    if not REDIS_URL:
        logger.warning("REDIS_URL не задан. Valkey отключён.")
        return False

    try:
        valkey_client = redis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            health_check_interval=30,
        )

        await valkey_client.ping()
        logger.info("Valkey подключён успешно: PONG")
        return True

    except Exception:
        logger.exception("Ошибка подключения к Valkey")
        valkey_client = None
        return False


async def close_valkey() -> None:
    global valkey_client

    if valkey_client is not None:
        await valkey_client.aclose()
        valkey_client = None
        logger.info("Соединение с Valkey закрыто")


async def check_valkey() -> bool:
    if valkey_client is None:
        return False

    try:
        return bool(await valkey_client.ping())
    except Exception:
        logger.exception("Ошибка проверки Valkey")
        return False
