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
    logger.info("🔥 Firebase Database Connected!")
except Exception as e:
    logger.error(f"❌ Firebase Error: {e}")
    sys.exit(1)

# সেমাফোর বাড়িয়ে দিয়েছি যাতে আরও দ্রুত প্রসেস হয়
semaphore = asyncio.Semaphore(15) 

def is_owner(uid):
    return str(uid) == str(OWNER_ID)

# --- AI Broad Keyword Expansion ---
async def get_expanded_keywords(base_kw):
    if not GEMINI_KEY: return [base_kw]
    try:
        client = Client(api_key=GEMINI_KEY)
        # জেমিনিকে আরও সহজ এবং ব্রড কিওয়ার্ড দিতে বলা হয়েছে
        prompt = (f"Generate 40 simple, high-traffic, and broad search phrases for Google Play Store "
                  f"to find newly released apps related to '{base_kw}'. Keep them general like "
                  f"'{base_kw} tool', 'new {base_kw}', '{base_kw} 2026', etc. Comma separated.")
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        kws = [k.strip() for k in response.text.split(',') if k.strip()]
        return list(set([base_kw] + kws))
    except:
        return [base_kw]

# --- App Processor ---
async def process_single_app(app_id, lang_country, ref, processed_apps):
    if app_id in processed_apps: return None
    processed_apps.add(app_id)
    
    async with semaphore:
        try:
            app = app_details(app_id, lang='en', country=lang_country)
            if app and app.get('developerEmail'):
                email_raw = app['developerEmail'].lower().strip()
                score = app.get('score', 0)
                reviews = app.get('reviews', 0)

                # কঠোর ফিল্টার: জিরো রেটিং এবং জিরো রিভিউ
                if (score == 0 or score is None) and (reviews == 0 or reviews is None):
                    email_key = email_raw.replace('.', '_').replace('@', '_at_')
                    # ডাটাবেজে আগে থেকে আছে কি না চেক
                    if not ref.child(email_key).get():
                        return {
                            'app_name': app.get('title'),
                            'email': email_raw,
                            'rating': 0,
                            'reviews': 0,
                            'installs': app.get('installs'),
                            'country': lang_country,
                            'dev': app.get('developer'),
                            'timestamp': datetime.now().isoformat()
                        }
        except:
            pass
    return None

# --- Main Engine ---
async def scrape_task(base_kw, context, uid):
    keywords = await get_expanded_keywords(base_kw)
    # যে দেশগুলোতে নতুন অ্যাপের সংখ্যা বেশি
    countries = ['us', 'in', 'br', 'id', 'gb', 'ca', 'de', 'ph', 'pk']
    
    await context.bot.send_message(uid, f"⚡ অতি শক্তিশালী সার্চ শুরু হয়েছে!\n🔍 নিস: {base_kw}\n🎯 কিওয়ার্ড সংখ্যা: {len(keywords)}\n🌍 দেশসমূহ: {', '.join(countries).upper()}")
    
    new_count = 0
    ref = db.reference('scraped_emails')
    processed_apps = set()
    current_session_leads = []

    for kw in keywords:
        for lc in countries:
            try:
                # n_hits=200 করে দেওয়া হয়েছে সর্বোচ্চ রেজাল্টের জন্য
                results = play_search(kw, n_hits=200, lang='en', country=lc)
                if not results: continue
                
                tasks = [process_single_app(r['appId'], lc, ref, processed_apps) for r in results]
                batch_results = await asyncio.gather(*tasks)
                
                for data in batch_results:
                    if data:
                        # ডাটাবেজে সেভ
                        email_key = data['email'].replace('.', '_').replace('@', '_at_')
                        ref.child(email_key).set(data)
                        current_session_leads.append(data)
                        new_count += 1
                
                # প্রতি ১০০ অ্যাপ চেকের পর ছোট বিরতি (ব্যান এড়াতে)
                await asyncio.sleep(0.1)
            except:
                continue

    # সেশন শেষ হলে রিপোর্ট
    if current_session_leads:
        await send_file(context, uid, current_session_leads, f"Fresh_Leads_{base_kw}")
        await context.bot.send_message(uid, f"✅ কাজ শেষ! এই সেশনে {new_count}টি নতুন ইমেইল পাওয়া গেছে।")
    else:
        await context.bot.send_message(uid, "⚠️ দুঃখিত, এই কিওয়ার্ডগুলোতে কোনো নতুন জিরো-রিভিউ অ্যাপ পাওয়া যায়নি। অন্য কিওয়ার্ড ট্রাই করুন।")

async def send_file(context, uid, data_list, filename_prefix):
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['App Name', 'Email', 'Rating', 'Reviews', 'Installs', 'Country', 'Developer', 'Date'])
    for v in data_list:
        cw.writerow([v.get('app_name'), v.get('email'), 0, 0, v.get('installs'), v.get('country'), v.get('dev'), v.get('timestamp')])
    
    output = io.BytesIO(si.getvalue().encode())
    output.name = f"{filename_prefix}_{datetime.now().strftime('%d_%m')}.csv"
    await context.bot.send_document(chat_id=uid, document=output, caption=f"📩 সাকসেস! মোট {len(data_list)}টি ফ্রেশ ইমেল পাওয়া গেছে।")

# --- Handlers ---
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    btn = [[InlineKeyboardButton("🚀 স্টার্ট অ্যাগ্রেসিভ স্ক্র্যাপিং", callback_data='s')]]
    await u.message.reply_text("বট এখন আরও শক্তিশালী! যেকোনো কিওয়ার্ড লিখলে আমি ২০০% বেশি অ্যাপ চেক করব।", reply_markup=InlineKeyboardMarkup(btn))

async def export(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    if not data:
        await u.message.reply_text("ডাটাবেজ খালি!")
        return
    await send_file(c, u.effective_user.id, data.values(), "Full_Database")

async def stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    count = len(data) if data else 0
    await u.message.reply_text(f"📊 মোট সংরক্ষিত লিড: {count}")

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
        await q.edit_message_text("নিস/কিওয়ার্ড দিন (যেমন: VPN বা Game):")

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    if c.user_data.get('state') == 'kw':
        c.user_data['state'] = None
        asyncio.create_task(scrape_task(u.message.text, c, u.effective_user.id))
        await u.message.reply_text(f"🔎 '{u.message.text}' নিয়ে গভীর গ্লোবাল সার্চ শুরু হয়েছে। ফাইল তৈরি হলে আমি পাঠিয়ে দেব।")

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
