"""
Логика авторизации:
- Хеширование и проверка паролей (для входа по email)
- Выдача и проверка JWT-токенов (чем сайт "узнаёт" залогиненного пользователя)
"""

import os
import time

import bcrypt
import jwt
from jwt import InvalidTokenError

SECRET_KEY = os.getenv("AUTH_SECRET_KEY")
if not SECRET_KEY or len(SECRET_KEY) < 32:
    raise ValueError(
        "Не найден AUTH_SECRET_KEY — придумай длинную случайную строку и добавь "
        "её в переменные окружения (это ключ для подписи токенов входа); минимум 32 символа"
    )

ALGORITHM = "HS256"
TOKEN_LIFETIME_SECONDS = 30 * 24 * 60 * 60  # 30 дней


# ==================== Пароли ====================
# Используем bcrypt напрямую (без passlib) — passlib давно не обновлялся
# и несовместим с новыми версиями bcrypt.

def hash_password(password: str) -> str:
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


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
    except (InvalidTokenError, KeyError, ValueError):
        return None
