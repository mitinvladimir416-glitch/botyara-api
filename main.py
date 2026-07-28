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

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

import ai_service
import auth
from database import get_db, init_db, User, Favorite

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Botyara API", version="0.2.0")


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


# ==================== Эндпоинты ====================

@app.get("/api/health")
async def health():
    """Проверка, что сервис жив (используем для мониторинга)."""
    return {"status": "ok"}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Обычный чат с ботом."""
    history = [m.model_dump() for m in req.history]
    reply = ai_service.get_chat_reply(history)
    return {"reply": reply}


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
    }


# ==================== Избранное (требует авторизации) ====================

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
