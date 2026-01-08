# -*- coding: utf-8 -*-
import logging
import os
import json
import asyncio
import csv
import io
import sys
import random
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
OWNER_ID = os.environ.get('BOT_OWNER_ID')
FB_JSON = os.environ.get('FIREBASE_CREDENTIALS_JSON')
FB_URL = os.environ.get('FIREBASE_DATABASE_URL')
GEMINI_KEY = os.environ.get('GEMINI_API_KEY')
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL')
PORT = int(os.environ.get('PORT', '8080'))

# --- Global Logic ---
IS_SCRAPING = False

# --- Firebase Init ---
try:
    if not firebase_admin._apps:
        cred_dict = json.loads(FB_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {'databaseURL': FB_URL})
    logger.info("🔥 Firebase Global Database Connected!")
except Exception as e:
    logger.error(f"❌ Firebase Error: {e}")
    sys.exit(1)

def is_owner(uid):
    return str(uid) == str(OWNER_ID)

# --- AI Keyword Expansion (Smarter & Verified) ---
async def get_expanded_keywords(base_kw):
    if not GEMINI_KEY: return []
    try:
        client = Client(api_key=GEMINI_KEY)
        # জেমিনিকে বাধ্য করা হচ্ছে ভালো কিওয়ার্ড দিতে
        prompt = f"Act as an ASO expert. Generate 60 high-traffic yet niche search phrases for Google Play to find unrated apps for '{base_kw}'. Return ONLY a comma-separated list."
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        
        if response and response.text:
            cleaned_text = response.text.replace('\n', ',').replace('*', '')
            kws = [k.strip() for k in cleaned_text.split(',') if len(k.strip()) > 2]
            return list(set([base_kw] + kws))
        return []
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return []

# --- Mega Scraper Engine (High Performance + Safety) ---
async def scrape_task(base_kw, context, uid):
    global IS_SCRAPING
    IS_SCRAPING = True
    
    # প্রথমে কিওয়ার্ড জেনারেট করা হচ্ছে
    keywords = await get_expanded_keywords(base_kw)
    
    # যদি কিওয়ার্ড না পাওয়া যায়, তবে মিশন বাতিল
    if not keywords or len(keywords) <= 1:
        await context.bot.send_message(uid, "❌ **Error:** জেমিনি কিওয়ার্ড জেনারেট করতে পারেনি। আপনার Gemini API Key চেক করুন বা কোটা শেষ হয়েছে কিনা দেখুন। মিশন বাতিল।")
        IS_SCRAPING = False
        return

    # আপনার চাহিদামতো ৩০টি দেশ
    countries = ['us', 'gb', 'in', 'ca', 'br', 'au', 'de', 'id', 'ph', 'pk', 'za', 'mx', 'tr', 'sa', 'ae', 'ru', 'fr', 'it', 'es', 'nl', 'bd', 'sg', 'my', 'vn', 'th', 'ng', 'eg', 'ar', 'co', 'pl']
    
    await context.bot.send_message(uid, f"🌍 **মেগা মিশন লাইভ!**\n🔍 নিস: {base_kw}\n🎯 কিওয়ার্ড: {len(keywords)}টি\n🏳️‍🌈 দেশ: {len(countries)}টি\n\nলিড খোঁজা হচ্ছে, বেশি লিড পেতে একটু সময় দিন...")
    
    new_count = 0
    session_leads = []
    ref = db.reference('scraped_emails')
    processed_apps = set()

    for kw in keywords:
        # ব্যালেন্সড কান্ট্রি অ্যাটাক (প্রতিবার ১০টি করে দেশ র্যান্ডমলি নিবে যাতে সব দেশ কভার হয় কিন্তু ব্লক না খায়)
        random.shuffle(countries)
        for lang_country in countries[:15]: 
            try:
                results = play_search(kw, n_hits=100, lang='en', country=lang_country)
                if not results: continue

                for r in results:
                    app_id = r['appId']
                    if app_id in processed_apps: continue
                    processed_apps.add(app_id)

                    try:
                        app = app_details(app_id, lang='en', country=lang_country)
                        email = app.get('developerEmail')
                        if email and (app.get('score', 0) == 0 or app.get('score') is None):
                            email_raw = email.lower().strip()
                            email_key = email_raw.replace('.', '_').replace('@', '_at_')
                            
                            if not ref.child(email_key).get():
                                data = {
                                    'app_name': app.get('title'),
                                    'email': email_raw,
                                    'rating': 0,
                                    'installs': app.get('installs'),
                                    'country': lang_country,
                                    'dev': app.get('developer'),
                                    'timestamp': datetime.now().isoformat()
                                }
                                ref.child(email_key).set(data)
                                session_leads.append(data)
                                new_count += 1
                    except: continue
                
                # অতি ক্ষুদ্র বিরতি যাতে গুগল একদম ব্লক না করে
                await asyncio.sleep(0.5)
            except: 
                await asyncio.sleep(5) # এরর খেলে একটু বেশি থামা
                continue

    IS_SCRAPING = False

    if session_leads:
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(['App Name', 'Email', 'Rating', 'Installs', 'Country', 'Developer', 'Date'])
        for v in session_leads:
            cw.writerow([v['app_name'], v['email'], 0, v['installs'], v['country'], v['dev'], v['timestamp']])
        
        output = io.BytesIO(si.getvalue().encode())
        output.name = f"Leads_{base_kw}_{datetime.now().strftime('%H%M')}.csv"
        await context.bot.send_document(chat_id=uid, document=output, caption=f"✅ **মিশন শেষ!**\n🔥 নতুন লিড: {new_count}টি।")
    else:
        await context.bot.send_message(uid, "❌ দুঃখিত, এই কিওয়ার্ডগুলোতে নতুন কোনো জিরো-রিভিউ অ্যাপ পাওয়া যায়নি।")

