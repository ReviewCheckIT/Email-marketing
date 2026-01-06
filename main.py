# -*- coding: utf-8 -*-
import logging
import os
import json
import asyncio
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from google_play_scraper import search as play_search, app as app_details
from google.genai import Client
import firebase_admin
from firebase_admin import credentials, db # Realtime Database ব্যবহার করছি

# --- Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Env Variables ---
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
OWNER_ID = os.environ.get('BOT_OWNER_ID')
FB_JSON = os.environ.get('FIREBASE_CREDENTIALS_JSON')
FB_URL = os.environ.get('FIREBASE_DATABASE_URL') # রিয়েলটাইম ডেটাবেজ লিংকের জন্য
GEMINI_KEY = os.environ.get('GEMINI_API_KEY')
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL')
PORT = int(os.environ.get('PORT', '8080'))

# --- Firebase Realtime DB Init ---
try:
    if not firebase_admin._apps:
        cred_dict = json.loads(FB_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {
            'databaseURL': FB_URL # এখানে আপনার Firebase URL দিতে হবে
        })
    logger.info("🔥 Realtime Database Connected!")
except Exception as e:
    logger.error(f"❌ Firebase Error: {e}")

# --- AI Logic (Error Handling Improved) ---
async def get_keywords(base_kw):
    if not GEMINI_KEY: return [base_kw]
    try:
        client = Client(api_key=GEMINI_KEY)
        prompt = f"Target: Low download niche apps. List 5 short Play Store search terms for '{base_kw}'. Provide only comma separated values."
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        kws = [k.strip() for k in response.text.split(',') if k.strip()]
        return list(set([base_kw] + kws))
    except Exception as e:
        return [base_kw]

# --- Scraper Engine (More Precise) ---
async def scrape_task(base_kw, context, uid):
    keywords = await get_keywords(base_kw)
    await context.bot.send_message(uid, f"🔍 Searching deeply for: {', '.join(keywords)}")
    
    new_count = 0
    ref = db.reference('scraped_emails') # Realtime DB Path

    for kw in keywords:
        try:
            results = play_search(kw, n_hits=40)
            for r in results:
                try:
                    app = app_details(r['appId'], lang='en', country='us')
                    if app and app.get('developerEmail'):
                        email = app['developerEmail'].lower().replace('.', '_') # Firebase keys cannot have dots
                        score = app.get('score', 0)
                        
                        # ফিল্টার: রেটিং ৩.৯ এর নিচে অথবা কোনো রেটিং নেই এমন অ্যাপ
                        if score is None or score < 3.9:
                            # ডুপ্লিকেট চেক
                            if not ref.child(email).get():
                                data = {
                                    'app_name': app.get('title'),
                                    'email': app['developerEmail'],
                                    'rating': score,
                                    'installs': app.get('installs'),
                                    'dev_name': app.get('developer'),
                                    'timestamp': datetime.now().isoformat()
                                }
                                ref.child(email).set(data)
                                new_count += 1
                except: continue
        except Exception as e:
            logger.error(f"Search error: {e}")

    await context.bot.send_message(uid, f"✅ Scrape Complete!\n🚀 New leads found: {new_count}")

# --- Standard Handlers ---
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if str(u.effective_user.id) != str(OWNER_ID): return
    btn = [[InlineKeyboardButton("🚀 Start Scrape", callback_data='s')]]
    await u.message.reply_text("Welcome Admin! Ready to find leads?", reply_markup=InlineKeyboardMarkup(btn))

async def cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.data == 's':
        c.user_data['state'] = 'kw'
        await q.edit_message_text("Enter your main keyword (e.g., 'Video Editor'):")

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if c.user_data.get('state') == 'kw':
        c.user_data['state'] = None
        # Background task
        asyncio.create_task(scrape_task(u.message.text, c, u.effective_user.id))
        await u.message.reply_text(f"Started searching for '{u.message.text}'. I will notify you when done.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))

    if RENDER_URL:
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN[-10:], 
                        webhook_url=f"{RENDER_URL}/{TOKEN[-10:]}")
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
