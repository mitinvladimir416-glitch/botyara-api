"""
API-сервер для сайта botyara.ru.
Переиспользует ту же AI-логику, что и Telegram-бот (см. ai_service.py).

Запуск локально:
    pip install -r requirements.txt
    uvicorn main:app --reload

На Timeweb Cloud App Platform команда запуска обычно определяется автоматически
из requirements.txt + Procfile/настроек — см. README.md.
"""

import base64
import logging
import math
import os
import secrets
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

import ai_service
import auth
from database import (
    get_db,
    init_db,
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
)

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Botyara API", version="0.2.0")

# Секрет для проверки, что запросы на /api/bot/* приходят именно от нашего Telegram-бота,
# а не от кого попало. Должен совпадать со значением BOT_INTERNAL_SECRET в Railway (у бота).
BOT_INTERNAL_SECRET = os.getenv("BOT_INTERNAL_SECRET")

# Telegram ID администратора сайта — тот же человек, что ADMIN_ID у бота. Аккаунт с таким
# telegram_id получает права модератора (может чистить общий чат/галерею).
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")


def is_site_admin(user: User) -> bool:
    return bool(ADMIN_TELEGRAM_ID) and user.telegram_id == ADMIN_TELEGRAM_ID


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
    "liked_10": {"label": "❤️ Народная любовь", "desc": "Твои посты в сумме набрали 10 лайков"},
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


# CORS — пока разрешаем запросы с любого источника (на этапе разработки).
# Когда появится домен сайта, сузим список до конкретного домена (https://botyara.ru).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


class RoomCreateRequest(BaseModel):
    category: str = "other"  # suno/image/video/other


class RoomJoinRequest(BaseModel):
    code: str


class RoomMessageRequest(BaseModel):
    content: str
    channel: str = "ai"  # "ai" — чат с нейросетью, "team" — приватное обсуждение участников


class GalleryPublishRequest(BaseModel):
    favorite_id: int


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


class BotPublicChatRequest(BaseModel):
    telegram_id: int
    telegram_username: str | None = None
    telegram_first_name: str | None = None
    content: str


# ==================== Авторизация: вспомогательное ====================

security = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """
    Зависимость FastAPI: проверяет токен из заголовка "Authorization: Bearer <токен>"
    и возвращает текущего пользователя. Если токена нет или он невалиден — ошибка 401.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Нужна авторизация")

    user_id = auth.decode_access_token(credentials.credentials)
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
    """Зависимость FastAPI: пускает только администратора сайта (см. is_site_admin)."""
    if not is_site_admin(current_user):
        raise HTTPException(status_code=403, detail="Доступно только администратору")
    return current_user


def verify_bot_secret(x_bot_secret: str | None = Header(default=None)):
    """
    Зависимость FastAPI: проверяет, что запрос на /api/bot/* пришёл от нашего бота
    (секрет передаётся в заголовке X-Bot-Secret и должен совпадать с BOT_INTERNAL_SECRET).
    """
    if not BOT_INTERNAL_SECRET:
        raise HTTPException(status_code=500, detail="BOT_INTERNAL_SECRET не настроен на сервере")
    if x_bot_secret != BOT_INTERNAL_SECRET:
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
    """Проверка, что сервис жив (используем для мониторинга)."""
    return {"status": "ok"}


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


@app.post("/api/prompt/image-from-photo")
async def prompt_image_from_photo(
    desired_change: str = Form(...),
    photo: UploadFile = File(...),
):
    """Анализ фото + составление промпта для его правки (раздел Промпты → Картинка)."""
    photo_bytes = await photo.read()
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
        first_b64 = base64.b64encode(await first_frame.read()).decode("utf-8")
    if last_frame is not None:
        last_b64 = base64.b64encode(await last_frame.read()).decode("utf-8")

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
        tmp.write(await audio.read())
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
async def register(req: RegisterRequest, db: Session = Depends(get_db)):
    """Регистрация по email и паролю."""
    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Пользователь с таким email уже зарегистрирован")

    user = User(email=req.email, password_hash=auth.hash_password(req.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    token = auth.create_access_token(user.id)
    return {"access_token": token, "user": {"id": user.id, "email": user.email}}


@app.post("/api/auth/login")
async def login(req: LoginRequest, db: Session = Depends(get_db)):
    """Вход по email и паролю."""
    user = db.query(User).filter(User.email == req.email).first()
    if not user or not user.password_hash or not auth.verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    token = auth.create_access_token(user.id)
    return {"access_token": token, "user": {"id": user.id, "email": user.email}}


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
async def telegram_login_start():
    """Генерирует одноразовый токен для входа через бота — сайт покажет ссылку и начнёт опрос."""
    token = secrets.token_urlsafe(24)
    pending_telegram_logins[token] = {"status": "pending", "created_at": time.time()}
    return {"token": token, "bot_username": BOT_USERNAME}


@app.get("/api/auth/telegram/poll")
async def telegram_login_poll(token: str, db: Session = Depends(get_db)):
    """Сайт опрашивает это раз в пару секунд, пока пользователь не подтвердит вход через бота."""
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
        "is_admin": is_site_admin(current_user),
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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Список опубликованных (прошедших модерацию) промптов, новые сверху."""
    posts = (
        db.query(GalleryPost)
        .filter(GalleryPost.status == "approved")
        .order_by(GalleryPost.created_at.desc())
        .limit(50)
        .all()
    )
    result = []
    for p in posts:
        comment_count = (
            db.query(GalleryComment)
            .filter(GalleryComment.post_id == p.id, GalleryComment.status == "approved")
            .count()
        )
        like_count = db.query(GalleryLike).filter(GalleryLike.post_id == p.id).count()
        liked_by_me = (
            db.query(GalleryLike)
            .filter(GalleryLike.post_id == p.id, GalleryLike.user_id == current_user.id)
            .first()
            is not None
        )
        result.append(
            {
                "id": p.id,
                "content": p.content,
                "category": p.category,
                "author": author_display_name(p.user),
                "author_avatar": p.user.avatar_base64,
                "author_level": calc_level(p.user.xp or 0),
                "created_at": p.created_at,
                "comment_count": comment_count,
                "like_count": like_count,
                "liked_by_me": liked_by_me,
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
        .filter(GalleryComment.post_id == post_id, GalleryComment.status == "approved")
        .order_by(GalleryComment.created_at.asc())
        .all()
    )
    like_count = db.query(GalleryLike).filter(GalleryLike.post_id == post_id).count()
    liked_by_me = (
        db.query(GalleryLike)
        .filter(GalleryLike.post_id == post_id, GalleryLike.user_id == current_user.id)
        .first()
        is not None
    )
    return {
        "id": post.id,
        "content": post.content,
        "category": post.category,
        "author": author_display_name(post.user),
        "author_avatar": post.user.avatar_base64,
        "author_level": calc_level(post.user.xp or 0),
        "created_at": post.created_at,
        "is_mine": post.user_id == current_user.id,
        "like_count": like_count,
        "liked_by_me": liked_by_me,
        "comments": [
            {
                "id": c.id,
                "content": c.content,
                "author": author_display_name(c.user),
                "author_avatar": c.user.avatar_base64,
                "author_level": calc_level(c.user.xp or 0),
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
    if not is_site_admin(current_user):
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
    if not is_site_admin(current_user):
        query = query.filter(GalleryComment.user_id == current_user.id)
    comment = query.first()
    if not comment:
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    db.delete(comment)
    db.commit()
    return {"status": "deleted"}


@app.post("/api/gallery/{post_id}/like")
async def gallery_toggle_like(
    post_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ставит лайк, если его ещё не было, либо убирает его (тогл)."""
    post = db.query(GalleryPost).filter(GalleryPost.id == post_id, GalleryPost.status == "approved").first()
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")

    existing = (
        db.query(GalleryLike)
        .filter(GalleryLike.post_id == post_id, GalleryLike.user_id == current_user.id)
        .first()
    )
    if existing:
        db.delete(existing)
        db.commit()
        liked = False
    else:
        db.add(GalleryLike(post_id=post_id, user_id=current_user.id))
        db.commit()
        liked = True

        if post.user_id != current_user.id:
            owner = db.query(User).filter(User.id == post.user_id).first()
            if owner:
                add_xp(db, owner, 3)
                notify(db, owner, f"❤️ {author_display_name(current_user)} оценил(а) твой промпт в галерее")
                total_likes_received = (
                    db.query(GalleryLike)
                    .join(GalleryPost, GalleryLike.post_id == GalleryPost.id)
                    .filter(GalleryPost.user_id == owner.id)
                    .count()
                )
                if total_likes_received >= 10:
                    grant_achievement(db, owner, "liked_10")

    like_count = db.query(GalleryLike).filter(GalleryLike.post_id == post_id).count()
    return {"liked": liked, "like_count": like_count}


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
    """Последние оповещения об обновлениях — публикует администратор через /announce в боте."""
    items = db.query(Announcement).order_by(Announcement.created_at.desc()).limit(20).all()
    return [{"id": a.id, "content": a.content, "created_at": a.created_at} for a in items]


@app.post("/api/bot/announcements", dependencies=[Depends(verify_bot_secret)])
async def bot_save_announcement(req: BotAnnouncementRequest, db: Session = Depends(get_db)):
    """Бот вызывает это после рассылки /announce — чтобы то же оповещение появилось на сайте."""
    item = Announcement(content=req.content)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id}


# ==================== Общий публичный чат (требует авторизации сайта) ====================

@app.get("/api/public-chat")
async def public_chat_list(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Последние 50 одобренных сообщений общего чата, от старых к новым."""
    messages = (
        db.query(PublicChatMessage)
        .filter(PublicChatMessage.status == "approved")
        .order_by(PublicChatMessage.created_at.desc())
        .limit(50)
        .all()
    )
    messages.reverse()
    return [
        {
            "id": m.id,
            "content": m.content,
            "author": author_display_name(m.user),
            "author_avatar": m.user.avatar_base64,
            "author_level": calc_level(m.user.xp or 0),
            "created_at": m.created_at,
            "is_mine": m.user_id == current_user.id,
        }
        for m in messages
    ]


@app.post("/api/public-chat")
async def public_chat_send(
    req: PublicChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Отправляет сообщение в общий чат — проходит модерацию перед показом всем."""
    allowed, reason = ai_service.moderate_text(req.content)
    message = PublicChatMessage(
        user_id=current_user.id,
        content=req.content,
        status="approved" if allowed else "rejected",
        reject_reason=None if allowed else reason,
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    if allowed:
        add_xp(db, current_user, 2)
    return {"id": message.id, "status": message.status, "reject_reason": message.reject_reason}


@app.delete("/api/public-chat/{message_id}")
async def public_chat_delete(
    message_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удаляет сообщение из общего чата — доступно автору сообщения или администратору."""
    message = db.query(PublicChatMessage).filter(PublicChatMessage.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    if message.user_id != current_user.id and not is_site_admin(current_user):
        raise HTTPException(status_code=403, detail="Удалить можно только своё сообщение")
    db.delete(message)
    db.commit()
    return {"status": "deleted"}


@app.delete("/api/public-chat")
async def public_chat_clear(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Полностью очищает общий чат — только для администратора."""
    if not is_site_admin(current_user):
        raise HTTPException(status_code=403, detail="Только администратор может очистить общий чат")
    db.query(PublicChatMessage).delete()
    db.commit()
    return {"status": "cleared"}


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

    for p in db.query(GalleryPost).order_by(GalleryPost.created_at.desc()).limit(20).all():
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

    for c in db.query(GalleryComment).order_by(GalleryComment.created_at.desc()).limit(20).all():
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

    for m in db.query(PublicChatMessage).order_by(PublicChatMessage.created_at.desc()).limit(20).all():
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
    participants = db.query(RoomParticipant).filter(RoomParticipant.room_id == room.id).all()
    messages = db.query(RoomMessage).filter(RoomMessage.room_id == room.id).order_by(RoomMessage.created_at.asc()).all()
    return {
        "code": room.code,
        "category": room.category,
        "status": room.status,
        "final_content": room.final_content,
        "is_owner": room.created_by == current_user.id,
        "participants": [
            {
                "id": p.user.id,
                "name": author_display_name(p.user),
                "avatar": p.user.avatar_base64,
                "level": calc_level(p.user.xp or 0),
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
