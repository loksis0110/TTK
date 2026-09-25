import sqlite3
import os
import logging
import html
import json
import hashlib
import hmac
import secrets
import re
import time
import zipfile
import threading
import asyncio
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager, contextmanager, AsyncExitStack
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
from aiogram import Bot
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pywebpush import webpush, WebPushException

try:
    from maxapi import Bot as MaxBot, Dispatcher as MaxDispatcher
    from maxapi.filters.command import Command as MaxCommand
    from maxapi.types import MessageCreated as MaxMessageCreated, BotStarted as MaxBotStarted
    from maxapi.webhook.fastapi import FastAPIMaxWebhook
except ImportError:
    MaxBot = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === КОНФИГУРАЦИЯ ===
# Токен бота лучше хранить в переменной окружения BOT_TOKEN, а не в коде.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = int(os.getenv("CHAT_ID", "-1004420801156"))
DB_PATH = os.getenv("DB_PATH", "orders.db")

MASTER_LOGIN = "admin"
MASTER_PASSWORD = "admin123"

# Фото смены: храним рядом с базой (на том же примонтированном volume в Railway), чтобы не терять файлы при деплое.
PHOTOS_DIR = os.getenv("PHOTOS_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".", "shift_photos")
PHOTO_REMINDER_MINUTES = 5                  # напомнить за 5 минут до срока
LATE_REPORT_MINUTES = 15                    # через 15 минут без отчёта — сообщить руководству

# Стена позора: фото + подпись + отметка сотрудника + комментарии.
SHAME_DIR = os.getenv("SHAME_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".", "shame_photos")

# Архивация: раз в ARCHIVE_INTERVAL_DAYS дней фото отчётов смен и фото со стены позора упаковываются
# в отдельные ZIP-архивы (файлы внутри подписываются как "Дата_Кто загрузил"), а оригиналы удаляются.
# Архив хранится до следующей архивации этой же категории, после чего удаляется автоматически.
ARCHIVES_DIR = os.getenv("ARCHIVES_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".", "archives")
ARCHIVE_INTERVAL_DAYS = 3
TEAM_ROLES = {"master", "bar_manager", "senior_bartender"}   # видят архивы во вкладке «Команда»

# Какие фото-чекпоинты нужны для смены (по времени начала/конца). Для смен, не описанных здесь,
# по умолчанию требуется одно фото к моменту окончания смены.
PHOTO_SHIFT_RULES = {
    ("08:00", "20:00"): ["12:00", "20:00"],   # дневная смена — в обед и на сдаче
    ("20:00", "08:00"): ["08:00"],            # ночная смена — только на сдаче утром
}

ORDER_HOUR, ORDER_MINUTE = 7, 0          # заявка уходит в 07:00 по Москве
MSK = timezone(timedelta(hours=3))

# Web Push (браузерные уведомления). Ключи сгенерировать один раз и положить в переменные окружения Railway.
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_CLAIMS_EMAIL = os.getenv("VAPID_CLAIMS_EMAIL", "mailto:admin@example.com")

# MAX (мессенджер) — бот-уведомления через webhook. Токен берётся у @MasterBot в MAX.
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
MAX_CHAT_ID = os.getenv("MAX_CHAT_ID", "")  # id группы/чата, куда дублировать заявку (необязательно)
MAX_WEBHOOK_PATH = "/max/webhook"

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# MAX-бот и диспетчер (создаём, только если библиотека установлена и токен задан)
max_bot = MaxBot(token=MAX_BOT_TOKEN) if (MaxBot and MAX_BOT_TOKEN) else None
max_dp = MaxDispatcher() if max_bot else None

if max_bot:
    @max_dp.bot_started()
    async def max_bot_started(event: "MaxBotStarted"):
        await max_bot.send_message(chat_id=event.chat_id, text="Привет! Я бот бара «Тугай». Команда «заявка» покажет текущий список закупки.")

    @max_dp.message_created(MaxCommand("start"))
    async def max_hello(event: "MaxMessageCreated"):
        await event.message.answer("Привет! Я бот бара «Тугай». Команда «заявка» покажет текущий список закупки.")

    @max_dp.message_created()
    async def max_router(event: "MaxMessageCreated"):
        text = (event.message.body.text or "").strip().lower()
        if text in ("заявка", "статус", "/order"):
            with db() as c:
                rows = c.execute("SELECT item_name, quantity FROM active_order ORDER BY timestamp ASC").fetchall()
            if not rows:
                await event.message.answer("Заявка сейчас пуста.")
            else:
                lines = [f"• {r['item_name']} — {r['quantity']}" for r in rows]
                await event.message.answer("Текущая заявка:\n" + "\n".join(lines))
        else:
            await event.message.answer("Доступные команды: /start, «заявка» — показать текущий список закупки.")

# === РОЛИ ===
ROLES = {
    "bartender": "Бармен",
    "waiter": "Официант",
    "bar_manager": "Бар-менеджер",
    "senior_bartender": "Старший бармен",
}
ADMIN_ROLES = {"master", "bar_manager"}                    # подтверждение, роли, меню
SCHEDULE_EDITORS = ADMIN_ROLES | {"senior_bartender"}      # правка графика
SESSION_TTL = 30 * 24 * 3600


# --- ПОЛНОЕ МЕНЮ (40 ПОЗИЦИЙ С КАЛЬКУЛЯТОРАМИ) ---
INITIAL_MENU = [
    {"tab": "drinks", "category": "cocktail", "title": "Апероль Спритз", "method": "Заливаем все в бокал для красного вина с кусковым льдом и перемешиваем, украшаем слайсом апельсина", "tags": '["220 мл", "Лед"]', "glass": "Бокал для красного вина", "ingredients": '["Ликер Aperol - 60 мл", "Игристое Абрау Дюрсо - 100 мл", "Содовая - 30 мл", "Апельсин - 30 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Беллини", "method": "Заливаем все в блендер, немного взбиваем, и переливаем в шале. Украшаем цветком", "tags": '["200 мл"]', "glass": "Креманка", "ingredients": '["Пф Беллини - 80 мл", "Игристое вино Шато Двуморье брют - 120 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Негрони", "method": "Заливаем все ингридиенты в смес, стируем и переливаем в бокал олд фешн со льдом, украшаем цедрой апельсина.", "tags": '["90 мл", "Лед"]', "glass": "Рокс", "ingredients": '["Джин Bikens - 30 мл", "Cinzano 1757 rosso - 30 мл", "Campari - 30 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Бэзил Смеш", "method": "Заливаем все в шейкер с кусковым льдом, подаем в олд фэшн со льдом и украшаем листом базилика.", "tags": '["80 мл", "Лед"]', "glass": "Рокс", "ingredients": '["Джин - 40 мл", "Сахарный сироп - 20 мл", "Пф лимонный фреш - 20 мл", "Базилик свежий - 8 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Татарский Олд Фэшн", "method": "Заливаем все в смес, стируем и переливаем в бокал олд фэшн. Украшение: Окуриваем бокал окуривателем и щепой.", "tags": '["70 мл"]', "glass": "Рокс", "ingredients": '["Пф Бурбон/Курага - 70 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Лимончелло Спритз", "method": "Заливаем все в бокал для красного вина с кусковым льдом и перемешиваем, украшаем листьями мяты и долькой лимона", "tags": '["180 мл", "Лед"]', "glass": "Бокал для красного вина", "ingredients": '["Пф Лимончелло - 60 мл", "Игристое Абрау Дюрсо - 90 мл", "Газированная вода - 30 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Мартини-Эспрессо", "method": "Заливаем все в шейкер с кусковым льдом, подаем в креманку и украшаем зерновым кофе.", "tags": '["115 мл", "Лед"]', "glass": "Креманка", "ingredients": '["Водка Белуга Нобл - 40 мл", "Эспрессо - 40 мл", "Мари Бризар Кофе - 25 мл", "Сахарный сироп - 10 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Май-Тай", "method": "Заливаем все в шейкер с кусковым льдом, подаем в олд фэшн с колотым льдом и украшаем сушеным апельсином и пылью из каркаде.", "tags": '["140 мл", "Лед", "Краш"]', "glass": "Рокс", "ingredients": '["Ром Барсело Бланко - 30 мл", "Ром Такамака - 30 мл", "Трипл Сек - 30 мл", "Амаретто - 10 мл", "Сахарный сироп - 10 мл", "Фреш лайма - 30 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Маргарита", "method": "Заливаем все в шейкер с кусковым льдом, подаем в креманку с каемкой из соли и долькой лайма", "tags": '["90 мл", "Лед"]', "glass": "Креманка", "ingredients": '["Текила Агавита - 40 мл", "Трипл Сек - 20 мл", "Сахарный Сироп - 10 мл", "Фреш лайма - 20 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Сарти Шпритц", "method": "Заливаем все в бокал для красного вина с кусковым льдом и перемешиваем, украшаем слайсом лайма", "tags": '["180 мл", "Лед"]', "glass": "Бокал для красного вина", "ingredients": '["Игристое Абрау Дюрсо - 90 мл", "Ликер Сарти - 60 мл", "Газированная вода - 30 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Венецианский Шпритц", "method": "Заливаем все в бокал для красного вина с кусковым льдом и перемешиваем, украшаем слайсом грейпфрута", "tags": '["190 мл", "Лед"]', "glass": "Бокал для красного вина", "ingredients": '["Кампари - 60 мл", "Игристое Абрау Дюрсо - 70 мл", "Сок грейпфрута - 30 мл", "Газированная вода - 30 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Порн Стар Мартини", "method": "Заливаем все в шейкер с кусковым льдом (шейк+драй шейк) или мини блендер с фрапе, переливаем в шале и урашаем и отдельно подаем шот игристого 50 мл", "tags": '["160 мл", "Лед"]', "glass": "Креманка", "ingredients": '["Пф Маракуйя - 60 мл", "Водка Lab ваниль и бобы тонка - 50 мл", "Игристое Шато Двуморье брют - 50 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "lemonade", "title": "Малина с персиком", "method": "Заливаем все ингридиенты в хайбол с кусковым льдом, перемешиваем и украшаем цветком", "tags": '["160 мл", "Лед"]', "glass": "Хайбол", "ingredients": '["Пф Малина/Персик - 70 мл", "Содовая - 90 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "lemonade", "title": "Манго и маракуйя", "method": "Заливаем все ингридиенты в хайбол с кусковым льдом, перемешиваем и украшаем цветком", "tags": '["160 мл", "Лед"]', "glass": "Хайбол", "ingredients": '["Пф Тропики - 70 мл", "Содовая - 90 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "smoothie", "title": "Зеленый смузи", "method": "Закидываем все в блендер и переливаем в хайбол", "tags": '["350 мл"]', "glass": "Хайбол", "ingredients": '["Огурец - 100 гр", "Яблоко - 100 гр", "Лимон - 40 гр", "Базилик - 5 гр", "Мята - 3 гр", "Сахарный сироп - 10 мл", "Вода - 50 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "smoothie", "title": "Цитрус-Имбирь", "method": "Закидываем все в блендер и переливаем в хайбол", "tags": '["350 мл", "Лед"]', "glass": "Хайбол", "ingredients": '["Апельсин - 120 гр", "Грейпфрут - 120 гр", "Лимон - 40 гр", "Имбирь - 15 гр", "Мед - 15 гр", "Лед - 50 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Чай Смородина с базиликом", "method": "Закидываем все в чайник и кипятим", "tags": '["700 мл"]', "glass": "Чайник", "ingredients": '["Пюре смородины - 120 гр", "Базилик свежий - 10 гр", "Пф сахарный сироп - 60 мл", "Эрл грей - 5 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Капучино", "method": "Готовим на 1/2 кофе", "tags": '["250 мл"]', "glass": "Кружка", "ingredients": '["Кофе - 18 гр", "Молоко - 200 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "nonalk", "title": "Маргарита 0.0", "method": "Заливаем все в шейкер с кусковым льдом, подаем в креманку с каемкой из соли и долькой лайма", "tags": '["90 мл", "Лед"]', "glass": "Креманка", "ingredients": '["Дринксом текила - 40 мл", "Дринксом апельсин - 20 мл", "Сахарный сироп - 10 мл", "Фреш лайма - 20 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "nonalk", "title": "Апероль-Спритз 0.0", "method": "Заливаем все в бокал для красного вина с кусковым льдом и перемешиваем, украшаем слайсом апельсина", "tags": '["220 мл", "Лед"]', "glass": "Бокал для красного вина", "ingredients": '["Дринксом апельсин - 100 мл", "Вода с газом - 100 мл", "Сахарный сироп - 10 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "nonalk", "title": "Виски-Кола 0.0", "method": "Наливаем все в рокс, смешиваем и украшаем лаймом", "tags": '["Лед"]', "glass": "Рокс", "ingredients": '["Дринксом виски - 40 мл", "Кока-Кола - 1 шт", "Лайм - 30 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "nonalk", "title": "Джин-Тоник 0.0", "method": "Наливаем все в рокс, смешиваем украшаем лимоном и огурцом", "tags": '["Лед"]', "glass": "Рокс", "ingredients": '["Дринксом Джин - 40 мл", "Тоник Гарденист - 1 шт", "Огурец - 50 гр", "Лимон - 25 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "cocktail", "title": "Кассэя", "method": "Заливаем все в шейкер с кусковым льдом, подаем в креманку украшенной шоколадным велюром", "tags": '["90 мл", "Лед"]', "glass": "Креманка", "ingredients": '["Водка Lab Смородина - 40 мл", "Сахарный сироп - 20 мл", "Фреш лимона - 20 мл", "Базилик - 5 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "lemonade", "title": "Лимонад Арбуз-Мята", "method": "Наливаем все в хайбол и украшаем огурцом и мятой", "tags": '["150 мл"]', "glass": "Хайбол", "ingredients": '["Пф Арбуз-Мята - 100 мл", "Газированная вода - 50 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "smoothie", "title": "Мятный Шейк", "method": "Мадлим мяту с молоком, фильтруем через сито и смешиваем все в блендере", "tags": '["300 мл"]', "glass": "Стакан", "ingredients": '["Молоко 3.2% - 150 гр", "Мороженое - 150 гр", "Мята - 5 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Чай Облепиховый", "method": "Закидываем все в чайник и кипятим", "tags": '["700 мл"]', "glass": "Чайник", "ingredients": '["Пюре облепиха - 120 гр", "Пф сахарный сироп - 80 мл", "Имбирь корень - 14 гр", "Чай сенча - 5 гр", "Лимон - 40 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Чай с травами", "method": "Закидываем все в чайник. Отдельно в соусничках варенье из шишек, чернослив и курага.", "tags": '["700 мл"]', "glass": "Чайник", "ingredients": '["Чабрец свежий - 6 гр", "Розмарин - 6 гр", "Душица сушеная - 10 гр", "Мята - 14 гр", "Мед - 50 гр", "Эрл грей - 5 гр", "Курага - 50 гр", "Чернослив - 50 гр", "Варенье из шишек - 50 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Эспрессо", "method": "", "tags": '["40 мл"]', "glass": "", "ingredients": '["Кофе - 18 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Допио", "method": "", "tags": '["80 мл"]', "glass": "", "ingredients": '["Кофе - 36 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Американо", "method": "", "tags": '["200 мл"]', "glass": "", "ingredients": '["Кофе - 18 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Флэт Уайт", "method": "Готовим на целой порции", "tags": '[]', "glass": "", "ingredients": '["Кофе - 18 гр", "Молоко - 200 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Латте", "method": "Готовим на 1/2 кофе", "tags": '["250 мл"]', "glass": "", "ingredients": '["Кофе - 18 гр", "Молоко - 200 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Раф кофе", "method": "Готовим на 1/2 кофе", "tags": '["250 мл"]', "glass": "", "ingredients": '["Кофе - 18 гр", "Молоко - 100 мл", "Сливки - 100 мл", "Ванильный сахар - 1 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Капучино на альтернативном молоке", "method": "Готовим на 1/2 кофе", "tags": '["250 мл"]', "glass": "", "ingredients": '["Кофе - 18 гр", "Альтернативное молоко (кокосовое/овсяное/миндальное/безлактозное/соевое) - 200 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Латте на альтернативном молоке", "method": "Готовим на 1/2 кофе", "tags": '["250 мл"]', "glass": "", "ingredients": '["Кофе - 18 гр", "Альтернативное молоко (кокосовое/овсяное/миндальное/безлактозное/соевое) - 200 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Декаф Американо", "method": "", "tags": '["200 мл"]', "glass": "", "ingredients": '["Кофе Декаф - 18 гр", "Молоко - 200 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Декаф Капучино", "method": "", "tags": '["250 мл"]', "glass": "", "ingredients": '["Кофе Декаф - 18 гр", "Молоко - 200 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Декаф Латте", "method": "", "tags": '["300 мл"]', "glass": "", "ingredients": '["Кофе Декаф - 18 гр", "Молоко - 200 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Какао", "method": "Смешиваем в питчере и взбиваем стимером", "tags": '["250 мл"]', "glass": "", "ingredients": '["Горячий шоколад - 80 мл", "Молоко - 200 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Горячий шоколад", "method": "Топим шоколад на водяной бане, после смешиваем с горячими сливками", "tags": '["80 мл"]', "glass": "", "ingredients": '["Шоколад - 20 гр", "Сливки 10% - 60 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Чай", "method": "", "tags": '["1000 мл"]', "glass": "", "ingredients": '["Чай - 8 гр"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {"tab": "drinks", "category": "tea_coffee", "title": "Морс", "method": "Смешиваем в хайболе", "tags": '["250 мл"]', "glass": "Хайбол", "ingredients": '["Основа для морса - 40 гр", "Вода - 210 мл"]', "baseYield": 0, "unit": "", "calcIngredients": "[]"},
    {
        "tab": "pf", "category": "pf", "title": "Пф Микс кислот", "method": "Все смешиваем в мернике и переливаем в бутылку", 
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 500, "unit": "мл", 
        "calcIngredients": '[{"name": "Лимонная кислота", "amount": 16, "unit": "гр"}, {"name": "Винная кислота", "amount": 8, "unit": "гр"}, {"name": "Яблочная кислота", "amount": 12, "unit": "гр"}, {"name": "Вода", "amount": 1000, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Сахарный сироп", "method": "Все засыпаем в сотейник, варим до растворения сахара, переливаем в бутылку", 
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 1000, "unit": "мл", 
        "calcIngredients": '[{"name": "Сахар", "amount": 900, "unit": "гр"}, {"name": "Вода", "amount": 700, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Алоэ", "method": "Алоэ воду фильтруем через сито и переливаем в мерный стакан, добавляем все ингридиенты и смешиваем", 
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 650, "unit": "мл", 
        "calcIngredients": '[{"name": "Вода", "amount": 250, "unit": "мл"}, {"name": "Напиток Алоэ", "amount": 250, "unit": "мл"}, {"name": "Пф сахарный сироп", "amount": 100, "unit": "мл"}, {"name": "Пф Микс кислот", "amount": 50, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Клубника/цитрус", "method": "Все ингридиенты засыпаем в вакуумный пакет и сювидим на 55 градусах 4 часа", 
        "tags": '["Заготовка", "Су-вид"]', "glass": "", "ingredients": "[]", "baseYield": 800, "unit": "мл", 
        "calcIngredients": '[{"name": "Клубника с/м", "amount": 250, "unit": "гр"}, {"name": "Luxardo Aperitivo", "amount": 400, "unit": "мл"}, {"name": "Сахар", "amount": 150, "unit": "гр"}, {"name": "Лимонный фреш", "amount": 150, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Мэри", "method": "Засыпаем все в блендер, перебиваем и заливаем в бутылку", 
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 500, "unit": "мл", 
        "calcIngredients": '[{"name": "Томаты в с/с", "amount": 300, "unit": "гр"}, {"name": "Сок томатный", "amount": 150, "unit": "мл"}, {"name": "Апельсины", "amount": 250, "unit": "гр"}, {"name": "Ворчестер", "amount": 35, "unit": "гр"}, {"name": "Табаско", "amount": 5, "unit": "гр"}, {"name": "Пф микс кислот", "amount": 10, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Бурбон/Курага", "method": "Засыпаем все в вакуумный пакет и сювидим 4 часа на 55 градусах", 
        "tags": '["Заготовка", "Су-вид"]', "glass": "", "ingredients": "[]", "baseYield": 450, "unit": "мл", 
        "calcIngredients": '[{"name": "Бурбон Jim Beam", "amount": 500, "unit": "мл"}, {"name": "Курага", "amount": 100, "unit": "гр"}, {"name": "Пф кордиал мед", "amount": 100, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Беллини", "method": "Заливаем все в сотейник и варим, затем цедим в бутылку", 
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 1200, "unit": "мл", 
        "calcIngredients": '[{"name": "Пюре персик", "amount": 500, "unit": "гр"}, {"name": "Сок персиковый", "amount": 250, "unit": "мл"}, {"name": "Сахар", "amount": 250, "unit": "гр"}, {"name": "Вода", "amount": 250, "unit": "мл"}, {"name": "Пф лимонный фреш", "amount": 300, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Лимончелло", "method": "Снимаем цедру без альбедо, в вакуум с водкой на 5 часов 50 град. Добавляем лимон фреш и сироп.", 
        "tags": '["Заготовка", "Су-вид"]', "glass": "", "ingredients": "[]", "baseYield": 800, "unit": "мл", 
        "calcIngredients": '[{"name": "Водка Organic", "amount": 500, "unit": "мл"}, {"name": "Лимоны", "amount": 400, "unit": "гр"}, {"name": "Пф сахарный сироп", "amount": 150, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Тропики", "method": "Смешиваем", 
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 700, "unit": "мл", 
        "calcIngredients": '[{"name": "Пюре манго", "amount": 300, "unit": "гр"}, {"name": "Пюре маракуйя", "amount": 150, "unit": "гр"}, {"name": "Пф сахарный сироп", "amount": 100, "unit": "мл"}, {"name": "Микс кислот", "amount": 100, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Черная смородина/джин", "method": "Засыпаем все в вакуумный пакет, сювидим на 55 градусах 4 часа, фильтруем и переливаем в бутылку, затем добавляем баланс (сахарный сироп и лимонный фреш)",
        "tags": '["Заготовка", "Су-вид"]', "glass": "", "ingredients": "[]", "baseYield": 350, "unit": "мл",
        "calcIngredients": '[{"name": "Черная смородина с/м", "amount": 100, "unit": "гр"}, {"name": "Джин Campobay", "amount": 250, "unit": "мл"}, {"name": "Мята свежая (листья)", "amount": 2, "unit": "гр"}, {"name": "Пф сахарный сироп", "amount": 100, "unit": "мл"}, {"name": "Лимонный фреш", "amount": 50, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Грейпфрутовый кордиал", "method": "Цедра с сахаром засыпается на 20 минут, всё остальное смешивается отдельно, затем закидываем в сотейник и растворяем сахар при 60 градусах, затем фильтруем и отжимаем цедру и сорбатим",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 650, "unit": "мл",
        "calcIngredients": '[{"name": "Грейпфрут", "amount": 800, "unit": "гр"}, {"name": "Сахар", "amount": 130, "unit": "гр"}, {"name": "Пф лаймовый фреш", "amount": 250, "unit": "мл"}, {"name": "Вода", "amount": 100, "unit": "мл"}, {"name": "Цедра", "amount": 18, "unit": "гр"}, {"name": "Лимонная кислота", "amount": 9, "unit": "гр"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Гибискус", "method": "Закидываем все в сотейник, выпариваем 100 мл от объёма, затем остужаем и фильтруем",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 400, "unit": "мл",
        "calcIngredients": '[{"name": "Вода", "amount": 400, "unit": "мл"}, {"name": "Пф сахарный сироп", "amount": 100, "unit": "мл"}, {"name": "Чай каркаде", "amount": 20, "unit": "гр"}, {"name": "Винная кислота", "amount": 6, "unit": "гр"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Кир", "method": "Перемешиваем и переливаем в бутылку",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 450, "unit": "мл",
        "calcIngredients": '[{"name": "Водка Organic", "amount": 100, "unit": "мл"}, {"name": "Bv Land cassis", "amount": 100, "unit": "мл"}, {"name": "Bv Land Blackberry", "amount": 100, "unit": "мл"}, {"name": "Пф Гибискус", "amount": 150, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Мятная вода", "method": "Перебиваем все в блендере и переливаем в мерный стакан, добавляем бентонит и перемешиваем венчиком, убираем в холодильник и ждём, пока всё осветлится, затем осветлённую жидкость переливаем в бутылку, остаток выливаем",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 700, "unit": "мл",
        "calcIngredients": '[{"name": "Вода", "amount": 500, "unit": "мл"}, {"name": "Мята с ветками", "amount": 50, "unit": "гр"}, {"name": "Пф микс кислот", "amount": 100, "unit": "мл"}, {"name": "Пф сахарный сироп", "amount": 300, "unit": "мл"}, {"name": "Бентонит", "amount": 2, "unit": "гр"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Фейхоа", "method": "Заливаем все в сотейник, вывариваем и цедим в бутылку",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 550, "unit": "мл",
        "calcIngredients": '[{"name": "Пюре фейхоа", "amount": 200, "unit": "гр"}, {"name": "Сок тетрапак (яблоко)", "amount": 100, "unit": "мл"}, {"name": "Пф лимонный фреш", "amount": 100, "unit": "мл"}, {"name": "Сахар", "amount": 50, "unit": "гр"}, {"name": "Вода", "amount": 150, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Текила/клубника", "method": "Добавляем все в вакуумный пакет и сювидим 4 часа на 55 градусах",
        "tags": '["Заготовка", "Су-вид"]', "glass": "", "ingredients": "[]", "baseYield": 250, "unit": "мл",
        "calcIngredients": '[{"name": "Клубника с/м", "amount": 100, "unit": "гр"}, {"name": "Текила La Mision del Siglo XXI silver", "amount": 300, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Кордиал мед", "method": "Перемешиваем в сотейнике на низких температурах",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 400, "unit": "мл",
        "calcIngredients": '[{"name": "Мёд цветочный", "amount": 100, "unit": "гр"}, {"name": "Вода", "amount": 300, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Карамельная пена", "method": "Греем сливки в сотейнике (не до кипения), добавляем карамельный сироп и ксантан, размешиваем венчиком и переливаем в кример",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 300, "unit": "мл",
        "calcIngredients": '[{"name": "Сливки 11% без лактозы", "amount": 300, "unit": "мл"}, {"name": "Ксантан", "amount": 1, "unit": "гр"}, {"name": "Карамельный сироп", "amount": 50, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Тархун", "method": "Перебиваем эстрагон с сиропом, затем фильтруем, потом балансируем яблочной кислотой",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 200, "unit": "мл",
        "calcIngredients": '[{"name": "Эстрагон свежий", "amount": 100, "unit": "гр"}, {"name": "Пф сахарный сироп", "amount": 300, "unit": "мл"}, {"name": "Яблочная кислота", "amount": 5, "unit": "гр"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Молочная кислота", "method": "Смешиваем всё в мернике",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 1000, "unit": "мл",
        "calcIngredients": '[{"name": "Молочная кислота 80%", "amount": 50, "unit": "мл"}, {"name": "Вода", "amount": 1000, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Маракуйя", "method": "",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 700, "unit": "мл",
        "calcIngredients": '[{"name": "Пюре маракуйя", "amount": 400, "unit": "гр"}, {"name": "Пф сахарный сироп", "amount": 150, "unit": "мл"}, {"name": "Пф лимонный фреш", "amount": 150, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Малина/Персик", "method": "",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 630, "unit": "мл",
        "calcIngredients": '[{"name": "Пюре малина", "amount": 200, "unit": "гр"}, {"name": "Пф Беллини", "amount": 300, "unit": "мл"}, {"name": "Пф сахарный сироп", "amount": 100, "unit": "мл"}, {"name": "Пф микс кислот", "amount": 100, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Лимонный фреш", "method": "",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 200, "unit": "мл",
        "calcIngredients": '[{"name": "Лимоны", "amount": 500, "unit": "гр"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Лаймовый фреш", "method": "",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 200, "unit": "мл",
        "calcIngredients": '[{"name": "Лаймы", "amount": 450, "unit": "гр"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Брусника", "method": "",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 850, "unit": "мл",
        "calcIngredients": '[{"name": "Пюре брусники", "amount": 500, "unit": "гр"}, {"name": "Пф сахарный сироп", "amount": 150, "unit": "мл"}, {"name": "Вода", "amount": 200, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Грог", "method": "Проварить все ингредиенты в сотейнике при 80° 10 минут",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 0, "unit": "мл",
        "calcIngredients": '[{"name": "Вода", "amount": 500, "unit": "мл"}, {"name": "Гвоздика", "amount": 5, "unit": "гр"}, {"name": "Бадьян", "amount": 2, "unit": "гр"}, {"name": "Апельсин", "amount": 6, "unit": "гр"}, {"name": "Лимон", "amount": 6, "unit": "гр"}, {"name": "Мёд", "amount": 30, "unit": "гр"}, {"name": "Варенье из шишек", "amount": 10, "unit": "гр"}, {"name": "Пф имбирь", "amount": 40, "unit": "мл"}, {"name": "Водка", "amount": 100, "unit": "мл"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Имбирь", "method": "Перебить имбирь и кипяток 1/1 в блендере, дать настояться 10 минут и отфильтровать через марлю",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 0, "unit": "мл",
        "calcIngredients": '[{"name": "Имбирь", "amount": 100, "unit": "гр"}, {"name": "Вода", "amount": 100, "unit": "гр"}]'
    },
    {
        "tab": "pf", "category": "pf", "title": "Пф Арбуз-Мята", "method": "Срезаем кожуру арбуза и перебиваем всё в блендере, после фильтруем через сито",
        "tags": '["Заготовка"]', "glass": "", "ingredients": "[]", "baseYield": 400, "unit": "мл",
        "calcIngredients": '[{"name": "Арбуз", "amount": 420, "unit": "гр"}, {"name": "Лимон", "amount": 150, "unit": "гр"}, {"name": "Сахарный сироп", "amount": 60, "unit": "мл"}, {"name": "Мята", "amount": 15, "unit": "гр"}]'
    }
]

# ======================= ЧЕК-ЛИСТЫ: СТАРТОВЫЕ ДАННЫЕ =======================
_BAR = ("Чистота на баре", ["Рабочие поверхности вымыты и продезинфицированы.", "Посуда и инвентарь вымыты и продезинфицированы.", "Санитайзеры в наличии.", "В конце смены мусор вынесен."])
_WH = ("Чистота на складе", ["Сухой склад приведён в порядок.", "Всё рассортировано и находится на своих местах.", "После сдачи смены склад закрыт."])
_FRUIT = ("Фруктовый склад", ["Нет пакетов и коробок.", "На фруктах нет наклеек и ценников.", "Соблюдается чистота.", "Фрукты рассортированы.", "Соблюдается ротация: старое — вперёд.", "После сдачи смены склад закрыт."])
_STOCK = ("Запасы и сроки", ["Станции укомплектованы.", "Сухой склад укомплектован.", "Проверены сроки годности скоропортящихся продуктов."])
_MARK = ("Маркировка", ["На всей нарезке есть дата и время.", "Заготовки промаркированы.", "Открытые упаковки промаркированы.", "Всё необходимое убрано в холодильник."])
_HAND = ("Сдача смены", ["Получена/передана информация по остаткам и проблемным вопросам.", "Ключи переданы.", "Смена сдана без замечаний.", "Нехватки, если есть, зафиксированы текстом."])
CHECKLIST_SEED = {
    "day": [
        _BAR, _WH, _FRUIT,
        ("Оборудование", ["Холодильники, блендер и ледогенератор чистые и исправны.", "Кофемашина и блендер промыты.", "Ёмкость льдогенератора пополнена."]),
        ("Кофейная станция", ["Станция заправлена и готова к сервису.", "Воронки подготовлены.", "Турки для шведской линии подготовлены."]),
        _STOCK, _MARK,
        ("Стоп/старт", ["Проверен и обновлён стоп/старт лист.", "Сняты позиции low stock."]),
        _HAND,
    ],
    "night": [
        _BAR, _WH, _FRUIT,
        ("Оборудование", ["Холодильники, блендер и ледогенератор чистые и исправны.", "Кофемашина и блендер промыты/выключены.", "Ёмкость льдогенератора пополнена."]),
        _STOCK, _MARK,
        ("Стоп/старт", ["Проверен и обновлён стоп/старт лист по меню."]),
        _HAND,
    ],
}
CHECKLIST_NOTES = {"day": "", "night": "Итоговая заявка на закупку составляется и передаётся в группу."}


# ======================= БАЗА ДАННЫХ =======================
@contextmanager
def db():
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_msk() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M")


def hash_pw(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${h}"


def check_pw(password: str, stored: str) -> bool:
    salt = stored.split("$")[0]
    return hmac.compare_digest(hash_pw(password, salt), stored)


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS active_order (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_name TEXT NOT NULL, quantity TEXT NOT NULL,
            comment TEXT, author_name TEXT NOT NULL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tab TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
            method TEXT, tags TEXT, glass TEXT, ingredients TEXT, baseYield INTEGER, unit TEXT, calcIngredients TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            password TEXT NOT NULL, role TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, user_id INTEGER NOT NULL,
            start_time TEXT NOT NULL, end_time TEXT NOT NULL, note TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS schedule_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor_id INTEGER, actor_name TEXT,
            action TEXT NOT NULL, description TEXT NOT NULL, before TEXT, after TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shifts_day ON shifts(day)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")

        c.execute("""CREATE TABLE IF NOT EXISTS checklist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shift TEXT NOT NULL, title TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '[]', position INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS checklist_progress (
            day TEXT NOT NULL, shift TEXT NOT NULL, item_id INTEGER NOT NULL, sub INTEGER NOT NULL,
            user_name TEXT, ts TEXT, PRIMARY KEY (day, shift, item_id, sub))""")
        c.execute("CREATE TABLE IF NOT EXISTS checklist_meta (shift TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '')")
        c.execute("""CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, title TEXT DEFAULT '', preview TEXT DEFAULT '',
            body TEXT NOT NULL DEFAULT '[]', pinned INTEGER DEFAULT 0, has_check INTEGER DEFAULT 0,
            updated_at INTEGER NOT NULL, deleted_at INTEGER)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id)")
        try:
            c.execute("ALTER TABLE schedule_history ADD COLUMN undone INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        c.execute("""CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL, auth TEXT NOT NULL, created_at INTEGER NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id)")

        c.execute("""CREATE TABLE IF NOT EXISTS shift_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shift_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            day TEXT NOT NULL, slot TEXT NOT NULL, file_path TEXT NOT NULL, uploaded_at INTEGER NOT NULL,
            UNIQUE(shift_id, slot))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shift_photos_day ON shift_photos(day)")
        c.execute("""CREATE TABLE IF NOT EXISTS photo_reminders_sent (
            shift_id INTEGER NOT NULL, slot TEXT NOT NULL, PRIMARY KEY (shift_id, slot))""")
        try:
            c.execute("ALTER TABLE shift_photos ADD COLUMN caption TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE shift_photos ADD COLUMN edited_at INTEGER")
        except sqlite3.OperationalError:
            pass

        # Отчёты смены (фото бара): 1 отчёт = 1 чек-поинт (или 1 доп. отчёт), внутри — несколько фото.
        # Заменяет старую модель shift_photos (1 строка = 1 фото, UNIQUE(shift_id, slot)), в которой
        # физически нельзя было привязать несколько фото к одному чек-поинту.
        # Комментарии к отчётам убраны: таблица shift_photo_comments остаётся в старых базах, но не используется.
        c.execute("""CREATE TABLE IF NOT EXISTS shift_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shift_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            day TEXT NOT NULL, slot TEXT NOT NULL, caption TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL, edited_at INTEGER,
            UNIQUE(shift_id, slot))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shift_reports_day ON shift_reports(day)")
        c.execute("""CREATE TABLE IF NOT EXISTS shift_report_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL, file_path TEXT NOT NULL,
            uploaded_at INTEGER NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shift_report_photos_report ON shift_report_photos(report_id)")

        # Миграция старых shift_photos → shift_reports + shift_report_photos (каждая старая запись = отдельный
        # отчёт с одним фото). Перенесённые строки сразу удаляются из shift_photos, поэтому миграция
        # выполняется ровно один раз и не «воскрешает» записи после архивации.
        old_photos = c.execute("SELECT * FROM shift_photos").fetchall()
        for p in old_photos:
            cur = c.execute("""INSERT OR IGNORE INTO shift_reports (shift_id, user_id, day, slot, caption, created_at, edited_at)
                                 VALUES (?,?,?,?,?,?,?)""",
                             (p["shift_id"], p["user_id"], p["day"], p["slot"], p["caption"] or "",
                              p["uploaded_at"], p["edited_at"]))
            rid = cur.lastrowid if cur.rowcount else c.execute(
                "SELECT id FROM shift_reports WHERE shift_id=? AND slot=?", (p["shift_id"], p["slot"])).fetchone()["id"]
            c.execute("INSERT INTO shift_report_photos (report_id, file_path, uploaded_at) VALUES (?,?,?)",
                      (rid, p["file_path"], p["uploaded_at"]))
        if old_photos:
            c.execute("DELETE FROM shift_photos")
            logger.info(f"Миграция shift_photos → shift_reports: перенесено {len(old_photos)} фото")

        # Стена позора: пост = подпись + отметка сотрудника; фото поста — в shame_post_photos (много на пост).
        # Колонка file_path оставлена только для совместимости со старыми базами (новый код пишет туда '').
        c.execute("""CREATE TABLE IF NOT EXISTS shame_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, day TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '', file_path TEXT NOT NULL DEFAULT '', tagged_user_id INTEGER,
            created_at INTEGER NOT NULL, edited_at INTEGER)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shame_posts_day ON shame_posts(day)")
        try:
            c.execute("ALTER TABLE shame_posts ADD COLUMN edited_at INTEGER")
        except sqlite3.OperationalError:
            pass
        c.execute("""CREATE TABLE IF NOT EXISTS shame_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            text TEXT NOT NULL, created_at INTEGER NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shame_comments_post ON shame_comments(post_id)")
        c.execute("""CREATE TABLE IF NOT EXISTS shame_post_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, file_path TEXT NOT NULL,
            uploaded_at INTEGER NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shame_post_photos_post ON shame_post_photos(post_id)")

        # Миграция старых постов (одно фото в shame_posts.file_path) → shame_post_photos.
        # После переноса file_path очищается, так что повторно пост не мигрирует.
        old_posts = c.execute("SELECT id, file_path, created_at FROM shame_posts WHERE file_path<>''").fetchall()
        for p in old_posts:
            c.execute("INSERT INTO shame_post_photos (post_id, file_path, uploaded_at) VALUES (?,?,?)",
                      (p["id"], p["file_path"], p["created_at"]))
            c.execute("UPDATE shame_posts SET file_path='' WHERE id=?", (p["id"],))
        if old_posts:
            logger.info(f"Миграция shame_posts.file_path → shame_post_photos: перенесено {len(old_posts)} фото")

        # Архивы: раз в ARCHIVE_INTERVAL_DAYS дней — по одной записи на категорию ('reports' / 'shame').
        c.execute("""CREATE TABLE IF NOT EXISTS archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, file_path TEXT NOT NULL,
            item_count INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_archives_category ON archives(category)")
        try:
            c.execute("ALTER TABLE archives ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # Стоп-лист: позиции меню (или произвольные товары), которые сейчас нельзя продавать.
        c.execute("""CREATE TABLE IF NOT EXISTS stop_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT, menu_item_id INTEGER, name TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '', user_id INTEGER, created_at INTEGER NOT NULL)""")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_stop_menu ON stop_list(menu_item_id) WHERE menu_item_id IS NOT NULL")

        # Лента объявлений от руководства + отметки «ознакомлен».
        c.execute("""CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, edited_at INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS announcement_reads (
            announcement_id INTEGER NOT NULL, user_id INTEGER NOT NULL, read_at INTEGER NOT NULL,
            PRIMARY KEY (announcement_id, user_id))""")

        # Служебные значения (например, время следующей архивации) — переживают перезапуски и деплои.
        c.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if not c.execute("SELECT 1 FROM app_meta WHERE key='next_archive_at'").fetchone():
            last = c.execute("SELECT MAX(created_at) FROM archives").fetchone()[0]
            base = int(last) if last else int(time.time())
            c.execute("INSERT INTO app_meta (key, value) VALUES ('next_archive_at', ?)",
                      (str(base + ARCHIVE_INTERVAL_DAYS * 86400),))

        if c.execute("SELECT COUNT(*) FROM checklist_items").fetchone()[0] == 0:
            for shift, items in CHECKLIST_SEED.items():
                for pos, (title, details) in enumerate(items):
                    c.execute("INSERT INTO checklist_items (shift, title, details, position) VALUES (?,?,?,?)",
                              (shift, title, json.dumps(details, ensure_ascii=False), pos))
            for shift, note in CHECKLIST_NOTES.items():
                c.execute("INSERT OR IGNORE INTO checklist_meta (shift, note) VALUES (?,?)", (shift, note))

        # мастер-аккаунт
        if not c.execute("SELECT 1 FROM users WHERE username=?", (MASTER_LOGIN,)).fetchone():
            c.execute("INSERT INTO users (username, name, password, role, status, created_at) VALUES (?,?,?,?,?,?)",
                      (MASTER_LOGIN, "Мастер", hash_pw(MASTER_PASSWORD), "master", "approved", now_msk()))

        # стартовое меню
        if c.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0] == 0:
            logger.info("База меню пуста. Загружаю стартовое меню...")
            for i in INITIAL_MENU:
                c.execute("""INSERT INTO menu_items (tab, category, title, method, tags, glass, ingredients, baseYield, unit, calcIngredients)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (i['tab'], i['category'], i['title'], i['method'], i['tags'],
                                                     i['glass'], i['ingredients'], i['baseYield'], i['unit'], i['calcIngredients']))


init_db()


# ======================= АВТОРИЗАЦИЯ =======================
def current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Требуется вход")
    token = authorization[7:]
    with db() as c:
        row = c.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
                           WHERE s.token=? AND s.created_at>?""", (token, time.time() - SESSION_TTL)).fetchone()
    if not row or row["status"] != "approved":
        raise HTTPException(401, "Сессия истекла, войдите заново")
    return dict(row)


def current_user_flexible(authorization: Optional[str] = Header(None), token: Optional[str] = None) -> dict:
    """Как current_user, но также принимает ?token=... — нужно для <img src>, который не может послать заголовок Authorization."""
    tok = authorization[7:] if (authorization and authorization.startswith("Bearer ")) else token
    if not tok:
        raise HTTPException(401, "Требуется вход")
    with db() as c:
        row = c.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
                           WHERE s.token=? AND s.created_at>?""", (tok, time.time() - SESSION_TTL)).fetchone()
    if not row or row["status"] != "approved":
        raise HTTPException(401, "Сессия истекла, войдите заново")
    return dict(row)


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in ADMIN_ROLES:
        raise HTTPException(403, "Недостаточно прав")
    return user


def require_schedule_editor(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in SCHEDULE_EDITORS:
        raise HTTPException(403, "Недостаточно прав для изменения графика")
    return user


def require_not_waiter(user: dict = Depends(current_user)) -> dict:
    """Для действий, доступных всем ролям, кроме официанта (например, принудительная отправка заявки)."""
    if user["role"] == "waiter":
        raise HTTPException(403, "Недостаточно прав")
    return user


def require_team(user: dict = Depends(current_user)) -> dict:
    """Архивы во вкладке «Команда» видят старший бармен, бар-менеджер и мастер."""
    if user["role"] not in TEAM_ROLES:
        raise HTTPException(403, "Недостаточно прав")
    return user


def require_team_flexible(user: dict = Depends(current_user_flexible)) -> dict:
    if user["role"] not in TEAM_ROLES:
        raise HTTPException(403, "Недостаточно прав")
    return user


def public_user(u) -> dict:
    return {"id": u["id"], "username": u["username"], "name": u["name"], "role": u["role"]}


class RegisterIn(BaseModel):
    username: str
    name: str
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


class RoleIn(BaseModel):
    role: str


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscribeIn(BaseModel):
    endpoint: str
    keys: PushKeys


class PushUnsubscribeIn(BaseModel):
    endpoint: str


# ======================= PUSH-УВЕДОМЛЕНИЯ =======================
# Важно для iPhone: Apple принимает пуши только с корректным VAPID «sub» (mailto: реального адреса или https-URL),
# поэтому VAPID_CLAIMS_EMAIL нужно задать своим адресом. TTL > 0 — чтобы пуш дошёл, если телефон был не в сети.
PUSH_TTL = 24 * 3600


def _push_now(user_ids: List[int], title: str, body: str, url: str = "/", tag: Optional[str] = None) -> dict:
    """Синхронная отправка. Возвращает подробный отчёт — используется и в фоне, и в «тестовом пуше»."""
    report = {"configured": bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY), "devices": 0, "sent": 0, "errors": []}
    if not report["configured"] or not user_ids:
        return report
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or url}, ensure_ascii=False)
    with db() as c:
        q = ",".join("?" * len(user_ids))
        subs = c.execute(f"SELECT * FROM push_subscriptions WHERE user_id IN ({q})", list(user_ids)).fetchall()
    report["devices"] = len(subs)
    dead = []
    for s in subs:
        try:
            webpush(
                subscription_info={"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIMS_EMAIL},   # новый словарь на каждую отправку: pywebpush дописывает в него aud
                ttl=PUSH_TTL,
                headers={"Urgency": "high"},
                timeout=10,
            )
            report["sent"] += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            text = (getattr(e.response, "text", "") or str(e))[:200]
            host = s["endpoint"].split("/")[2] if "//" in s["endpoint"] else s["endpoint"][:40]
            report["errors"].append({"status": code, "host": host, "detail": text})
            if code in (404, 410):
                dead.append(s["id"])
            logger.warning(f"Push не доставлен ({host}, {code}): {text}")
        except Exception as e:
            report["errors"].append({"status": None, "host": "", "detail": str(e)[:200]})
            logger.warning(f"Push: ошибка отправки: {e}")
    if dead:
        with db() as c:
            c.executemany("DELETE FROM push_subscriptions WHERE id=?", [(i,) for i in dead])
    return report


def send_push(user_ids: List[int], title: str, body: str, url: str = "/", tag: Optional[str] = None):
    """Отправляет пуш в фоне — запрос пользователя не ждёт сетевого ответа от Apple/Google."""
    ids = sorted({int(u) for u in user_ids or [] if u})
    if not ids or not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return
    threading.Thread(target=_push_now, args=(ids, title, body, url, tag), daemon=True).start()


def approved_ids(c, exclude: Optional[int] = None, roles: Optional[set] = None) -> List[int]:
    rows = c.execute("SELECT id, role FROM users WHERE status='approved'").fetchall()
    return [r["id"] for r in rows if r["id"] != exclude and (roles is None or r["role"] in roles)]


# ======================= TELEGRAM (только 07:00) =======================
async def send_order_to_tg():
    with db() as c:
        rows = c.execute("SELECT item_name, quantity, comment, author_name FROM active_order ORDER BY timestamp ASC").fetchall()
        if not rows:
            logger.info("Заявка пуста — отправлять нечего")
            return False
        if not bot:
            logger.error("BOT_TOKEN не задан — заявка не отправлена")
            return False

        authors, items = [], []
        for r in rows:
            a = html.escape(r["author_name"])
            if a not in authors:
                authors.append(a)
            comm = f" (<i>{html.escape(r['comment'])}</i>)" if r["comment"] else ""
            items.append(f"• <b>{html.escape(r['item_name'])}</b> — {html.escape(r['quantity'])}{comm}")
        message = "📦 <b>НОВАЯ ЗАЯВКА</b>\n\n" + "\n".join(items) + f"\n\n👤 <b>Кто составил:</b> {', '.join(authors)}"
        try:
            await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode=ParseMode.HTML)
            if max_bot and MAX_CHAT_ID:
                try:
                    plain = "НОВАЯ ЗАЯВКА\n\n" + "\n".join(
                        f"• {r['item_name']} — {r['quantity']}" + (f" ({r['comment']})" if r["comment"] else "")
                        for r in rows
                    ) + f"\n\nКто составил: {', '.join(authors)}"
                    await max_bot.send_message(chat_id=int(MAX_CHAT_ID), text=plain)
                except Exception as e:
                    logger.warning(f"Не удалось продублировать заявку в MAX: {e}")
            c.execute("DELETE FROM active_order")
            return True
        except Exception as e:
            logger.error(f"Ошибка ТГ: {e}")
            return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not bot:
        logger.warning("BOT_TOKEN не задан: отправка в Telegram отключена")
    if MaxBot and not MAX_BOT_TOKEN:
        logger.info("MAX_BOT_TOKEN не задан: интеграция с MAX отключена")
    scheduler.add_job(send_order_to_tg, "cron", hour=ORDER_HOUR, minute=ORDER_MINUTE,
                      misfire_grace_time=3600, coalesce=True, max_instances=1)
    scheduler.add_job(check_photo_reminders, "cron", minute="*", second=5,
                      misfire_grace_time=50, coalesce=True, max_instances=1)
    scheduler.add_job(archive_if_due, "interval", minutes=5,
                      misfire_grace_time=600, coalesce=True, max_instances=1)
    scheduler.start()
    async with AsyncExitStack() as stack:
        if max_webhook:
            # Регистрирует webhook в MAX при старте и снимает подписку при остановке.
            await stack.enter_async_context(max_webhook.lifespan(app))
        yield
    scheduler.shutdown()
    if bot:
        await bot.session.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=500)

max_webhook = FastAPIMaxWebhook(dp=max_dp, bot=max_bot) if max_bot else None
if max_webhook:
    max_webhook.setup(app, path=MAX_WEBHOOK_PATH)


# ======================= PUSH: РОУТЫ (сами роуты; логика отправки — send_push выше) =======================
@app.get("/api/push/public_key")
async def push_public_key():
    return {"key": VAPID_PUBLIC_KEY}


@app.get("/api/push/status")
async def push_status(user: dict = Depends(current_user)):
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM push_subscriptions WHERE user_id=?", (user["id"],)).fetchone()[0]
    return {"configured": bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY), "devices": n,
            "claims_ok": VAPID_CLAIMS_EMAIL.startswith(("mailto:", "https://")) and "example.com" not in VAPID_CLAIMS_EMAIL}


@app.post("/api/push/test")
async def push_test(user: dict = Depends(current_user)):
    """Тестовый пуш себе — с подробным ответом сервисов Apple/Google, чтобы сразу видеть причину проблемы."""
    report = await asyncio.to_thread(_push_now, [user["id"]], "Тугай", "Уведомления работают ✅", "/", "test")
    report["claims_ok"] = VAPID_CLAIMS_EMAIL.startswith(("mailto:", "https://")) and "example.com" not in VAPID_CLAIMS_EMAIL
    return report


@app.post("/api/push/subscribe")
async def push_subscribe(body: PushSubscribeIn, user: dict = Depends(current_user)):
    with db() as c:
        c.execute("""INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at)
                     VALUES (?,?,?,?,?)
                     ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth""",
                  (user["id"], body.endpoint, body.keys.p256dh, body.keys.auth, int(time.time())))
    return {"status": "success"}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(body: PushUnsubscribeIn, user: dict = Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint=? AND user_id=?", (body.endpoint, user["id"]))
    return {"status": "success"}


# ======================= АККАУНТЫ =======================
@app.post("/api/register")
async def register(body: RegisterIn):
    username, name = body.username.strip().lower(), body.name.strip()
    if not re.fullmatch(r"[a-z0-9_.\-]{3,32}", username):
        raise HTTPException(400, "Логин: 3–32 символа, латиница, цифры, . _ -")
    if not 2 <= len(name) <= 40:
        raise HTTPException(400, "Имя: от 2 до 40 символов")
    if len(body.password) < 6:
        raise HTTPException(400, "Пароль: минимум 6 символов")
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(409, "Такой логин уже занят")
        c.execute("INSERT INTO users (username, name, password, role, status, created_at) VALUES (?,?,?,?,?,?)",
                  (username, name, hash_pw(body.password), "", "pending", now_msk()))
        admins = approved_ids(c, roles=ADMIN_ROLES)
    send_push(admins, "Новая регистрация", f"{name} ждёт подтверждения аккаунта", "/#team", "pending")
    return {"status": "pending"}


@app.post("/api/login")
async def login(body: LoginIn):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (body.username.strip().lower(),)).fetchone()
        if not u or not check_pw(body.password, u["password"]):
            raise HTTPException(401, "Неверный логин или пароль")
        if u["status"] != "approved":
            raise HTTPException(403, "Аккаунт ожидает подтверждения администратором")
        token = secrets.token_urlsafe(32)
        c.execute("DELETE FROM sessions WHERE created_at<?", (time.time() - SESSION_TTL,))
        c.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)", (token, u["id"], int(time.time())))
    return {"token": token, "user": public_user(u)}


@app.post("/api/logout")
async def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (authorization[7:],))
    return {"status": "success"}


@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return public_user(user)


@app.get("/api/staff")
async def staff(user: dict = Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM users WHERE status='approved' ORDER BY name COLLATE NOCASE").fetchall()
    return [public_user(r) for r in rows]


@app.get("/api/users")
async def list_users(admin: dict = Depends(require_admin)):
    with db() as c:
        rows = c.execute("SELECT id, username, name, role, status, created_at FROM users ORDER BY created_at DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def get_target(c, uid: int, actor: dict):
    t = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not t:
        raise HTTPException(404, "Пользователь не найден")
    if t["role"] == "master":
        raise HTTPException(403, "Мастер-аккаунт изменить нельзя")
    if t["id"] == actor["id"]:
        raise HTTPException(400, "Нельзя менять собственный аккаунт")
    return t


@app.post("/api/users/{uid}/approve")
async def approve_user(uid: int, body: RoleIn, admin: dict = Depends(require_admin)):
    if body.role not in ROLES:
        raise HTTPException(400, "Неизвестная роль")
    with db() as c:
        t = get_target(c, uid, admin)
        c.execute("UPDATE users SET status='approved', role=? WHERE id=?", (body.role, uid))
    return {"status": "success"}


@app.post("/api/users/{uid}/role")
async def set_role(uid: int, body: RoleIn, admin: dict = Depends(require_admin)):
    if body.role not in ROLES:
        raise HTTPException(400, "Неизвестная роль")
    with db() as c:
        get_target(c, uid, admin)
        c.execute("UPDATE users SET role=? WHERE id=? AND status='approved'", (body.role, uid))
    return {"status": "success"}


@app.delete("/api/users/{uid}")
async def delete_user(uid: int, admin: dict = Depends(require_admin)):
    with db() as c:
        t = get_target(c, uid, admin)
        today = datetime.now(MSK).strftime("%Y-%m-%d")
        removed = c.execute("SELECT COUNT(*) FROM shifts WHERE user_id=? AND day>=?", (uid, today)).fetchone()[0]
        c.execute("DELETE FROM shifts WHERE user_id=? AND day>=?", (uid, today))
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        if removed:
            log_change(c, admin, "staff", f"Удалён сотрудник {t['name']}: снято будущих смен — {removed}")
    return {"status": "success"}


# ======================= МЕНЮ =======================
class MenuItemBase(BaseModel):
    tab: str
    category: str
    title: str
    method: str = ""
    tags: str = "[]"
    glass: str = ""
    ingredients: str = "[]"
    baseYield: int = 0
    unit: str = ""
    calcIngredients: str = "[]"


@app.get("/api/menu")
async def get_menu(user: dict = Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM menu_items").fetchall()
    return [{
        "id": r["id"], "tab": r["tab"], "category": r["category"], "title": r["title"], "method": r["method"],
        "tags": json.loads(r["tags"] or "[]"), "glass": r["glass"],
        "ingredients": json.loads(r["ingredients"] or "[]"),
        "baseYield": r["baseYield"], "unit": r["unit"],
        "calcIngredients": json.loads(r["calcIngredients"] or "[]"),
    } for r in rows]


@app.post("/api/menu")
async def add_menu_item(item: MenuItemBase, admin: dict = Depends(require_admin)):
    with db() as c:
        c.execute("""INSERT INTO menu_items (tab, category, title, method, tags, glass, ingredients, baseYield, unit, calcIngredients)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (item.tab, item.category, item.title, item.method, item.tags, item.glass,
                                             item.ingredients, item.baseYield, item.unit, item.calcIngredients))
    return {"status": "success"}


@app.delete("/api/menu/{item_id}")
async def delete_menu_item(item_id: int, admin: dict = Depends(require_admin)):
    with db() as c:
        c.execute("DELETE FROM menu_items WHERE id=?", (item_id,))
        c.execute("DELETE FROM stop_list WHERE menu_item_id=?", (item_id,))
    return {"status": "success"}


@app.post("/api/admin/reset_menu")
async def reset_menu(admin: dict = Depends(require_admin)):
    with db() as c:
        c.execute("DROP TABLE IF EXISTS menu_items")
        c.execute("DELETE FROM stop_list WHERE menu_item_id IS NOT NULL")
    init_db()
    return {"status": "success"}


# ======================= ЗАЯВКИ =======================
class OrderItem(BaseModel):
    item_name: str
    quantity: str
    comment: str = ""


@app.get("/api/get_orders")
async def get_orders(user: dict = Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT id, item_name, quantity, comment, author_name FROM active_order ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/add_order")
async def add_order(item: OrderItem, user: dict = Depends(current_user)):
    if not item.item_name.strip() or not item.quantity.strip():
        raise HTTPException(400, "Укажите позицию и количество")
    with db() as c:
        c.execute("INSERT INTO active_order (item_name, quantity, comment, author_name) VALUES (?,?,?,?)",
                  (item.item_name.strip(), item.quantity.strip(), item.comment.strip(), user["name"]))
    return {"status": "success"}


@app.delete("/api/delete_order/{order_id}")
async def delete_order(order_id: int, user: dict = Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM active_order WHERE id=?", (order_id,))
    return {"status": "success"}


@app.post("/api/orders/send_now")
async def send_order_now(actor: dict = Depends(require_not_waiter)):
    """Принудительная отправка текущей заявки в Telegram-группу, не дожидаясь 07:00."""
    if not bot:
        raise HTTPException(400, "BOT_TOKEN не настроен на сервере — отправка недоступна")
    ok = await send_order_to_tg()
    if not ok:
        raise HTTPException(400, "Список закупки пуст — отправлять нечего")
    logger.info(f"Заявка отправлена вручную пользователем {actor['name']}")
    return {"status": "success"}


# ======================= ГРАФИК СМЕН =======================
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SHIFT_SQL = """SELECT s.id, s.day, s.user_id, COALESCE(u.name, 'Удалённый сотрудник') AS user_name,
               COALESCE(u.role, '') AS role, s.start_time, s.end_time, s.note
               FROM shifts s LEFT JOIN users u ON u.id = s.user_id"""


def shift_dict(r) -> dict:
    return {"id": r["id"], "date": r["day"], "user_id": r["user_id"], "user_name": r["user_name"], "role": r["role"],
            "start": r["start_time"], "end": r["end_time"], "note": r["note"] or ""}


def get_shift(c, sid: int) -> dict:
    r = c.execute(SHIFT_SQL + " WHERE s.id=?", (sid,)).fetchone()
    if not r:
        raise HTTPException(404, "Смена не найдена")
    return shift_dict(r)


def ddmm(d: str) -> str:
    return f"{d[8:10]}.{d[5:7]}"


def fmt_shift(s: dict) -> str:
    return f"{s['user_name']}, {ddmm(s['date'])} {s['start']}–{s['end']}"


def parse_day(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        raise HTTPException(400, "Неверная дата")


def check_times(start: str, end: str):
    if not TIME_RE.match(start or "") or not TIME_RE.match(end or ""):
        raise HTTPException(400, "Время в формате ЧЧ:ММ")
    if start == end:
        raise HTTPException(400, "Начало и конец смены совпадают")


def check_user(c, uid: int):
    if not c.execute("SELECT 1 FROM users WHERE id=? AND status='approved'", (uid,)).fetchone():
        raise HTTPException(400, "Сотрудник не найден")


def log_change(c, actor: dict, action: str, description: str, before=None, after=None):
    c.execute("""INSERT INTO schedule_history (ts, actor_id, actor_name, action, description, before, after)
                 VALUES (?,?,?,?,?,?,?)""",
              (now_msk(), actor["id"], actor["name"], action, description,
               json.dumps(before, ensure_ascii=False) if before is not None else None,
               json.dumps(after, ensure_ascii=False) if after is not None else None))


class ShiftIn(BaseModel):
    user_id: int
    date: str
    start: str = "10:00"
    end: str = "22:00"
    note: str = ""
    dates: Optional[List[str]] = None       # несколько дней сразу


class SwapIn(BaseModel):
    a: int
    b: int


class WeekIn(BaseModel):
    start: str                               # понедельник недели


class CopyWeekIn(BaseModel):
    from_start: str
    to_start: str


@app.get("/api/schedule")
async def get_schedule(start: str, end: str, user: dict = Depends(current_user)):
    parse_day(start), parse_day(end)
    with db() as c:
        rows = c.execute(SHIFT_SQL + " WHERE s.day BETWEEN ? AND ? ORDER BY s.day, s.start_time, user_name", (start, end)).fetchall()
    return [shift_dict(r) for r in rows]


@app.post("/api/schedule")
async def add_shift(body: ShiftIn, actor: dict = Depends(require_schedule_editor)):
    check_times(body.start, body.end)
    days = list(dict.fromkeys(body.dates or [body.date]))
    for d in days:
        parse_day(d)
    created = 0
    with db() as c:
        check_user(c, body.user_id)
        for d in days:
            if c.execute("SELECT 1 FROM shifts WHERE day=? AND user_id=? AND start_time=? AND end_time=?",
                         (d, body.user_id, body.start, body.end)).fetchone():
                continue
            cur = c.execute("INSERT INTO shifts (day, user_id, start_time, end_time, note) VALUES (?,?,?,?,?)",
                            (d, body.user_id, body.start, body.end, body.note.strip()))
            s = get_shift(c, cur.lastrowid)
            log_change(c, actor, "add", f"Добавлена смена: {fmt_shift(s)}", None, s)
            created += 1
    if created:
        send_push([body.user_id], "Новая смена", f"Вам назначена смена: {fmt_shift(s)}", "/#schedule")
    return {"created": created}


@app.put("/api/schedule/{sid}")
async def edit_shift(sid: int, body: ShiftIn, actor: dict = Depends(require_schedule_editor)):
    check_times(body.start, body.end)
    parse_day(body.date)
    with db() as c:
        before = get_shift(c, sid)
        check_user(c, body.user_id)
        c.execute("UPDATE shifts SET day=?, user_id=?, start_time=?, end_time=?, note=? WHERE id=?",
                  (body.date, body.user_id, body.start, body.end, body.note.strip(), sid))
        after = get_shift(c, sid)
        if before != after:
            log_change(c, actor, "edit", f"Изменена смена: {fmt_shift(before)} → {fmt_shift(after)}", before, after)
    if before != after:
        notify_ids = {before["user_id"], after["user_id"]}
        send_push(list(notify_ids), "Смена изменена", f"{fmt_shift(before)} → {fmt_shift(after)}", "/#schedule")
    return {"status": "success"}


@app.delete("/api/schedule/{sid}")
async def delete_shift(sid: int, actor: dict = Depends(require_schedule_editor)):
    with db() as c:
        s = get_shift(c, sid)
        c.execute("DELETE FROM shifts WHERE id=?", (sid,))
        log_change(c, actor, "delete", f"Удалена смена: {fmt_shift(s)}", s, None)
    send_push([s["user_id"]], "Смена отменена", f"Снята смена: {fmt_shift(s)}", "/#schedule", "schedule")
    return {"status": "success"}


@app.post("/api/schedule/swap")
async def swap_shifts(body: SwapIn, actor: dict = Depends(require_schedule_editor)):
    if body.a == body.b:
        raise HTTPException(400, "Выберите две разные смены")
    with db() as c:
        a, b = get_shift(c, body.a), get_shift(c, body.b)
        c.execute("UPDATE shifts SET user_id=? WHERE id=?", (b["user_id"], a["id"]))
        c.execute("UPDATE shifts SET user_id=? WHERE id=?", (a["user_id"], b["id"]))
        log_change(c, actor, "swap", f"Обмен сменами: {fmt_shift(a)} ⇄ {fmt_shift(b)}", [a, b],
                   [get_shift(c, a["id"]), get_shift(c, b["id"])])
    send_push([a["user_id"], b["user_id"]], "Смены обменяны", f"{fmt_shift(a)} ⇄ {fmt_shift(b)}", "/#schedule")
    return {"status": "success"}


@app.post("/api/schedule/copy_week")
async def copy_week(body: CopyWeekIn, actor: dict = Depends(require_schedule_editor)):
    frm, to = parse_day(body.from_start), parse_day(body.to_start)
    delta = (to - frm).days
    added = []
    with db() as c:
        rows = c.execute(SHIFT_SQL + " WHERE s.day BETWEEN ? AND ?",
                         (body.from_start, (frm + timedelta(days=6)).strftime("%Y-%m-%d"))).fetchall()
        for r in rows:
            if not r["role"]:            # сотрудник удалён
                continue
            new_day = (parse_day(r["day"]) + timedelta(days=delta)).strftime("%Y-%m-%d")
            if c.execute("SELECT 1 FROM shifts WHERE day=? AND user_id=? AND start_time=? AND end_time=?",
                         (new_day, r["user_id"], r["start_time"], r["end_time"])).fetchone():
                continue
            cur = c.execute("INSERT INTO shifts (day, user_id, start_time, end_time, note) VALUES (?,?,?,?,?)",
                            (new_day, r["user_id"], r["start_time"], r["end_time"], r["note"] or ""))
            added.append(get_shift(c, cur.lastrowid))
        if added:
            log_change(c, actor, "copy", f"Скопирована неделя {ddmm(body.from_start)} → {ddmm(body.to_start)}: добавлено смен — {len(added)}", None, added)
    return {"created": len(added)}


@app.post("/api/schedule/clear_week")
async def clear_week(body: WeekIn, actor: dict = Depends(require_schedule_editor)):
    start = parse_day(body.start)
    end = (start + timedelta(days=6)).strftime("%Y-%m-%d")
    with db() as c:
        rows = [shift_dict(r) for r in c.execute(SHIFT_SQL + " WHERE s.day BETWEEN ? AND ?", (body.start, end)).fetchall()]
        if rows:
            c.execute("DELETE FROM shifts WHERE day BETWEEN ? AND ?", (body.start, end))
            log_change(c, actor, "clear", f"Очищена неделя {ddmm(body.start)}–{ddmm(end)}: удалено смен — {len(rows)}", rows, None)
    return {"deleted": len(rows)}


class BulkOp(BaseModel):
    op: str                     # set — заменить смены сотрудника в этот день; add — добавить; clear — убрать все; delete — по id
    user_id: Optional[int] = None
    date: Optional[str] = None
    start: str = ""
    end: str = ""
    note: str = ""
    id: Optional[int] = None


class BulkIn(BaseModel):
    ops: List[BulkOp] = []


@app.post("/api/schedule/bulk")
async def bulk_schedule(body: BulkIn, actor: dict = Depends(require_schedule_editor)):
    """Сохраняет сразу пачку изменений графика (режим «кисти» и офлайн-очередь).
    Одна запись в истории (её можно отменить) и одно уведомление каждому затронутому сотруднику."""
    if len(body.ops) > 500:
        raise HTTPException(400, "Слишком много изменений за раз")
    removed, added, touched = [], [], {}
    with db() as c:
        for o in body.ops:
            if o.op == "delete":
                if not o.id or not shift_exists(c, o.id):
                    continue
                s = get_shift(c, o.id)
                c.execute("DELETE FROM shifts WHERE id=?", (o.id,))
                removed.append(s)
                touched.setdefault(s["user_id"], set()).add(s["date"])
                continue
            if o.op not in ("set", "add", "clear") or not o.user_id or not o.date:
                raise HTTPException(400, "Неизвестное изменение графика")
            parse_day(o.date)
            if o.op != "clear":
                check_times(o.start, o.end)
                check_user(c, o.user_id)
            existing = [shift_dict(r) for r in c.execute(SHIFT_SQL + " WHERE s.day=? AND s.user_id=?", (o.date, o.user_id)).fetchall()]
            note = (o.note or "").strip()[:200]
            if o.op == "set" and len(existing) == 1 and existing[0]["start"] == o.start and existing[0]["end"] == o.end \
                    and existing[0]["note"] == note:
                continue                                        # уже так — ничего не меняем
            if o.op == "add" and any(x["start"] == o.start and x["end"] == o.end for x in existing):
                continue
            if o.op in ("set", "clear"):
                for s in existing:
                    c.execute("DELETE FROM shifts WHERE id=?", (s["id"],))
                    removed.append(s)
            if o.op in ("set", "add"):
                sid = c.execute("INSERT INTO shifts (day, user_id, start_time, end_time, note) VALUES (?,?,?,?,?)",
                                (o.date, o.user_id, o.start, o.end, note)).lastrowid
                added.append(get_shift(c, sid))
            touched.setdefault(o.user_id, set()).add(o.date)
        if added or removed:
            log_change(c, actor, "bulk", f"Изменён график: добавлено {len(added)}, снято {len(removed)}", removed, added)
    for uid, days in touched.items():
        if uid == actor["id"] or not any(x["user_id"] == uid for x in added + removed):
            continue
        dates = ", ".join(ddmm(d) for d in sorted(days)[:6]) + ("…" if len(days) > 6 else "")
        send_push([uid], "График обновлён", f"Изменения в ваших сменах: {dates}", "/#schedule", "schedule")
    return {"added": len(added), "removed": len(removed), "shifts": added}


# ---------- отмена изменений графика ----------
UNDOABLE = {"add", "edit", "delete", "swap", "copy", "clear", "bulk"}


def shift_exists(c, sid) -> bool:
    return c.execute("SELECT 1 FROM shifts WHERE id=?", (sid,)).fetchone() is not None


def restore_shift(c, s: dict) -> bool:
    """Возвращает смену из снимка. False — если сотрудник удалён или такая смена уже есть."""
    if not c.execute("SELECT 1 FROM users WHERE id=? AND status='approved'", (s["user_id"],)).fetchone():
        return False
    if c.execute("SELECT 1 FROM shifts WHERE day=? AND user_id=? AND start_time=? AND end_time=?",
                 (s["date"], s["user_id"], s["start"], s["end"])).fetchone():
        return False
    c.execute("INSERT INTO shifts (day, user_id, start_time, end_time, note) VALUES (?,?,?,?,?)",
              (s["date"], s["user_id"], s["start"], s["end"], s.get("note", "")))
    return True


def apply_undo(c, actor: dict, h):
    if h["undone"]:
        raise HTTPException(409, "Это изменение уже отменено")
    action = h["action"]
    if action not in UNDOABLE:
        raise HTTPException(400, "Это изменение нельзя отменить")
    before = json.loads(h["before"]) if h["before"] else None
    after = json.loads(h["after"]) if h["after"] else None

    if action == "add":
        if not shift_exists(c, after["id"]):
            raise HTTPException(409, "Смена уже удалена")
        c.execute("DELETE FROM shifts WHERE id=?", (after["id"],))
    elif action == "delete":
        if not restore_shift(c, before):
            raise HTTPException(409, "Не удалось вернуть смену: сотрудник удалён или такая смена уже есть")
    elif action == "edit":
        if not shift_exists(c, before["id"]):
            raise HTTPException(409, "Смена была удалена — вернуть прежние значения нельзя")
        check_user(c, before["user_id"])
        c.execute("UPDATE shifts SET day=?, user_id=?, start_time=?, end_time=?, note=? WHERE id=?",
                  (before["date"], before["user_id"], before["start"], before["end"], before["note"], before["id"]))
    elif action == "swap":
        if not all(shift_exists(c, s["id"]) for s in before):
            raise HTTPException(409, "Одна из смен уже удалена")
        for s in before:
            c.execute("UPDATE shifts SET user_id=? WHERE id=?", (s["user_id"], s["id"]))
    elif action == "copy":
        for s in after:
            c.execute("DELETE FROM shifts WHERE id=?", (s["id"],))
    elif action == "clear":
        if sum(1 for s in before if restore_shift(c, s)) == 0:
            raise HTTPException(409, "Смены уже возвращены или сотрудники удалены")
    elif action == "bulk":
        for s in after or []:
            c.execute("DELETE FROM shifts WHERE id=?", (s["id"],))
        for s in before or []:
            restore_shift(c, s)
    c.execute("UPDATE schedule_history SET undone=1 WHERE id=?", (h["id"],))
    log_change(c, actor, "undo", f"Отменено: {h['description']}")


@app.post("/api/schedule/history/{hid}/undo")
async def undo_entry(hid: int, actor: dict = Depends(require_schedule_editor)):
    with db() as c:
        h = c.execute("SELECT * FROM schedule_history WHERE id=?", (hid,)).fetchone()
        if not h:
            raise HTTPException(404, "Запись не найдена")
        apply_undo(c, actor, h)
    return {"status": "success"}


@app.post("/api/schedule/undo_last")
async def undo_last(actor: dict = Depends(require_schedule_editor)):
    with db() as c:
        h = c.execute("""SELECT * FROM schedule_history WHERE undone=0
                         AND action IN ('add','edit','delete','swap','copy','clear','bulk') ORDER BY id DESC LIMIT 1""").fetchone()
        if not h:
            raise HTTPException(404, "Нечего отменять")
        apply_undo(c, actor, h)
        return {"description": h["description"]}


@app.get("/api/schedule/history")
async def schedule_history(limit: int = 200, user: dict = Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT id, ts, actor_name, action, description, undone FROM schedule_history ORDER BY id DESC LIMIT ?",
                         (min(max(limit, 1), 500),)).fetchall()
    return [{**dict(r), "undone": bool(r["undone"]), "undoable": r["action"] in UNDOABLE and not r["undone"]} for r in rows]


# ======================= ФОТО СМЕНЫ (обязательные фото бара + напоминания + автоудаление) =======================
# Расширения по content-type; если браузер/камера не прислали content-type (нередко на мобильных),
# подстраховываемся расширением из имени файла — иначе валидные фото могли ошибочно отклоняться.
CONTENT_TYPE_EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/heic": "heic", "image/heif": "heif", "image/gif": "gif",
}
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "heic", "heif", "gif"}


def is_image_upload(file: UploadFile) -> bool:
    ct = (file.content_type or "").lower()
    if ct.startswith("image/"):
        return True
    ext = os.path.splitext(file.filename or "")[1].lstrip(".").lower()
    return ext in IMAGE_EXTS


def photo_ext(file: UploadFile) -> str:
    ext = CONTENT_TYPE_EXT.get((file.content_type or "").lower())
    if ext:
        return ext
    ext = os.path.splitext(file.filename or "")[1].lstrip(".").lower()
    return ext if ext in IMAGE_EXTS else "jpg"


def hm_to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def photo_checkpoints_for_shift(s: dict) -> list:
    """Возвращает список обязательных чекпоинтов для смены: [{slot, date, due_dt}, ...].
    Дневная смена 08:00–20:00 → 12:00 и 20:00. Ночная 20:00–08:00 → только 08:00 (сутра, уже следующая дата).
    Для остальных вариантов смен по умолчанию — одно фото к моменту окончания смены."""
    times = PHOTO_SHIFT_RULES.get((s["start"], s["end"])) or [s["end"]]
    start_date = parse_day(s["date"])
    crosses = hm_to_min(s["end"]) <= hm_to_min(s["start"])
    out = []
    for t in times:
        date_obj = start_date + timedelta(days=1) if (crosses and hm_to_min(t) < hm_to_min(s["start"])) else start_date
        h, m = map(int, t.split(":"))
        due_dt = datetime(date_obj.year, date_obj.month, date_obj.day, h, m, tzinfo=MSK)
        out.append({"slot": t, "date": date_obj.strftime("%Y-%m-%d"), "due_dt": due_dt})
    return out


MAX_PHOTOS_PER_POST = 10   # лимит фото в одном отчёте смены / посте стены позора
MAX_PHOTO_BYTES = 25 * 1024 * 1024   # лимит размера одного фото


def remove_files(paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def parse_keep_ids(raw: str) -> set:
    return {int(x) for x in (raw or "").split(",") if x.strip().isdigit()}


async def read_image_uploads(files: Optional[List[UploadFile]]) -> list:
    """Читает загруженные файлы и возвращает [(ext, bytes), ...] — только непустые изображения."""
    out = []
    for f in files or []:
        if not is_image_upload(f):
            continue
        data = await f.read()
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(400, f"Фото «{f.filename or 'без имени'}» больше {MAX_PHOTO_BYTES // (1024 * 1024)} МБ")
        if data:
            out.append((photo_ext(f), data))
    return out


def write_photo_files(day_dir: str, prefix: str, uploads: list) -> list:
    os.makedirs(day_dir, exist_ok=True)
    paths = []
    for ext, data in uploads:
        fpath = os.path.join(day_dir, f"{prefix}_{int(time.time() * 1000)}_{secrets.token_hex(3)}.{ext}")
        with open(fpath, "wb") as f:
            f.write(data)
        paths.append(fpath)
    return paths


def report_photos_list(c, report_id: int) -> list:
    rows = c.execute("SELECT id, uploaded_at FROM shift_report_photos WHERE report_id=? ORDER BY id", (report_id,)).fetchall()
    return [{"id": r["id"], "uploaded_at": r["uploaded_at"]} for r in rows]


def report_can_edit(viewer: dict, owner_id: int) -> bool:
    """Изменять и удалять отчёт может только его автор. Старший бармен / бар-менеджер / мастер
    видят отчёты команды, но чужие не редактируют."""
    return viewer["id"] == owner_id


def report_out(c, r, viewer: dict) -> dict:
    return {"id": r["id"], "shift_id": r["shift_id"], "slot": r["slot"], "caption": r["caption"] or "",
            "uploaded_at": r["created_at"], "edited_at": r["edited_at"], "user_id": r["user_id"],
            "photos": report_photos_list(c, r["id"]), "can_edit": report_can_edit(viewer, r["user_id"])}


@app.get("/api/shifts/photos/status")
async def photo_status(day: Optional[str] = None, user: dict = Depends(current_user)):
    target = day or datetime.now(MSK).strftime("%Y-%m-%d")
    parse_day(target)
    prev_day = (parse_day(target) - timedelta(days=1)).strftime("%Y-%m-%d")
    now = datetime.now(MSK)
    admin = user["role"] in TEAM_ROLES
    with db() as c:
        rows = c.execute(SHIFT_SQL + " WHERE s.day IN (?,?) ORDER BY s.start_time", (target, prev_day)).fetchall()
        result = []
        for r in rows:
            s = shift_dict(r)
            if not s["user_id"] or (not admin and s["user_id"] != user["id"]):
                continue
            cps = [cp for cp in photo_checkpoints_for_shift(s) if cp["date"] == target]
            if not cps:
                continue
            reports = {p["slot"]: p for p in c.execute("SELECT * FROM shift_reports WHERE shift_id=?", (s["id"],)).fetchall()}
            checkpoints = []
            for cp in cps:
                p = reports.get(cp["slot"])
                checkpoints.append({
                    "slot": cp["slot"], "due_at": cp["due_dt"].strftime("%H:%M"),
                    "uploaded": bool(p), "overdue": (not p) and now > cp["due_dt"],
                    "report": report_out(c, p, user) if p else None,
                })
            extra = [p for p in sorted(reports.values(), key=lambda x: x["created_at"]) if p["slot"].startswith("extra_")]
            result.append({"shift_id": s["id"], "user_id": s["user_id"], "user_name": s["user_name"],
                           "start": s["start"], "end": s["end"], "checkpoints": checkpoints,
                           "extra_reports": [report_out(c, p, user) for p in extra]})
    return result


@app.get("/api/shifts/photos/mine")
async def my_shift_photos(days: int = 14, user: dict = Depends(current_user)):
    """Смены пользователя за последние N дней с количеством уже загруженных фото — чтобы можно было
    задним числом дозагрузить забытый отчёт, выбрав свою смену."""
    days = min(max(days, 1), 60)
    start = (datetime.now(MSK) - timedelta(days=days)).strftime("%Y-%m-%d")
    end = datetime.now(MSK).strftime("%Y-%m-%d")
    with db() as c:
        rows = c.execute(SHIFT_SQL + " WHERE s.day BETWEEN ? AND ? AND s.user_id=? ORDER BY s.day DESC, s.start_time",
                         (start, end, user["id"])).fetchall()
        now = datetime.now(MSK)
        result = []
        for r in rows:
            s = shift_dict(r)
            n = c.execute("""SELECT COUNT(*) FROM shift_report_photos p JOIN shift_reports sr ON sr.id = p.report_id
                             WHERE sr.shift_id=?""", (s["id"],)).fetchone()[0]
            done = {x["slot"] for x in c.execute("SELECT slot FROM shift_reports WHERE shift_id=?", (s["id"],)).fetchall()}
            cps = [{"slot": cp["slot"], "date": cp["date"], "uploaded": cp["slot"] in done,
                    "overdue": cp["slot"] not in done and now > cp["due_dt"]} for cp in photo_checkpoints_for_shift(s)]
            result.append({**s, "photo_count": n, "checkpoints": cps})
    return result


@app.get("/api/shifts/reports/mine")
async def my_reports(user: dict = Depends(current_user)):
    """Все собственные отчёты, которые ещё не ушли в архив — автор может менять их до архивации."""
    with db() as c:
        rows = c.execute("""SELECT sr.*, s.start_time, s.end_time, s.day AS shift_day
                            FROM shift_reports sr LEFT JOIN shifts s ON s.id = sr.shift_id
                            WHERE sr.user_id=? ORDER BY sr.day DESC, sr.created_at DESC""", (user["id"],)).fetchall()
        return [{**report_out(c, r, user), "day": r["day"], "shift_day": r["shift_day"],
                 "shift_start": r["start_time"], "shift_end": r["end_time"]} for r in rows]


def get_own_report(c, report_id: int, user: dict):
    rep = c.execute("SELECT * FROM shift_reports WHERE id=?", (report_id,)).fetchone()
    if not rep:
        raise HTTPException(404, "Отчёт не найден")
    if not report_can_edit(user, rep["user_id"]):
        raise HTTPException(403, "Изменять и удалять отчёт может только его автор")
    return rep


@app.post("/api/shifts/reports")
async def create_shift_report(shift_id: int = Form(...), slot: str = Form(...), caption: str = Form(""),
                              files: List[UploadFile] = File(...), user: dict = Depends(current_user)):
    """Один запрос = один отчёт со всеми фото и подписью. slot = время чек-поинта ('12:00', '20:00', '08:00')
    или 'extra' для доп. отчёта. Повторная отправка на тот же чек-поинт полностью заменяет отчёт."""
    with db() as c:
        s = get_shift(c, shift_id)
    if s["user_id"] != user["id"]:
        raise HTTPException(403, "Отчёт можно загрузить только за свою смену")
    if slot == "extra":
        day, real_slot = s["date"], f"extra_{int(time.time() * 1000)}_{secrets.token_hex(3)}"
    else:
        valid = {cp["slot"]: cp for cp in photo_checkpoints_for_shift(s)}
        if slot not in valid:
            raise HTTPException(400, "Для этой смены не требуется фото в это время")
        day, real_slot = valid[slot]["date"], slot
    uploads = await read_image_uploads(files)
    if not uploads:
        raise HTTPException(400, "Добавьте хотя бы одно фото")
    if len(uploads) > MAX_PHOTOS_PER_POST:
        raise HTTPException(400, f"Не больше {MAX_PHOTOS_PER_POST} фото в одном отчёте")
    paths = write_photo_files(os.path.join(PHOTOS_DIR, day), f"{shift_id}_{real_slot.replace(':', '')}", uploads)
    now = int(time.time())
    old_files = []
    try:
        with db() as c:
            old = c.execute("SELECT id FROM shift_reports WHERE shift_id=? AND slot=?", (shift_id, real_slot)).fetchone()
            if old:
                old_files = [x["file_path"] for x in c.execute(
                    "SELECT file_path FROM shift_report_photos WHERE report_id=?", (old["id"],)).fetchall()]
                c.execute("DELETE FROM shift_report_photos WHERE report_id=?", (old["id"],))
                c.execute("DELETE FROM shift_reports WHERE id=?", (old["id"],))
            rid = c.execute("""INSERT INTO shift_reports (shift_id, user_id, day, slot, caption, created_at)
                               VALUES (?,?,?,?,?,?)""",
                            (shift_id, s["user_id"], day, real_slot, caption.strip()[:1000], now)).lastrowid
            for p in paths:
                c.execute("INSERT INTO shift_report_photos (report_id, file_path, uploaded_at) VALUES (?,?,?)", (rid, p, now))
    except Exception:
        remove_files(paths)
        raise
    remove_files(old_files)
    return {"status": "success", "id": rid, "photos": len(paths)}


@app.put("/api/shifts/reports/{report_id}")
async def update_shift_report(report_id: int, caption: str = Form(""), keep_photo_ids: str = Form(""),
                              files: Optional[List[UploadFile]] = File(None), user: dict = Depends(current_user)):
    """Редактирование отчёта (только автор): keep_photo_ids — id старых фото, которые остаются (через запятую),
    остальные старые удаляются; files — новые фото. В итоге в отчёте должно остаться хотя бы одно фото."""
    with db() as c:
        rep = get_own_report(c, report_id, user)
        existing = c.execute("SELECT id, file_path FROM shift_report_photos WHERE report_id=?", (report_id,)).fetchall()
    keep = parse_keep_ids(keep_photo_ids) & {p["id"] for p in existing}
    uploads = await read_image_uploads(files)
    total = len(keep) + len(uploads)
    if total == 0:
        raise HTTPException(400, "В отчёте должно быть хотя бы одно фото")
    if total > MAX_PHOTOS_PER_POST:
        raise HTTPException(400, f"Не больше {MAX_PHOTOS_PER_POST} фото в одном отчёте")
    paths = write_photo_files(os.path.join(PHOTOS_DIR, rep["day"]), f"{rep['shift_id']}_{rep['slot'].replace(':', '')}", uploads)
    removed = [p for p in existing if p["id"] not in keep]
    now = int(time.time())
    try:
        with db() as c:
            for p in removed:
                c.execute("DELETE FROM shift_report_photos WHERE id=?", (p["id"],))
            for p in paths:
                c.execute("INSERT INTO shift_report_photos (report_id, file_path, uploaded_at) VALUES (?,?,?)", (report_id, p, now))
            c.execute("UPDATE shift_reports SET caption=?, edited_at=? WHERE id=?", (caption.strip()[:1000], now, report_id))
    except Exception:
        remove_files(paths)
        raise
    remove_files([p["file_path"] for p in removed])
    return {"status": "success"}


@app.delete("/api/shifts/reports/{report_id}")
async def delete_shift_report(report_id: int, user: dict = Depends(current_user)):
    with db() as c:
        get_own_report(c, report_id, user)
        files = [x["file_path"] for x in c.execute("SELECT file_path FROM shift_report_photos WHERE report_id=?", (report_id,)).fetchall()]
        c.execute("DELETE FROM shift_report_photos WHERE report_id=?", (report_id,))
        c.execute("DELETE FROM shift_reports WHERE id=?", (report_id,))
    remove_files(files)
    return {"status": "success"}


@app.get("/api/shifts/reports/{report_id}/photos/{photo_id}/file")
async def get_shift_report_photo(report_id: int, photo_id: int, user: dict = Depends(current_user_flexible)):
    with db() as c:
        r = c.execute("SELECT file_path FROM shift_report_photos WHERE id=? AND report_id=?", (photo_id, report_id)).fetchone()
    if not r or not os.path.exists(r["file_path"]):
        raise HTTPException(404, "Фото не найдено")
    return FileResponse(r["file_path"])


@app.post("/api/shifts/photos/remind_now")
async def remind_photo_now(shift_id: int = Form(...), slot: str = Form(...), actor: dict = Depends(require_admin)):
    """Принудительно отправляет пуш-напоминание о фото прямо сейчас, минуя расписание."""
    with db() as c:
        s = get_shift(c, shift_id)
    valid_slots = {cp["slot"] for cp in photo_checkpoints_for_shift(s)}
    if slot not in valid_slots:
        raise HTTPException(400, "Для этой смены не требуется фото в это время")
    if not s["user_id"]:
        raise HTTPException(400, "У смены нет сотрудника")
    send_push([s["user_id"]], "Фото бара", f"Напоминание: нужно сфотографировать бар (к {slot})", "/#photos")
    return {"status": "success"}


# Примечание: обычное удаление отдельных фото отчёта смены намеренно недоступно — вместо этого
# фото каждые ARCHIVE_INTERVAL_DAYS дней упаковываются в подписанный ZIP-архив (см. run_archiving ниже),
# который хранится во вкладке «Команда» до следующей архивации.


async def check_photo_reminders():
    """Каждую минуту: за PHOTO_REMINDER_MINUTES до срока — пуш сотруднику; через LATE_REPORT_MINUTES после срока,
    если отчёта так и нет, — пуш старшему бармену / бар-менеджеру / мастеру. Каждое напоминание — один раз."""
    now = datetime.now(MSK).replace(second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    to_send = []
    with db() as c:
        rows = c.execute(SHIFT_SQL + " WHERE s.day IN (?,?)", (today, yesterday)).fetchall()
        team = approved_ids(c, roles=TEAM_ROLES)
        for r in rows:
            s = shift_dict(r)
            if not s["user_id"]:
                continue
            for cp in photo_checkpoints_for_shift(s):
                if c.execute("SELECT 1 FROM shift_reports WHERE shift_id=? AND slot=?", (s["id"], cp["slot"])).fetchone():
                    continue                                   # отчёт уже есть — напоминать не о чем
                remind_at = cp["due_dt"] - timedelta(minutes=PHOTO_REMINDER_MINUTES)
                late_at = cp["due_dt"] + timedelta(minutes=LATE_REPORT_MINUTES)
                # Окна, а не точное совпадение минуты: если задача запустилась с опозданием, напоминание не теряется.
                if remind_at <= now < cp["due_dt"] and c.execute(
                        "INSERT OR IGNORE INTO photo_reminders_sent (shift_id, slot) VALUES (?,?)", (s["id"], cp["slot"])).rowcount:
                    to_send.append(([s["user_id"]], "Фото бара",
                                    f"Через {PHOTO_REMINDER_MINUTES} мин нужно сфотографировать бар (к {cp['slot']})"))
                if late_at <= now < late_at + timedelta(hours=2) and c.execute(
                        "INSERT OR IGNORE INTO photo_reminders_sent (shift_id, slot) VALUES (?,?)", (s["id"], "late:" + cp["slot"])).rowcount:
                    to_send.append(([u for u in team if u != s["user_id"]], "Нет отчёта",
                                    f"{s['user_name']} не прислал(а) отчёт к {cp['slot']}"))
    for ids, title, text in to_send:
        send_push(ids, title, text, "/#photos", "photos")


# ======================= СТЕНА ПОЗОРА =======================
# Пост = подпись + (необязательно) отмеченный сотрудник + несколько фото (shame_post_photos).
# Публиковать могут все, кроме официантов; комментировать и смотреть — все, включая официантов.
# Изменять и удалять пост может только его автор (модерации админскими ролями нет).
class ShameCommentIn(BaseModel):
    text: str


def shame_photos_list(c, post_id: int) -> list:
    rows = c.execute("SELECT id, uploaded_at FROM shame_post_photos WHERE post_id=? ORDER BY id", (post_id,)).fetchall()
    return [{"id": r["id"], "uploaded_at": r["uploaded_at"]} for r in rows]


def shame_post_dict(c, r, umap: dict, viewer: dict) -> dict:
    own = viewer["id"] == r["user_id"]
    return {
        "id": r["id"], "day": r["day"], "caption": r["caption"] or "", "created_at": r["created_at"],
        "edited_at": r["edited_at"],
        "user_id": r["user_id"], "user_name": umap.get(r["user_id"], "Удалён"),
        "tagged_user_id": r["tagged_user_id"],
        "tagged_user_name": umap.get(r["tagged_user_id"]) if r["tagged_user_id"] else None,
        "photos": shame_photos_list(c, r["id"]),
        "comment_count": c.execute("SELECT COUNT(*) FROM shame_comments WHERE post_id=?", (r["id"],)).fetchone()[0],
        "can_edit": own, "can_delete": own,
    }


def check_tagged_user(c, tagged_user_id: Optional[int]):
    if tagged_user_id and not c.execute("SELECT 1 FROM users WHERE id=? AND status='approved'", (tagged_user_id,)).fetchone():
        raise HTTPException(400, "Отмеченный пользователь не найден")


def get_own_shame_post(c, post_id: int, user: dict):
    r = c.execute("SELECT * FROM shame_posts WHERE id=?", (post_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Запись не найдена")
    if r["user_id"] != user["id"]:
        raise HTTPException(403, "Изменять и удалять запись может только её автор")
    return r


@app.get("/api/shame")
async def list_shame(limit: int = 200, user: dict = Depends(current_user)):
    limit = min(max(limit, 1), 200)
    with db() as c:
        rows = c.execute("SELECT * FROM shame_posts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        umap = {u["id"]: u["name"] for u in c.execute("SELECT id, name FROM users").fetchall()}
        return [shame_post_dict(c, r, umap, user) for r in rows]


@app.post("/api/shame")
async def create_shame(caption: str = Form(""), tagged_user_id: Optional[int] = Form(None),
                       files: List[UploadFile] = File(...), user: dict = Depends(require_not_waiter)):
    """Один запрос = один пост со всеми выбранными фото (раньше на каждое фото создавался отдельный пост)."""
    uploads = await read_image_uploads(files)
    if not uploads:
        raise HTTPException(400, "Добавьте хотя бы одно фото")
    if len(uploads) > MAX_PHOTOS_PER_POST:
        raise HTTPException(400, f"Не больше {MAX_PHOTOS_PER_POST} фото в одной записи")
    with db() as c:
        check_tagged_user(c, tagged_user_id)
    day = datetime.now(MSK).strftime("%Y-%m-%d")
    paths = write_photo_files(os.path.join(SHAME_DIR, day), str(user["id"]), uploads)
    now = int(time.time())
    try:
        with db() as c:
            post_id = c.execute("""INSERT INTO shame_posts (user_id, day, caption, file_path, tagged_user_id, created_at)
                                   VALUES (?,?,?,?,?,?)""",
                                (user["id"], day, caption.strip()[:1000], "", tagged_user_id, now)).lastrowid
            for p in paths:
                c.execute("INSERT INTO shame_post_photos (post_id, file_path, uploaded_at) VALUES (?,?,?)", (post_id, p, now))
    except Exception:
        remove_files(paths)
        raise
    if tagged_user_id and tagged_user_id != user["id"]:
        send_push([tagged_user_id], "Стена позора", f"{user['name']} отметил(а) вас в записи на стене позора", "/#shame")
    return {"status": "success", "id": post_id, "photos": len(paths)}


@app.get("/api/shame/{post_id}/photos/{photo_id}/file")
async def get_shame_photo(post_id: int, photo_id: int, user: dict = Depends(current_user_flexible)):
    with db() as c:
        r = c.execute("SELECT file_path FROM shame_post_photos WHERE id=? AND post_id=?", (photo_id, post_id)).fetchone()
    if not r or not os.path.exists(r["file_path"]):
        raise HTTPException(404, "Фото не найдено")
    return FileResponse(r["file_path"])


@app.get("/api/shame/{post_id}/comments")
async def list_shame_comments(post_id: int, user: dict = Depends(current_user)):
    with db() as c:
        if not c.execute("SELECT 1 FROM shame_posts WHERE id=?", (post_id,)).fetchone():
            raise HTTPException(404, "Запись не найдена")
        rows = c.execute("SELECT * FROM shame_comments WHERE post_id=? ORDER BY created_at, id", (post_id,)).fetchall()
        umap = {u["id"]: u["name"] for u in c.execute("SELECT id, name FROM users").fetchall()}
    return [{"id": cm["id"], "user_id": cm["user_id"], "user_name": umap.get(cm["user_id"], "Удалён"),
             "text": cm["text"], "created_at": cm["created_at"]} for cm in rows]


@app.post("/api/shame/{post_id}/comments")
async def add_shame_comment(post_id: int, body: ShameCommentIn, user: dict = Depends(current_user)):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Пустой комментарий")
    with db() as c:
        post = c.execute("SELECT * FROM shame_posts WHERE id=?", (post_id,)).fetchone()
        if not post:
            raise HTTPException(404, "Запись не найдена")
        c.execute("INSERT INTO shame_comments (post_id, user_id, text, created_at) VALUES (?,?,?,?)",
                  (post_id, user["id"], text[:1000], int(time.time())))
    notify_ids = {post["user_id"]}
    if post["tagged_user_id"]:
        notify_ids.add(post["tagged_user_id"])
    notify_ids.discard(user["id"])
    if notify_ids:
        send_push(list(notify_ids), "Новый комментарий", f"{user['name']}: {text[:120]}", "/#shame")
    return {"status": "success"}


@app.put("/api/shame/{post_id}")
async def edit_shame_post(post_id: int, caption: str = Form(""), tagged_user_id: Optional[int] = Form(None),
                          keep_photo_ids: str = Form(""), files: Optional[List[UploadFile]] = File(None),
                          user: dict = Depends(current_user)):
    """Редактирование поста (только автор): подпись, отмеченный сотрудник и набор фото
    (keep_photo_ids — какие старые оставить, files — новые)."""
    with db() as c:
        r = get_own_shame_post(c, post_id, user)
        check_tagged_user(c, tagged_user_id)
        existing = c.execute("SELECT id, file_path FROM shame_post_photos WHERE post_id=?", (post_id,)).fetchall()
    keep = parse_keep_ids(keep_photo_ids) & {p["id"] for p in existing}
    uploads = await read_image_uploads(files)
    total = len(keep) + len(uploads)
    if total == 0:
        raise HTTPException(400, "В записи должно быть хотя бы одно фото")
    if total > MAX_PHOTOS_PER_POST:
        raise HTTPException(400, f"Не больше {MAX_PHOTOS_PER_POST} фото в одной записи")
    paths = write_photo_files(os.path.join(SHAME_DIR, r["day"]), str(user["id"]), uploads)
    removed = [p for p in existing if p["id"] not in keep]
    now = int(time.time())
    try:
        with db() as c:
            for p in removed:
                c.execute("DELETE FROM shame_post_photos WHERE id=?", (p["id"],))
            for p in paths:
                c.execute("INSERT INTO shame_post_photos (post_id, file_path, uploaded_at) VALUES (?,?,?)", (post_id, p, now))
            c.execute("UPDATE shame_posts SET caption=?, tagged_user_id=?, edited_at=? WHERE id=?",
                      (caption.strip()[:1000], tagged_user_id, now, post_id))
    except Exception:
        remove_files(paths)
        raise
    remove_files([p["file_path"] for p in removed])
    if tagged_user_id and tagged_user_id not in (r["tagged_user_id"], user["id"]):
        send_push([tagged_user_id], "Стена позора", f"{user['name']} отметил(а) вас в записи на стене позора", "/#shame")
    return {"status": "success"}


@app.delete("/api/shame/{post_id}")
async def delete_shame_post(post_id: int, user: dict = Depends(current_user)):
    with db() as c:
        get_own_shame_post(c, post_id, user)
        files = [x["file_path"] for x in c.execute("SELECT file_path FROM shame_post_photos WHERE post_id=?", (post_id,)).fetchall()]
        c.execute("DELETE FROM shame_post_photos WHERE post_id=?", (post_id,))
        c.execute("DELETE FROM shame_comments WHERE post_id=?", (post_id,))
        c.execute("DELETE FROM shame_posts WHERE id=?", (post_id,))
    remove_files(files)
    return {"status": "success"}


# ======================= АРХИВАЦИЯ (каждые ARCHIVE_INTERVAL_DAYS дней) =======================
# Один запуск архивации = ОДИН ZIP, внутри две папки: «Отчёты смен» и «Стена позора».
# В каждой папке — «журнал.txt» с полным описанием каждой записи (кто, когда, подпись, отметка,
# комментарии, какие файлы относятся к записи). Журнал пишется всегда, даже если записей нет
# или файл фото пропал с диска, — стена позора больше не может «потеряться» в архиве.
# Как только создан новый архив, все предыдущие архивы удаляются (и файлы, и записи).
CATEGORY_LABELS = {"reports": "Отчёты смен", "shame": "Стена позора"}


def safe_fs_name(name: str) -> str:
    name = re.sub(r"[^\w\-]+", "_", name or "", flags=re.UNICODE).strip("_")
    return name or "user"


def fmt_ts(ts) -> str:
    return datetime.fromtimestamp(int(ts), MSK).strftime("%d.%m.%Y %H:%M") if ts else "—"


def get_meta(c, key: str, default=None):
    r = c.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(c, key: str, value):
    c.execute("INSERT INTO app_meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, str(value)))


def next_archive_at(c) -> int:
    return int(get_meta(c, "next_archive_at", int(time.time()) + ARCHIVE_INTERVAL_DAYS * 86400))


def slot_title(slot: str) -> str:
    return "Доп. отчёт" if (slot or "").startswith("extra_") else f"Отчёт к {slot}"


def collect_reports(c):
    """[(zip_folder, [file_paths], журнал_строки)] для отчётов смены."""
    rows = c.execute("""SELECT sr.*, COALESCE(u.name, 'Сотрудник') AS who, s.start_time, s.end_time
                        FROM shift_reports sr LEFT JOIN users u ON u.id = sr.user_id
                        LEFT JOIN shifts s ON s.id = sr.shift_id
                        ORDER BY sr.day, sr.created_at""").fetchall()
    out = []
    for r in rows:
        photos = [x["file_path"] for x in c.execute(
            "SELECT file_path FROM shift_report_photos WHERE report_id=? ORDER BY id", (r["id"],)).fetchall()]
        t = datetime.fromtimestamp(r["created_at"], MSK).strftime("%H-%M")
        folder = f"{safe_fs_name(CATEGORY_LABELS['reports'])}/{r['day']}/{safe_fs_name(r['who'])}/{t}_{safe_fs_name(slot_title(r['slot']))}_{r['id']}"
        shift = f"{r['start_time']}–{r['end_time']}" if r["start_time"] else "смена удалена"
        lines = [f"{slot_title(r['slot'])} · {r['day']} · смена {shift}",
                 f"  Автор: {r['who']}",
                 f"  Загружено: {fmt_ts(r['created_at'])}" + (f" · изменено: {fmt_ts(r['edited_at'])}" if r["edited_at"] else ""),
                 f"  Подпись: {r['caption'] or '—'}"]
        out.append((folder, photos, lines))
    return rows, out


def collect_shame(c):
    rows = c.execute("""SELECT sp.*, COALESCE(u.name, 'Сотрудник') AS who
                        FROM shame_posts sp LEFT JOIN users u ON u.id = sp.user_id
                        ORDER BY sp.day, sp.created_at""").fetchall()
    umap = {u["id"]: u["name"] for u in c.execute("SELECT id, name FROM users").fetchall()}
    out = []
    for r in rows:
        photos = [x["file_path"] for x in c.execute(
            "SELECT file_path FROM shame_post_photos WHERE post_id=? ORDER BY id", (r["id"],)).fetchall()]
        comments = c.execute("SELECT * FROM shame_comments WHERE post_id=? ORDER BY created_at, id", (r["id"],)).fetchall()
        t = datetime.fromtimestamp(r["created_at"], MSK).strftime("%H-%M")
        folder = f"{safe_fs_name(CATEGORY_LABELS['shame'])}/{r['day']}/{safe_fs_name(r['who'])}/{t}_запись_{r['id']}"
        lines = [f"Запись №{r['id']} · {r['day']}",
                 f"  Автор: {r['who']}",
                 f"  Отмечен: {umap.get(r['tagged_user_id'], 'удалённый сотрудник') if r['tagged_user_id'] else '—'}",
                 f"  Опубликовано: {fmt_ts(r['created_at'])}" + (f" · изменено: {fmt_ts(r['edited_at'])}" if r["edited_at"] else ""),
                 f"  Подпись: {r['caption'] or '—'}",
                 f"  Комментарии ({len(comments)}):" + ("" if comments else " —")]
        lines += [f"    [{fmt_ts(cm['created_at'])}] {umap.get(cm['user_id'], 'Удалён')}: {cm['text']}" for cm in comments]
        out.append((folder, photos, lines))
    return rows, out


def write_category(zf, category: str, entries: list, created_at: int) -> int:
    """Пишет фото категории и её журнал. Возвращает число реально упакованных фото."""
    cat = safe_fs_name(CATEGORY_LABELS[category])
    packed = 0
    log = [f"{CATEGORY_LABELS[category]} — архив от {fmt_ts(created_at)}", f"Записей: {len(entries)}", "=" * 60, ""]
    for folder, photos, lines in entries:
        log += lines
        log.append(f"  Фото ({len(photos)}):")
        for i, fp in enumerate(photos, 1):
            ext = (os.path.splitext(fp)[1] or ".jpg").lstrip(".") or "jpg"
            arc = f"{folder}/фото_{i}.{ext}"
            if os.path.exists(fp):
                zf.write(fp, arc)
                packed += 1
                log.append(f"    {arc}")
            else:
                log.append(f"    {arc} — файл не найден на сервере")
        log.append("")
    if not entries:
        log.append("За этот период записей нет.")
    zf.writestr(f"{cat}/журнал.txt", "\n".join(log))
    return packed


def delete_archive_rows(c, keep_id: Optional[int] = None):
    rows = c.execute("SELECT id, file_path FROM archives").fetchall()
    remove_files([r["file_path"] for r in rows if r["id"] != keep_id])
    c.execute("DELETE FROM archives WHERE id<>?", (keep_id or -1,))


async def run_archiving(manual: bool = False) -> dict:
    """Упаковывает все текущие отчёты смены и записи стены позора в один ZIP, очищает их из приложения,
    удаляет все предыдущие архивы и назначает следующую архивацию через ARCHIVE_INTERVAL_DAYS дней."""
    now = int(time.time())
    result = {"reports": 0, "shame": 0, "report_photos": 0, "shame_photos": 0, "archive_id": None}
    with db() as c:
        rep_rows, rep_entries = collect_reports(c)
        sh_rows, sh_entries = collect_shame(c)
        set_meta(c, "next_archive_at", now + ARCHIVE_INTERVAL_DAYS * 86400)
        if not rep_rows and not sh_rows:
            logger.info("Архивация: архивировать нечего — прежний архив сохранён")
            return result
        os.makedirs(ARCHIVES_DIR, exist_ok=True)
        stamp = datetime.fromtimestamp(now, MSK).strftime("%Y-%m-%d_%H-%M")
        zip_path = os.path.join(ARCHIVES_DIR, f"Тугай_архив_{stamp}_{secrets.token_hex(2)}.zip")
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                result["report_photos"] = write_category(zf, "reports", rep_entries, now)
                result["shame_photos"] = write_category(zf, "shame", sh_entries, now)
                zf.writestr("сводка.txt", "\n".join([
                    f"Архив «Тугай» от {fmt_ts(now)}{' (вручную)' if manual else ''}",
                    f"Отчёты смен: {len(rep_rows)} (фото: {result['report_photos']})",
                    f"Стена позора: {len(sh_rows)} записей (фото: {result['shame_photos']})",
                    f"Следующая архивация: {fmt_ts(now + ARCHIVE_INTERVAL_DAYS * 86400)}",
                ]))
        except Exception:
            remove_files([zip_path])
            raise
        result.update(reports=len(rep_rows), shame=len(sh_rows))
        summary = json.dumps({k: result[k] for k in ("reports", "shame", "report_photos", "shame_photos")})
        aid = c.execute("""INSERT INTO archives (category, file_path, item_count, created_at, expires_at, summary)
                           VALUES ('all',?,?,?,?,?)""",
                        (zip_path, result["report_photos"] + result["shame_photos"], now,
                         now + ARCHIVE_INTERVAL_DAYS * 86400, summary)).lastrowid
        result["archive_id"] = aid
        delete_archive_rows(c, keep_id=aid)          # новый архив появился → все старые удаляются
        files = [fp for _, photos, _ in rep_entries + sh_entries for fp in photos]
        c.execute("DELETE FROM shift_report_photos")
        c.execute("DELETE FROM shift_reports")
        c.execute("DELETE FROM shame_comments")
        c.execute("DELETE FROM shame_post_photos")
        c.execute("DELETE FROM shame_posts")
        cutoff_day = (datetime.now(MSK) - timedelta(days=ARCHIVE_INTERVAL_DAYS + 2)).strftime("%Y-%m-%d")
        c.execute("DELETE FROM photo_reminders_sent WHERE shift_id IN (SELECT id FROM shifts WHERE day<?)", (cutoff_day,))
    remove_files(files)
    logger.info(f"Архивация {'(вручную) ' if manual else ''}завершена: {result}")
    return result


async def archive_if_due():
    """Проверяется каждые 5 минут: время следующей архивации хранится в базе, поэтому деплой
    или перезапуск сервера больше не сбрасывает трёхдневный отсчёт."""
    with db() as c:
        due = next_archive_at(c) <= int(time.time())
    if due:
        await run_archiving()


def archive_dict(r) -> dict:
    try:
        summary = json.loads(r["summary"] or "{}")
    except (ValueError, TypeError):
        summary = {}
    return {"id": r["id"], "category": r["category"], "item_count": r["item_count"], "created_at": r["created_at"],
            "summary": summary, "filename": os.path.basename(r["file_path"])}


@app.get("/api/archive/info")
async def archive_info(user: dict = Depends(current_user)):
    """Когда будет следующая архивация — до этого момента автор может менять свои отчёты и записи."""
    with db() as c:
        last = c.execute("SELECT MAX(created_at) FROM archives").fetchone()[0]
        return {"next_at": next_archive_at(c), "last_at": last, "interval_days": ARCHIVE_INTERVAL_DAYS}


@app.get("/api/archives")
async def list_archives(actor: dict = Depends(require_team)):
    with db() as c:
        rows = c.execute("SELECT * FROM archives ORDER BY created_at DESC").fetchall()
    return [archive_dict(r) for r in rows if os.path.exists(r["file_path"])]


@app.get("/api/team/reports")
async def team_reports(actor: dict = Depends(require_team)):
    """Все отчёты смены, которые ещё не ушли в архив, — показываются во вкладке «Фото»
    старшему бармену, бар-менеджеру и мастеру (только просмотр; менять может только автор)."""
    with db() as c:
        rows = c.execute("""SELECT sr.*, COALESCE(u.name, 'Сотрудник') AS who
                             FROM shift_reports sr LEFT JOIN users u ON u.id = sr.user_id
                             ORDER BY sr.day DESC, sr.created_at DESC""").fetchall()
        days: dict = {}
        for r in rows:
            day_bucket = days.setdefault(r["day"], {})
            day_bucket.setdefault(r["who"], []).append(report_out(c, r, actor))
    return [{"day": day, "people": [{"name": name, "photos": photos} for name, photos in people.items()]}
            for day, people in sorted(days.items(), reverse=True)]


@app.get("/api/archives/{archive_id}/download")
async def download_archive(archive_id: int, actor: dict = Depends(require_team_flexible)):
    with db() as c:
        r = c.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
    if not r or not os.path.exists(r["file_path"]):
        raise HTTPException(404, "Архив не найден — возможно, он уже заменён более новым")
    name = datetime.fromtimestamp(r["created_at"], MSK).strftime("Тугай_архив_%Y-%m-%d_%H-%M.zip")
    return FileResponse(r["file_path"], filename=name, media_type="application/zip")


@app.post("/api/archives/force")
async def force_archive(actor: dict = Depends(require_team)):
    """Кнопка «Архивировать сейчас» — запускает архивацию немедленно, не дожидаясь расписания."""
    result = await run_archiving(manual=True)
    logger.info(f"Принудительная архивация запущена пользователем {actor['name']}")
    return {"status": "success", "archived": result}


# ======================= СТОП-ЛИСТ =======================
class StopIn(BaseModel):
    menu_item_id: Optional[int] = None
    name: str = ""
    note: str = ""


def stop_dict(r, umap: dict) -> dict:
    return {"id": r["id"], "menu_item_id": r["menu_item_id"], "name": r["name"], "note": r["note"] or "",
            "user_name": umap.get(r["user_id"], ""), "created_at": r["created_at"]}


@app.get("/api/stoplist")
async def get_stoplist(user: dict = Depends(current_user)):
    with db() as c:
        umap = {u["id"]: u["name"] for u in c.execute("SELECT id, name FROM users").fetchall()}
        rows = c.execute("SELECT * FROM stop_list ORDER BY created_at DESC").fetchall()
    return [stop_dict(r, umap) for r in rows]


@app.post("/api/stoplist")
async def add_stop(body: StopIn, actor: dict = Depends(require_not_waiter)):
    """Ставит позицию в стоп. Повторная постановка той же позиции ничего не дублирует."""
    note = body.note.strip()[:200]
    with db() as c:
        if body.menu_item_id:
            m = c.execute("SELECT title FROM menu_items WHERE id=?", (body.menu_item_id,)).fetchone()
            if not m:
                raise HTTPException(404, "Позиция меню не найдена")
            name = m["title"]
            old = c.execute("SELECT * FROM stop_list WHERE menu_item_id=?", (body.menu_item_id,)).fetchone()
            if old:
                if note != (old["note"] or ""):
                    c.execute("UPDATE stop_list SET note=? WHERE id=?", (note, old["id"]))
                return {"status": "success", "id": old["id"], "existed": True}
        else:
            name = body.name.strip()[:80]
            if not name:
                raise HTTPException(400, "Укажите, что закончилось")
            old = find_custom_stop(c, name)
            if old:
                return {"status": "success", "id": old["id"], "existed": True}
        sid = c.execute("INSERT INTO stop_list (menu_item_id, name, note, user_id, created_at) VALUES (?,?,?,?,?)",
                        (body.menu_item_id, name, note, actor["id"], int(time.time()))).lastrowid
        others = approved_ids(c, exclude=actor["id"])
    send_push(others, "Стоп-лист", f"⛔ {name}" + (f" — {note}" if note else "") + f" · {actor['name']}", "/#drinks", "stop")
    return {"status": "success", "id": sid}


def find_custom_stop(c, name: str):
    """Поиск «своей» позиции без учёта регистра. SQLite lower() не понимает кириллицу, поэтому сравниваем в Python."""
    key = (name or "").strip().casefold()
    for r in c.execute("SELECT * FROM stop_list WHERE menu_item_id IS NULL").fetchall():
        if r["name"].strip().casefold() == key:
            return r
    return None


def _remove_stop(c, row, actor: dict):
    c.execute("DELETE FROM stop_list WHERE id=?", (row["id"],))
    return approved_ids(c, exclude=actor["id"]), row["name"]


@app.delete("/api/stoplist/{stop_id}")
async def remove_stop(stop_id: int, actor: dict = Depends(require_not_waiter)):
    with db() as c:
        r = c.execute("SELECT * FROM stop_list WHERE id=?", (stop_id,)).fetchone()
        if not r:
            return {"status": "success"}          # уже снято (например, с другого телефона) — не ошибка
        others, name = _remove_stop(c, r, actor)
    send_push(others, "Снова в наличии", f"✅ {name}", "/#drinks", "stop")
    return {"status": "success"}


@app.delete("/api/stoplist/menu/{menu_item_id}")
async def remove_stop_by_menu(menu_item_id: int, actor: dict = Depends(require_not_waiter)):
    """Снять со стопа по позиции меню — удобно из карточки и для офлайн-очереди (id записи там неизвестен)."""
    with db() as c:
        r = c.execute("SELECT * FROM stop_list WHERE menu_item_id=?", (menu_item_id,)).fetchone()
        if not r:
            return {"status": "success"}
        others, name = _remove_stop(c, r, actor)
    send_push(others, "Снова в наличии", f"✅ {name}", "/#drinks", "stop")
    return {"status": "success"}


@app.delete("/api/stoplist/name/{name}")
async def remove_stop_by_name(name: str, actor: dict = Depends(require_not_waiter)):
    """Снять со стопа произвольную позицию по названию — для офлайн-очереди."""
    with db() as c:
        r = find_custom_stop(c, name)
        if not r:
            return {"status": "success"}
        others, nm = _remove_stop(c, r, actor)
    send_push(others, "Снова в наличии", f"✅ {nm}", "/#drinks", "stop")
    return {"status": "success"}


# ======================= ОБЪЯВЛЕНИЯ =======================
class AnnouncementIn(BaseModel):
    title: str = ""
    body: str = ""
    pinned: bool = False


def ann_can_edit(user: dict, r) -> bool:
    return user["id"] == r["user_id"] or user["role"] in ADMIN_ROLES


@app.get("/api/announcements")
async def list_announcements(user: dict = Depends(current_user)):
    team = user["role"] in TEAM_ROLES
    with db() as c:
        umap = {u["id"]: u["name"] for u in c.execute("SELECT id, name FROM users").fetchall()}
        staff = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM users WHERE status='approved' AND role<>'master'").fetchall()}
        rows = c.execute("SELECT * FROM announcements ORDER BY pinned DESC, created_at DESC LIMIT 200").fetchall()
        out = []
        for r in rows:
            reads = {x["user_id"]: x["read_at"] for x in c.execute(
                "SELECT user_id, read_at FROM announcement_reads WHERE announcement_id=?", (r["id"],)).fetchall()}
            item = {"id": r["id"], "title": r["title"], "body": r["body"], "pinned": bool(r["pinned"]),
                    "created_at": r["created_at"], "edited_at": r["edited_at"], "user_id": r["user_id"],
                    "user_name": umap.get(r["user_id"], "Удалён"),
                    "read": r["user_id"] == user["id"] or user["id"] in reads, "read_at": reads.get(user["id"]),
                    "can_edit": ann_can_edit(user, r)}
            if team:
                audience = {uid: n for uid, n in staff.items() if uid != r["user_id"]}
                item["readers"] = sorted([{"name": n, "read_at": reads[uid]} for uid, n in audience.items() if uid in reads],
                                         key=lambda x: x["read_at"])
                item["not_read"] = sorted(n for uid, n in audience.items() if uid not in reads)
            out.append(item)
    return out


@app.get("/api/announcements/unread")
async def unread_announcements(user: dict = Depends(current_user)):
    with db() as c:
        n = c.execute("""SELECT COUNT(*) FROM announcements a WHERE a.user_id<>? AND NOT EXISTS
                         (SELECT 1 FROM announcement_reads r WHERE r.announcement_id=a.id AND r.user_id=?)""",
                      (user["id"], user["id"])).fetchone()[0]
    return {"count": n}


@app.post("/api/announcements")
async def create_announcement(body: AnnouncementIn, actor: dict = Depends(require_team)):
    title, text = body.title.strip()[:120], body.body.strip()[:4000]
    if not title and not text:
        raise HTTPException(400, "Напишите заголовок или текст объявления")
    with db() as c:
        aid = c.execute("INSERT INTO announcements (user_id, title, body, pinned, created_at) VALUES (?,?,?,?,?)",
                        (actor["id"], title, text, int(body.pinned), int(time.time()))).lastrowid
        others = approved_ids(c, exclude=actor["id"])
    send_push(others, "📣 " + (title or "Новое объявление"), (text or title)[:140], "/#news", f"ann{aid}")
    return {"status": "success", "id": aid}


@app.put("/api/announcements/{aid}")
async def edit_announcement(aid: int, body: AnnouncementIn, user: dict = Depends(current_user)):
    title, text = body.title.strip()[:120], body.body.strip()[:4000]
    if not title and not text:
        raise HTTPException(400, "Напишите заголовок или текст объявления")
    with db() as c:
        r = c.execute("SELECT * FROM announcements WHERE id=?", (aid,)).fetchone()
        if not r:
            raise HTTPException(404, "Объявление не найдено")
        if not ann_can_edit(user, r):
            raise HTTPException(403, "Изменять объявление может автор или администратор")
        c.execute("UPDATE announcements SET title=?, body=?, pinned=?, edited_at=? WHERE id=?",
                  (title, text, int(body.pinned), int(time.time()), aid))
    return {"status": "success"}


@app.delete("/api/announcements/{aid}")
async def delete_announcement(aid: int, user: dict = Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT * FROM announcements WHERE id=?", (aid,)).fetchone()
        if not r:
            return {"status": "success"}
        if not ann_can_edit(user, r):
            raise HTTPException(403, "Удалять объявление может автор или администратор")
        c.execute("DELETE FROM announcement_reads WHERE announcement_id=?", (aid,))
        c.execute("DELETE FROM announcements WHERE id=?", (aid,))
    return {"status": "success"}


@app.post("/api/announcements/{aid}/read")
async def read_announcement(aid: int, user: dict = Depends(current_user)):
    with db() as c:
        if not c.execute("SELECT 1 FROM announcements WHERE id=?", (aid,)).fetchone():
            return {"status": "success"}
        c.execute("INSERT OR IGNORE INTO announcement_reads (announcement_id, user_id, read_at) VALUES (?,?,?)",
                  (aid, user["id"], int(time.time())))
    return {"status": "success"}


# ======================= ЧЕК-ЛИСТ СМЕНЫ =======================
def checklist_day() -> str:
    """Чек-лист «сутки» переключается в 06:00 МСК, чтобы ночная смена не сбрасывалась в полночь."""
    return (datetime.now(MSK) - timedelta(hours=6)).strftime("%Y-%m-%d")


def check_shift_name(shift: str):
    if shift not in ("day", "night"):
        raise HTTPException(400, "Смена: day или night")


def load_progress(c, day: str, shift: str) -> dict:
    rows = c.execute("SELECT item_id, sub, user_name, ts FROM checklist_progress WHERE day=? AND shift=?", (day, shift)).fetchall()
    return {(r["item_id"], r["sub"]): (r["user_name"], r["ts"]) for r in rows}


def cl_item(r, prog: dict) -> dict:
    details = json.loads(r["details"] or "[]")
    main = prog.get((r["id"], -1))
    return {"id": r["id"], "title": r["title"], "details": details, "done": main is not None,
            "by": main[0] if main else "", "at": main[1] if main else "",
            "subs": [(r["id"], i) in prog for i in range(len(details))]}


def set_prog(c, day, shift, item_id, sub, on: bool, user_name: str):
    if on:
        c.execute("INSERT OR REPLACE INTO checklist_progress (day, shift, item_id, sub, user_name, ts) VALUES (?,?,?,?,?,?)",
                  (day, shift, item_id, sub, user_name, now_msk()[11:]))
    else:
        c.execute("DELETE FROM checklist_progress WHERE day=? AND shift=? AND item_id=? AND sub=?", (day, shift, item_id, sub))


class ToggleIn(BaseModel):
    shift: str
    item_id: int
    sub: int = -1          # -1 — сам пункт, 0.. — подпункт
    checked: bool


class ChecklistItemIn(BaseModel):
    shift: str = "day"
    title: str
    details: List[str] = []


class MoveIn(BaseModel):
    dir: int


class NoteTextIn(BaseModel):
    note: str = ""


def clean_details(lst: List[str]) -> List[str]:
    return [x.strip()[:300] for x in lst if x and x.strip()][:30]


@app.get("/api/checklist/{shift}")
async def get_checklist(shift: str, user: dict = Depends(current_user)):
    check_shift_name(shift)
    day = checklist_day()
    with db() as c:
        prog = load_progress(c, day, shift)
        rows = c.execute("SELECT * FROM checklist_items WHERE shift=? ORDER BY position, id", (shift,)).fetchall()
        meta = c.execute("SELECT note FROM checklist_meta WHERE shift=?", (shift,)).fetchone()
    return {"day": day, "note": meta["note"] if meta else "", "items": [cl_item(r, prog) for r in rows]}


@app.post("/api/checklist/toggle")
async def toggle_check(body: ToggleIn, user: dict = Depends(current_user)):
    check_shift_name(body.shift)
    day = checklist_day()
    with db() as c:
        r = c.execute("SELECT * FROM checklist_items WHERE id=? AND shift=?", (body.item_id, body.shift)).fetchone()
        if not r:
            raise HTTPException(404, "Пункт не найден")
        n = len(json.loads(r["details"] or "[]"))
        if body.sub >= n or body.sub < -1:
            raise HTTPException(400, "Нет такого подпункта")
        args = (c, day, body.shift, r["id"])
        if body.sub == -1:                       # пункт целиком → все подпункты
            for i in range(n):
                set_prog(*args, i, body.checked, user["name"])
            set_prog(*args, -1, body.checked, user["name"])
        else:                                    # подпункт → пересчитать пункт
            set_prog(*args, body.sub, body.checked, user["name"])
            done = c.execute("SELECT COUNT(*) FROM checklist_progress WHERE day=? AND shift=? AND item_id=? AND sub>=0",
                             (day, body.shift, r["id"])).fetchone()[0]
            set_prog(*args, -1, done == n, user["name"])
        c.execute("DELETE FROM checklist_progress WHERE day < ?", ((datetime.now(MSK) - timedelta(days=60)).strftime("%Y-%m-%d"),))
        return cl_item(r, load_progress(c, day, body.shift))


@app.post("/api/checklist/items")
async def add_cl_item(body: ChecklistItemIn, actor: dict = Depends(require_schedule_editor)):
    check_shift_name(body.shift)
    title = body.title.strip()[:120]
    if not title:
        raise HTTPException(400, "Введите название пункта")
    with db() as c:
        pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM checklist_items WHERE shift=?", (body.shift,)).fetchone()[0]
        c.execute("INSERT INTO checklist_items (shift, title, details, position) VALUES (?,?,?,?)",
                  (body.shift, title, json.dumps(clean_details(body.details), ensure_ascii=False), pos))
    return {"status": "success"}


@app.put("/api/checklist/items/{item_id}")
async def edit_cl_item(item_id: int, body: ChecklistItemIn, actor: dict = Depends(require_schedule_editor)):
    title = body.title.strip()[:120]
    if not title:
        raise HTTPException(400, "Введите название пункта")
    with db() as c:
        r = c.execute("SELECT * FROM checklist_items WHERE id=?", (item_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Пункт не найден")
        details = clean_details(body.details)
        if details != json.loads(r["details"] or "[]"):
            c.execute("DELETE FROM checklist_progress WHERE item_id=?", (item_id,))   # индексы подпунктов сменились
        c.execute("UPDATE checklist_items SET title=?, details=? WHERE id=?", (title, json.dumps(details, ensure_ascii=False), item_id))
    return {"status": "success"}


@app.delete("/api/checklist/items/{item_id}")
async def delete_cl_item(item_id: int, actor: dict = Depends(require_schedule_editor)):
    with db() as c:
        c.execute("DELETE FROM checklist_items WHERE id=?", (item_id,))
        c.execute("DELETE FROM checklist_progress WHERE item_id=?", (item_id,))
    return {"status": "success"}


@app.post("/api/checklist/items/{item_id}/move")
async def move_cl_item(item_id: int, body: MoveIn, actor: dict = Depends(require_schedule_editor)):
    with db() as c:
        r = c.execute("SELECT shift FROM checklist_items WHERE id=?", (item_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Пункт не найден")
        ids = [x["id"] for x in c.execute("SELECT id FROM checklist_items WHERE shift=? ORDER BY position, id", (r["shift"],))]
        i = ids.index(item_id)
        j = i + (1 if body.dir > 0 else -1)
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            for pos, iid in enumerate(ids):
                c.execute("UPDATE checklist_items SET position=? WHERE id=?", (pos, iid))
    return {"status": "success"}


@app.put("/api/checklist/{shift}/note")
async def set_cl_note(shift: str, body: NoteTextIn, actor: dict = Depends(require_schedule_editor)):
    check_shift_name(shift)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO checklist_meta (shift, note) VALUES (?,?)", (shift, body.note.strip()[:500]))
    return {"status": "success"}


# ======================= ЛИЧНЫЕ ЗАМЕТКИ =======================
NOTE_TYPES = {"text", "h1", "h2", "check", "bullet", "dash", "num"}


class NoteRow(BaseModel):
    t: str = "text"
    x: str = ""
    c: bool = False
    i: int = 0


class NoteIn(BaseModel):
    body: List[NoteRow] = []


class PinIn(BaseModel):
    pinned: bool


def clean_rows(rows: List[NoteRow]) -> list:
    out = [{"t": r.t if r.t in NOTE_TYPES else "text", "x": r.x[:5000], "c": bool(r.c), "i": min(max(r.i, 0), 3)} for r in rows[:1000]]
    return out or [{"t": "text", "x": "", "c": False, "i": 0}]


def note_meta(rows: list):
    texts = [r["x"].strip() for r in rows if r["x"].strip()]
    return (texts[0][:100] if texts else ""), (texts[1][:120] if len(texts) > 1 else ""), int(any(r["t"] == "check" for r in rows))


def note_dict(r) -> dict:
    return {"id": r["id"], "title": r["title"], "preview": r["preview"], "pinned": bool(r["pinned"]),
            "has_check": bool(r["has_check"]), "updated_at": r["updated_at"], "deleted_at": r["deleted_at"],
            "body": json.loads(r["body"] or "[]")}


def own_note(c, nid: int, user: dict):
    r = c.execute("SELECT * FROM notes WHERE id=? AND user_id=?", (nid, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Заметка не найдена")
    return r


@app.get("/api/notes")
async def list_notes(user: dict = Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM notes WHERE user_id=? AND deleted_at IS NOT NULL AND deleted_at<?", (user["id"], int(time.time()) - 30 * 86400))
        rows = c.execute("SELECT * FROM notes WHERE user_id=? ORDER BY updated_at DESC", (user["id"],)).fetchall()
    return [note_dict(r) for r in rows]


@app.post("/api/notes")
async def create_note(body: NoteIn, user: dict = Depends(current_user)):
    rows = clean_rows(body.body)
    title, preview, chk = note_meta(rows)
    with db() as c:
        cur = c.execute("INSERT INTO notes (user_id, title, preview, body, has_check, updated_at) VALUES (?,?,?,?,?,?)",
                        (user["id"], title, preview, json.dumps(rows, ensure_ascii=False), chk, int(time.time())))
        return note_dict(c.execute("SELECT * FROM notes WHERE id=?", (cur.lastrowid,)).fetchone())


@app.put("/api/notes/{nid}")
async def update_note(nid: int, body: NoteIn, user: dict = Depends(current_user)):
    rows = clean_rows(body.body)
    title, preview, chk = note_meta(rows)
    with db() as c:
        own_note(c, nid, user)
        c.execute("UPDATE notes SET title=?, preview=?, body=?, has_check=?, updated_at=? WHERE id=?",
                  (title, preview, json.dumps(rows, ensure_ascii=False), chk, int(time.time()), nid))
        return note_dict(c.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone())


@app.post("/api/notes/{nid}/pin")
async def pin_note(nid: int, body: PinIn, user: dict = Depends(current_user)):
    with db() as c:
        own_note(c, nid, user)
        c.execute("UPDATE notes SET pinned=? WHERE id=?", (int(body.pinned), nid))
    return {"status": "success"}


@app.delete("/api/notes/{nid}")
async def trash_note(nid: int, user: dict = Depends(current_user)):
    with db() as c:
        own_note(c, nid, user)
        c.execute("UPDATE notes SET deleted_at=? WHERE id=?", (int(time.time()), nid))
    return {"status": "success"}


@app.post("/api/notes/{nid}/restore")
async def restore_note(nid: int, user: dict = Depends(current_user)):
    with db() as c:
        own_note(c, nid, user)
        c.execute("UPDATE notes SET deleted_at=NULL WHERE id=?", (nid,))
    return {"status": "success"}


@app.delete("/api/notes/{nid}/purge")
async def purge_note(nid: int, user: dict = Depends(current_user)):
    with db() as c:
        own_note(c, nid, user)
        c.execute("DELETE FROM notes WHERE id=?", (nid,))
    return {"status": "success"}


# ======================= ФРОНТЕНД =======================
@app.get("/")
async def serve_frontend():
    return FileResponse("index.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
async def serve_sw():
    if not os.path.exists("sw.js"):
        raise HTTPException(status_code=404, detail="sw.js не найден")
    return FileResponse("sw.js", media_type="application/javascript",
                         headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.json")
async def serve_manifest():
    if not os.path.exists("manifest.json"):
        raise HTTPException(status_code=404, detail="manifest.json не найден")
    return FileResponse("manifest.json", media_type="application/manifest+json",
                         headers={"Cache-Control": "no-cache"})


@app.get("/logo.png")
async def serve_logo():
    if os.path.exists("logo.png"):
        return FileResponse("logo.png", headers={"Cache-Control": "public, max-age=604800, immutable"})
    raise HTTPException(status_code=404, detail="Логотип не найден")
