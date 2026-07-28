"""
Подключение к базе данных и модели (SQLAlchemy).

Использует PostgreSQL — строка подключения берётся из переменной окружения DATABASE_URL,
которую Timeweb Cloud выдаёт при создании управляемой базы данных.

Формат DATABASE_URL обычно такой:
    postgresql://пользователь:пароль@хост:порт/имя_базы
"""

import os

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.sql import func

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("Не найден DATABASE_URL — проверь переменные окружения")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    email = Column(String, unique=True, index=True, nullable=True)
    password_hash = Column(String, nullable=True)

    telegram_id = Column(String, unique=True, index=True, nullable=True)
    telegram_username = Column(String, nullable=True)
    telegram_first_name = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    favorites = relationship("Favorite", back_populates="user", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="user", cascade="all, delete-orphan")


class Favorite(Base):
    __tablename__ = "favorites"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="favorites")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role = Column(String, nullable=False)      # "user" или "assistant"
    content = Column(Text, nullable=False)
    source = Column(String, nullable=False)    # "bot" или "web" — откуда пришло сообщение
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="messages")


def init_db():
    """Создаёт таблицы, если их ещё нет. Вызывается один раз при старте приложения."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI-зависимость: даёт сессию БД на время запроса и закрывает её после."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
