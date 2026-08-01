"""
Подключение к базе данных и модели (SQLAlchemy).

Использует PostgreSQL — строка подключения берётся из переменной окружения DATABASE_URL,
которую Timeweb Cloud выдаёт при создании управляемой базы данных.

Формат DATABASE_URL обычно такой:
    postgresql://пользователь:пароль@хост:порт/имя_базы
"""

import os

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Date, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.sql import func

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("Не найден DATABASE_URL — проверь переменные окружения")

# Timeweb (как и многие провайдеры) может выдавать строку вида "postgres://",
# а SQLAlchemy с психопг2 хочет "postgresql://" — подстрахуемся
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    # Вход по email — оба поля могут быть NULL, если человек вошёл только через Telegram
    email = Column(String, unique=True, index=True, nullable=True)
    password_hash = Column(String, nullable=True)

    # Вход через Telegram — тоже может быть NULL, если человек регистрировался только по email
    telegram_id = Column(String, unique=True, index=True, nullable=True)
    telegram_username = Column(String, nullable=True)
    telegram_first_name = Column(String, nullable=True)

    # Профиль (заполняется пользователем вручную в разделе "Аккаунт")
    display_name = Column(String, nullable=True)
    avatar_base64 = Column(Text, nullable=True)  # data URL целиком: "data:image/jpeg;base64,..."

    # Геймификация
    xp = Column(Integer, nullable=False, server_default="0")
    current_streak = Column(Integer, nullable=False, server_default="0")
    last_active_date = Column(Date, nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)  # для "онлайн сейчас" в админке

    # Модерация и роли — управляется через админ-панель (см. main.py: is_moderator/is_full_admin)
    role = Column(String, nullable=False, server_default="user")  # "user" / "moderator" / "admin"
    is_banned = Column(Boolean, nullable=False, server_default="false")
    badge_text = Column(String, nullable=True)   # кастомное "украшение" — короткий титул рядом с именем
    badge_color = Column(String, nullable=True)  # цвет титула (hex), задаётся админом

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    favorites = relationship("Favorite", back_populates="user", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="user", cascade="all, delete-orphan")


class Favorite(Base):
    __tablename__ = "favorites"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    # category — папка: "suno" / "image" / "video" / "cover" / "other"
    category = Column(String, nullable=False, server_default="other")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="favorites")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role = Column(String, nullable=False)      # "user" или "assistant"
    content = Column(Text, nullable=False)
    source = Column(String, nullable=False)    # "bot" или "web" — откуда пришло сообщение
    # persona — какая "вкладка"/роль общения: "default" (обычное общение) или id роли
    # (friend/mentor/listener/wit/motivator/flirty). Сообщения из бота пока всегда "default",
    # т.к. роли в боте ещё не подключены.
    persona = Column(String, nullable=False, server_default="default")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="messages")


class GalleryPost(Base):
    """Опубликованный в галерее промпт (можно опубликовать только то, что уже в Избранном)."""
    __tablename__ = "gallery_posts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    category = Column(String, nullable=False, server_default="other")
    status = Column(String, nullable=False, server_default="pending")  # pending/approved/rejected
    reject_reason = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")
    comments = relationship("GalleryComment", back_populates="post", cascade="all, delete-orphan")


class GalleryComment(Base):
    """Комментарий к посту в галерее — тоже проходит модерацию перед публикацией."""
    __tablename__ = "gallery_comments"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("gallery_posts.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String, nullable=False, server_default="pending")
    reject_reason = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    post = relationship("GalleryPost", back_populates="comments")
    user = relationship("User")


class PublicChatMessage(Base):
    """Сообщение в общем публичном чате — видно всем вошедшим пользователям, проходит модерацию."""
    __tablename__ = "public_chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String, nullable=False, server_default="pending")  # pending/approved/rejected
    reject_reason = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")


class Announcement(Base):
    """Оповещение об обновлении — публикуется ботом (/announce), показывается в ленте на сайте."""
    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class GalleryLike(Base):
    """Реакция на пост галереи (эмодзи) — один пользователь может оставить только одну реакцию
    на пост, повторный клик по другому эмодзи заменяет её."""
    __tablename__ = "gallery_likes"
    __table_args__ = (UniqueConstraint("post_id", "user_id", name="uq_gallery_like_post_user"),)

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("gallery_posts.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    emoji = Column(String, nullable=False, server_default="❤️")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    """Личное уведомление пользователю (лайк/комментарий к его посту) — не путать с Announcement
    (это общая лента обновлений от админа, а это — персональные события)."""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class UserAchievement(Base):
    """Полученное пользователем достижение — key соответствует ключу из списка в main.py."""
    __tablename__ = "user_achievements"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_achievement"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    key = Column(String, nullable=False)
    earned_at = Column(DateTime(timezone=True), server_default=func.now())


class Room(Base):
    """Совместная комната — 2+ человек сочиняют один промпт вместе."""
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True, nullable=False)
    category = Column(String, nullable=False, server_default="other")  # suno/image/video/other
    status = Column(String, nullable=False, server_default="open")  # open/finished
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    final_content = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    participants = relationship("RoomParticipant", back_populates="room", cascade="all, delete-orphan")
    messages = relationship("RoomMessage", back_populates="room", cascade="all, delete-orphan")


class RoomParticipant(Base):
    """Участник комнаты."""
    __tablename__ = "room_participants"
    __table_args__ = (UniqueConstraint("room_id", "user_id", name="uq_room_participant"),)

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    room = relationship("Room", back_populates="participants")
    user = relationship("User")


class RoomMessage(Base):
    """Сообщение в совместной комнате — от участника или от нейросети (user_id=NULL).
    channel: "ai" — общий чат с нейросетью (виден и влияет на итоговый промпт),
             "team" — приватное обсуждение только между участниками, ИИ его не видит."""
    __tablename__ = "room_messages"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # NULL — сообщение от ИИ
    channel = Column(String, nullable=False, server_default="ai")
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    room = relationship("Room", back_populates="messages")
    user = relationship("User")


def init_db():
    """Создаёт таблицы, если их ещё нет. Вызывается один раз при старте приложения.
    ВАЖНО: create_all создаёт только отсутствующие ТАБЛИЦЫ целиком, но не добавляет
    новые колонки в уже существующие таблицы. Если добавляешь новое поле в уже
    существующую таблицу (как сейчас — persona в messages), его нужно один раз
    добавить вручную через SQL в Adminer."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI-зависимость: даёт сессию БД на время запроса и закрывает её после."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