# --- Handlers (আগের মতোই) ---
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    btn = [[InlineKeyboardButton("🚀 স্টার্ট মেগা স্ক্র্যাপিং", callback_data='s')]]
    await u.message.reply_text(f"বট অনলাইন। বর্তমান অবস্থা: {'🔴 ব্যস্ত' if IS_SCRAPING else '🟢 ফ্রি'}", reply_markup=InlineKeyboardMarkup(btn))

async def cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    if not is_owner(q.from_user.id) or IS_SCRAPING: 
        await q.answer("⚠️ ব্যস্ত আছি বা পারমিশন নেই।")
        return
    await q.answer()
    if q.data == 's':
        c.user_data['state'] = 'kw'
        await q.edit_message_text("কিওয়ার্ড দিন:")

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id) or IS_SCRAPING: return
    if c.user_data.get('state') == 'kw':
        c.user_data['state'] = None
        asyncio.create_task(scrape_task(u.message.text, c, u.effective_user.id))
        await u.message.reply_text("🔍 কিওয়ার্ড প্রসেস হচ্ছে...")

async def stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    count = len(data) if data else 0
    await u.message.reply_text(f"📊 মোট লিড: {count}")

async def export(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    if not data: return
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['App Name', 'Email', 'Rating', 'Installs', 'Country', 'Developer', 'Date'])
    for k, v in data.items():
        cw.writerow([v.get('app_name'), v.get('email'), 0, v.get('installs'), v.get('country'), v.get('dev'), v.get('timestamp')])
    output = io.BytesIO(si.getvalue().encode()); output.name = "All_DB.csv"
    await u.message.reply_document(document=output)

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("clear", CommandHandler("clear", lambda u,c: db.reference('scraped_emails').delete())))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))
    if RENDER_URL:
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN[-10:], webhook_url=f"{RENDER_URL}/{TOKEN[-10:]}")
    else: app.run_polling()

if __name__ == "__main__": main()
