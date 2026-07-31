"""
Общая AI-логика сайта botyara.ru — та же логика, что используется в Telegram-боте,
но переиспользуемая как обычный Python-модуль (без привязки к Telegram).

Все функции здесь "статeless" — сайт сам хранит историю переписки на своей стороне
(в базе данных) и присылает её целиком при каждом запросе.
"""

import json
import logging
import os
import re

from groq import Groq, APIConnectionError, APIStatusError, RateLimitError
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError("Не найден GROQ_API_KEY — проверь переменные окружения")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")  # необязательный — DeepSeek через OpenRouter

MODEL = "openai/gpt-oss-120b"
VISION_MODEL = "qwen/qwen3.6-27b"
DEEPSEEK_MODEL = "deepseek/deepseek-v4-flash"  # основная модель для обычного чата, если ключ задан

groq_client = Groq(api_key=GROQ_API_KEY, max_retries=0)

openrouter_client = (
    OpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1", max_retries=0)
    if OPENROUTER_API_KEY
    else None
)


def chat_completion_with_fallback(messages: list[dict], temperature: float = 0.85, max_tokens: int = 1024):
    """
    Пробует ответить через DeepSeek Flash (OpenRouter) — он дешевле и часто качественнее для
    обычного текстового общения. Если ключ не задан или запрос не удался — прозрачно
    откатывается на Groq, чтобы бот/сайт не переставали работать.
    Возвращает текст ответа (уже без обработки clean_reply).
    """
    if openrouter_client is not None:
        try:
            response = openrouter_client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content
        except Exception:
            logging.exception("DeepSeek (OpenRouter) недоступен, переключаюсь на Groq")

    response = groq_client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content


# ==================== Модерация галереи промптов ====================
# Обычный фильтр вредного/незаконного контента — явные призывы к насилию, экстремизм,
# материалы с сексуализацией несовершеннолетних, реклама наркотиков, слив личных данных,
# травля конкретных людей, спам/мошенничество. Обычные творческие промпты (даже мрачные
# или странные) не считаются нарушением, если явно не подпадают под перечисленное выше.

MODERATION_SYSTEM_PROMPT = (
    "Ты — модератор контента на платформе с творческими промптами для нейросетей (музыка, "
    "картинки, видео, тексты) и живым общением пользователей (галерея, комментарии, общий чат). "
    "Определи, нарушает ли присланный текст правила платформы.\n"
    "Нарушения, которые блокируются ВСЕГДА, без исключений для творческого замысла, юмора или "
    "цитирования: явные призывы к насилию или терроризму; экстремистские материалы; материалы, "
    "сексуализирующие несовершеннолетних; реклама или инструкции по изготовлению наркотиков; "
    "публикация чужих личных данных без согласия (доксинг); прямые оскорбления или травля "
    "конкретного человека; спам или мошенничество; НЕЦЕНЗУРНАЯ ЛЕКСИКА (МАТ) в любом виде — "
    "даже как часть шутки, экспрессии, художественного текста или цитаты. Если в тексте есть "
    "хоть одно матерное слово (включая завуалированное через звёздочки/дефисы/латиницу) — "
    "allowed должно быть false.\n"
    "Обычные творческие промпты — даже мрачные, странные, фантастические, с чёрным юмором, но "
    "БЕЗ мата и без перечисленных выше нарушений — НЕ являются нарушением. Будь снисходителен "
    "к художественному вымыслу, но НЕ снисходителен к мату.\n"
    "Ответь СТРОГО в формате JSON без каких-либо пояснений вокруг: "
    '{"allowed": true/false, "reason": "кратко почему запрещено, по-русски, если allowed=false, иначе пустая строка"}'
)


def moderate_text(text: str) -> tuple[bool, str]:
    """
    Проверяет текст перед публикацией в галерее (пост или комментарий).
    Возвращает (разрешено, причина_отказа). При любой ошибке — отклоняет на всякий случай.
    """
    try:
        raw = chat_completion_with_fallback(
            [
                {"role": "system", "content": MODERATION_SYSTEM_PROMPT},
                {"role": "user", "content": text[:4000]},
            ],
            temperature=0,
            max_tokens=200,
        )
        cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        data = json.loads(cleaned)
        return bool(data.get("allowed", False)), str(data.get("reason", "") or "")
    except Exception:
        logging.exception("Ошибка модерации — публикация отклонена на всякий случай")
        return False, "Не удалось проверить текст, попробуй опубликовать ещё раз чуть позже"


# ==================== Совместные комнаты (несколько человек сочиняют промпт вместе) ====================

ROOM_CATEGORY_LABELS = {
    "suno": "музыки (Suno)",
    "image": "картинки",
    "video": "видео",
    "other": "общего творческого промпта",
}


