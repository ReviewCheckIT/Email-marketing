# -*- coding: utf-8 -*-
import logging
import os
import sys
import json
import asyncio
import csv
import io
from datetime import datetime

# Libraries
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from google_play_scraper import search as play_search, app as app_details
from google.genai import Client
import firebase_admin
from firebase_admin import credentials, firestore

# --- Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Load Environment Variables ---
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
OWNER_ID = os.environ.get('BOT_OWNER_ID')
FB_JSON = os.environ.get('FIREBASE_CREDENTIALS_JSON')
GEMINI_KEY = os.environ.get('GEMINI_API_KEY')
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL')
PORT = int(os.environ.get('PORT', '8080'))

# --- Firebase Init (Safe Mode) ---
db = None
try:
    if not firebase_admin._apps:
        if not FB_JSON:
            logger.error("FIREBASE_CREDENTIALS_JSON is missing!")
            sys.exit(1)
        
        # Handling JSON potential formatting issues from Render env
        try:
            cred_dict = json.loads(FB_JSON)
        except json.JSONDecodeError:
            # Try fixing single quotes if any
            cred_dict = json.loads(FB_JSON.replace("'", '"'))
            
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("🔥 Firebase Connected!")
except Exception as e:
    logger.error(f"❌ Firebase Critical Error: {e}")
    sys.exit(1)

# --- AI Logic ---
async def get_keywords(base_kw):
    if not GEMINI_KEY: return [base_kw]
    try:
        client = Client(api_key=GEMINI_KEY)
        prompt = f"Target: Low download apps. List 5 Play Store search terms for '{base_kw}'. CSV format only."
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        kws = [k.strip() for k in response.text.split(',') if k.strip()]
        return list(set([base_kw] + kws))
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return [base_kw]

# --- Admin Check ---
async def is_admin(uid):
    return str(uid) == str(OWNER_ID)

# --- Scraper Engine ---
async def scrape_task(base_kw, context, uid):
    keywords = await get_keywords(base_kw)
    await context.bot.send_message(uid, f"🔍 Searching: {', '.join(keywords)}")
    
    new_count = 0
    semaphore = asyncio.Semaphore(5)

    async def get_app_data(aid):
        async with semaphore:
            try:
                await asyncio.sleep(1)
                return app_details(aid, lang='en', country='us')
            except: return None

    for kw in keywords:
        try:
            results = play_search(kw, n_hits=30)
            tasks = [get_app_data(r['appId']) for r in results]
            apps = await asyncio.gather(*tasks)

            for app in apps:
                if app and app.get('developerEmail'):
                    email = app['developerEmail'].lower().strip()
                    # Filter: Rating < 3.8 or No Ratings
                    score = app.get('score', 0)
                    if score is None or score < 3.8:
                        doc_ref = db.collection('scraped_app_emails').document(email)
                        if not doc_ref.get().exists:
                            data = {
                                'name': app.get('title'),
                                'email': email,
                                'rating': score,
                                'dev': app.get('developer'),
                                'date': datetime.now().isoformat()
                            }
                            doc_ref.set(data)
                            new_count += 1
        except Exception as e:
            logger.error(f"Loop Error: {e}")

    await context.bot.send_message(uid, f"✅ Done! Found {new_count} new leads.")

# --- Handlers ---
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(u.effective_user.id): return
    btn = [[InlineKeyboardButton("🚀 Start Search", callback_data='s')]]
    await u.message.reply_text("Play Store Scraper Bot Live!", reply_markup=InlineKeyboardMarkup(btn))

async def cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.data == 's':
        c.user_data['state'] = 'kw'
        await q.edit_message_text("Type Keyword:")

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if c.user_data.get('state') == 'kw':
        c.user_data['state'] = None
        asyncio.create_task(scrape_task(u.message.text, c, u.effective_user.id))
        await u.message.reply_text("Processing...")

# --- Main ---
def main():
    if not TOKEN:
        logger.error("No Bot Token!")
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))

    if RENDER_URL:
        # Webhook setup
        url_path = TOKEN[-10:]
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=url_path, webhook_url=f"{RENDER_URL}/{url_path}")
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
