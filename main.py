import os
import logging
import json
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from telegram import Update
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, db
from flask import Flask, request
import asyncio

# Загружаем переменные окружения из .env (для локальной разработки)
load_dotenv()

# --- Получаем секреты из переменных окружения ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")
FIREBASE_CRED_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Инициализация Gemini ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash-latest')

# --- Инициализация Firebase ---
firebase_ref = None
if FIREBASE_CRED_JSON:
    try:
        cred_dict = json.loads(FIREBASE_CRED_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {
            'databaseURL': FIREBASE_DATABASE_URL
        })
        firebase_ref = db.reference('/')
        logger.info("Firebase initialized")
    except Exception as e:
        logger.error(f"Firebase init error: {e}")

# --- Flask приложение для приёма вебхуков от Telegram ---
flask_app = Flask(__name__)
application = None  # сюда позже сохраним экземпляр бота

# --- Функция, которая решает, отвечать ли в группе ---
def should_respond(update: Update, bot_username: str) -> bool:
    # Если личный чат — всегда отвечаем
    if update.effective_chat.type == "private":
        return True
    # Если группа или супергруппа
    if update.effective_chat.type in ["group", "supergroup"]:
        # Проверяем, есть ли упоминание бота
        if update.message.entities:
            for entity in update.message.entities:
                if entity.type == "mention":
                    mention = update.message.text[entity.offset:entity.offset+entity.length]
                    if mention == f"@{bot_username}":
                        return True
        # Проверяем, ответили ли на сообщение бота
        if update.message.reply_to_message and update.message.reply_to_message.from_user.id == application.bot.id:
            return True
    return False

# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Привет, {update.effective_user.first_name}! Я HK AI, помощник на базе Gemini. "
        "В группах отвечу, если меня упомянуть (@username_bot)."
    )

# --- Основной обработчик сообщений ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Игнорируем сообщения без текста
    if not update.message or not update.message.text:
        return

    # Проверяем, нужно ли отвечать (для групп)
    if not should_respond(update, context.bot.username):
        return

    user = update.effective_user
    chat = update.effective_chat
    message_text = update.message.text.strip()
    message_id = update.message.message_id

    # Если в группе — убираем упоминание из текста, чтобы Gemini не видел @
    if chat.type != "private" and f"@{context.bot.username}" in message_text:
        message_text = message_text.replace(f"@{context.bot.username}", "").strip()

    logger.info(f"Message from {user.id} in {chat.id}: {message_text}")

    # --- Сохраняем входящее сообщение в Firebase (папка inbox_logs) ---
    if firebase_ref:
        try:
            log_ref = firebase_ref.child('inbox_logs').push()
            log_ref.set({
                'user_id': user.id,
                'chat_id': chat.id,
                'message_id': message_id,
                'text': message_text,
                'timestamp': {'.sv': 'timestamp'}
            })
        except Exception as e:
            logger.error(f"Failed to save to inbox_logs: {e}")

    # --- Загружаем последние 10 сообщений из истории чата ---
    history = []
    if firebase_ref:
        try:
            history_query = firebase_ref.child('chats').child(str(chat.id)).order_by_child('timestamp').limit_to_last(10)
            snapshot = history_query.get()
            if snapshot:
                for msg_id, msg_data in snapshot.items():
                    role = "user" if msg_data['sender'] == 'user' else "model"
                    history.append({"role": role, "parts": [msg_data['text']]})
        except Exception as e:
            logger.error(f"Failed to load history: {e}")

    # --- Запрос к Gemini ---
    try:
        chat_session = model.start_chat(history=history)
        response = chat_session.send_message(message_text)
        reply_text = response.text

        # --- Сохраняем сообщение пользователя и ответ бота в историю чата ---
        if firebase_ref:
            try:
                # Сообщение пользователя
                user_msg_ref = firebase_ref.child('chats').child(str(chat.id)).push()
                user_msg_ref.set({
                    'sender': 'user',
                    'text': message_text,
                    'timestamp': {'.sv': 'timestamp'}
                })
                # Ответ бота
                bot_msg_ref = firebase_ref.child('chats').child(str(chat.id)).push()
                bot_msg_ref.set({
                    'sender': 'bot',
                    'text': reply_text,
                    'timestamp': {'.sv': 'timestamp'}
                })
            except Exception as e:
                logger.error(f"Failed to save chat history: {e}")

        # --- Отправляем ответ пользователю ---
        await update.message.reply_text(reply_text)

    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        await update.message.reply_text("Извини, сейчас проблемы с подключением к нейросети. Попробуй позже.")

# --- Flask endpoint для приёма вебхуков ---
@flask_app.route('/webhook', methods=['POST'])
def webhook():
    if application:
        update = Update.de_json(request.get_json(force=True), application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), application.loop)
    return 'OK', 200

# --- Функция запуска ---
def main():
    global application
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return

    # Создаём Application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Инициализируем и запускаем
    application.initialize()
    application.start()

    logger.info("Bot is running, waiting for webhook...")

    # Запускаем Flask сервер (Render будет использовать gunicorn)
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()