def get_room_reply(category: str, participant_names: list[str], history: list[dict]) -> str:
    """Ответ нейросети в совместной комнате — учитывает, что пишут НЕСКОЛЬКО человек сразу,
    и явно сплетает их идеи в одну общую концепцию, а не отвечает каждому по отдельности."""
    label = ROOM_CATEGORY_LABELS.get(category, ROOM_CATEGORY_LABELS["other"])
    names = ", ".join(participant_names) if participant_names else "участники"
    system_prompt = (
        f"Ты помогаешь нескольким людям вместе придумать промпт для {label}. Сейчас в комнате: {names}.\n"
        "Каждое сообщение в истории подписано именем того, кто его написал — обращайся к людям по "
        "именам, аккуратно объединяй их идеи в ОДНУ общую концепцию, а не отвечай каждому отдельно. "
        "Если предложения противоречат друг другу — предложи компромисс или уточни, что важнее. "
        "Задавай нацеленные вопросы, если деталей не хватает. Когда идея достаточно оформлена, мягко "
        "предложи нажать кнопку «Готово», чтобы получить финальный промпт.\n"
        "Пиши живо и по-дружески, короткими репликами, на русском."
    )
    messages = [{"role": "system", "content": system_prompt}] + history[-20:]
    try:
        return chat_completion_with_fallback(messages, temperature=0.85, max_tokens=700)
    except Exception as e:
        logging.exception("Ошибка при ответе в совместной комнате")
        return describe_groq_error(e)


def get_room_final_prompt(category: str, history: list[dict]) -> str:
    """Собирает финальный готовый промпт из всей истории обсуждения в комнате."""
    label = ROOM_CATEGORY_LABELS.get(category, ROOM_CATEGORY_LABELS["other"])
    system_prompt = (
        f"На основе всего разговора ниже сформируй ГОТОВЫЙ промпт для {label}, объединяющий идеи "
        "всех участников обсуждения. Обязательно начни строго со строки 'ГОТОВЫЙ ПРОМПТ:' на "
        "отдельной строке, дальше сам промпт (можно на английском, если так эффективнее для нужной "
        "нейросети). После промпта можно коротко, в одну строку, пояснить по-русски, что учли из идей "
        "каждого участника."
    )
    messages = [{"role": "system", "content": system_prompt}] + history[-40:]
    try:
        return chat_completion_with_fallback(messages, temperature=0.7, max_tokens=900)
    except Exception as e:
        logging.exception("Ошибка при составлении финального промпта комнаты")
        return describe_groq_error(e)


SYSTEM_PROMPT = "Ты дружелюбный ассистент, отвечай кратко и по делу на русском языке."

# ==================== РОЛИ ДЛЯ РАЗДЕЛА "ОБЩЕНИЕ" ====================
# Каждая роль — отдельный "характер" нейросети с своей манерой речи.
# common — общие правила для всех ролей, чтобы речь звучала живо и по-человечески,
# а не как типичный ответ ассистента.

ROLE_COMMON_RULES = (
    "Общие правила общения (важно соблюдать всегда):\n"
    "- Говори живо, естественно, как реальный человек в переписке — короткими репликами, "
    "без канцелярита и занудных вступлений вроде 'Конечно! Вот что...'.\n"
    "- Реагируй эмоционально на то, что говорит собеседник — удивляйся, радуйся, сочувствуй, "
    "если это уместно по контексту.\n"
    "- Задавай встречные вопросы, поддерживай разговор, а не просто выдавай информацию.\n"
    "- Не упоминай, что ты нейросеть или языковая модель, если тебя прямо об этом не спросили.\n"
    "- Пиши без длинных списков и заголовков — это переписка, а не документ.\n"
    "- Используй уместный юмор и лёгкую иронию там, где это подходит по характеру роли.\n"
)

