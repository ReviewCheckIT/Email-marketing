# -*- coding: utf-8 -*-
import logging
import os
import json
import asyncio
import csv
import io
import sys
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from google_play_scraper import search as play_search, app as app_details
from google.genai import Client
import firebase_admin
from firebase_admin import credentials, db

# --- Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Env Variables ---
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
OWNER_ID = os.environ.get('BOT_OWNER_ID') # এটি আপনার Chat ID
FB_JSON = os.environ.get('FIREBASE_CREDENTIALS_JSON')
FB_URL = os.environ.get('FIREBASE_DATABASE_URL')
GEMINI_KEY = os.environ.get('GEMINI_API_KEY')
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL')
PORT = int(os.environ.get('PORT', '8080'))

# --- Firebase Init ---
try:
    if not firebase_admin._apps:
        cred_dict = json.loads(FB_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {'databaseURL': FB_URL})
    logger.info("🔥 Realtime Database Connected!")
except Exception as e:
    logger.error(f"❌ Firebase Error: {e}")
    sys.exit(1)

# --- Admin Security Check ---
def is_owner(uid):
    """এটি নিশ্চিত করে যে কেবল আপনার Chat ID থেকে কমান্ড কাজ করবে"""
    return str(uid) == str(OWNER_ID)

# --- AI Logic ---
async def get_keywords(base_kw):
    if not GEMINI_KEY: return [base_kw]
    try:
        client = Client(api_key=GEMINI_KEY)
        prompt = f"List 5 specific Play Store search keywords related to '{base_kw}' for finding niche apps. CSV format only."
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        kws = [k.strip() for k in response.text.split(',') if k.strip()]
        return list(set([base_kw] + kws))
    except: return [base_kw]

# --- Scraper Engine ---
async def scrape_task(base_kw, context, uid):
    keywords = await get_keywords(base_kw)
    await context.bot.send_message(uid, f"🔍 কাজ শুরু হয়েছে। কিওয়ার্ডসমূহ: {', '.join(keywords)}")
    
    new_count = 0
    ref = db.reference('scraped_emails')

    for kw in keywords:
        try:
            results = play_search(kw, n_hits=50)
            for r in results:
                try:
                    app = app_details(r['appId'], lang='en', country='us')
                    if app and app.get('developerEmail'):
                        email_raw = app['developerEmail'].lower().strip()
                        # Firebase key হিসেবে ইমেল ব্যবহার করার জন্য ডট ক্লিন করা
                        email_key = email_raw.replace('.', '_').replace('@', '_at_')
                        
                        score = app.get('score', 0)
                        if score is None or score < 3.9:
                            if not ref.child(email_key).get():
                                data = {
                                    'app_name': app.get('title'),
                                    'email': email_raw,
                                    'rating': score,
                                    'installs': app.get('installs'),
                                    'dev': app.get('developer'),
                                    'timestamp': datetime.now().isoformat()
                                }
                                ref.child(email_key).set(data)
                                new_count += 1
                except: continue
        except: continue

    await context.bot.send_message(uid, f"✅ কাজ সফলভাবে শেষ!\n🚀 নতুন {new_count}টি ইউনিক ইমেল পাওয়া গেছে।")

# --- Commands Implementation ---

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    btn = [[InlineKeyboardButton("🚀 নতুন সার্চ শুরু করুন", callback_data='s')]]
    await u.message.reply_text(
        "👋 স্বাগতম এডমিন!\n\nকমান্ড লিস্ট:\n/stats - মোট ইমেল সংখ্যা দেখতে\n/export - CSV ডাউনলোড করতে\n/clear - সব মুছতে", 
        reply_markup=InlineKeyboardMarkup(btn)
    )

async def stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    count = len(data) if data else 0
    await u.message.reply_text(f"📊 আপনার ডেটাবেজে বর্তমানে মোট {count}টি ইমেল আছে।")

async def export(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    if not data:
        await u.message.reply_text("ডেটাবেজে কোনো তথ্য নেই!")
        return

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['App Name', 'Email', 'Rating', 'Installs', 'Developer', 'Date'])
    for k, v in data.items():
        cw.writerow([v.get('app_name'), v.get('email'), v.get('rating'), v.get('installs'), v.get('dev'), v.get('timestamp')])
    
    output = io.BytesIO(si.getvalue().encode())
    output.name = f"leads_{datetime.now().strftime('%d_%m_%Y')}.csv"
    await u.message.reply_document(document=output, caption="✅ আপনার সংগৃহীত লিড ফাইল।")

async def clear_db(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    db.reference('scraped_emails').delete()
    await u.message.reply_text("🗑️ ডেটাবেজ সম্পূর্ণ পরিষ্কার করা হয়েছে!")

async def cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    if not is_owner(q.from_user.id): return
    await q.answer()
    if q.data == 's':
        c.user_data['state'] = 'kw'
        await q.edit_message_text("কোন বিষয়ের অ্যাপ খুঁজছেন? কিওয়ার্ড দিন (যেমন: Photo Editor):")

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    if c.user_data.get('state') == 'kw':
        c.user_data['state'] = None
        asyncio.create_task(scrape_task(u.message.text, c, u.effective_user.id))
        await u.message.reply_text(f"'{u.message.text}' নিয়ে গভীর অনুসন্ধান শুরু হয়েছে...")

# --- Main Initialization ---
def main():
    if not TOKEN: return
    app = Application.builder().token(TOKEN).build()

    # কমান্ড রেজিস্টার করা
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("clear", clear_db))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))

    if RENDER_URL:
        # Webhook setup for Render
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN[-10:], 
                        webhook_url=f"{RENDER_URL}/{TOKEN[-10:]}")
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
