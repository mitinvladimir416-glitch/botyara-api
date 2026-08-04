"""
API-сервер для сайта botyara.ru.
Переиспользует ту же AI-логику, что и Telegram-бот (см. ai_service.py).

Запуск локально:
    pip install -r requirements.txt
    uvicorn main:app --reload

На Timeweb Cloud App Platform команда запуска обычно определяется автоматически
из requirements.txt + Procfile/настроек — см. README.md.
"""

import asyncio
import html as html_module
import hmac
import base64
import json
import logging
import math
import os
import secrets
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Header, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func as sa_func

import ai_service
import auth
from database import (
    get_db,
    init_db,
    SessionLocal,
    User,
    Favorite,
    Message,
    GalleryPost,
    GalleryComment,
    PublicChatMessage,
    Announcement,
    GalleryLike,
    Notification,
    UserAchievement,
    Room,
    RoomParticipant,
    RoomMessage,
    PublicChatReaction,
    ShopItem,
    UserInventoryItem,
    Subscription,
    ShopPurchase,
)
from valkey_client import connect_valkey, close_valkey, check_valkey
import valkey_client as vk

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Botyara API", version="0.2.0")

# Секрет для проверки, что запросы на /api/bot/* приходят именно от нашего Telegram-бота,
# а не от кого попало. Должен совпадать со значением BOT_INTERNAL_SECRET в Railway (у бота).
BOT_INTERNAL_SECRET = os.getenv("BOT_INTERNAL_SECRET")
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(20 * 1024 * 1024)))
SHOP_PURCHASES_ENABLED = os.getenv("SHOP_PURCHASES_ENABLED", "false").lower() == "true"

# Telegram ID администратора сайта — тот же человек, что ADMIN_ID у бота. Аккаунт с таким
# telegram_id получает права модератора (может чистить общий чат/галерею).
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")


def is_site_admin(user: User) -> bool:
    """Супер-админ — единственный, кто задан переменной окружения ADMIN_TELEGRAM_ID.
    Только он может назначать/снимать роли модератора и админа у других — это защищает
    от случайной "цепочки" самоназначений."""
    return bool(ADMIN_TELEGRAM_ID) and user.telegram_id == ADMIN_TELEGRAM_ID


def get_effective_role(user: User) -> str:
    """Супер-админ всегда 'admin', даже если в БД у него почему-то другая роль —
    так аккаунт с ADMIN_TELEGRAM_ID никогда не может остаться без доступа."""
    if is_site_admin(user):
        return "admin"
    return user.role or "user"


def is_moderator(user: User) -> bool:
    """Может модерировать контент (чистить чат/галерею) — модератор или админ."""
    return get_effective_role(user) in ("moderator", "admin")


def is_full_admin(user: User) -> bool:
    """Полный админ — доступ к управлению аккаунтами пользователей, ролями, украшениями."""
    return get_effective_role(user) == "admin"


def ensure_not_banned(user: User) -> None:
    if user.is_banned:
        raise HTTPException(status_code=403, detail="Твой аккаунт ограничен модератором")


# Простая защита от спама — в памяти, без БД. Не переживает перезапуск сервиса, и это ок:
# цель просто не дать засыпать чат/галерею сообщениями быстрее разумного.
_rate_limit_state: dict[str, list[float]] = {}


def check_rate_limit(key: str, max_calls: int, window_seconds: float) -> None:
    now = time.time()
    if len(_rate_limit_state) > 10_000:
        stale_before = now - 3600
        for stale_key in [k for k, values in _rate_limit_state.items() if not values or values[-1] < stale_before]:
            _rate_limit_state.pop(stale_key, None)
    calls = _rate_limit_state.setdefault(key, [])
    calls[:] = [t for t in calls if now - t < window_seconds]
    if len(calls) >= max_calls:
        raise HTTPException(status_code=429, detail="Слишком часто — подожди немного и попробуй снова")
    calls.append(now)


def client_ip(request: Request) -> str:
    """IP клиента — для лимитов на эндпоинтах без авторизации (вход/регистрация),
    где нет current_user.id, чтобы ограничивать по нему."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def author_badge(user: User) -> dict | None:
    """Кастомное 'украшение' — короткий титул с цветом, который админ выдал аккаунту вручную."""
    if not user.badge_text:
        return None
    return {"text": user.badge_text, "color": user.badge_color or "#a78bfa"}


# ==================== Геймификация: уровни, опыт, стрик, достижения ====================

LEVEL_TITLES = [
    (1, 4, "🌱 Новичок квартала"),
    (5, 9, "😎 Свой в доску"),
    (10, 19, "🔥 Мастер вайба"),
    (20, 34, "👑 Легенда района"),
    (35, 9999, "💎 Ботяра №1"),
]

ACHIEVEMENTS = {
    "first_post": {"label": "🎨 Первый пост", "desc": "Опубликовал первый промпт в галерее"},
    "hundred_prompts": {"label": "💯 Сотня", "desc": "Сохранил 100 промптов в избранное"},
    "soul_of_party": {"label": "🎉 Душа компании", "desc": "Оставил 10 комментариев в галерее"},
    "streak_7": {"label": "🔥 Неделя подряд", "desc": "7 дней подряд на сайте"},
    "streak_30": {"label": "🏆 Месяц подряд", "desc": "30 дней подряд на сайте"},
    "liked_10": {"label": "❤️ Народная любовь", "desc": "Твои посты в сумме набрали 10 реакций"},
    "reactions_50": {"label": "🌟 Звезда галереи", "desc": "Твои посты в сумме набрали 50 реакций"},
    "role_explorer": {"label": "🎭 Исследователь ролей", "desc": "Пообщался со всеми ролями в разделе «Общение»"},
    "room_organizer": {"label": "🤝 Организатор", "desc": "Создал 5 совместных комнат"},
    "night_owl": {"label": "🦉 Полуночник", "desc": "Написал сообщение глубокой ночью"},
    "active_reactor": {"label": "👀 Активный зритель", "desc": "Поставил 50 реакций на чужие посты"},
}


def calc_level(xp: int) -> int:
    """Уровень растёт по нарастающей — каждый следующий требует больше опыта."""
    return int(math.sqrt(max(xp, 0) / 20)) + 1


def xp_for_next_level(level: int) -> int:
    """Сколько всего XP нужно, чтобы достичь следующего уровня."""
    return 20 * level * level


def level_title(level: int) -> str:
    for lo, hi, title in LEVEL_TITLES:
        if lo <= level <= hi:
            return title
    return LEVEL_TITLES[-1][2]


def add_xp(db: Session, user: User, amount: int):
    user.xp = (user.xp or 0) + amount
    db.commit()


def notify(db: Session, user: User, content: str):
    db.add(Notification(user_id=user.id, content=content))
    db.commit()


def grant_achievement(db: Session, user: User, key: str):
    """Выдаёт достижение, если его ещё не было. Возвращает True, если выдано впервые."""
    exists = (
        db.query(UserAchievement)
        .filter(UserAchievement.user_id == user.id, UserAchievement.key == key)
        .first()
    )
    if exists:
        return False
    db.add(UserAchievement(user_id=user.id, key=key))
    db.commit()
    notify(db, user, f"🏅 Новое достижение: {ACHIEVEMENTS[key]['label']} — {ACHIEVEMENTS[key]['desc']}")
    return True


def update_streak(db: Session, user: User):
    """Обновляет серию дней подряд — вызывается при каждом /api/me (то есть при каждом заходе)."""
    today = date.today()
    if user.last_active_date == today:
        return
    if user.last_active_date == today - timedelta(days=1):
        user.current_streak = (user.current_streak or 0) + 1
    else:
        user.current_streak = 1
    user.last_active_date = today
    db.commit()

    if user.current_streak == 7:
        grant_achievement(db, user, "streak_7")
    elif user.current_streak == 30:
        grant_achievement(db, user, "streak_30")


@app.on_event("startup")
def on_startup():
    """Создаёт таблицы в базе данных при первом запуске (если их ещё нет)."""
    init_db()
    db = SessionLocal()
    try:
        seed_shop_items(db)
    finally:
        db.close()


@app.on_event("startup")
async def on_startup_valkey():
    """Отдельный обработчик — подключение к Valkey (Redis). Намеренно отделён от on_startup
    (там синхронная работа с БД), чтобы не трогать уже рабочую логику. Если Valkey недоступен —
    приложение всё равно запустится (см. connect_valkey — при ошибке просто оставляет клиента None
    и пишет warning в лог), сайт/бот/БД от этого не пострадают."""
    connected = await connect_valkey()
    if connected:
        # Запускаем подписку в фоне — не блокируем старт приложения ожиданием сообщений
        chat_manager._subscriber_task = asyncio.create_task(chat_manager.start_subscriber())


@app.on_event("shutdown")
async def on_shutdown_valkey():
    """Корректно закрывает соединение с Valkey и останавливает фоновую подписку при остановке сервиса."""
    if chat_manager._subscriber_task is not None:
        chat_manager._subscriber_task.cancel()
    await close_valkey()


cors_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "https://24promtbot.ru").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Bot-Secret"],
)


async def read_upload_limited(upload: UploadFile, max_bytes: int, kind: str) -> bytes:
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"{kind} is too large")
    return data


# ==================== Схемы запросов ====================

class ChatMessage(BaseModel):
    role: str  # "user" или "assistant"
    content: str


class ChatRequest(BaseModel):
    history: list[ChatMessage]  # вся история диалога, присылает сайт
    role: str | None = None  # выбранный "характер" общения (см. ai_service.ROLE_CONFIG)


class TranslateRequest(BaseModel):
    text: str
    target_lang: str | None = None  # например "en", "fr", "испанский". None = автоопределение


class PromptRequest(BaseModel):
    topic: str  # "suno" / "image" / "video"
    target: str | None = None  # выбранная версия/нейросеть, например "Suno 4.5"
    history: list[ChatMessage]


class ImprovePromptRequest(BaseModel):
    topic: str
    target: str | None = None
    draft: str


class CoverRequest(BaseModel):
    lyrics: str
    ratio: str  # один из: 1:1, 4:3, 16:9, 3:4, 9:16
    cover_text: str | None = None
    photo_base64: str | None = None  # опционально, если есть референс-фото


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class FavoriteCreateRequest(BaseModel):
    content: str
    category: str = "other"  # "suno" / "image" / "video" / "cover" / "other"


class BotAnnouncementRequest(BaseModel):
    content: str


class AdminAnnouncementRequest(BaseModel):
    content: str
    ai_polish: bool = True


class LinkEmailRequest(BaseModel):
    email: EmailStr
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class FavoriteUpdateRequest(BaseModel):
    content: str


class BotMessageRequest(BaseModel):
    # Поля, которые присылает бот про пользователя Telegram
    telegram_id: int
    telegram_username: str | None = None
    telegram_first_name: str | None = None
    role: str  # "user" или "assistant"
    content: str
    persona: str | None = None  # какая роль/вкладка общения — "default" или id роли


class BotFavoriteRequest(BaseModel):
    telegram_id: int
    telegram_username: str | None = None
    telegram_first_name: str | None = None
    content: str
    category: str = "other"


class BotFavoriteDeleteRequest(BaseModel):
    telegram_id: int
    favorite_id: int


class AdminAnnouncementRequest(BaseModel):
    content: str
    ai_polish: bool = True


class RoomCreateRequest(BaseModel):
    category: str = "other"  # suno/image/video/other


class RoomJoinRequest(BaseModel):
    code: str


class AdminUserUpdateRequest(BaseModel):
    display_name: str | None = None
    role: str | None = None  # "user" / "moderator" / "admin" — менять может только супер-админ
    is_banned: bool | None = None
    badge_text: str | None = None
    badge_color: str | None = None


class RoomMessageRequest(BaseModel):
    content: str
    channel: str = "ai"  # "ai" — чат с нейросетью, "team" — приватное обсуждение участников


class GalleryPublishRequest(BaseModel):
    favorite_id: int


class GalleryReactRequest(BaseModel):
    emoji: str


class GalleryCommentRequest(BaseModel):
    content: str


class BotGalleryPublishRequest(BaseModel):
    telegram_id: int
    telegram_username: str | None = None
    telegram_first_name: str | None = None
    content: str
    category: str = "other"


class BotGalleryCommentRequest(BaseModel):
    telegram_id: int
    telegram_username: str | None = None
    telegram_first_name: str | None = None
    post_id: int
    content: str


class PublicChatRequest(BaseModel):
    content: str
    reply_to_id: int | None = None


class PublicChatReactRequest(BaseModel):
    emoji: str


class BotPublicChatRequest(BaseModel):
    telegram_id: int
    telegram_username: str | None = None
    telegram_first_name: str | None = None
    content: str


# ==================== Авторизация: вспомогательное ====================

security = HTTPBearer(auto_error=False)
SESSION_COOKIE_NAME = "botyara_session"


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=auth.TOKEN_LIFETIME_SECONDS,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
    )


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """
    Зависимость FastAPI: проверяет токен из заголовка "Authorization: Bearer <токен>"
    и возвращает текущего пользователя. Если токена нет или он невалиден — ошибка 401.
    """
    token = credentials.credentials if credentials is not None else request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Нужна авторизация")

    user_id = auth.decode_access_token(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Недействительный или истёкший токен")

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="Пользователь не найден")

    now = datetime.now(timezone.utc)
    if not user.last_seen_at or (now - user.last_seen_at).total_seconds() > 60:
        user.last_seen_at = now
        db.commit()

    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Зависимость FastAPI: пускает только полного администратора."""
    if not is_full_admin(current_user):
        raise HTTPException(status_code=403, detail="Доступно только администратору")
    return current_user


def require_moderator(current_user: User = Depends(get_current_user)) -> User:
    """Зависимость FastAPI: пускает модераторов и администраторов."""
    if not is_moderator(current_user):
        raise HTTPException(status_code=403, detail="Доступно только модератору")
    return current_user


def verify_bot_secret(x_bot_secret: str | None = Header(default=None)):
    """
    Зависимость FastAPI: проверяет, что запрос на /api/bot/* пришёл от нашего бота
    (секрет передаётся в заголовке X-Bot-Secret и должен совпадать с BOT_INTERNAL_SECRET).
    """
    if not BOT_INTERNAL_SECRET:
        raise HTTPException(status_code=500, detail="BOT_INTERNAL_SECRET не настроен на сервере")
    if not x_bot_secret or not hmac.compare_digest(x_bot_secret, BOT_INTERNAL_SECRET):
        raise HTTPException(status_code=401, detail="Неверный секрет бота")