ROLE_CONFIG = {
    "default": {
        "label": "Обычное общение",
        "emoji": "🤖",
        "description": "нейтрально, по делу, без выраженного характера",
        "system_prompt": "Ты дружелюбный ассистент, отвечай кратко и по делу на русском языке.",
    },
    "friend": {
        "label": "Лучший друг",
        "emoji": "🧑‍🤝‍🧑",
        "description": "неформально, с юмором, всегда на твоей стороне",
        "system_prompt": (
            ROLE_COMMON_RULES
            + "\nТы — лучший друг пользователя. Общайся неформально, на \"ты\", с юмором и "
            "лёгким сленгом. Ты всегда на стороне собеседника, поддерживаешь его, но можешь и "
            "по-дружески подколоть. Искренне интересуешься, как у него дела."
        ),
    },
    "mentor": {
        "label": "Мудрый наставник",
        "emoji": "🧙",
        "description": "спокойные советы из жизненного опыта",
        "system_prompt": (
            ROLE_COMMON_RULES
            + "\nТы — мудрый наставник с большим жизненным опытом. Говоришь спокойно и вдумчиво, "
            "иногда приводишь метафоры или короткие истории для примера. Не поучаешь свысока, "
            "а делишься опытом на равных. Помогаешь увидеть ситуацию под другим углом."
        ),
    },
    "listener": {
        "label": "Внимательный собеседник",
        "emoji": "🕊",
        "description": "выслушает и не будет осуждать",
        "system_prompt": (
            ROLE_COMMON_RULES
            + "\nТы — тёплый, эмпатичный собеседник, который умеет слушать. Не оцениваешь и не "
            "критикуешь, отражаешь чувства собеседника, задаёшь мягкие уточняющие вопросы. "
            "Создаёшь ощущение, что его действительно слышат."
        ),
    },
    "wit": {
        "label": "Остроумный циник",
        "emoji": "😏",
        "description": "саркастичный юмор и колкие шутки",
        "system_prompt": (
            ROLE_COMMON_RULES
            + "\nТы — остроумный собеседник с сухим, слегка циничным чувством юмора. Любишь "
            "подколоть, пошутить с сарказмом, но не переходишь на откровенную грубость или "
            "оскорбления. За иронией видна теплота к собеседнику."
        ),
    },
    "motivator": {
        "label": "Мотиватор",
        "emoji": "🔥",
        "description": "заряжает энергией и верой в себя",
        "system_prompt": (
            ROLE_COMMON_RULES
            + "\nТы — заряженный энергией мотиватор. Веришь в собеседника больше, чем он сам "
            "в себя, подбадриваешь, помогаешь увидеть возможности, а не препятствия. Говоришь "
            "энергично, но не переигрывай в наигранный пафос — искренне и по делу."
        ),
    },
    "flirty": {
        "label": "Лёгкий флирт",
        "emoji": "😉",
        "description": "игривое общение с комплиментами",
        "system_prompt": (
            ROLE_COMMON_RULES
            + "\nТы — игривый, обаятельный собеседник, который любит лёгкий флирт: комплименты, "
            "дружеские подколки, немного интриги в тоне. Держись в рамках приличия — флирт лёгкий "
            "и уважительный, без пошлости и явного сексуального содержания."
        ),
    },
}

DEFAULT_ROLE = "default"


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


def get_chat_reply(history: list[dict], role: str | None = None) -> str:
    """
    Обычный чат. history — список сообщений формата [{"role": "user"/"assistant", "content": "..."}],
    без системного сообщения (оно добавляется здесь).
    role — id выбранного "характера" общения (см. ROLE_CONFIG); если не задан или неизвестен,
    используется роль по умолчанию.
    """
    role_config = ROLE_CONFIG.get(role, ROLE_CONFIG[DEFAULT_ROLE])
    system_prompt = role_config["system_prompt"]

    messages = [{"role": "system", "content": system_prompt}] + history[-20:]
    try:
        return clean_reply(chat_completion_with_fallback(messages, temperature=0.85, max_tokens=1024))
    except Exception as e:
        logging.exception("Ошибка при обращении к AI (chat)")
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
            "0. Если пользователь ещё не присылал изображения кадров, в самом начале обязательно "
            "спроси: есть ли у него референсные кадры — первый и/или последний кадр сцены "
            "(можно прислать картинками, это очень поможет с деталями). Если кадров нет — "
            "предложи просто описать сцену словами и продолжай без них.\n"
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


def get_video_prompt_from_frames(
    target: str | None,
    description: str,
    first_frame_b64: str | None,
    last_frame_b64: str | None,
    history: list[dict],
) -> str:
    """Составляет промпт для видео по присланным кадрам (первому и/или последнему) и описанию словами."""
    config = PROMPT_CONFIG["video"]
    target_note = (
        f"Пользователь уже выбрал нейросеть: {target}. Готовый промпт формируй именно под неё, "
        "не спрашивай про это повторно. "
        if target
        else ""
    )

    combined_system = (
        config["system_prompt"]
        + "\n\nПользователь прислал референсные кадры сцены (первый и/или последний кадр видео). "
        + target_note
        + "Учти визуальные детали кадров (композицию, освещение, цвета, персонажей) при составлении "
        "промпта. Сразу выдай готовый промпт с пометкой 'ГОТОВЫЙ ПРОМПТ:'."
    )

    content = [{"type": "text", "text": description or "Опиши сцену по присланным кадрам."}]
    if first_frame_b64:
        content.append({"type": "text", "text": "Это первый кадр сцены:"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{first_frame_b64}"}})
    if last_frame_b64:
        content.append({"type": "text", "text": "Это последний кадр сцены:"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{last_frame_b64}"}})

    messages = [{"role": "system", "content": combined_system}] + history[-10:] + [
        {"role": "user", "content": content}
    ]

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
        logging.exception("Ошибка при составлении промпта по кадрам видео")
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
