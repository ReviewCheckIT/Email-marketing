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
    logger.info("🔥 Firebase Global Database Connected!")
except Exception as e:
    logger.error(f"❌ Firebase Error: {e}")
    sys.exit(1)

def is_owner(uid):
    return str(uid) == str(OWNER_ID)

# --- AI Deep Keyword Expansion (ক্ষমতা বাড়ানো হয়েছে) ---
async def get_expanded_keywords(base_kw):
    if not GEMINI_KEY: return [base_kw]
    try:
        client = Client(api_key=GEMINI_KEY)
        # জেমিনিকে ১০০টি ব্রড কিওয়ার্ড দিতে বলা হয়েছে যাতে রেজাল্ট হাজার হাজার আসে
        prompt = f"Generate 100 unique, broad, and popular search phrases for Google Play Store to find new and unrated apps related to '{base_kw}'. Focus on terms that return maximum results. Provide only comma-separated values."
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        kws = [k.strip() for k in response.text.split(',') if k.strip()]
        return list(set([base_kw] + kws))[:100]
    except:
        return [base_kw]

# --- Global Scraper Engine ---
async def scrape_task(base_kw, context, uid):
    keywords = await get_expanded_keywords(base_kw)
    # ৩০টিরও বেশি কান্ট্রি যাতে সারা পৃথিবীর অ্যাপ কভার হয়
    countries = ['us', 'gb', 'in', 'ca', 'br', 'au', 'de', 'id', 'ph', 'pk', 'za', 'mx', 'tr', 'sa', 'ae', 'ru', 'fr', 'it', 'es', 'nl'] 
    
    await context.bot.send_message(uid, f"🌍 **মেগা সার্চ শুরু!** \n🔍 নিস: {base_kw}\n🎯 ১০০টি কিওয়ার্ড এবং ২০টি দেশে তল্লাশি চলছে।\nপ্রচুর অ্যাপ স্ক্যান হচ্ছে, একটু সময় দিন...")
    
    new_count = 0
    session_leads = []
    ref = db.reference('scraped_emails')
    processed_apps = set()

    for kw in keywords:
        for lang_country in countries:
            try:
                # n_hits বাড়িয়ে ২৫০ করা হয়েছে যাতে সর্বোচ্চ রেজাল্ট আসে
                results = play_search(kw, n_hits=250, lang='en', country=lang_country) 
                if not results: continue

                for r in results:
                    app_id = r['appId']
                    if app_id in processed_apps: continue
                    processed_apps.add(app_id)

                    try:
                        app = app_details(app_id, lang='en', country=lang_country)
                        if app and app.get('developerEmail'):
                            email_raw = app['developerEmail'].lower().strip()
                            score = app.get('score', 0)
                            reviews = app.get('reviews', 0)

                            # টার্গেট: জিরো রেটিং এবং জিরো রিভিউ অ্যাপ
                            if (score == 0 or score is None) and (reviews == 0 or reviews is None):
                                email_key = email_raw.replace('.', '_').replace('@', '_at_')
                                
                                if not ref.child(email_key).get():
                                    data = {
                                        'app_name': app.get('title'),
                                        'email': email_raw,
                                        'rating': 0,
                                        'reviews': 0,
                                        'installs': app.get('installs'),
                                        'country': lang_country,
                                        'dev': app.get('developer'),
                                        'timestamp': datetime.now().isoformat()
                                    }
                                    ref.child(email_key).set(data)
                                    session_leads.append(data)
                                    new_count += 1
                    except: continue
                
                # প্রতি ৩০টি ইমেল পাওয়ার পর লগ আপডেট
                if new_count > 0 and new_count % 30 == 0:
                    logger.info(f"Progress: Found {new_count} leads...")
                
                await asyncio.sleep(0.1) # ব্যান এড়াতে সামান্য বিরতি
            except: continue

    # কাজ শেষ হলে অটোমেটিক ফাইল পাঠানো
    if session_leads:
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(['App Name', 'Email', 'Rating', 'Reviews', 'Installs', 'Country', 'Developer', 'Date'])
        for v in session_leads:
            cw.writerow([v.get('app_name'), v.get('email'), 0, 0, v.get('installs'), v.get('country'), v.get('dev'), v.get('timestamp')])
        
        output = io.BytesIO(si.getvalue().encode())
        output.name = f"Leads_{base_kw}_{datetime.now().strftime('%d_%m')}.csv"
        await context.bot.send_document(chat_id=uid, document=output, caption=f"✅ কাজ শেষ!\n🔥 এই সেশনে মোট {new_count}টি নতুন ইমেল পাওয়া গেছে।")
    else:
        await context.bot.send_message(uid, "❌ এই কিওয়ার্ড দিয়ে কোনো নতুন জিরো-রেটিং অ্যাপ পাওয়া যায়নি।")

# --- Handlers (আপনার অরিজিনাল কমান্ডগুলো ঠিক রাখা হয়েছে) ---
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    btn = [[InlineKeyboardButton("🌍 স্টার্ট মেগা স্ক্র্যাপিং", callback_data='s')]]
    await u.message.reply_text("বট অনলাইন! এখন এটি বিশাল পরিসরে জিরো-রিভিউ অ্যাপ খুঁজবে।", reply_markup=InlineKeyboardMarkup(btn))

async def stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    count = len(data) if data else 0
    await u.message.reply_text(f"📊 ডাটাবেজে মোট লিড সংখ্যা: {count}")

async def export(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    if not data:
        await u.message.reply_text("কোনো ডেটা নেই!")
        return

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['App Name', 'Email', 'Rating', 'Reviews', 'Installs', 'Country', 'Developer', 'Date'])
    for k, v in data.items():
        cw.writerow([v.get('app_name'), v.get('email'), 0, 0, v.get('installs'), v.get('country'), v.get('dev'), v.get('timestamp')])
    
    output = io.BytesIO(si.getvalue().encode())
    output.name = f"Global_Database_Export_{datetime.now().strftime('%d_%m')}.csv"
    await u.message.reply_document(document=output, caption="✅ ডাটাবেজের সব লিড লিস্ট।")

async def clear_db(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    db.reference('scraped_emails').delete()
    await u.message.reply_text("🗑️ সব ডেটা ডিলিট করা হয়েছে।")

async def cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    if not is_owner(q.from_user.id): return
    await q.answer()
    if q.data == 's':
        c.user_data['state'] = 'kw'
        await q.edit_message_text("কোন নিশের ইমেল চান? কিওয়ার্ড দিন (যেমন: VPN):")

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    if c.user_data.get('state') == 'kw':
        c.user_data['state'] = None
        asyncio.create_task(scrape_task(u.message.text, c, u.effective_user.id))
        await u.message.reply_text(f"🔍 '{u.message.text}' নিয়ে মেগা সার্চ চলছে... ফাইল তৈরি হলে অটো পাঠিয়ে দেব।")

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
