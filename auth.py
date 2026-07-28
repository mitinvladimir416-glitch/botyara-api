"""
Логика авторизации:
- Хеширование и проверка паролей (для входа по email)
- Выдача и проверка JWT-токенов (чем сайт "узнаёт" залогиненного пользователя)
- Проверка подлинности данных от Telegram Login Widget
"""

import hashlib
import hmac
import os
import time

from jose import jwt, JWTError
from passlib.context import CryptContext

SECRET_KEY = os.getenv("AUTH_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError(
        "Не найден AUTH_SECRET_KEY — придумай длинную случайную строку и добавь "
        "её в переменные окружения (это ключ для подписи токенов входа)"
    )

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")  # тот же токен, что использует сам бот

ALGORITHM = "HS256"
TOKEN_LIFETIME_SECONDS = 30 * 24 * 60 * 60  # 30 дней

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ==================== Пароли ====================

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


# ==================== JWT-токены ====================

def create_access_token(user_id: int) -> str:
    """Создаёт токен, который сайт будет присылать в заголовке Authorization при каждом запросе."""
    payload = {
        "sub": str(user_id),
        "exp": int(time.time()) + TOKEN_LIFETIME_SECONDS,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> int | None:
    """Проверяет токен и возвращает user_id, если он валиден, иначе None."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        return None


# ==================== Проверка входа через Telegram ====================

def verify_telegram_login(data: dict) -> bool:
    """
    Проверяет, что данные действительно пришли от Telegram (а не подделаны кем-то).
    Алгоритм — официальный, из документации Telegram Login Widget:
    https://core.telegram.org/widgets/login#checking-authorization
    """
    if not TELEGRAM_BOT_TOKEN:
        return False

    received_hash = data.get("hash")
    if not received_hash:
        return False

    check_fields = {k: v for k, v in data.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(check_fields.items()))

    secret_key = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if computed_hash != received_hash:
        return False

    # Данные от Telegram действительны только ограниченное время (защита от повторного использования)
    auth_date = int(data.get("auth_date", 0))
    if time.time() - auth_date > 86400:  # старше суток — считаем протухшим
        return False

    return True