def get_or_create_bot_user(
    db: Session, telegram_id: int, telegram_username: str | None, telegram_first_name: str | None
) -> User:
    """
    Находит пользователя по telegram_id, а если такого ещё нет — создаёт нового.
    Используется эндпоинтами /api/bot/*, чтобы сообщения и избранное из бота
    попадали к тому же пользователю, что и на сайте.
    """
    telegram_id_str = str(telegram_id)
    user = db.query(User).filter(User.telegram_id == telegram_id_str).first()

    if user is None:
        user = User(
            telegram_id=telegram_id_str,
            telegram_username=telegram_username,
            telegram_first_name=telegram_first_name,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Обновляем актуальные данные профиля на случай, если человек их поменял в Telegram
        user.telegram_username = telegram_username
        user.telegram_first_name = telegram_first_name
        db.commit()

    return user


def author_display_name(user: User) -> str:
    """Имя автора для показа в галерее/чате — заданное имя, иначе Telegram-имя, иначе email, иначе 'Аноним'."""
    if user.display_name:
        return user.display_name
    if user.telegram_first_name:
        return user.telegram_first_name
    if user.email:
        return user.email.split("@")[0]
    return "Аноним"


# ==================== Эндпоинты ====================

@app.get("/api/health")
async def health():
    """Проверка, что сервис жив (используем для мониторинга). Valkey — необязательный,
    если он недоступен, это НЕ делает весь сервис нездоровым (status всё равно "ok"),
    просто отдельно видно его состояние."""
    valkey_ok = await check_valkey()
    return {"status": "ok", "valkey": "connected" if valkey_ok else "disconnected"}


@app.post("/api/chat")
async def chat(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Обычный чат с ботом. Требует авторизации, чтобы сохранять историю за конкретным пользователем."""
    persona = req.role or "default"
    history = [m.model_dump() for m in req.history]
    reply = ai_service.get_chat_reply(history, role=req.role)

    if history:
        last_user_message = history[-1]
        db.add(
            Message(
                user_id=current_user.id,
                role="user",
                content=last_user_message["content"],
                source="web",
                persona=persona,
            )
        )
    db.add(
        Message(user_id=current_user.id, role="assistant", content=reply, source="web", persona=persona)
    )
    db.commit()

    add_xp(db, current_user, 1)

    distinct_personas = (
        db.query(Message.persona)
        .filter(Message.user_id == current_user.id, Message.role == "user")
        .distinct()
        .count()
    )
    if distinct_personas >= len(ai_service.ROLE_CONFIG):
        grant_achievement(db, current_user, "role_explorer")

    current_hour = datetime.now(timezone.utc).hour
    if current_hour in (0, 1, 2, 3, 4):
        grant_achievement(db, current_user, "night_owl")

    return {"reply": reply}


@app.get("/api/chat/roles")
async def chat_roles():
    """Список доступных 'характеров' общения — для отрисовки вкладок на сайте."""
    return {
        role_id: {
            "label": cfg["label"],
            "emoji": cfg["emoji"],
            "description": cfg["description"],
        }
        for role_id, cfg in ai_service.ROLE_CONFIG.items()
    }


@app.get("/api/history")
async def get_history(
    persona: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Возвращает сохранённую историю переписки текущего пользователя.
    Если передан параметр persona — только сообщения этой вкладки/роли (плюс старые сообщения
    без persona, если запрошена вкладка "default" — они считаются обычным общением).
    Если persona не передан — возвращает всё вместе (для обратной совместимости).
    """
    query = db.query(Message).filter(Message.user_id == current_user.id)

    if persona:
        if persona == "default":
            query = query.filter((Message.persona == "default") | (Message.persona.is_(None)))
        else:
            query = query.filter(Message.persona == persona)

    messages = query.order_by(Message.created_at.asc()).all()
    return {"history": [{"role": m.role, "content": m.content} for m in messages]}


@app.delete("/api/history")
async def clear_history(
    persona: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Очищает историю переписки. Если передан persona — только эту вкладку/роль
    (для вкладки "default" это общая с ботом история — она тоже очистится).
    Если persona не передан — очищает вообще всё.
    """
    query = db.query(Message).filter(Message.user_id == current_user.id)

    if persona:
        if persona == "default":
            query = query.filter((Message.persona == "default") | (Message.persona.is_(None)))
        else:
            query = query.filter(Message.persona == persona)

    query.delete(synchronize_session=False)
    db.commit()
    return {"status": "cleared"}



@app.post("/api/translate")
async def translate(req: TranslateRequest):
    """Перевод текста."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Пустой текст для перевода")
    translation = ai_service.translate_text(req.text, req.target_lang)
    return {"translation": translation}


@app.get("/api/prompt/topics")
async def prompt_topics():
    """Список доступных тем промптов и их вариантов версий/нейросетей — для отрисовки кнопок на сайте."""
    return {
        topic: {"label": cfg["label"], "targets": cfg["targets"]}
        for topic, cfg in ai_service.PROMPT_CONFIG.items()
    }


@app.post("/api/prompt")
async def prompt(req: PromptRequest):
    """Диалог по составлению промпта (Suno/Картинка/Видео)."""
    if req.topic not in ai_service.PROMPT_CONFIG:
        raise HTTPException(status_code=400, detail="Неизвестная тема промпта")
    history = [m.model_dump() for m in req.history]
    reply = ai_service.get_prompt_reply(req.topic, req.target, history)
    is_final = "ГОТОВЫЙ ПРОМПТ:" in reply
    return {"reply": reply, "is_final": is_final}


@app.post("/api/prompt/improve")
async def improve_prompt_endpoint(req: ImprovePromptRequest):
    """«Прокачай мой промпт» — доводит уже написанный пользователем черновик до ума."""
    if not req.draft.strip():
        raise HTTPException(status_code=400, detail="Пришли черновик текста")
    reply = ai_service.improve_prompt(req.topic, req.target, req.draft)
    is_final = "ГОТОВЫЙ ПРОМПТ:" in reply
    return {"reply": reply, "is_final": is_final}


@app.post("/api/prompt/image-from-photo")
async def prompt_image_from_photo(
    desired_change: str = Form(...),
    photo: UploadFile = File(...),
):
    """Анализ фото + составление промпта для его правки (раздел Промпты → Картинка)."""
    photo_bytes = await read_upload_limited(photo, MAX_IMAGE_BYTES, "Image")
    photo_base64 = base64.b64encode(photo_bytes).decode("utf-8")
    reply = ai_service.get_image_prompt_from_photo(photo_base64, desired_change, history=[])
    is_final = "ГОТОВЫЙ ПРОМПТ:" in reply
    return {"reply": reply, "is_final": is_final}


@app.post("/api/prompt/video-frames")
async def prompt_video_frames(
    target: str = Form(""),
    description: str = Form(""),
    first_frame: UploadFile | None = File(None),
    last_frame: UploadFile | None = File(None),
):
    """Составление видео-промпта по первому/последнему кадру (раздел Промпты → Видео)."""
    first_b64 = None
    last_b64 = None
    if first_frame is not None:
        first_b64 = base64.b64encode(await read_upload_limited(first_frame, MAX_IMAGE_BYTES, "Image")).decode("utf-8")
    if last_frame is not None:
        last_b64 = base64.b64encode(await read_upload_limited(last_frame, MAX_IMAGE_BYTES, "Image")).decode("utf-8")

    reply = ai_service.get_video_prompt_from_frames(
        target or None, description, first_b64, last_b64, history=[]
    )
    is_final = "ГОТОВЫЙ ПРОМПТ:" in reply
    return {"reply": reply, "is_final": is_final}


@app.post("/api/voice-transcribe")
async def voice_transcribe(
    audio: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Распознаёт голосовое сообщение с сайта в текст (раздел Общение)."""
    suffix = os.path.splitext(audio.filename or "audio.webm")[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await read_upload_limited(audio, MAX_AUDIO_BYTES, "Audio"))
        tmp_path = tmp.name

    try:
        text = ai_service.transcribe_audio(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not text:
        raise HTTPException(status_code=422, detail="Не удалось распознать голосовое сообщение")

    return {"text": text}


@app.get("/api/cover/formats")
async def cover_formats():
    """Список доступных форматов обложки — для кнопок на сайте."""
    return ai_service.COVER_FORMATS


@app.post("/api/cover")
async def cover(req: CoverRequest):
    """Генерация промпта для обложки трека."""
    if req.ratio not in ai_service.COVER_FORMATS:
        raise HTTPException(status_code=400, detail="Неизвестный формат обложки")
    if not req.lyrics.strip():
        raise HTTPException(status_code=400, detail="Не указан текст песни")

    reply = ai_service.generate_cover_prompt(
        lyrics=req.lyrics,
        photo_base64=req.photo_base64,
        ratio=req.ratio,
        cover_text=req.cover_text,
    )
    return {"reply": reply}


# ==================== Авторизация ====================

@app.post("/api/auth/register")
async def register(req: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """Регистрация по email и паролю."""
    check_rate_limit(f"register:{client_ip(request)}", max_calls=5, window_seconds=300)
    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Пользователь с таким email уже зарегистрирован")

    user = User(email=req.email, password_hash=auth.hash_password(req.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    token = auth.create_access_token(user.id)
    set_session_cookie(response, token)
    return {"access_token": token, "user": {"id": user.id, "email": user.email}}


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """Вход по email и паролю."""
    check_rate_limit(f"login:{client_ip(request)}", max_calls=10, window_seconds=300)
    user = db.query(User).filter(User.email == req.email).first()
    if not user or not user.password_hash or not auth.verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    token = auth.create_access_token(user.id)
    set_session_cookie(response, token)
    return {"access_token": token, "user": {"id": user.id, "email": user.email}}


@app.post("/api/auth/session")
async def restore_auth_session(response: Response, current_user: User = Depends(get_current_user)):
    """Восстанавливает JS-сессию из защищённой cookie после повторного открытия сайта."""
    token = auth.create_access_token(current_user.id)
    set_session_cookie(response, token)
    return {"access_token": token}


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
    )
    return {"ok": True}


# ==================== Вход через бота (альтернатива виджету, без привязки к домену) ====================
# Сайт создаёт одноразовый токен и показывает ссылку на бота с этим токеном в /start.
# Бот, получив /start web_auth_<токен>, подтверждает его на бэкенде — сайт узнаёт об этом через опрос.

BOT_USERNAME = "halpervovan_bot"
TELEGRAM_LOGIN_TTL_SECONDS = 300  # 5 минут на подтверждение

# token -> {"status": "pending"/"confirmed", "created_at": float, "telegram_id"/"telegram_username"/"telegram_first_name"}
pending_telegram_logins: dict[str, dict] = {}


class BotTelegramAuthConfirmRequest(BaseModel):
    token: str
    telegram_id: int
    telegram_username: str | None = None
    telegram_first_name: str | None = None


@app.post("/api/auth/telegram/start")
async def telegram_login_start(request: Request):
    """Генерирует одноразовый токен для входа через бота — сайт покажет ссылку и начнёт опрос."""
    check_rate_limit(f"tg_login_start:{client_ip(request)}", max_calls=10, window_seconds=300)
    token = secrets.token_urlsafe(24)
    pending_telegram_logins[token] = {"status": "pending", "created_at": time.time()}
    return {"token": token, "bot_username": BOT_USERNAME}


@app.get("/api/auth/telegram/poll")
async def telegram_login_poll(token: str, request: Request, response: Response, db: Session = Depends(get_db)):
    """Сайт опрашивает это раз в пару секунд, пока пользователь не подтвердит вход через бота."""
    check_rate_limit(f"tg_login_poll:{client_ip(request)}", max_calls=200, window_seconds=300)
    entry = pending_telegram_logins.get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="Токен не найден или уже использован")

    if time.time() - entry["created_at"] > TELEGRAM_LOGIN_TTL_SECONDS:
        pending_telegram_logins.pop(token, None)
        raise HTTPException(status_code=410, detail="Время авторизации истекло — попробуй ещё раз")

    if entry["status"] != "confirmed":
        return {"status": "pending"}

    pending_telegram_logins.pop(token, None)

    telegram_id = str(entry["telegram_id"])
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if user is None:
        user = User(
            telegram_id=telegram_id,
            telegram_username=entry.get("telegram_username"),
            telegram_first_name=entry.get("telegram_first_name"),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.telegram_username = entry.get("telegram_username")
        user.telegram_first_name = entry.get("telegram_first_name")
        db.commit()

    access_token = auth.create_access_token(user.id)
    set_session_cookie(response, access_token)
    return {
        "status": "confirmed",
        "access_token": access_token,
        "user": {
            "id": user.id,
            "telegram_username": user.telegram_username,
            "telegram_first_name": user.telegram_first_name,
        },
    }


@app.post("/api/bot/telegram-auth-confirm", dependencies=[Depends(verify_bot_secret)])
async def bot_telegram_auth_confirm(req: BotTelegramAuthConfirmRequest):
    """Бот вызывает это, когда пользователь прислал ему /start web_auth_<токен>."""
    entry = pending_telegram_logins.get(req.token)
    if not entry:
        return {"status": "not_found"}

    entry["status"] = "confirmed"
    entry["telegram_id"] = req.telegram_id
    entry["telegram_username"] = req.telegram_username
    entry["telegram_first_name"] = req.telegram_first_name
    return {"status": "ok"}


@app.get("/api/me")
async def get_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Возвращает данные текущего залогиненного пользователя (проверка, что токен рабочий)."""
    update_streak(db, current_user)
    level = calc_level(current_user.xp or 0)
    return {
        "id": current_user.id,
        "email": current_user.email,
        "telegram_username": current_user.telegram_username,
        "telegram_first_name": current_user.telegram_first_name,
        "display_name": current_user.display_name,
        "avatar_base64": current_user.avatar_base64,
        "is_admin": is_full_admin(current_user),
        "is_moderator": is_moderator(current_user),
        "badge": author_badge(current_user),
        "avatar_frame": current_user.active_frame,
        "name_color": current_user.active_name_color,
        "is_premium": is_premium_active(db, current_user),
        "xp": current_user.xp or 0,
        "level": level,
        "level_title": level_title(level),
        "xp_for_next_level": xp_for_next_level(level),
        "current_streak": current_user.current_streak or 0,
    }


@app.post("/api/me/profile")
async def update_profile(
    display_name: str = Form(""),
    avatar: UploadFile | None = File(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Сохраняет имя и/или фото профиля. Оба поля необязательны — можно менять по отдельности."""
    if display_name.strip():
        current_user.display_name = display_name.strip()[:50]

    if avatar is not None:
        avatar_bytes = await avatar.read()
        if len(avatar_bytes) > 1_500_000:
            raise HTTPException(status_code=400, detail="Фото слишком большое (максимум ~1.5 МБ)")
        b64 = base64.b64encode(avatar_bytes).decode("utf-8")
        content_type = avatar.content_type or "image/jpeg"
        current_user.avatar_base64 = f"data:{content_type};base64,{b64}"

    db.commit()
    db.refresh(current_user)
    return {"display_name": current_user.display_name, "avatar_base64": current_user.avatar_base64}


@app.post("/api/me/link-email")
async def link_email(
    req: LinkEmailRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Добавляет email и пароль к уже залогиненному аккаунту (например, вошедшему через Telegram) —
    чтобы в дальнейшем можно было зайти в тот же аккаунт по email, если Telegram недоступен.
    """
    if current_user.email:
        raise HTTPException(status_code=400, detail="К этому аккаунту уже привязан email")

    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Этот email уже используется другим аккаунтом")

    current_user.email = req.email
    current_user.password_hash = auth.hash_password(req.password)
    db.commit()

    return {"status": "ok", "email": current_user.email}


@app.post("/api/me/change-password")
async def change_password(
    req: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Меняет пароль уже привязанного email. Требует ввести текущий пароль для подтверждения."""
    check_rate_limit(f"change_password:{current_user.id}", max_calls=10, window_seconds=300)
    if not current_user.password_hash:
        raise HTTPException(
            status_code=400, detail="У аккаунта ещё нет пароля — сначала привяжи email в разделе Аккаунт"
        )
    if not auth.verify_password(req.current_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный текущий пароль")

    current_user.password_hash = auth.hash_password(req.new_password)
    db.commit()

    return {"status": "ok"}


# ==================== Избранное (требует авторизации сайта) ====================

@app.get("/api/favorites")
async def list_favorites(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Список сохранённых промптов текущего пользователя."""
    favorites = (
        db.query(Favorite)
        .filter(Favorite.user_id == current_user.id)
        .order_by(Favorite.created_at.desc())
        .all()
    )
    return [
        {"id": f.id, "content": f.content, "category": f.category, "created_at": f.created_at}
        for f in favorites
    ]


@app.post("/api/favorites")
async def add_favorite(
    req: FavoriteCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Сохраняет промпт в избранное текущего пользователя."""
    favorite = Favorite(user_id=current_user.id, content=req.content, category=req.category or "other")
    db.add(favorite)
    db.commit()
    db.refresh(favorite)

    add_xp(db, current_user, 5)
    total_favorites = db.query(Favorite).filter(Favorite.user_id == current_user.id).count()
    if total_favorites >= 100:
        grant_achievement(db, current_user, "hundred_prompts")

    return {"id": favorite.id, "content": favorite.content, "category": favorite.category, "created_at": favorite.created_at}


@app.patch("/api/favorites/{favorite_id}")
async def update_favorite(
    favorite_id: int,
    req: FavoriteUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Редактирует текст сохранённого промпта (только свой)."""
    favorite = (
        db.query(Favorite)
        .filter(Favorite.id == favorite_id, Favorite.user_id == current_user.id)
        .first()
    )
    if not favorite:
        raise HTTPException(status_code=404, detail="Не найдено")

    favorite.content = req.content
    db.commit()
    db.refresh(favorite)
    return {"id": favorite.id, "content": favorite.content, "created_at": favorite.created_at}


@app.delete("/api/favorites/{favorite_id}")
async def delete_favorite(
    favorite_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удаляет один сохранённый промпт (только свой)."""
    favorite = (
        db.query(Favorite)
        .filter(Favorite.id == favorite_id, Favorite.user_id == current_user.id)
        .first()
    )
    if not favorite:
        raise HTTPException(status_code=404, detail="Не найдено")

    db.delete(favorite)
    db.commit()
    return {"status": "deleted"}


@app.delete("/api/favorites")
async def clear_favorites(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Очищает всё избранное текущего пользователя."""
    db.query(Favorite).filter(Favorite.user_id == current_user.id).delete()
    db.commit()
    return {"status": "cleared"}


# ==================== Галерея промптов (требует авторизации сайта) ====================

@app.post("/api/gallery/publish")
async def gallery_publish(
    req: GalleryPublishRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Публикует промпт из Избранного в общую галерею — проходит модерацию перед публикацией."""
    ensure_not_banned(current_user)
    favorite = (
        db.query(Favorite)
        .filter(Favorite.id == req.favorite_id, Favorite.user_id == current_user.id)
        .first()
    )
    if not favorite:
        raise HTTPException(status_code=404, detail="Такого промпта нет в твоём избранном")

    allowed, reason = ai_service.moderate_text(favorite.content)
    post = GalleryPost(
        user_id=current_user.id,
        content=favorite.content,
        category=favorite.category,
        status="approved" if allowed else "rejected",
        reject_reason=None if allowed else reason,
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    if allowed:
        add_xp(db, current_user, 15)
        total_posts = db.query(GalleryPost).filter(GalleryPost.user_id == current_user.id).count()
        if total_posts == 1:
            grant_achievement(db, current_user, "first_post")

    return {"id": post.id, "status": post.status, "reject_reason": post.reject_reason}


@app.get("/api/gallery")
async def gallery_list(
    q: str = "",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Список опубликованных (прошедших модерацию) промптов, новые сверху. q — поиск по тексту."""
    query = db.query(GalleryPost).filter(GalleryPost.status == "approved")
    if q.strip():
        query = query.filter(GalleryPost.content.ilike(f"%{q.strip()}%"))
    posts = (
        query.options(joinedload(GalleryPost.user))
        .order_by(GalleryPost.created_at.desc())
        .limit(50)
        .all()
    )
    post_ids = [p.id for p in posts]

    comment_counts = {}
    reactions_by_post = {}
    my_reaction_by_post = {}
    if post_ids:
        comment_counts = dict(
            db.query(GalleryComment.post_id, sa_func.count(GalleryComment.id))
            .filter(GalleryComment.post_id.in_(post_ids), GalleryComment.status == "approved")
            .group_by(GalleryComment.post_id)
            .all()
        )
        for r in db.query(GalleryLike).filter(GalleryLike.post_id.in_(post_ids)).all():
            bucket = reactions_by_post.setdefault(r.post_id, {})
            bucket[r.emoji] = bucket.get(r.emoji, 0) + 1
            if r.user_id == current_user.id:
                my_reaction_by_post[r.post_id] = r.emoji

    result = []
    for p in posts:
        result.append(
            {
                "id": p.id,
                "content": p.content,
                "category": p.category,
                "author": author_display_name(p.user),
                "author_id": p.user.id,
                "author_avatar": p.user.avatar_base64,
                "author_level": calc_level(p.user.xp or 0),
                "author_badge": author_badge(p.user),
                "created_at": p.created_at,
                "comment_count": comment_counts.get(p.id, 0),
                "reactions": reactions_by_post.get(p.id, {}),
                "my_reaction": my_reaction_by_post.get(p.id),
                "is_mine": p.user_id == current_user.id,
            }
        )
    return result


@app.get("/api/gallery/{post_id}")
async def gallery_detail(
    post_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Один пост галереи целиком, вместе со всеми одобренными комментариями."""
    post = db.query(GalleryPost).filter(GalleryPost.id == post_id, GalleryPost.status == "approved").first()
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")

    comments = (
        db.query(GalleryComment)
        .options(joinedload(GalleryComment.user))
        .filter(GalleryComment.post_id == post_id, GalleryComment.status == "approved")
        .order_by(GalleryComment.created_at.asc())
        .all()
    )
    reaction_rows = db.query(GalleryLike).filter(GalleryLike.post_id == post_id).all()
    reactions = {}
    my_reaction = None
    for r in reaction_rows:
        reactions[r.emoji] = reactions.get(r.emoji, 0) + 1
        if r.user_id == current_user.id:
            my_reaction = r.emoji
    return {
        "id": post.id,
        "content": post.content,
        "category": post.category,
        "author": author_display_name(post.user),
        "author_id": post.user.id,
        "author_avatar": post.user.avatar_base64,
        "author_level": calc_level(post.user.xp or 0),
        "author_badge": author_badge(post.user),
        "created_at": post.created_at,
        "is_mine": post.user_id == current_user.id,
        "reactions": reactions,
        "my_reaction": my_reaction,
        "comments": [
            {
                "id": c.id,
                "content": c.content,
                "author": author_display_name(c.user),
                "author_id": c.user.id,
                "author_avatar": c.user.avatar_base64,
                "author_level": calc_level(c.user.xp or 0),
                "author_badge": author_badge(c.user),
                "created_at": c.created_at,
            }
            for c in comments
        ],
    }


@app.post("/api/gallery/{post_id}/comments")
async def gallery_add_comment(
    post_id: int,
    req: GalleryCommentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Добавляет комментарий к посту — тоже проходит модерацию перед публикацией."""
    ensure_not_banned(current_user)
    check_rate_limit(f"gallery_comment:{current_user.id}", max_calls=15, window_seconds=60)
    post = db.query(GalleryPost).filter(GalleryPost.id == post_id, GalleryPost.status == "approved").first()
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")

    allowed, reason = ai_service.moderate_text(req.content)
    comment = GalleryComment(
        post_id=post_id,
        user_id=current_user.id,
        content=req.content,
        status="approved" if allowed else "rejected",
        reject_reason=None if allowed else reason,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)

    if allowed:
        add_xp(db, current_user, 3)
        total_comments = (
            db.query(GalleryComment)
            .filter(GalleryComment.user_id == current_user.id, GalleryComment.status == "approved")
            .count()
        )
        if total_comments >= 10:
            grant_achievement(db, current_user, "soul_of_party")

        if post.user_id != current_user.id:
            owner = db.query(User).filter(User.id == post.user_id).first()
            if owner:
                add_xp(db, owner, 5)
                notify(db, owner, f"💬 {author_display_name(current_user)} прокомментировал(а) твой промпт в галерее")

    return {"id": comment.id, "status": comment.status, "reject_reason": comment.reject_reason}


@app.delete("/api/gallery/{post_id}")
async def gallery_delete(
    post_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удаляет пост из галереи — свой, либо любой, если ты администратор."""
    query = db.query(GalleryPost).filter(GalleryPost.id == post_id)
    if not is_moderator(current_user):
        query = query.filter(GalleryPost.user_id == current_user.id)
    post = query.first()
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")
    db.delete(post)
    db.commit()
    return {"status": "deleted"}


@app.delete("/api/gallery/{post_id}/comments/{comment_id}")
async def gallery_delete_comment(
    post_id: int,
    comment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удаляет комментарий — свой, либо любой, если ты администратор."""
    query = db.query(GalleryComment).filter(GalleryComment.id == comment_id, GalleryComment.post_id == post_id)
    if not is_moderator(current_user):
        query = query.filter(GalleryComment.user_id == current_user.id)
    comment = query.first()
    if not comment:
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    db.delete(comment)
    db.commit()
    return {"status": "deleted"}


REACTION_EMOJIS = ["❤️", "🔥", "😂", "👀", "💯"]


@app.post("/api/gallery/{post_id}/react")
async def gallery_react(
    post_id: int,
    req: GalleryReactRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ставит реакцию (один из набора эмодзи). Повторный клик тем же эмодзи убирает реакцию,
    клик другим эмодзи — заменяет."""
    if req.emoji not in REACTION_EMOJIS:
        raise HTTPException(status_code=400, detail="Неизвестная реакция")

    post = db.query(GalleryPost).filter(GalleryPost.id == post_id, GalleryPost.status == "approved").first()
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")

    existing = (
        db.query(GalleryLike)
        .filter(GalleryLike.post_id == post_id, GalleryLike.user_id == current_user.id)
        .first()
    )

    if existing and existing.emoji == req.emoji:
        db.delete(existing)
        db.commit()
        my_reaction = None
    else:
        is_new = existing is None
        if existing:
            existing.emoji = req.emoji
        else:
            db.add(GalleryLike(post_id=post_id, user_id=current_user.id, emoji=req.emoji))
        db.commit()
        my_reaction = req.emoji

        if is_new and post.user_id != current_user.id:
            owner = db.query(User).filter(User.id == post.user_id).first()
            if owner:
                add_xp(db, owner, 3)
                notify(db, owner, f"{req.emoji} {author_display_name(current_user)} отреагировал(а) на твой промпт в галерее")
                total_reactions_received = (
                    db.query(GalleryLike)
                    .join(GalleryPost, GalleryLike.post_id == GalleryPost.id)
                    .filter(GalleryPost.user_id == owner.id)
                    .count()
                )
                if total_reactions_received >= 10:
                    grant_achievement(db, owner, "liked_10")
                if total_reactions_received >= 50:
                    grant_achievement(db, owner, "reactions_50")

        if is_new:
            my_total_reactions_given = (
                db.query(GalleryLike).filter(GalleryLike.user_id == current_user.id).count()
            )
            if my_total_reactions_given >= 50:
                grant_achievement(db, current_user, "active_reactor")

    rows = db.query(GalleryLike).filter(GalleryLike.post_id == post_id).all()
    counts = {}
    for r in rows:
        counts[r.emoji] = counts.get(r.emoji, 0) + 1

    return {"reactions": counts, "my_reaction": my_reaction}


# ==================== Достижения и личные уведомления ====================

@app.get("/api/achievements")
async def list_achievements(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Все достижения платформы + отметка, какие из них уже получены текущим пользователем."""
    earned = {
        a.key: a.earned_at
        for a in db.query(UserAchievement).filter(UserAchievement.user_id == current_user.id).all()
    }
    return [
        {
            "key": key,
            "label": info["label"],
            "desc": info["desc"],
            "earned": key in earned,
            "earned_at": earned.get(key),
        }
        for key, info in ACHIEVEMENTS.items()
    ]


@app.get("/api/notifications")
async def list_notifications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Личные уведомления (лайки/комментарии/достижения) — отдельно от общей ленты обновлений."""
    items = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(30)
        .all()
    )
    return [{"id": n.id, "content": n.content, "created_at": n.created_at} for n in items]


@app.delete("/api/notifications")
async def clear_notifications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Полностью очищает личные уведомления текущего пользователя (лайки/комменты/достижения)."""
    db.query(Notification).filter(Notification.user_id == current_user.id).delete()
    db.commit()
    return {"status": "cleared"}


# ==================== Лента оповещений об обновлениях ====================

@app.get("/api/announcements")
async def list_announcements(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Последние оповещения об обновлениях — публикуются через /announce в боте или из админки сайта."""
    items = db.query(Announcement).order_by(Announcement.created_at.desc()).limit(20).all()
    return [{"id": a.id, "content": a.content, "created_at": a.created_at} for a in items]


@app.post("/api/admin/announcements")
async def admin_post_announcement(
    req: AdminAnnouncementRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Публикует анонс обновления прямо с сайта (аналог /announce в боте) — виден только на сайте,
    в Telegram не рассылается."""
    text = req.content.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустой текст анонса")

    if req.ai_polish:
        text = ai_service.polish_announcement(text)

    item = Announcement(content=text)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "content": item.content, "created_at": item.created_at}


@app.post("/api/bot/announcements", dependencies=[Depends(verify_bot_secret)])
async def bot_save_announcement(req: BotAnnouncementRequest, db: Session = Depends(get_db)):
    """Бот вызывает это после рассылки /announce — чтобы то же оповещение появилось на сайте."""
    item = Announcement(content=req.content)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id}


# ==================== Общий публичный чат (требует авторизации сайта) ====================

PUBLIC_CHAT_REACTIONS = ["❤️", "🔥", "😂", "👍", "😮"]


# ==================== WebSocket общего чата — мгновенная доставка ====================
# Сама отправка/модерация/лимиты остаются на REST (/api/public-chat) — WebSocket только
# оповещает уже подключённых клиентов, что что-то произошло, без дублирования логики.

class ChatConnectionManager:
    """
    Рассылка событий общего чата всем подключённым по WebSocket.

    Раньше (и как аварийный вариант сейчас) — просто рассылка по локальному списку
    соединений этого процесса. Теперь, если Valkey доступен, broadcast() публикует
    событие в канал Valkey, а не рассылает напрямую — событие получает КАЖДЫЙ процесс
    (в т.ч. этот же), подписанный на канал, и уже он раздаёт его своим локальным
    WebSocket-подключениям через _local_broadcast(). Это и даёт настоящий realtime
    между несколькими инстансами сервера в будущем, а не только внутри одного процесса.

    Если Valkey недоступен (не настроен / упал) — broadcast() автоматически откатывается
    на прямую локальную рассылку, как было раньше. Чат при этом не ломается, просто
    работает в пределах одного процесса, как и всегда работал.
    """

    CHANNEL = "botyara:public_chat"

    def __init__(self):
        self.active: list[WebSocket] = []
        self._subscriber_task: asyncio.Task | None = None

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def _local_broadcast(self, data: dict):
        """Рассылает только тем, кто подключён именно к ЭТОМУ процессу."""
        if not self.active:
            return

        async def send_one(ws: WebSocket):
            try:
                await ws.send_json(data)
                return None
            except Exception:
                return ws

        results = await asyncio.gather(*(send_one(ws) for ws in self.active), return_exceptions=False)
        for dead_ws in results:
            if dead_ws is not None:
                self.disconnect(dead_ws)

    async def broadcast(self, data: dict):
        """Публикует событие в Valkey (если он подключён) — иначе безопасно откатывается
        на локальную рассылку, чтобы чат не ломался при недоступности Valkey."""
        client = vk.valkey_client
        if client is not None:
            try:
                await client.publish(self.CHANNEL, json.dumps(data))
                return
            except Exception:
                logging.exception("Не удалось опубликовать в Valkey — рассылаю локально как раньше")
        await self._local_broadcast(data)

    async def start_subscriber(self):
        """Запускается один раз при старте приложения (если Valkey подключён) — слушает
        канал Valkey и раздаёт полученные события локальным WebSocket-подключениям."""
        client = vk.valkey_client
        if client is None:
            logging.warning(
                "Valkey недоступен при старте — подписка на канал чата не запущена, "
                "общий чат продолжит работать в пределах одного процесса (как раньше)"
            )
            return
        try:
            pubsub = client.pubsub()
            await pubsub.subscribe(self.CHANNEL)
            logging.info(f"Valkey: подписались на канал '{self.CHANNEL}'")
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                except Exception:
                    continue
                await self._local_broadcast(data)
        except Exception:
            logging.exception("Подписка на канал Valkey оборвалась — общий чат продолжит работать локально")


chat_manager = ChatConnectionManager()


def serialize_public_message_public(db: Session, m: PublicChatMessage) -> dict:
    """Версия сообщения БЕЗ привязки к конкретному зрителю (без is_mine/my_reaction) —
    именно её рассылаем всем через WebSocket, т.к. эти поля у каждого свои."""
    reply_to = None
    if m.reply_to_id:
        original = db.query(PublicChatMessage).filter(PublicChatMessage.id == m.reply_to_id).first()
        if original:
            snippet = original.content[:80] + ("…" if len(original.content) > 80 else "")
            reply_to = {"id": original.id, "author": author_display_name(original.user), "content": snippet}
    return {
        "id": m.id,
        "content": m.content,
        "author": author_display_name(m.user),
        "author_id": m.user.id,
        "author_avatar": m.user.avatar_base64,
        "author_level": calc_level(m.user.xp or 0),
        "author_badge": author_badge(m.user),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "is_pinned": m.is_pinned,
        "reply_to": reply_to,
        "reactions": {},
        "reactors": {},
    }


@app.websocket("/ws/public-chat")
async def public_chat_ws(websocket: WebSocket, token: str | None = None):
    """Держит соединение открытым и присылает событие всем, у кого открыт чат, как только что-то
    меняется — сайт реагирует мгновенно, вместо ожидания следующего опроса."""
    if not token or not auth.decode_access_token(token):
        await websocket.close(code=4001)
        return
    await chat_manager.connect(websocket)
    try:
        while True:
            # Клиент ничего осмысленного не шлёт — просто держим соединение живым
            await websocket.receive_text()
    except WebSocketDisconnect:
        chat_manager.disconnect(websocket)
    except Exception:
        chat_manager.disconnect(websocket)


def serialize_public_message(db: Session, m: PublicChatMessage, current_user: User) -> dict:
    reaction_rows = db.query(PublicChatReaction).filter(PublicChatReaction.message_id == m.id).all()
    reactions = {}
    my_reaction = None
    reactors = {}
    for r in reaction_rows:
        reactions[r.emoji] = reactions.get(r.emoji, 0) + 1
        reactors.setdefault(r.emoji, []).append(author_display_name(r.user))
        if r.user_id == current_user.id:
            my_reaction = r.emoji

    reply_to = None
    if m.reply_to_id:
        original = db.query(PublicChatMessage).filter(PublicChatMessage.id == m.reply_to_id).first()
        if original:
            snippet = original.content[:80] + ("…" if len(original.content) > 80 else "")
            reply_to = {"id": original.id, "author": author_display_name(original.user), "content": snippet}

    return {
        "id": m.id,
        "content": m.content,
        "author": author_display_name(m.user),
        "author_id": m.user.id,
        "author_avatar": m.user.avatar_base64,
        "author_level": calc_level(m.user.xp or 0),
        "author_badge": author_badge(m.user),
        "created_at": m.created_at,
        "is_mine": m.user_id == current_user.id,
        "is_pinned": m.is_pinned,
        "reply_to": reply_to,
        "reactions": reactions,
        "reactors": reactors,
        "my_reaction": my_reaction,
    }


def serialize_public_messages_batch(db: Session, messages: list, current_user: User) -> list:
    """Как serialize_public_message, но для СПИСКА сообщений разом — считает реакции и подгружает
    оригиналы ответов одним запросом на всех, а не по одному на каждое сообщение (N+1)."""
    if not messages:
        return []
    message_ids = [m.id for m in messages]

    reaction_rows = db.query(PublicChatReaction).filter(PublicChatReaction.message_id.in_(message_ids)).all()
    reactor_ids = {r.user_id for r in reaction_rows}
    reactor_users = {}
    if reactor_ids:
        reactor_users = {u.id: u for u in db.query(User).filter(User.id.in_(reactor_ids)).all()}

    reactions_by_msg = {}
    reactors_by_msg = {}
    my_reaction_by_msg = {}
    for r in reaction_rows:
        bucket = reactions_by_msg.setdefault(r.message_id, {})
        bucket[r.emoji] = bucket.get(r.emoji, 0) + 1
        reactor_name = author_display_name(reactor_users[r.user_id]) if r.user_id in reactor_users else "?"
        reactors_by_msg.setdefault(r.message_id, {}).setdefault(r.emoji, []).append(reactor_name)
        if r.user_id == current_user.id:
            my_reaction_by_msg[r.message_id] = r.emoji

    reply_ids = {m.reply_to_id for m in messages if m.reply_to_id}
    reply_originals = {}
    if reply_ids:
        originals = (
            db.query(PublicChatMessage)
            .options(joinedload(PublicChatMessage.user))
            .filter(PublicChatMessage.id.in_(reply_ids))
            .all()
        )
        reply_originals = {o.id: o for o in originals}

    result = []
    for m in messages:
        reply_to = None
        if m.reply_to_id and m.reply_to_id in reply_originals:
            orig = reply_originals[m.reply_to_id]
            snippet = orig.content[:80] + ("…" if len(orig.content) > 80 else "")
            reply_to = {"id": orig.id, "author": author_display_name(orig.user), "content": snippet}
        result.append(
            {
                "id": m.id,
                "content": m.content,
                "author": author_display_name(m.user),
                "author_id": m.user.id,
                "author_avatar": m.user.avatar_base64,
                "author_level": calc_level(m.user.xp or 0),
                "author_badge": author_badge(m.user),
                "created_at": m.created_at,
                "is_mine": m.user_id == current_user.id,
                "is_pinned": m.is_pinned,
                "reply_to": reply_to,
                "reactions": reactions_by_msg.get(m.id, {}),
                "reactors": reactors_by_msg.get(m.id, {}),
                "my_reaction": my_reaction_by_msg.get(m.id),
            }
        )
    return result


@app.get("/api/public-chat")
async def public_chat_list(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Последние 50 одобренных сообщений общего чата, от старых к новым, плюс закреплённое."""
    messages = (
        db.query(PublicChatMessage)
        .options(joinedload(PublicChatMessage.user))
        .filter(PublicChatMessage.status == "approved")
        .order_by(PublicChatMessage.created_at.desc())
        .limit(50)
        .all()
    )
    messages.reverse()

    pinned = db.query(PublicChatMessage).filter(PublicChatMessage.is_pinned == True).first()  # noqa: E712

    # Закреплённое сообщение может не входить в последние 50 — сериализуем отдельным батчем при надобности
    to_serialize = list(messages)
    if pinned and pinned.id not in {m.id for m in messages}:
        to_serialize.append(pinned)
    serialized = {s["id"]: s for s in serialize_public_messages_batch(db, to_serialize, current_user)}

    return {
        "messages": [serialized[m.id] for m in messages],
        "pinned": serialized[pinned.id] if pinned else None,
    }


@app.get("/api/public-chat/search")
async def public_chat_search(
    q: str = "",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Поиск по тексту или автору — быстрый обзор совпадений, без загрузки всей истории."""
    if not q.strip():
        return []
    like = f"%{q.strip()}%"
    rows = (
        db.query(PublicChatMessage)
        .join(User, PublicChatMessage.user_id == User.id)
        .filter(
            PublicChatMessage.status == "approved",
            (PublicChatMessage.content.ilike(like))
            | (User.display_name.ilike(like))
            | (User.telegram_first_name.ilike(like)),
        )
        .order_by(PublicChatMessage.created_at.desc())
        .limit(30)
        .all()
    )
    return [
        {
            "id": m.id,
            "content": m.content[:120] + ("…" if len(m.content) > 120 else ""),
            "author": author_display_name(m.user),
            "created_at": m.created_at,
        }
        for m in rows
    ]


@app.get("/api/public-chat/context/{message_id}")
async def public_chat_context(
    message_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Окно сообщений вокруг конкретного (для перехода 'к оригиналу' из ответа/поиска)."""
    target = db.query(PublicChatMessage).filter(PublicChatMessage.id == message_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")

    before = (
        db.query(PublicChatMessage)
        .filter(PublicChatMessage.status == "approved", PublicChatMessage.created_at < target.created_at)
        .order_by(PublicChatMessage.created_at.desc())
        .limit(15)
        .all()
    )
    after = (
        db.query(PublicChatMessage)
        .filter(PublicChatMessage.status == "approved", PublicChatMessage.created_at > target.created_at)
        .order_by(PublicChatMessage.created_at.asc())
        .limit(15)
        .all()
    )
    window = list(reversed(before)) + [target] + after
    return {
        "messages": serialize_public_messages_batch(db, window, current_user),
        "target_id": target.id,
    }


@app.post("/api/public-chat")
async def public_chat_send(
    req: PublicChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Отправляет сообщение в общий чат — проходит модерацию перед показом всем."""
    ensure_not_banned(current_user)
    check_rate_limit(f"public_chat:{current_user.id}", max_calls=10, window_seconds=30)
    allowed, reason = ai_service.moderate_text(req.content)

    reply_to_id = None
    if req.reply_to_id:
        original = db.query(PublicChatMessage).filter(PublicChatMessage.id == req.reply_to_id).first()
        if original:
            reply_to_id = original.id

    message = PublicChatMessage(
        user_id=current_user.id,
        content=req.content,
        status="approved" if allowed else "rejected",
        reject_reason=None if allowed else reason,
        reply_to_id=reply_to_id,
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    if allowed:
        add_xp(db, current_user, 2)
        await chat_manager.broadcast({"type": "new_message", "message": serialize_public_message_public(db, message)})
        return {
            "id": message.id,
            "status": message.status,
            "reject_reason": message.reject_reason,
            "message": serialize_public_message(db, message, current_user),
        }
    return {"id": message.id, "status": message.status, "reject_reason": message.reject_reason, "message": None}


@app.delete("/api/public-chat/{message_id}")
async def public_chat_delete(
    message_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удаляет сообщение из общего чата — доступно автору сообщения, модератору или администратору."""
    message = db.query(PublicChatMessage).filter(PublicChatMessage.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    if message.user_id != current_user.id and not is_moderator(current_user):
        raise HTTPException(status_code=403, detail="Удалить можно только своё сообщение")
    db.delete(message)
    db.commit()
    await chat_manager.broadcast({"type": "message_deleted", "id": message_id})
    return {"status": "deleted"}


@app.delete("/api/public-chat")
async def public_chat_clear(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Полностью очищает общий чат — модератору или администратору."""
    if not is_moderator(current_user):
        raise HTTPException(status_code=403, detail="Только модератор может очистить общий чат")
    db.query(PublicChatMessage).delete()
    db.commit()
    await chat_manager.broadcast({"type": "cleared"})
    return {"status": "cleared"}


@app.post("/api/public-chat/{message_id}/react")
async def public_chat_react(
    message_id: int,
    req: PublicChatReactRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Реакция на сообщение — один эмодзи на пользователя, повторный клик убирает/меняет."""
    if req.emoji not in PUBLIC_CHAT_REACTIONS:
        raise HTTPException(status_code=400, detail="Неизвестная реакция")
    message = db.query(PublicChatMessage).filter(PublicChatMessage.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")

    existing = (
        db.query(PublicChatReaction)
        .filter(PublicChatReaction.message_id == message_id, PublicChatReaction.user_id == current_user.id)
        .first()
    )
    if existing and existing.emoji == req.emoji:
        db.delete(existing)
    elif existing:
        existing.emoji = req.emoji
    else:
        db.add(PublicChatReaction(message_id=message_id, user_id=current_user.id, emoji=req.emoji))
    db.commit()
    await chat_manager.broadcast({"type": "message_updated", "message": serialize_public_message_public(db, message)})

    return serialize_public_message(db, message, current_user)


@app.post("/api/public-chat/{message_id}/pin")
async def public_chat_pin(
    message_id: int,
    current_user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Закрепляет сообщение (снимая предыдущий закреп) — только модератор/админ. Повторный клик снимает."""
    message = db.query(PublicChatMessage).filter(PublicChatMessage.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")

    previous_pinned = None
    if message.is_pinned:
        message.is_pinned = False
    else:
        previous_pinned = (
            db.query(PublicChatMessage)
            .filter(PublicChatMessage.is_pinned == True, PublicChatMessage.id != message_id)  # noqa: E712
            .first()
        )
        db.query(PublicChatMessage).filter(PublicChatMessage.is_pinned == True).update({"is_pinned": False})  # noqa: E712
        message.is_pinned = True
    db.commit()

    await chat_manager.broadcast({"type": "message_updated", "message": serialize_public_message_public(db, message)})
    if previous_pinned:
        db.refresh(previous_pinned)
        await chat_manager.broadcast(
            {"type": "message_updated", "message": serialize_public_message_public(db, previous_pinned)}
        )

    return serialize_public_message(db, message, current_user)


# ==================== Эндпоинты для бота (требуют секрет BOT_INTERNAL_SECRET) ====================

@app.post("/api/bot/message", dependencies=[Depends(verify_bot_secret)])
async def bot_save_message(req: BotMessageRequest, db: Session = Depends(get_db)):
    """
    Бот вызывает это после каждого сообщения (своего и ответа нейросети),
    чтобы история попадала в общую базу и была видна на сайте.
    """
    user = get_or_create_bot_user(db, req.telegram_id, req.telegram_username, req.telegram_first_name)

    message = Message(
        user_id=user.id, role=req.role, content=req.content, source="bot", persona=req.persona or "default"
    )
    db.add(message)
    db.commit()

    return {"status": "ok"}


@app.get("/api/bot/history/{telegram_id}", dependencies=[Depends(verify_bot_secret)])
async def bot_get_history(telegram_id: int, db: Session = Depends(get_db)):
    """
    Возвращает всю сохранённую историю переписки пользователя (из бота и с сайта вместе),
    отсортированную по времени. Пригодится дальше, когда будем синхронизировать историю чата.
    """
    user = db.query(User).filter(User.telegram_id == str(telegram_id)).first()
    if user is None:
        return {"history": []}

    messages = (
        db.query(Message)
        .filter(Message.user_id == user.id)
        .order_by(Message.created_at.asc())
        .all()
    )
    return {"history": [{"role": m.role, "content": m.content} for m in messages]}


@app.post("/api/bot/favorite", dependencies=[Depends(verify_bot_secret)])
async def bot_add_favorite(req: BotFavoriteRequest, db: Session = Depends(get_db)):
    """Бот вызывает это, когда пользователь жмёт '⭐ Сохранить в избранное' — сохраняет в общую БД."""
    user = get_or_create_bot_user(db, req.telegram_id, req.telegram_username, req.telegram_first_name)

    favorite = Favorite(user_id=user.id, content=req.content, category=req.category or "other")
    db.add(favorite)
    db.commit()
    db.refresh(favorite)

    return {"id": favorite.id, "content": favorite.content, "category": favorite.category, "created_at": favorite.created_at}


@app.get("/api/bot/favorites/{telegram_id}", dependencies=[Depends(verify_bot_secret)])
async def bot_list_favorites(telegram_id: int, db: Session = Depends(get_db)):
    """Возвращает список избранного пользователя вместе с настоящими ID из базы —
    боту это нужно, чтобы показывать промпты по одному и удалять их индивидуально."""
    user = db.query(User).filter(User.telegram_id == str(telegram_id)).first()
    if user is None:
        return {"favorites": []}

    favorites = (
        db.query(Favorite)
        .filter(Favorite.user_id == user.id)
        .order_by(Favorite.created_at.desc())
        .all()
    )
    return {"favorites": [{"id": f.id, "content": f.content, "category": f.category} for f in favorites]}


@app.post("/api/bot/favorite/delete", dependencies=[Depends(verify_bot_secret)])
async def bot_delete_favorite(req: BotFavoriteDeleteRequest, db: Session = Depends(get_db)):
    """Удаляет один промпт из избранного по его ID (только если он принадлежит этому telegram_id)."""
    user = db.query(User).filter(User.telegram_id == str(req.telegram_id)).first()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    favorite = (
        db.query(Favorite)
        .filter(Favorite.id == req.favorite_id, Favorite.user_id == user.id)
        .first()
    )
    if not favorite:
        raise HTTPException(status_code=404, detail="Не найдено")

    db.delete(favorite)
    db.commit()
    return {"status": "deleted"}


# ==================== Галерея промптов для бота ====================

@app.post("/api/bot/gallery/publish", dependencies=[Depends(verify_bot_secret)])
async def bot_gallery_publish(req: BotGalleryPublishRequest, db: Session = Depends(get_db)):
    """Бот вызывает это, когда пользователь публикует промпт из своего избранного в галерею."""
    user = get_or_create_bot_user(db, req.telegram_id, req.telegram_username, req.telegram_first_name)

    allowed, reason = ai_service.moderate_text(req.content)
    post = GalleryPost(
        user_id=user.id,
        content=req.content,
        category=req.category or "other",
        status="approved" if allowed else "rejected",
        reject_reason=None if allowed else reason,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return {"id": post.id, "status": post.status, "reject_reason": post.reject_reason}


@app.get("/api/bot/gallery", dependencies=[Depends(verify_bot_secret)])
async def bot_gallery_list(limit: int = 10, db: Session = Depends(get_db)):
    """Список последних опубликованных промптов — для показа в боте."""
    posts = (
        db.query(GalleryPost)
        .filter(GalleryPost.status == "approved")
        .order_by(GalleryPost.created_at.desc())
        .limit(limit)
        .all()
    )
    result = []
    for p in posts:
        comment_count = (
            db.query(GalleryComment)
            .filter(GalleryComment.post_id == p.id, GalleryComment.status == "approved")
            .count()
        )
        result.append(
            {
                "id": p.id,
                "content": p.content,
                "category": p.category,
                "author": author_display_name(p.user),
                "comment_count": comment_count,
            }
        )
    return {"posts": result}


@app.get("/api/bot/gallery/{post_id}", dependencies=[Depends(verify_bot_secret)])
async def bot_gallery_detail(post_id: int, db: Session = Depends(get_db)):
    """Один пост галереи с комментариями — для показа в боте."""
    post = db.query(GalleryPost).filter(GalleryPost.id == post_id, GalleryPost.status == "approved").first()
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")

    comments = (
        db.query(GalleryComment)
        .filter(GalleryComment.post_id == post_id, GalleryComment.status == "approved")
        .order_by(GalleryComment.created_at.asc())
        .limit(15)
        .all()
    )
    return {
        "id": post.id,
        "content": post.content,
        "category": post.category,
        "author": author_display_name(post.user),
        "comments": [{"author": author_display_name(c.user), "content": c.content} for c in comments],
    }


@app.post("/api/bot/gallery/comment", dependencies=[Depends(verify_bot_secret)])
async def bot_gallery_comment(req: BotGalleryCommentRequest, db: Session = Depends(get_db)):
    """Бот вызывает это, когда пользователь присылает текст комментария к посту галереи."""
    user = get_or_create_bot_user(db, req.telegram_id, req.telegram_username, req.telegram_first_name)

    post = db.query(GalleryPost).filter(GalleryPost.id == req.post_id, GalleryPost.status == "approved").first()
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")

    allowed, reason = ai_service.moderate_text(req.content)
    comment = GalleryComment(
        post_id=req.post_id,
        user_id=user.id,
        content=req.content,
        status="approved" if allowed else "rejected",
        reject_reason=None if allowed else reason,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return {"status": comment.status, "reject_reason": comment.reject_reason}


# ==================== Общий публичный чат для бота ====================

@app.get("/api/bot/public-chat", dependencies=[Depends(verify_bot_secret)])
async def bot_public_chat_list(limit: int = 15, db: Session = Depends(get_db)):
    """Последние сообщения общего чата — для показа в боте."""
    messages = (
        db.query(PublicChatMessage)
        .filter(PublicChatMessage.status == "approved")
        .order_by(PublicChatMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    messages.reverse()
    return {
        "messages": [
            {"author": author_display_name(m.user), "content": m.content} for m in messages
        ]
    }


@app.post("/api/bot/public-chat", dependencies=[Depends(verify_bot_secret)])
async def bot_public_chat_send(req: BotPublicChatRequest, db: Session = Depends(get_db)):
    """Бот вызывает это, когда пользователь пишет сообщение в общий чат."""
    user = get_or_create_bot_user(db, req.telegram_id, req.telegram_username, req.telegram_first_name)

    allowed, reason = ai_service.moderate_text(req.content)
    message = PublicChatMessage(
        user_id=user.id,
        content=req.content,
        status="approved" if allowed else "rejected",
        reject_reason=None if allowed else reason,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return {"status": message.status, "reject_reason": message.reject_reason}


# ==================== Админ-панель (только для администратора) ====================

@app.get("/api/admin/stats")
async def admin_stats(current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Общая статистика проекта — для админ-панели на сайте."""
    now = datetime.now(timezone.utc)
    online_cutoff = now - timedelta(minutes=5)
    today_start = datetime.combine(date.today(), datetime.min.time())
    week_start = today_start - timedelta(days=7)

    total_users = db.query(User).count()
    online_now = db.query(User).filter(User.last_seen_at != None, User.last_seen_at >= online_cutoff).count()
    active_today = db.query(User).filter(User.last_seen_at != None, User.last_seen_at >= today_start).count()
    active_week = db.query(User).filter(User.last_seen_at != None, User.last_seen_at >= week_start).count()
    new_today = db.query(User).filter(User.created_at >= today_start).count()
    new_week = db.query(User).filter(User.created_at >= week_start).count()

    total_chat_messages = db.query(Message).count()
    total_public_messages = db.query(PublicChatMessage).count()

    total_posts = db.query(GalleryPost).count()
    approved_posts = db.query(GalleryPost).filter(GalleryPost.status == "approved").count()
    rejected_posts = db.query(GalleryPost).filter(GalleryPost.status == "rejected").count()

    total_comments = db.query(GalleryComment).count()
    rejected_comments = db.query(GalleryComment).filter(GalleryComment.status == "rejected").count()
    rejected_public_chat = db.query(PublicChatMessage).filter(PublicChatMessage.status == "rejected").count()

    rejected_today = (
        db.query(GalleryPost)
        .filter(GalleryPost.status == "rejected", GalleryPost.created_at >= today_start)
        .count()
        + db.query(GalleryComment)
        .filter(GalleryComment.status == "rejected", GalleryComment.created_at >= today_start)
        .count()
        + db.query(PublicChatMessage)
        .filter(PublicChatMessage.status == "rejected", PublicChatMessage.created_at >= today_start)
        .count()
    )

    total_likes = db.query(GalleryLike).count()
    total_favorites = db.query(Favorite).count()

    signups_by_day = []
    for i in range(13, -1, -1):
        day = date.today() - timedelta(days=i)
        day_start = datetime.combine(day, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        count = db.query(User).filter(User.created_at >= day_start, User.created_at < day_end).count()
        signups_by_day.append({"date": day.isoformat(), "count": count})

    return {
        "online_now": online_now,
        "total_users": total_users,
        "active_today": active_today,
        "active_week": active_week,
        "new_today": new_today,
        "new_week": new_week,
        "total_messages": total_chat_messages + total_public_messages,
        "total_gallery_posts": total_posts,
        "approved_posts": approved_posts,
        "rejected_posts": rejected_posts,
        "total_comments": total_comments,
        "rejected_comments": rejected_comments,
        "rejected_public_chat": rejected_public_chat,
        "rejected_today": rejected_today,
        "total_likes": total_likes,
        "total_favorites": total_favorites,
        "signups_by_day": signups_by_day,
    }


@app.get("/api/admin/users")
async def admin_users(current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Список пользователей (последние активные — сверху) — для админ-панели."""
    now = datetime.now(timezone.utc)
    users = db.query(User).order_by(User.last_seen_at.desc().nullslast()).limit(150).all()
    result = []
    for u in users:
        is_online = bool(u.last_seen_at and (now - u.last_seen_at).total_seconds() <= 300)
        result.append(
            {
                "id": u.id,
                "name": author_display_name(u),
                "avatar": u.avatar_base64,
                "email": u.email,
                "telegram_username": u.telegram_username,
                "level": calc_level(u.xp or 0),
                "xp": u.xp or 0,
                "current_streak": u.current_streak or 0,
                "created_at": u.created_at,
                "last_seen_at": u.last_seen_at,
                "is_online": is_online,
                "is_admin": is_site_admin(u),
            }
        )
    return result


@app.get("/api/admin/leaderboard")
async def admin_leaderboard(current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Топ-15 пользователей по опыту."""
    users = db.query(User).order_by(User.xp.desc()).limit(15).all()
    return [
        {
            "id": u.id,
            "name": author_display_name(u),
            "avatar": u.avatar_base64,
            "xp": u.xp or 0,
            "level": calc_level(u.xp or 0),
        }
        for u in users
    ]


@app.get("/api/admin/activity")
async def admin_activity(current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Лента последних событий (посты, комментарии, сообщения общего чата) — быстрый обзор модерации."""
    events = []

    for p in (
        db.query(GalleryPost)
        .options(joinedload(GalleryPost.user))
        .order_by(GalleryPost.created_at.desc())
        .limit(20)
        .all()
    ):
        preview = p.content[:100] + ("…" if len(p.content) > 100 else "")
        events.append(
            {
                "kind": "gallery_post",
                "author": author_display_name(p.user),
                "content": preview,
                "status": p.status,
                "reject_reason": p.reject_reason,
                "created_at": p.created_at,
            }
        )

    for c in (
        db.query(GalleryComment)
        .options(joinedload(GalleryComment.user))
        .order_by(GalleryComment.created_at.desc())
        .limit(20)
        .all()
    ):
        preview = c.content[:100] + ("…" if len(c.content) > 100 else "")
        events.append(
            {
                "kind": "gallery_comment",
                "author": author_display_name(c.user),
                "content": preview,
                "status": c.status,
                "reject_reason": c.reject_reason,
                "created_at": c.created_at,
            }
        )

    for m in (
        db.query(PublicChatMessage)
        .options(joinedload(PublicChatMessage.user))
        .order_by(PublicChatMessage.created_at.desc())
        .limit(20)
        .all()
    ):
        preview = m.content[:100] + ("…" if len(m.content) > 100 else "")
        events.append(
            {
                "kind": "public_chat",
                "author": author_display_name(m.user),
                "content": preview,
                "status": m.status,
                "reject_reason": m.reject_reason,
                "created_at": m.created_at,
            }
        )

    events.sort(key=lambda e: e["created_at"], reverse=True)
    return events[:30]


# ==================== Совместные комнаты (несколько человек сочиняют промпт вместе) ====================

MAX_ROOM_PARTICIPANTS = 5

# Временное состояние "печатает…" в комнатах — не персистентное (в памяти), это нормально:
# room_code -> {user_id: время последнего "тик" от клиента}
room_typing_state: dict[str, dict[int, float]] = {}


def generate_room_code() -> str:
    """Короткий код комнаты для приглашения (например, 8X2F4K)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих символов (0/O, 1/I)
    return "".join(secrets.choice(alphabet) for _ in range(6))


def get_room_or_404(db: Session, code: str) -> Room:
    room = db.query(Room).filter(Room.code == code.upper()).first()
    if not room:
        raise HTTPException(status_code=404, detail="Комната не найдена — проверь код")
    return room


def require_room_participant(db: Session, room: Room, user: User) -> None:
    is_in = (
        db.query(RoomParticipant)
        .filter(RoomParticipant.room_id == room.id, RoomParticipant.user_id == user.id)
        .first()
    )
    if not is_in:
        raise HTTPException(status_code=403, detail="Ты не участник этой комнаты")


def serialize_room(db: Session, room: Room, current_user: User) -> dict:
    participants = (
        db.query(RoomParticipant)
        .options(joinedload(RoomParticipant.user))
        .filter(RoomParticipant.room_id == room.id)
        .all()
    )
    messages = (
        db.query(RoomMessage)
        .options(joinedload(RoomMessage.user))
        .filter(RoomMessage.room_id == room.id)
        .order_by(RoomMessage.created_at.asc())
        .all()
    )

    now = time.time()
    typing_state = room_typing_state.get(room.code, {})
    typing_names = [
        author_display_name(p.user)
        for p in participants
        if p.user.id != current_user.id and now - typing_state.get(p.user.id, 0) < 4
    ]

    return {
        "code": room.code,
        "category": room.category,
        "status": room.status,
        "final_content": room.final_content,
        "is_owner": room.created_by == current_user.id,
        "typing": typing_names,
        "participants": [
            {
                "id": p.user.id,
                "name": author_display_name(p.user),
                "avatar": p.user.avatar_base64,
                "level": calc_level(p.user.xp or 0),
                "badge": author_badge(p.user),
                "is_me": p.user.id == current_user.id,
            }
            for p in participants
        ],
        "messages": [
            {
                "id": m.id,
                "content": m.content,
                "channel": m.channel,
                "author": author_display_name(m.user) if m.user else "🤖 Нейросеть",
                "author_avatar": m.user.avatar_base64 if m.user else None,
                "is_mine": m.user_id == current_user.id if m.user_id else False,
                "is_ai": m.user_id is None,
                "created_at": m.created_at,
            }
            for m in messages
        ],
    }


@app.post("/api/rooms")
async def create_room(
    req: RoomCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Создаёт новую совместную комнату — автор автоматически становится первым участником."""
    code = generate_room_code()
    while db.query(Room).filter(Room.code == code).first():
        code = generate_room_code()

    room = Room(code=code, category=req.category or "other", created_by=current_user.id)
    db.add(room)
    db.commit()
    db.refresh(room)

    db.add(RoomParticipant(room_id=room.id, user_id=current_user.id))
    db.commit()

    total_created = db.query(Room).filter(Room.created_by == current_user.id).count()
    if total_created >= 5:
        grant_achievement(db, current_user, "room_organizer")

    return serialize_room(db, room, current_user)


@app.post("/api/rooms/join")
async def join_room(
    req: RoomJoinRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Присоединяет текущего пользователя к комнате по коду."""
    room = get_room_or_404(db, req.code)

    if room.status != "open":
        raise HTTPException(status_code=400, detail="Эта комната уже завершена")

    already_in = (
        db.query(RoomParticipant)
        .filter(RoomParticipant.room_id == room.id, RoomParticipant.user_id == current_user.id)
        .first()
    )
    if not already_in:
        count = db.query(RoomParticipant).filter(RoomParticipant.room_id == room.id).count()
        if count >= MAX_ROOM_PARTICIPANTS:
            raise HTTPException(status_code=400, detail="В комнате уже максимум участников")
        db.add(RoomParticipant(room_id=room.id, user_id=current_user.id))
        db.commit()

    return serialize_room(db, room, current_user)


@app.get("/api/rooms/{code}")
async def get_room(
    code: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Состояние комнаты целиком — участники и вся история сообщений."""
    room = get_room_or_404(db, code)
    require_room_participant(db, room, current_user)
    return serialize_room(db, room, current_user)


@app.post("/api/rooms/{code}/typing")
async def room_typing_ping(
    code: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Лёгкий "тик" — сайт вызывает это, пока пользователь печатает в комнате."""
    room = get_room_or_404(db, code)
    require_room_participant(db, room, current_user)
    room_typing_state.setdefault(room.code, {})[current_user.id] = time.time()
    return {"status": "ok"}


@app.post("/api/rooms/{code}/messages")
async def send_room_message(
    code: str,
    req: RoomMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Сообщение в комнате. Два канала:
    - "team" — приватное обсуждение между участниками, ИИ его не видит и не отвечает
    - "ai" (по умолчанию) — общий чат с нейросетью, она сразу отвечает всем участникам
    """
    room = get_room_or_404(db, code)
    require_room_participant(db, room, current_user)

    if room.status != "open":
        raise HTTPException(status_code=400, detail="Комната уже завершена")

    check_rate_limit(f"room_message:{current_user.id}", max_calls=20, window_seconds=30)

    channel = "team" if req.channel == "team" else "ai"
    db.add(RoomMessage(room_id=room.id, user_id=current_user.id, channel=channel, content=req.content))
    db.commit()
    add_xp(db, current_user, 1 if channel == "team" else 2)

    if channel == "team":
        # Приватное обсуждение — просто сохраняем, нейросеть сюда не подключаем
        return serialize_room(db, room, current_user)

    participants = db.query(RoomParticipant).filter(RoomParticipant.room_id == room.id).all()
    participant_names = [author_display_name(p.user) for p in participants]

    history_rows = (
        db.query(RoomMessage)
        .filter(RoomMessage.room_id == room.id, RoomMessage.channel == "ai")
        .order_by(RoomMessage.created_at.asc())
        .all()
    )
    history = [
        {
            "role": "assistant" if h.user_id is None else "user",
            "content": h.content if h.user_id is None else f"{author_display_name(h.user)}: {h.content}",
        }
        for h in history_rows
    ]

    reply = ai_service.get_room_reply(room.category, participant_names, history)
    db.add(RoomMessage(room_id=room.id, user_id=None, channel="ai", content=reply))
    db.commit()

    return serialize_room(db, room, current_user)


@app.post("/api/rooms/{code}/finish")
async def finish_room(
    code: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Завершает комнату — нейросеть собирает финальный промпт, он попадает в избранное всем участникам."""
    room = get_room_or_404(db, code)
    require_room_participant(db, room, current_user)

    if room.status != "open":
        return serialize_room(db, room, current_user)

    history_rows = (
        db.query(RoomMessage)
        .filter(RoomMessage.room_id == room.id, RoomMessage.channel == "ai")
        .order_by(RoomMessage.created_at.asc())
        .all()
    )
    if not history_rows:
        raise HTTPException(status_code=400, detail="В чате с нейросетью пока нет ни одного сообщения")

    history = [
        {
            "role": "assistant" if h.user_id is None else "user",
            "content": h.content if h.user_id is None else f"{author_display_name(h.user)}: {h.content}",
        }
        for h in history_rows
    ]

    final_text = ai_service.get_room_final_prompt(room.category, history)
    room.final_content = final_text
    room.status = "finished"
    db.commit()

    participants = db.query(RoomParticipant).filter(RoomParticipant.room_id == room.id).all()
    for p in participants:
        db.add(Favorite(user_id=p.user_id, content=final_text, category=room.category))
        add_xp(db, p.user, 10)
    db.commit()

    return serialize_room(db, room, current_user)


@app.get("/api/rooms")
async def list_my_rooms(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Комнаты, в которых участвует текущий пользователь (для быстрого возврата)."""
    rows = (
        db.query(RoomParticipant)
        .filter(RoomParticipant.user_id == current_user.id)
        .join(Room, RoomParticipant.room_id == Room.id)
        .order_by(Room.created_at.desc())
        .limit(20)
        .all()
    )
    return [
        {
            "code": r.room.code,
            "category": r.room.category,
            "status": r.room.status,
            "created_at": r.room.created_at,
            "is_owner": r.room.created_by == current_user.id,
        }
        for r in rows
    ]


@app.delete("/api/rooms/{code}")
async def delete_room(
    code: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удаляет комнату целиком (вместе со всеми сообщениями) — доступно создателю или администратору."""
    room = get_room_or_404(db, code)
    if room.created_by != current_user.id and not is_site_admin(current_user):
        raise HTTPException(status_code=403, detail="Удалить комнату может только её создатель")
    db.delete(room)
    db.commit()
    return {"status": "deleted"}


# ==================== Управление пользователями (только полный администратор) ====================

def serialize_admin_user(u: User, viewer: User) -> dict:
    return {
        "id": u.id,
        "name": author_display_name(u),
        "avatar": u.avatar_base64,
        "email": u.email,
        "telegram_username": u.telegram_username,
        "telegram_first_name": u.telegram_first_name,
        "xp": u.xp or 0,
        "level": calc_level(u.xp or 0),
        "role": get_effective_role(u),
        "is_banned": bool(u.is_banned),
        "badge_text": u.badge_text,
        "badge_color": u.badge_color,
        "created_at": u.created_at,
        "is_super_admin": is_site_admin(u),
        # Роль может менять только супер-админ — фронт скрывает этот контрол для обычных админов
        "can_manage_roles": is_site_admin(viewer),
    }


@app.get("/api/admin/users/search")
async def admin_search_users(
    q: str = "",
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Поиск пользователя по имени/email/Telegram — для управления аккаунтом."""
    query = db.query(User)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            (User.display_name.ilike(like))
            | (User.email.ilike(like))
            | (User.telegram_username.ilike(like))
            | (User.telegram_first_name.ilike(like))
        )
    users = query.order_by(User.created_at.desc()).limit(30).all()
    return [serialize_admin_user(u, current_user) for u in users]


@app.patch("/api/admin/users/{user_id}")
async def admin_update_user(
    user_id: int,
    req: AdminUserUpdateRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Изменяет аккаунт пользователя: имя, украшение (бейдж), бан, роль (роль — только супер-админ)."""
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if req.display_name is not None:
        target.display_name = req.display_name.strip()[:50] or None

    if req.badge_text is not None:
        target.badge_text = req.badge_text.strip()[:30] or None

    if req.badge_color is not None:
        target.badge_color = req.badge_color.strip() or None
        if target.badge_color:
            notify(db, target, f"🎨 Тебе выдали украшение аккаунта: {target.badge_text or ''}")

    if req.is_banned is not None:
        if is_site_admin(target):
            raise HTTPException(status_code=400, detail="Нельзя ограничить главного администратора")
        target.is_banned = req.is_banned

    if req.role is not None:
        if not is_site_admin(current_user):
            raise HTTPException(status_code=403, detail="Менять роли может только главный администратор")
        if req.role not in ("user", "moderator", "admin"):
            raise HTTPException(status_code=400, detail="Неизвестная роль")
        if is_site_admin(target):
            raise HTTPException(status_code=400, detail="Нельзя изменить роль главного администратора")
        target.role = req.role
        notify(db, target, f"🛡 Твоя роль на сайте изменена: {req.role}")

    db.commit()
    return serialize_admin_user(target, current_user)


# ==================== Публичный профиль пользователя ====================

@app.get("/api/users/{user_id}/public")
async def public_user_profile(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Публичная страница профиля — видна любому вошедшему пользователю.
    Никаких приватных данных (email, telegram id) сюда не попадает."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    level = calc_level(user.xp or 0)
    earned = {
        a.key: a.earned_at
        for a in db.query(UserAchievement).filter(UserAchievement.user_id == user.id).all()
    }
    achievements = [
        {"key": key, "label": info["label"], "desc": info["desc"], "earned_at": earned[key]}
        for key, info in ACHIEVEMENTS.items()
        if key in earned
    ]

    posts = (
        db.query(GalleryPost)
        .filter(GalleryPost.user_id == user.id, GalleryPost.status == "approved")
        .order_by(GalleryPost.created_at.desc())
        .limit(20)
        .all()
    )
    gallery_posts = [
        {
            "id": p.id,
            "content": p.content[:200] + ("…" if len(p.content) > 200 else ""),
            "category": p.category,
            "created_at": p.created_at,
        }
        for p in posts
    ]

    return {
        "id": user.id,
        "name": author_display_name(user),
        "avatar": user.avatar_base64,
        "level": level,
        "level_title": level_title(level),
        "xp": user.xp or 0,
        "current_streak": user.current_streak or 0,
        "badge": author_badge(user),
        "avatar_frame": user.active_frame,
        "name_color": user.active_name_color,
        "is_premium": is_premium_active(db, user),
        "achievements": achievements,
        "gallery_posts": gallery_posts,
        "joined_at": user.created_at,
        "is_me": user.id == current_user.id,
    }


# ==================== Публичный шаринг постов галереи (БЕЗ входа на сайт) ====================
# Цель — виральность: друг видит промпт и красивое превью-ссылки, не заходя на сайт,
# и только после этого решает зарегистрироваться.

CATEGORY_SHARE_LABELS = {
    "suno": "музыки в Suno",
    "image": "картинки",
    "video": "видео",
    "cover": "обложки трека",
    "lyrics": "текста песни",
    "other": "творчества",
}


@app.get("/api/public/gallery/{post_id}")
async def public_gallery_post(post_id: int, db: Session = Depends(get_db)):
    """Пост галереи БЕЗ авторизации — для просмотра по прямой ссылке, без входа на сайт."""
    post = (
        db.query(GalleryPost)
        .options(joinedload(GalleryPost.user))
        .filter(GalleryPost.id == post_id, GalleryPost.status == "approved")
        .first()
    )
    if not post:
        raise HTTPException(status_code=404, detail="Промпт не найден")

    reaction_rows = db.query(GalleryLike).filter(GalleryLike.post_id == post_id).all()
    reactions = {}
    for r in reaction_rows:
        reactions[r.emoji] = reactions.get(r.emoji, 0) + 1

    return {
        "id": post.id,
        "content": post.content,
        "category": post.category,
        "author": author_display_name(post.user),
        "author_avatar": post.user.avatar_base64,
        "author_level": calc_level(post.user.xp or 0),
        "created_at": post.created_at,
        "reactions": reactions,
    }


@app.get("/share/gallery/{post_id}", response_class=HTMLResponse)
async def share_gallery_post(post_id: int, db: Session = Depends(get_db)):
    """
    Лёгкая HTML-страница специально под расшаривание в мессенджеры/соцсети — боты превью
    (Telegram, VK и т.д.) не выполняют JS, поэтому им нужны настоящие <meta property="og:..">
    прямо в HTML. Живому человеку страница сразу перенаправляет на настоящий сайт.
    """
    post = (
        db.query(GalleryPost)
        .options(joinedload(GalleryPost.user))
        .filter(GalleryPost.id == post_id, GalleryPost.status == "approved")
        .first()
    )
    if not post:
        return HTMLResponse("<h1>Промпт не найден или ещё не одобрен</h1>", status_code=404)

    snippet_raw = post.content[:180] + ("…" if len(post.content) > 180 else "")
    author_raw = author_display_name(post.user)
    category_label = CATEGORY_SHARE_LABELS.get(post.category, CATEGORY_SHARE_LABELS["other"])

    snippet = html_module.escape(snippet_raw)
    author = html_module.escape(author_raw)
    site_url = f"https://24promtbot.ru/gallery/{post.id}"
    title = html_module.escape(f"Промпт для {category_label} от {author_raw} — Ботяра")

    page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta property="og:title" content="{title}">
<meta property="og:description" content="{snippet}">
<meta property="og:type" content="website">
<meta property="og:url" content="{site_url}">
<meta property="og:site_name" content="Ботяра">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{snippet}">
<meta http-equiv="refresh" content="0; url={site_url}">
<script>window.location.replace("{site_url}");</script>
<style>body{{font-family:sans-serif;background:#050b16;color:#fff;padding:40px;text-align:center}}</style>
</head>
<body>
<p>Открываю промпт на сайте…</p>
<p><a href="{site_url}" style="color:#8b5cf6">Перейти вручную →</a></p>
</body>
</html>"""
    return HTMLResponse(page)


# ==================== Магазин, инвентарь, XP-паки, Premium-подписка ====================
# Оплата пока идёт вручную (нет подключённого платёжного шлюза ЮKassa — ждём реальные ключи) —
# заявка на покупку уведомляет админа, после получения оплаты вне сайта он подтверждает её
# в админке, и покупка применяется к аккаунту автоматически и безопасно (только сервером).

PREMIUM_PLANS = {
    "premium_1m": {"name": "💎 Ботяра Premium — 1 месяц", "price": 149, "days": 30},
    "premium_1y": {"name": "💎 Ботяра Premium — 1 год", "price": 1190, "days": 365},
}

SHOP_PACKAGES = {
    "starter": {
        "name": "Стартовый набор украшений",
        "description": "Огненная рамка, золотой ник и титул «Легенда»",
        "price": 39,
        "original_price": 67,
        "item_keys": ["frame_fire", "name_gold", "title_legend"],
    },
}


def seed_shop_items(db: Session) -> None:
    """Добавляет недостающие стандартные товары без изменения существующих позиций."""

    items = [
        # ---- Рамки для аватарки (CSS) ----
        {"key": "frame_rose", "name": "🌸 Розовая рамка", "category": "frame", "price": 19, "css_value": "rose"},
        {"key": "frame_ocean", "name": "🌊 Океан", "category": "frame", "price": 19, "css_value": "ocean"},
        {"key": "frame_fire", "name": "🔥 Огненная рамка", "category": "frame", "price": 29, "css_value": "fire"},
        {"key": "frame_electro", "name": "⚡ Электро", "category": "frame", "price": 29, "css_value": "electro"},
        {"key": "frame_magenta", "name": "💜 Неон-магента", "category": "frame", "price": 35, "css_value": "magenta"},
        {"key": "frame_cyber", "name": "🌀 Кибер-циан", "category": "frame", "price": 35, "css_value": "cyber"},
        {"key": "frame_toxic", "name": "☣️ Токсик", "category": "frame", "price": 35, "css_value": "toxic"},
        {"key": "frame_danger", "name": "🚨 Опасность", "category": "frame", "price": 39, "css_value": "danger"},
        {"key": "frame_rainbow", "name": "🌈 Радужная (анимация)", "category": "frame", "price": 49, "css_value": "rainbow"},
        {"key": "frame_stardust", "name": "✨ Звёздная пыль (анимация)", "category": "frame", "price": 49, "css_value": "stardust"},
        {"key": "frame_gold", "name": "👑 Королевская (золото)", "category": "frame", "price": 59, "css_value": "gold"},
        {"key": "frame_crystal", "name": "💎 Кристальная (анимация)", "category": "frame", "price": 79, "css_value": "crystal"},
        # ---- Музыкальная коллекция ----
        {"key": "frame_music_rock", "name": "🎸 Rock", "category": "frame", "price": 49, "css_value": "music-rock"},
        {"key": "frame_music_hiphop", "name": "⛓ Hip-Hop", "category": "frame", "price": 59, "css_value": "music-hiphop"},
        {"key": "frame_music_rap", "name": "🎙 Rap", "category": "frame", "price": 49, "css_value": "music-rap"},
        {"key": "frame_music_blues", "name": "🎤 Blues", "category": "frame", "price": 49, "css_value": "music-blues"},
        {"key": "frame_music_jazz", "name": "🎷 Jazz", "category": "frame", "price": 59, "css_value": "music-jazz"},
        {"key": "frame_music_classical", "name": "🎻 Classical", "category": "frame", "price": 59, "css_value": "music-classical"},
        {"key": "frame_music_electronic", "name": "🎛 Electronic", "category": "frame", "price": 69, "css_value": "music-electronic"},
        {"key": "frame_music_metal", "name": "☠ Metal", "category": "frame", "price": 69, "css_value": "music-metal"},
        {"key": "frame_music_reggae", "name": "🦁 Reggae", "category": "frame", "price": 59, "css_value": "music-reggae"},
        {"key": "frame_music_country", "name": "🤠 Country", "category": "frame", "price": 49, "css_value": "music-country"},
        # ---- Коллекция уровней ----
        {"key": "frame_level_novice", "name": "🌿 Новичок", "category": "frame", "price": 19, "css_value": "level-novice"},
        {"key": "frame_level_student", "name": "💠 Ученик", "category": "frame", "price": 25, "css_value": "level-student"},
        {"key": "frame_level_active", "name": "🏅 Активный", "category": "frame", "price": 29, "css_value": "level-active"},
        {"key": "frame_level_explorer", "name": "🔷 Исследователь", "category": "frame", "price": 35, "css_value": "level-explorer"},
        {"key": "frame_level_curious", "name": "🔮 Пытливый ум", "category": "frame", "price": 39, "css_value": "level-curious"},
        {"key": "frame_level_observer", "name": "💚 Наблюдатель", "category": "frame", "price": 39, "css_value": "level-observer"},
        {"key": "frame_level_enthusiast", "name": "✨ Энтузиаст", "category": "frame", "price": 45, "css_value": "level-enthusiast"},
        {"key": "frame_level_creator", "name": "🟣 Создатель", "category": "frame", "price": 49, "css_value": "level-creator"},
        {"key": "frame_level_analyst", "name": "🔹 Аналитик", "category": "frame", "price": 49, "css_value": "level-analyst"},
        {"key": "frame_level_ai_explorer", "name": "🌐 Исследователь AI", "category": "frame", "price": 59, "css_value": "level-ai-explorer"},
        {"key": "frame_level_pioneer", "name": "🔥 Первопроходец", "category": "frame", "price": 69, "css_value": "level-pioneer"},
        {"key": "frame_level_master", "name": "❄ Мастер знаний", "category": "frame", "price": 79, "css_value": "level-master"},
        {"key": "frame_level_visionary", "name": "🔮 Визионер", "category": "frame", "price": 89, "css_value": "level-visionary"},
        {"key": "frame_level_legend", "name": "👑 Легенда", "category": "frame", "price": 99, "css_value": "level-legend"},
        {"key": "frame_level_ai_lord", "name": "🌈 Повелитель AI", "category": "frame", "price": 119, "css_value": "level-ai-lord"},
        # ---- Неоновая коллекция ----
        {"key": "frame_neon_magenta_square", "name": "💗 Magenta Square", "category": "frame", "price": 49, "css_value": "neon-magenta-square"},
        {"key": "frame_neon_magenta_round", "name": "🟣 Magenta Ring", "category": "frame", "price": 49, "css_value": "neon-magenta-round"},
        {"key": "frame_neon_cyan_square", "name": "🧊 Cyan Tech", "category": "frame", "price": 55, "css_value": "neon-cyan-square"},
        {"key": "frame_neon_danger", "name": "🚨 Red Danger", "category": "frame", "price": 59, "css_value": "neon-danger"},
        {"key": "frame_neon_cyan_round", "name": "🌀 Cyber Ring", "category": "frame", "price": 55, "css_value": "neon-cyan-round"},
        {"key": "frame_neon_violet_square", "name": "💜 Violet Signal", "category": "frame", "price": 59, "css_value": "neon-violet-square"},
        {"key": "frame_neon_pink_square", "name": "🌺 Pink 03", "category": "frame", "price": 55, "css_value": "neon-pink-square"},
        {"key": "frame_neon_orange_round", "name": "🟠 Amber Ring", "category": "frame", "price": 55, "css_value": "neon-orange-round"},
        {"key": "frame_neon_toxic", "name": "☣ Toxic Grid", "category": "frame", "price": 59, "css_value": "neon-toxic"},
        {"key": "frame_neon_blue_square", "name": "🔵 Blue Signal", "category": "frame", "price": 59, "css_value": "neon-blue-square"},
        # ---- Цвет ника ----
        {"key": "name_red", "name": "❤️ Красный ник", "category": "name_color", "price": 15, "css_value": "#f87171"},
        {"key": "name_blue", "name": "💙 Синий ник", "category": "name_color", "price": 15, "css_value": "#60a5fa"},
        {"key": "name_green", "name": "💚 Зелёный ник", "category": "name_color", "price": 15, "css_value": "#4ade80"},
        {"key": "name_gold", "name": "💛 Золотой ник", "category": "name_color", "price": 19, "css_value": "#facc15"},
        {"key": "name_violet", "name": "💜 Фиолетовый ник", "category": "name_color", "price": 15, "css_value": "#a78bfa"},
        # ---- Титулы ----
        {"key": "title_legend", "name": "🏆 Легенда", "category": "title", "price": 19, "badge_text": "🏆 Легенда", "badge_color": "#facc15"},
        {"key": "title_vip", "name": "💎 VIP", "category": "title", "price": 29, "badge_text": "💎 VIP", "badge_color": "#22d3ee"},
        {"key": "title_boss", "name": "😎 Босс движухи", "category": "title", "price": 19, "badge_text": "😎 Босс движухи", "badge_color": "#fb7185"},
        {"key": "title_founder", "name": "🚀 Первопроходец", "category": "title", "price": 25, "badge_text": "🚀 Первопроходец", "badge_color": "#4ade80"},
        # ---- XP-паки ----
        {"key": "xp_small", "name": "⚡ 200 XP", "category": "xp", "price": 29, "xp_amount": 200},
        {"key": "xp_medium", "name": "⚡ 600 XP", "category": "xp", "price": 69, "xp_amount": 600},
        {"key": "xp_large", "name": "⚡ 1500 XP", "category": "xp", "price": 149, "xp_amount": 1500},
    ]
    existing_keys = {row[0] for row in db.query(ShopItem.key).all()}
    added = 0
    for i, data in enumerate(items):
        if data["key"] in existing_keys:
            continue
        db.add(ShopItem(sort_order=i, is_active=True, **data))
        added += 1
    if added:
        db.commit()
        logging.info(f"Магазин: добавлено {added} новых товаров")


def get_active_subscription(db: Session, user: User):
    return (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id, Subscription.expires_at > datetime.now(timezone.utc))
        .order_by(Subscription.expires_at.desc())
        .first()
    )


def is_premium_active(db: Session, user: User) -> bool:
    return get_active_subscription(db, user) is not None


def apply_purchased_item(db: Session, user: User, purchase: ShopPurchase) -> None:
    """Применяет купленное к аккаунту. Единственное место, где это происходит — только
    после подтверждения оплаты админом. Пользователь никак не может вызвать это сам за
    себя без реального подтверждённого платежа."""
    if purchase.plan and purchase.plan.startswith("package:"):
        package = SHOP_PACKAGES.get(purchase.plan.split(":", 1)[1])
        if not package:
            return
        items = db.query(ShopItem).filter(ShopItem.key.in_(package["item_keys"])).all()
        for item in items:
            exists = db.query(UserInventoryItem).filter(
                UserInventoryItem.user_id == user.id,
                UserInventoryItem.shop_item_id == item.id,
            ).first()
            if not exists:
                db.add(UserInventoryItem(user_id=user.id, shop_item_id=item.id))
        return

    if purchase.plan:
        plan_info = PREMIUM_PLANS.get(purchase.plan)
        days = plan_info["days"] if plan_info else 30
        existing = get_active_subscription(db, user)
        base = existing.expires_at if existing else datetime.now(timezone.utc)
        if existing:
            existing.expires_at = base + timedelta(days=days)
            existing.auto_renew = True
        else:
            db.add(Subscription(user_id=user.id, plan="premium", expires_at=base + timedelta(days=days)))
        return

    item = purchase.item
    if not item:
        return
    if item.category == "xp":
        add_xp(db, user, item.xp_amount or 0)
        return

    existing_inv = (
        db.query(UserInventoryItem)
        .filter(UserInventoryItem.user_id == user.id, UserInventoryItem.shop_item_id == item.id)
        .first()
    )
    if not existing_inv:
        db.add(UserInventoryItem(user_id=user.id, shop_item_id=item.id))


def serialize_shop_item(item: ShopItem) -> dict:
    price = item.price
    final_price = round(price * (100 - item.discount_percent) / 100) if item.discount_percent else price
    return {
        "id": item.id,
        "key": item.key,
        "name": item.name,
        "description": item.description,
        "category": item.category,
        "price": final_price,
        "original_price": price if item.discount_percent else None,
        "discount_percent": item.discount_percent,
        "css_value": item.css_value,
        "badge_text": item.badge_text,
        "badge_color": item.badge_color,
        "xp_amount": item.xp_amount,
        "is_active": item.is_active,
    }


class ShopPurchaseRequest(BaseModel):
    item_key: str | None = None
    plan: str | None = None


class ShopEquipRequest(BaseModel):
    category: str  # frame / name_color / title
    shop_item_id: int | None = None  # None для unequip


@app.get("/api/shop/catalog")
async def shop_catalog(db: Session = Depends(get_db)):
    """Каталог магазина — доступен всем вошедшим. Только активные товары."""
    items = db.query(ShopItem).filter(ShopItem.is_active == True).order_by(ShopItem.sort_order).all()  # noqa: E712
    return {
        "items": [serialize_shop_item(i) for i in items],
        "premium_plans": PREMIUM_PLANS,
        "packages": SHOP_PACKAGES,
        "purchases_enabled": SHOP_PURCHASES_ENABLED,
    }


@app.get("/api/shop/inventory")
async def shop_inventory(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Всё, чем реально владеет пользователь, плюс что сейчас надето."""
    rows = (
        db.query(UserInventoryItem)
        .options(joinedload(UserInventoryItem.item))
        .filter(UserInventoryItem.user_id == current_user.id)
        .all()
    )
    return {
        "items": [serialize_shop_item(r.item) for r in rows if r.item],
        "active_frame": current_user.active_frame,
        "active_name_color": current_user.active_name_color,
    }


@app.post("/api/shop/equip")
async def shop_equip(
    req: ShopEquipRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Надеть/снять купленное украшение. Владение проверяется на сервере — подделать нельзя."""
    if req.category not in ("frame", "name_color", "title"):
        raise HTTPException(status_code=400, detail="Неизвестная категория")

    if req.shop_item_id is None:
        # Снять
        if req.category == "frame":
            current_user.active_frame = None
        elif req.category == "name_color":
            current_user.active_name_color = None
        elif req.category == "title":
            current_user.badge_text = None
            current_user.badge_color = None
        db.commit()
        return {"status": "unequipped"}

    owned = (
        db.query(UserInventoryItem)
        .join(ShopItem, UserInventoryItem.shop_item_id == ShopItem.id)
        .filter(
            UserInventoryItem.user_id == current_user.id,
            UserInventoryItem.shop_item_id == req.shop_item_id,
            ShopItem.category == req.category,
        )
        .first()
    )
    if not owned:
        raise HTTPException(status_code=403, detail="Этот предмет не куплен")

    item = owned.item
    if req.category == "frame":
        current_user.active_frame = item.css_value
    elif req.category == "name_color":
        current_user.active_name_color = item.css_value
    elif req.category == "title":
        current_user.badge_text = item.badge_text
        current_user.badge_color = item.badge_color
    db.commit()
    return {"status": "equipped"}


@app.get("/api/shop/subscription")
async def shop_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sub = get_active_subscription(db, current_user)
    return {
        "is_active": sub is not None,
        "expires_at": sub.expires_at if sub else None,
        "auto_renew": sub.auto_renew if sub else None,
    }


@app.get("/api/shop/my-purchases")
async def shop_my_purchases(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(ShopPurchase)
        .filter(ShopPurchase.user_id == current_user.id)
        .order_by(ShopPurchase.created_at.desc())
        .all()
    )
    return [
        {
            "id": p.id,
            "item_id": p.shop_item_id,
            "item_key": p.item.key if p.item else p.plan,
            "item_name": p.item_name,
            "price": p.price,
            "status": p.status,
            "created_at": p.created_at,
        }
        for p in rows
    ]


@app.post("/api/shop/purchase")
async def shop_purchase(
    req: ShopPurchaseRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Оформляет заявку на покупку (товар или Premium). Реальную оплату обсуждаете отдельно
    (кнопка «Связь со мной») — после получения денег админ подтверждает заявку в панели."""
    if not SHOP_PURCHASES_ENABLED:
        raise HTTPException(status_code=503, detail="Purchases are temporarily unavailable until YooKassa is connected")

    shop_item = None
    plan = None

    if req.plan:
        plan_info = PREMIUM_PLANS.get(req.plan)
        if not plan_info:
            raise HTTPException(status_code=404, detail="Такого плана подписки нет")
        item_name, price, plan = plan_info["name"], plan_info["price"], req.plan
    elif req.item_key and req.item_key.startswith("package:"):
        package_key = req.item_key.split(":", 1)[1]
        package = SHOP_PACKAGES.get(package_key)
        if not package:
            raise HTTPException(status_code=404, detail="Package not found")
        item_name, price, plan = package["name"], package["price"], req.item_key
        owned_keys = {
            row[0]
            for row in db.query(ShopItem.key)
            .join(UserInventoryItem, UserInventoryItem.shop_item_id == ShopItem.id)
            .filter(UserInventoryItem.user_id == current_user.id, ShopItem.key.in_(package["item_keys"]))
            .all()
        }
        if owned_keys == set(package["item_keys"]):
            raise HTTPException(status_code=400, detail="You already own every item in this package")
    elif req.item_key:
        shop_item = db.query(ShopItem).filter(ShopItem.key == req.item_key, ShopItem.is_active == True).first()  # noqa: E712
        if not shop_item:
            raise HTTPException(status_code=404, detail="Товар не найден")
        item_name = shop_item.name
        price = round(shop_item.price * (100 - shop_item.discount_percent) / 100) if shop_item.discount_percent else shop_item.price
    else:
        raise HTTPException(status_code=400, detail="Не указан товар или план подписки")

    if shop_item and shop_item.category != "xp" and shop_item.category != "premium":
        already_owned = (
            db.query(UserInventoryItem)
            .filter(UserInventoryItem.user_id == current_user.id, UserInventoryItem.shop_item_id == shop_item.id)
            .first()
        )
        if already_owned:
            raise HTTPException(status_code=400, detail="У тебя уже есть этот предмет")

    existing_pending = db.query(ShopPurchase).filter(
        ShopPurchase.user_id == current_user.id,
        ShopPurchase.status == "pending",
        ShopPurchase.item_name == item_name,
    ).first()
    if existing_pending:
        raise HTTPException(status_code=400, detail="Заявка на это уже отправлена, ждём подтверждения оплаты")

    purchase = ShopPurchase(
        user_id=current_user.id,
        shop_item_id=shop_item.id if shop_item else None,
        item_name=item_name,
        price=price,
        plan=plan,
    )
    db.add(purchase)
    db.commit()
    db.refresh(purchase)
    logging.info(f"[shop] Заявка #{purchase.id}: {author_display_name(current_user)} -> «{item_name}» за {price}₽")

    if ADMIN_TELEGRAM_ID:
        admin = db.query(User).filter(User.telegram_id == ADMIN_TELEGRAM_ID).first()
        if admin:
            notify(db, admin, f"🛍 Новая заявка на покупку: {author_display_name(current_user)} хочет «{item_name}» за {price}₽")

    return {"status": "pending", "purchase_id": purchase.id, "item_name": item_name, "price": price}


# ---- Админ: управление товарами ----

class ShopItemCreateRequest(BaseModel):
    key: str
    name: str
    description: str | None = None
    category: str
    price: int
    discount_percent: int = 0
    css_value: str | None = None
    badge_text: str | None = None
    badge_color: str | None = None
    xp_amount: int | None = None
    is_active: bool = True


class ShopItemUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    price: int | None = None
    discount_percent: int | None = None
    css_value: str | None = None
    badge_text: str | None = None
    badge_color: str | None = None
    xp_amount: int | None = None
    is_active: bool | None = None


@app.get("/api/admin/shop/items")
async def admin_shop_items(current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    items = db.query(ShopItem).order_by(ShopItem.category, ShopItem.sort_order).all()
    return [serialize_shop_item(i) | {"key": i.key} for i in items]


@app.post("/api/admin/shop/items")
async def admin_create_shop_item(
    req: ShopItemCreateRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.query(ShopItem).filter(ShopItem.key == req.key).first():
        raise HTTPException(status_code=400, detail="Товар с таким ключом уже есть")
    item = ShopItem(**req.dict())
    db.add(item)
    db.commit()
    db.refresh(item)
    return serialize_shop_item(item)


@app.patch("/api/admin/shop/items/{item_id}")
async def admin_update_shop_item(
    item_id: int,
    req: ShopItemUpdateRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    item = db.query(ShopItem).filter(ShopItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Товар не найден")
    for field, value in req.dict(exclude_unset=True).items():
        setattr(item, field, value)
    db.commit()
    return serialize_shop_item(item)


@app.delete("/api/admin/shop/items/{item_id}")
async def admin_delete_shop_item(
    item_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    item = db.query(ShopItem).filter(ShopItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Товар не найден")
    owned_count = db.query(UserInventoryItem).filter(UserInventoryItem.shop_item_id == item_id).count()
    if owned_count > 0:
        item.is_active = False
        db.commit()
        return {"status": "deactivated", "reason": f"Товар уже куплен {owned_count} пользователями — скрыт вместо удаления"}
    db.delete(item)
    db.commit()
    return {"status": "deleted"}


# ---- Админ: заявки на покупку ----

@app.get("/api/admin/shop/purchases")
async def admin_shop_purchases(
    status: str = "pending",
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(ShopPurchase).options(joinedload(ShopPurchase.user))
    if status != "all":
        query = query.filter(ShopPurchase.status == status)
    rows = query.order_by(ShopPurchase.created_at.desc()).limit(150).all()
    return [
        {
            "id": p.id,
            "user_id": p.user_id,
            "user_name": author_display_name(p.user),
            "item_name": p.item_name,
            "price": p.price,
            "status": p.status,
            "created_at": p.created_at,
        }
        for p in rows
    ]


@app.post("/api/admin/shop/purchases/{purchase_id}/fulfill")
async def admin_fulfill_purchase(
    purchase_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    purchase = db.query(ShopPurchase).options(joinedload(ShopPurchase.item)).filter(ShopPurchase.id == purchase_id).first()
    if not purchase:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    if purchase.status != "pending":
        raise HTTPException(status_code=400, detail="Заявка уже обработана")

    target = db.query(User).filter(User.id == purchase.user_id).first()
    if target:
        apply_purchased_item(db, target, purchase)
        notify(db, target, f"✅ Оплата подтверждена! «{purchase.item_name}» уже у тебя 🎉")
    purchase.status = "fulfilled"
    db.commit()
    logging.info(f"[shop] Заявка #{purchase.id} подтверждена админом {author_display_name(current_user)}")
    return {"status": "fulfilled"}


@app.post("/api/admin/shop/purchases/{purchase_id}/cancel")
async def admin_cancel_purchase(
    purchase_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    purchase = db.query(ShopPurchase).filter(ShopPurchase.id == purchase_id).first()
    if not purchase:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    purchase.status = "cancelled"
    db.commit()
    logging.info(f"[shop] Заявка #{purchase.id} отменена админом {author_display_name(current_user)}")
    return {"status": "cancelled"}


@app.get("/api/admin/shop/stats")
async def admin_shop_stats(current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    fulfilled = db.query(ShopPurchase).filter(ShopPurchase.status == "fulfilled").all()
    total_revenue = sum(p.price for p in fulfilled)
    by_item: dict[str, dict] = {}
    for p in fulfilled:
        bucket = by_item.setdefault(p.item_name, {"item_name": p.item_name, "count": 0, "revenue": 0})
        bucket["count"] += 1
        bucket["revenue"] += p.price
    active_subs = db.query(Subscription).filter(Subscription.expires_at > datetime.now(timezone.utc)).count()
    return {
        "total_revenue": total_revenue,
        "total_sales": len(fulfilled),
        "pending_count": db.query(ShopPurchase).filter(ShopPurchase.status == "pending").count(),
        "active_subscriptions": active_subs,
        "by_item": sorted(by_item.values(), key=lambda x: -x["revenue"]),
    }
