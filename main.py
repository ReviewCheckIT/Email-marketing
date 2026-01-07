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
    logger.info("🔥 Firebase Connected!")
except Exception as e:
    logger.error(f"❌ Firebase Error: {e}")
    sys.exit(1)

def is_owner(uid):
    return str(uid) == str(OWNER_ID)

# --- AI মাসিভ কিওয়ার্ড জেনারেশন (১০০ কিওয়ার্ড) ---
async def get_expanded_keywords(base_kw):
    if not GEMINI_KEY: return [base_kw]
    try:
        client = Client(api_key=GEMINI_KEY)
        prompt = (f"Generate 100 unique and extremely diverse search phrases for Google Play Store to find 'New' and 'Unrated' apps for the niche: '{base_kw}'. "
                  f"Use words like: new, beta, early access, 2026, tools, simple, trial, upcoming, and long-tail phrases. "
                  f"Provide only comma-separated values.")
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        kws = [k.strip() for k in response.text.split(',') if k.strip()]
        return list(set([base_kw] + kws))[:100]
    except:
        return [base_kw] * 5 # Fallback

# --- অ্যাপ ডিটেইলস ফেচার ---
async def get_app_info(app_id, country, ref, session_cache):
    try:
        # একই সেশনে একই অ্যাপ দুইবার চেক করবে না
        if app_id in session_cache: return None
        session_cache.add(app_id)

        app = app_details(app_id, lang='en', country=country)
        if app and app.get('developerEmail'):
            email = app['developerEmail'].lower().strip()
            score = app.get('score', 0)
            reviews = app.get('reviews', 0)

            # টার্গেট: জিরো রেটিং এবং জিরো রিভিউ (নতুন অ্যাপ)
            if (score == 0 or score is None) and (reviews == 0 or reviews is None):
                email_key = email.replace('.', '_').replace('@', '_at_')
                return {
                    'app_name': app.get('title'),
                    'email': email,
                    'installs': app.get('installs'),
                    'country': country,
                    'dev': app.get('developer'),
                    'id': email_key
                }
    except:
        pass
    return None

# --- Main Scraper Engine ---
async def scrape_task(base_kw, context, uid):
    keywords = await get_expanded_keywords(base_kw)
    # ৪০টি দেশের বিশাল লিস্ট (সব বড় মার্কেট কভার করা হয়েছে)
    countries = [
        'us', 'gb', 'in', 'ca', 'au', 'br', 'id', 'ph', 'pk', 'de', 'fr', 'es', 'it', 'nl', 'ru', 'za', 
        'mx', 'my', 'th', 'vn', 'tr', 'sa', 'ae', 'eg', 'pl', 'se', 'no', 'dk', 'fi', 'ar', 'cl', 'co', 
        'ng', 'ke', 'bd', 'sg', 'ie', 'nz', 'pt', 'be'
    ]
    
    await context.bot.send_message(uid, f"🚀 **ম্যাসভ সার্চ শুরু!**\n🔍 নিস: {base_kw}\n🎯 কিওয়ার্ড: {len(keywords)}টি\n🌍 দেশ: {len(countries)}টি\n\nহাজার হাজার অ্যাপ চেক করা হচ্ছে, একটু সময় দিন।")
    
    ref = db.reference('scraped_emails')
    session_cache = set()
    total_found = 0
    all_leads = []

    # স্পিড বাড়ানোর জন্য ব্যাচ প্রসেসিং
    for kw in keywords:
        search_tasks = []
        for country in countries:
            try:
                # n_hits=250 (গুগলের সর্বোচ্চ লিমিট)
                results = play_search(kw, n_hits=250, lang='en', country=country)
                if not results: continue
                
                for r in results:
                    search_tasks.append(get_app_info(r['appId'], country, ref, session_cache))
                
                # এক সাথে ২০টি করে অ্যাপ প্রসেস করবে (রেন্ডারের সিপিইউ লিমিট মাথায় রেখে)
                if len(search_tasks) >= 20:
                    batch_results = await asyncio.gather(*search_tasks)
                    for res in batch_results:
                        if res:
                            ref.child(res['id']).set(res)
                            all_leads.append(res)
                            total_found += 1
                    search_tasks = [] # রিসেট
                    
                # প্রতি ৩০টি লিড পাওয়ার পর ইউজারকে আপডেট দিবে
                if total_found > 0 and total_found % 50 == 0:
                    logger.info(f"Progress: {total_found} leads found...")

            except:
                continue
        await asyncio.sleep(0.1) # সেফটি গ্যাপ

    # কাজ শেষে ফাইল পাঠানো
    if all_leads:
        await send_csv(context, uid, all_leads, f"Massive_{base_kw}")
        await context.bot.send_message(uid, f"✅ মিশন কমপ্লিট!\n🔥 মোট {total_found}টি নতুন ইমেইল পাওয়া গেছে।")
    else:
        await context.bot.send_message(uid, "❌ কোনো লিড পাওয়া যায়নি। কিওয়ার্ড পরিবর্তন করে দেখুন।")

async def send_csv(context, uid, data_list, name):
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['App Name', 'Email', 'Installs', 'Country', 'Developer'])
    for d in data_list:
        cw.writerow([d['app_name'], d['email'], d['installs'], d['country'], d['dev']])
    
    output = io.BytesIO(si.getvalue().encode())
    output.name = f"{name}_{datetime.now().strftime('%H%M')}.csv"
    await context.bot.send_document(chat_id=uid, document=output, caption=f"📁 এখানে {len(data_list)}টি ফ্রেশ লিড আছে।")

# --- Handlers ---
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    btn = [[InlineKeyboardButton("🔥 স্টার্ট আলটিমেট স্ক্র্যাপিং", callback_data='run')]]
    await u.message.reply_text("বট রেডি! এখন এটি হাজার হাজার অ্যাপ থেকে ইমেল ছেঁকে বের করবে।", reply_markup=InlineKeyboardMarkup(btn))

async def cb_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    if not is_owner(q.from_user.id): return
    await q.answer()
    if q.data == 'run':
        c.user_data['state'] = 'wait_kw'
        await q.edit_message_text("কোন বিষয়ের ওপর লিড চান? কিওয়ার্ড দিন (যেমন: VPN, Editor, Game):")

async def msg_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    if c.user_data.get('state') == 'wait_kw':
        c.user_data['state'] = None
        asyncio.create_task(scrape_task(u.message.text, c, u.effective_user.id))
        await u.message.reply_text(f"🔎 '{u.message.text}' নিয়ে গ্লোবাল অপারেশন শুরু হয়েছে। প্রচুর ডেটা স্ক্যান হচ্ছে, শেষ হলে অটোমেটিক ফাইল পাবেন।")

async def export(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    if data:
        await send_csv(c, u.effective_user.id, data.values(), "Full_DB")
    else:
        await u.message.reply_text("ডাটাবেজ খালি!")

async def clear(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    db.reference('scraped_emails').delete()
    await u.message.reply_text("🗑️ সব ডিলিট করা হয়েছে।")

def main():
    if not TOKEN: return
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))

    if RENDER_URL:
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN[-10:], webhook_url=f"{RENDER_URL}/{TOKEN[-10:]}")
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
