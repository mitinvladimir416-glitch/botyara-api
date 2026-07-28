"""
Общая AI-логика сайта botyara.ru — та же логика, что используется в Telegram-боте,
но переиспользуемая как обычный Python-модуль (без привязки к Telegram).

Все функции здесь "статeless" — сайт сам хранит историю переписки на своей стороне
(в базе данных) и присылает её целиком при каждом запросе.
"""

import logging
import os
import re

from groq import Groq, APIConnectionError, APIStatusError, RateLimitError
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError("Не найден GROQ_API_KEY — проверь переменные окружения")

MODEL = "openai/gpt-oss-120b"
VISION_MODEL = "qwen/qwen3.6-27b"

groq_client = Groq(api_key=GROQ_API_KEY, max_retries=0)

SYSTEM_PROMPT = "Ты дружелюбный ассистент, отвечай кратко и по делу на русском языке."


def clean_reply(text: str) -> str:
    """Убирает следы внутренних рассуждений модели, если они просочились в ответ."""
    if not text:
        return text
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return cleaned.strip()


def describe_groq_error(e: Exception) -> str:
    """Превращает техническую ошибку Groq в понятное пользователю сообщение."""
    if isinstance(e, RateLimitError):
        return "Сейчас слишком много запросов к нейросети. Подожди немного и попробуй снова."
    if isinstance(e, APIConnectionError):
        return "Не получилось связаться с нейросетью — проблема с сетью. Попробуй ещё раз через минуту."
    if isinstance(e, APIStatusError):
        return f"Нейросеть вернула ошибку (код {e.status_code}). Попробуй чуть позже."
    return "Произошла непредвиденная ошибка. Попробуй ещё раз."


