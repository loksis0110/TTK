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
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager, contextmanager
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
from aiogram import Bot
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === КОНФИГУРАЦИЯ ===
# Токен бота лучше хранить в переменной окружения BOT_TOKEN, а не в коде.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = int(os.getenv("CHAT_ID", "-1004420801156"))
DB_PATH = os.getenv("DB_PATH", "orders.db")

MASTER_LOGIN = "admin"
MASTER_PASSWORD = "admin123"

ORDER_HOUR, ORDER_MINUTE = 7, 0          # заявка уходит в 07:00 по Москве
MSK = timezone(timedelta(hours=3))

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

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


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in ADMIN_ROLES:
        raise HTTPException(403, "Недостаточно прав")
    return user


def require_schedule_editor(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in SCHEDULE_EDITORS:
        raise HTTPException(403, "Недостаточно прав для изменения графика")
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
            c.execute("DELETE FROM active_order")
            return True
        except Exception as e:
            logger.error(f"Ошибка ТГ: {e}")
            return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not bot:
        logger.warning("BOT_TOKEN не задан: отправка в Telegram отключена")
    scheduler.add_job(send_order_to_tg, "cron", hour=ORDER_HOUR, minute=ORDER_MINUTE,
                      misfire_grace_time=3600, coalesce=True, max_instances=1)
    scheduler.start()
    yield
    scheduler.shutdown()
    if bot:
        await bot.session.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=500)


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
    return {"status": "success"}


@app.post("/api/admin/reset_menu")
async def reset_menu(admin: dict = Depends(require_admin)):
    with db() as c:
        c.execute("DROP TABLE IF EXISTS menu_items")
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
async def send_order_now(admin: dict = Depends(require_admin)):
    """Принудительная отправка текущей заявки в Telegram-группу, не дожидаясь 07:00."""
    if not bot:
        raise HTTPException(400, "BOT_TOKEN не настроен на сервере — отправка недоступна")
    ok = await send_order_to_tg()
    if not ok:
        raise HTTPException(400, "Список закупки пуст — отправлять нечего")
    logger.info(f"Заявка отправлена вручную пользователем {admin['name']}")
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
    return {"status": "success"}


@app.delete("/api/schedule/{sid}")
async def delete_shift(sid: int, actor: dict = Depends(require_schedule_editor)):
    with db() as c:
        s = get_shift(c, sid)
        c.execute("DELETE FROM shifts WHERE id=?", (sid,))
        log_change(c, actor, "delete", f"Удалена смена: {fmt_shift(s)}", s, None)
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


# ---------- отмена изменений графика ----------
UNDOABLE = {"add", "edit", "delete", "swap", "copy", "clear"}


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
                         AND action IN ('add','edit','delete','swap','copy','clear') ORDER BY id DESC LIMIT 1""").fetchone()
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
