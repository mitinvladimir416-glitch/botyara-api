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

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import ai_service

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Botyara API", version="0.1.0")

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