def get_chat_reply(history: list[dict]) -> str:
    """
    Обычный чат. history — список сообщений формата [{"role": "user"/"assistant", "content": "..."}],
    без системного сообщения (оно добавляется здесь).
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history[-20:]
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=1024,
        )
        return clean_reply(response.choices[0].message.content)
    except Exception as e:
        logging.exception("Ошибка при обращении к Groq API (chat)")
        return describe_groq_error(e)


def translate_text(text: str, target_lang: str | None = None) -> str:
    """Переводит текст. Если target_lang не задан — авто: RU->EN, иначе->RU."""
    if target_lang:
        instruction = (
            f"Переведи следующий текст на язык с кодом '{target_lang}'. "
            "Ответь ТОЛЬКО переводом, без пояснений, кавычек и комментариев."
        )
    else:
        instruction = (
            "Определи язык текста. Если текст на русском — переведи на английский. "
            "Если текст на любом другом языке — переведи на русский. "
            "Сохрани тон и смысл максимально точно. "
            "Ответь ТОЛЬКО переводом, без пояснений, кавычек, комментариев и указания языка."
        )

    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            max_tokens=1024,
        )
        return clean_reply(response.choices[0].message.content)
    except Exception as e:
        logging.exception("Ошибка при переводе")
        return describe_groq_error(e)


# ==================== ПРОМПТЫ (Suno / Картинка / Видео) ====================

PROMPT_CONFIG = {
    "suno": {
        "label": "Suno (музыка)",
        "targets": ["4", "4.5", "5", "5.5"],
        "system_prompt": (
            "Ты — опытный саунд-продюсер и эксперт по составлению промптов для Suno AI "
            "(нейросеть для генерации музыки). Твоя задача — помочь пользователю составить "
            "качественный промпт. Веди диалог по существу:\n"
            "1. Уточни жанр/стиль, настроение, темп, наличие и пол вокала, референсы-исполнителей, "
            "структуру трека (куплет/припев/бридж), нужен ли текст песни (лирика).\n"
            "Задавай не больше 1-2 уточняющих вопросов за раз, не заваливай пользователя вопросами сразу.\n"
            "Когда данных достаточно — сформируй готовый промпт для Suno (структурированный, с тегами, "
            "если версия их поддерживает) и обязательно начни этот момент строкой 'ГОТОВЫЙ ПРОМПТ:' "
            "на отдельной строке, дальше сам промпт. Отвечай на русском, но сам текст промпта пиши "
            "так, как эффективнее для Suno (обычно это английский для тегов стиля)."
        ),
    },
    "image": {
        "label": "Картинка",
        "targets": ["Midjourney", "DALL-E 3", "Stable Diffusion", "Flux"],
        "system_prompt": (
            "Ты — эксперт по составлению промптов для генерации изображений через нейросети. "
            "Твоя задача — помочь пользователю составить качественный промпт. Веди диалог по существу:\n"
            "1. Уточни сюжет и объект, стиль (фотореализм, аниме, живопись, 3D и т.д.), композицию, "
            "освещение, цветовую палитру, ракурс, соотношение сторон, дополнительные детали и настроение.\n"
            "Задавай не больше 1-2 уточняющих вопросов за раз.\n"
            "Когда данных достаточно — сформируй готовый промпт, оформленный по стандартам именно "
            "выбранной нейросети (для Midjourney — с параметрами --ar --v --style и т.д., если уместно). "
            "Обязательно начни этот момент строкой 'ГОТОВЫЙ ПРОМПТ:' на отдельной строке, дальше сам "
            "промпт. Отвечай на русском, промпт можно писать на английском, если так эффективнее."
        ),
    },
    "video": {
        "label": "Видео",
        "targets": ["Sora", "Runway", "Kling", "Veo"],
        "system_prompt": (
            "Ты — эксперт по составлению промптов для генерации видео через нейросети "
            "(Sora, Runway, Kling, Veo, Pika и подобные). Твоя задача — помочь пользователю "
            "составить качественный промпт. Веди диалог по существу:\n"
            "1. Уточни сюжет сцены, движение камеры (панорама, наезд, статика и т.д.), стиль "
            "(кино, реализм, анимация), освещение, длительность, темп действия, звук/музыку если поддерживается.\n"
            "Задавай не больше 1-2 уточняющих вопросов за раз.\n"
            "Когда данных достаточно — сформируй готовый промпт под конкретную нейросеть. "
            "Обязательно начни этот момент строкой 'ГОТОВЫЙ ПРОМПТ:' на отдельной строке, дальше сам "
            "промпт. Отвечай на русском, промпт можно писать на английском, если так эффективнее."
        ),
    },
}


def get_prompt_reply(topic: str, target: str | None, history: list[dict]) -> str:
    """
    Ведёт диалог по составлению промпта в выбранной теме (suno/image/video).
    history — список сообщений [{"role":..., "content":...}] без системного сообщения.
    target — выбранная пользователем версия/нейросеть (например "Suno 4.5" или "Midjourney").
    """
    if topic not in PROMPT_CONFIG:
        return "Неизвестная тема промпта."

    config = PROMPT_CONFIG[topic]
    system_content = config["system_prompt"]
    if target:
        system_content += (
            f"\n\nПользователь уже выбрал: {target}. Не спрашивай про версию/нейросеть повторно — "
            "сразу переходи к остальным уточняющим вопросам, а готовый промпт формируй именно под неё."
        )

    messages = [{"role": "system", "content": system_content}] + history[-20:]

    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=1500,
        )
        return clean_reply(response.choices[0].message.content)
    except Exception as e:
        logging.exception("Ошибка при составлении промпта")
        return describe_groq_error(e)


def get_image_prompt_from_photo(image_base64: str, desired_change: str, history: list[dict]) -> str:
    """Анализирует присланное фото и составляет промпт для его правки в генеративной нейросети."""
    config = PROMPT_CONFIG["image"]

    combined_system = (
        config["system_prompt"]
        + "\n\nПользователь прислал фотографию и хочет внести в неё правки через генеративную "
        "нейросеть. Сначала кратко опиши (для себя, но можно упомянуть в ответе), что видишь на фото, "
        "затем сразу выдай готовый промпт под правку этого фото с пометкой 'ГОТОВЫЙ ПРОМПТ:'."
    )

    messages = (
        [{"role": "system", "content": combined_system}]
        + history[-10:]
        + [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Хочу изменить в этом фото: {desired_change}"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                    },
                ],
            }
        ]
    )

    try:
        response = groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=2048,
            reasoning_format="hidden",
        )
        return clean_reply(response.choices[0].message.content)
    except Exception as e:
        logging.exception("Ошибка при анализе фото для промпта")
        return describe_groq_error(e)


# ==================== ОБЛОЖКА ТРЕКА ====================

COVER_FORMATS = {
    "1:1": "Квадрат 1:1",
    "4:3": "Классический 4:3",
    "16:9": "Широкий 16:9",
    "3:4": "Портретный 3:4",
    "9:16": "Вертикальный 9:16",
}

COVER_SYSTEM_PROMPT = (
    "Ты — эксперт по составлению промптов для генерации обложек музыкальных треков в модели "
    "ChatGPT Image 2 (нейросеть OpenAI для генерации изображений). Тебе присылают текст песни "
    "(лирику), возможно — референсное фото, возможно — текст для размещения на обложке (название "
    "трека/исполнителя), и нужное соотношение сторон обложки. Твоя задача:\n"
    "1. Проанализируй текст песни — определи настроение, тематику, ключевые образы и символы.\n"
    "2. Если есть референсное фото — учти его стиль, цветовую палитру, композицию.\n"
    "3. Составь единый выразительный промпт для обложки: визуальная композиция, художественный стиль "
    "(фотореализм/иллюстрация/абстракция и т.д.), цветовая палитра, настроение, ключевые визуальные "
    "элементы.\n"
    "4. Если пользователь указал текст для обложки — обязательно включи в промпт точную инструкцию "
    "разместить именно этот текст (шрифт, расположение). Если текста нет — обложка должна быть БЕЗ "
    "текста и букв.\n"
    "Сначала коротко (1-2 предложения на русском) опиши, какое настроение считал из лирики. "
    "Затем сразу выдай готовый промпт под пометкой 'ГОТОВЫЙ ПРОМПТ:' на отдельной строке — "
    "сам промпт пиши на английском (так эффективнее для генерации), обязательно укажи в конце "
    "нужный aspect ratio."
)

COVER_PROMPT_FOOTER = "\n\n💡 Промпт составлен для ChatGPT Image 2"


def generate_cover_prompt(
    lyrics: str, photo_base64: str | None, ratio: str, cover_text: str | None
) -> str:
    """Анализирует текст песни (и фото, если есть) и составляет промпт для обложки трека."""
    user_content_text = f"Текст песни:\n{lyrics}\n\nНужное соотношение сторон: {ratio}"
    if cover_text:
        user_content_text += f"\n\nТекст, который должен быть на обложке: {cover_text}"
    else:
        user_content_text += "\n\nТекста на обложке быть не должно."

    if photo_base64:
        messages = [
            {"role": "system", "content": COVER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_content_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{photo_base64}"},
                    },
                ],
            },
        ]
        model = VISION_MODEL
        extra_kwargs = {"reasoning_format": "hidden"}
    else:
        messages = [
            {"role": "system", "content": COVER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content_text},
        ]
        model = MODEL
        extra_kwargs = {}

    try:
        response = groq_client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.7,
            max_tokens=1500,
            **extra_kwargs,
        )
        reply = clean_reply(response.choices[0].message.content)
    except Exception as e:
        logging.exception("Ошибка при составлении промпта обложки")
        return describe_groq_error(e)

    return reply + COVER_PROMPT_FOOTER


def transcribe_audio(file_path: str) -> str:
    """Распознаёт речь из аудиофайла через Groq Whisper."""
    try:
        with open(file_path, "rb") as audio_file:
            transcription = groq_client.audio.transcriptions.create(
                file=audio_file,
                model="whisper-large-v3",
                language="ru",
            )
        return transcription.text
    except Exception as e:
        logging.exception("Ошибка распознавания голоса")
        return ""
