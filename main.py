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
import os
import tempfile

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

import ai_service
import auth
from database import get_db, init_db, User, Favorite, Message, GalleryPost, GalleryComment, PublicChatMessage

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Botyara API", version="0.2.0")

# Секрет для проверки, что запросы на /api/bot/* приходят именно от нашего Telegram-бота,
# а не от кого попало. Должен совпадать со значением BOT_INTERNAL_SECRET в Railway (у бота).
BOT_INTERNAL_SECRET = os.getenv("BOT_INTERNAL_SECRET")


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


class TelegramAuthRequest(BaseModel):
    # Поля, которые присылает Telegram Login Widget
    id: int
    first_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int
    hash: str


class FavoriteCreateRequest(BaseModel):
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


class BotFavoriteDeleteRequest(BaseModel):
    telegram_id: int
    favorite_id: int


class GalleryPublishRequest(BaseModel):
    favorite_id: int


class GalleryCommentRequest(BaseModel):
    content: str


class BotGalleryPublishRequest(BaseModel):
    telegram_id: int
    telegram_username: str | None = None
    telegram_first_name: str | None = None
    content: str


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

    return user


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


@app.post("/api/auth/telegram")
async def telegram_login(req: TelegramAuthRequest, db: Session = Depends(get_db)):
    """Вход через Telegram Login Widget. Если пользователь с таким Telegram ID уже есть — логиним его,
    иначе создаём нового."""
    if not auth.verify_telegram_login(req.model_dump()):
        raise HTTPException(status_code=401, detail="Не удалось подтвердить данные от Telegram")

    telegram_id = str(req.id)
    user = db.query(User).filter(User.telegram_id == telegram_id).first()

    if user is None:
        user = User(
            telegram_id=telegram_id,
            telegram_username=req.username,
            telegram_first_name=req.first_name,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Обновляем актуальные данные профиля на случай, если человек их поменял в Telegram
        user.telegram_username = req.username
        user.telegram_first_name = req.first_name
        db.commit()

    token = auth.create_access_token(user.id)
    return {
        "access_token": token,
        "user": {
            "id": user.id,
            "telegram_username": user.telegram_username,
            "telegram_first_name": user.telegram_first_name,
        },
    }


@app.get("/api/me")
async def get_me(current_user: User = Depends(get_current_user)):
    """Возвращает данные текущего залогиненного пользователя (проверка, что токен рабочий)."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "telegram_username": current_user.telegram_username,
        "telegram_first_name": current_user.telegram_first_name,
        "display_name": current_user.display_name,
        "avatar_base64": current_user.avatar_base64,
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
    return [{"id": f.id, "content": f.content, "created_at": f.created_at} for f in favorites]


@app.post("/api/favorites")
async def add_favorite(
    req: FavoriteCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Сохраняет промпт в избранное текущего пользователя."""
    favorite = Favorite(user_id=current_user.id, content=req.content)
    db.add(favorite)
    db.commit()
    db.refresh(favorite)
    return {"id": favorite.id, "content": favorite.content, "created_at": favorite.created_at}


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
        status="approved" if allowed else "rejected",
        reject_reason=None if allowed else reason,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
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
        result.append(
            {
                "id": p.id,
                "content": p.content,
                "author": author_display_name(p.user),
                "author_avatar": p.user.avatar_base64,
                "created_at": p.created_at,
                "comment_count": comment_count,
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
    return {
        "id": post.id,
        "content": post.content,
        "author": author_display_name(post.user),
        "author_avatar": post.user.avatar_base64,
        "created_at": post.created_at,
        "is_mine": post.user_id == current_user.id,
        "comments": [
            {
                "id": c.id,
                "content": c.content,
                "author": author_display_name(c.user),
                "author_avatar": c.user.avatar_base64,
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
    return {"id": comment.id, "status": comment.status, "reject_reason": comment.reject_reason}


@app.delete("/api/gallery/{post_id}")
async def gallery_delete(
    post_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удаляет свой пост из галереи."""
    post = db.query(GalleryPost).filter(GalleryPost.id == post_id, GalleryPost.user_id == current_user.id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")
    db.delete(post)
    db.commit()
    return {"status": "deleted"}


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
    return {"id": message.id, "status": message.status, "reject_reason": message.reject_reason}


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

    favorite = Favorite(user_id=user.id, content=req.content)
    db.add(favorite)
    db.commit()
    db.refresh(favorite)

    return {"id": favorite.id, "content": favorite.content, "created_at": favorite.created_at}


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
    return {"favorites": [{"id": f.id, "content": f.content} for f in favorites]}


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
