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
from google_play_scraper import search as play_search, app as app_details, Sort
from google.genai import Client
import firebase_admin
from firebase_admin import credentials, db

# --- Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Env Variables ---
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
OWNER_ID = os.environ.get('BOT_OWNER_ID')
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
    logger.info("🔥 Firebase Connected!")
except Exception as e:
    logger.error(f"❌ Firebase Error: {e}")
    sys.exit(1)

def is_owner(uid):
    return str(uid) == str(OWNER_ID)

# --- AI Logic (Focused on NEW/UNRATED apps) ---
async def get_keywords(base_kw):
    if not GEMINI_KEY: return [base_kw]
    try:
        client = Client(api_key=GEMINI_KEY)
        # AI-কে নির্দেশ দেওয়া হচ্ছে নতুন অ্যাপের কিওয়ার্ড বের করতে
        prompt = f"Provide 8 search terms to find brand new or unrated Android apps for the niche: '{base_kw}'. Focus on terms that would show new releases. CSV format only."
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        kws = [k.strip() for k in response.text.split(',') if k.strip()]
        return list(set([base_kw] + kws))
    except: return [base_kw]

# --- Scraper Engine (Targeting Zero Ratings) ---
async def scrape_task(base_kw, context, uid):
    keywords = await get_keywords(base_kw)
    await context.bot.send_message(uid, f"🚀 অনুসন্ধান শুরু! টার্গেট: নতুন ও রেটিংহীন অ্যাপ।\nকিওয়ার্ড: {', '.join(keywords)}")
    
    new_count = 0
    ref = db.reference('scraped_emails')

    for kw in keywords:
        try:
            # সার্চ রেজাল্ট বাড়ানো হয়েছে (n_hits=100) যাতে নতুন অ্যাপ পাওয়ার সম্ভাবনা বাড়ে
            results = play_search(kw, n_hits=100) 
            for r in results:
                try:
                    app = app_details(r['appId'], lang='en', country='us')
                    if app and app.get('developerEmail'):
                        email_raw = app['developerEmail'].lower().strip()
                        email_key = email_raw.replace('.', '_').replace('@', '_at_')
                        
                        score = app.get('score', 0)
                        reviews = app.get('reviews', 0)

                        # কন্ডিশন: রেটিং একদম নেই (0.0) অথবা রিভিউ ০ এমন অ্যাপ টার্গেট
                        if score == 0 or score is None or reviews == 0:
                            if not ref.child(email_key).get():
                                data = {
                                    'app_name': app.get('title'),
                                    'email': email_raw,
                                    'rating': score,
                                    'reviews': reviews,
                                    'installs': app.get('installs'),
                                    'dev': app.get('developer'),
                                    'timestamp': datetime.now().isoformat()
                                }
                                ref.child(email_key).set(data)
                                new_count += 1
                                # প্রতি ১০টি ইমেল পাওয়ার পর আপডেট দেবে
                                if new_count % 10 == 0:
                                    logger.info(f"Found {new_count} leads so far...")
                except: continue
        except: continue

    await context.bot.send_message(uid, f"✅ মিশন সফল!\n🔥 মোট {new_count}টি নতুন/রেটিংহীন অ্যাপের ইমেল পাওয়া গেছে।\n/export লিখে ফাইলটি নিন।")

# --- Handlers ---
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    btn = [[InlineKeyboardButton("🎯 স্টার্ট নিউ স্ক্র্যাপিং", callback_data='s')]]
    await u.message.reply_text("বট প্রস্তুত! এই মোডটি শুধুমাত্র 'Zero Rating' বা নতুন অ্যাপ টার্গেট করবে।", reply_markup=InlineKeyboardMarkup(btn))

async def stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    count = len(data) if data else 0
    await u.message.reply_text(f"📊 বর্তমানে মোট লিড সংখ্যা: {count}")

async def export(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    if not data:
        await u.message.reply_text("ডেটাবেজ ফাঁকা!")
        return

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['App Name', 'Email', 'Rating', 'Reviews', 'Installs', 'Developer', 'Date'])
    for k, v in data.items():
        cw.writerow([v.get('app_name'), v.get('email'), v.get('rating'), v.get('reviews'), v.get('installs'), v.get('dev'), v.get('timestamp')])
    
    output = io.BytesIO(si.getvalue().encode())
    output.name = f"Zero_Rating_Leads_{datetime.now().strftime('%d_%m')}.csv"
    await u.message.reply_document(document=output, caption="✅ রেটিংহীন অ্যাপের লিড লিস্ট।")

async def clear_db(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    db.reference('scraped_emails').delete()
    await u.message.reply_text("🗑️ সব ডেটা মুছে ফেলা হয়েছে।")

async def cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    if not is_owner(q.from_user.id): return
    await q.answer()
    if q.data == 's':
        c.user_data['state'] = 'kw'
        await q.edit_message_text("কোন নিশের (Niche) নতুন অ্যাপ খুঁজছেন? কিওয়ার্ড লিখুন:")

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    if c.user_data.get('state') == 'kw':
        c.user_data['state'] = None
        asyncio.create_task(scrape_task(u.message.text, c, u.effective_user.id))
        await u.message.reply_text(f"🔍 '{u.message.text}' নিশে রেটিংহীন অ্যাপ খোঁজা হচ্ছে...")

def main():
    if not TOKEN: return
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("clear", clear_db))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))

    if RENDER_URL:
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN[-10:], 
                        webhook_url=f"{RENDER_URL}/{TOKEN[-10:]}")
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
