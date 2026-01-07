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

# সেমাফোর (একসাথে কয়টি অ্যাপ ডিটেইলস চেক করবে - স্পিড বাড়ানোর জন্য)
MAX_CONCURRENT_REQUESTS = 10 
semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

def is_owner(uid):
    return str(uid) == str(OWNER_ID)

# --- AI Deep Keyword Expansion (বড় পরিসরে কিওয়ার্ড জেনারেশন) ---
async def get_expanded_keywords(base_kw):
    if not GEMINI_KEY: return [base_kw]
    try:
        client = Client(api_key=GEMINI_KEY)
        prompt = (f"Generate 50 diverse search terms for Google Play Store to find brand new, 'unrated' apps related to '{base_kw}'. "
                  f"Include variations with 'new', 'beta', 'early access', 'tracker' and niche long-tail versions. "
                  f"Provide only comma-separated values.")
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        kws = [k.strip() for k in response.text.split(',') if k.strip()]
        return list(set([base_kw] + kws))
    except:
        return [base_kw]

# --- সিঙ্গেল অ্যাপ প্রসেসিং (এটি স্পিড বাড়াবে) ---
async def process_single_app(app_id, lang_country, ref, processed_apps):
    if app_id in processed_apps: return None
    processed_apps.add(app_id)
    
    async with semaphore:
        try:
            # সিঙ্ক্রোনাস কলকে ব্লকিং এড়াতে একটু রান করা
            app = app_details(app_id, lang='en', country=lang_country)
            if app and app.get('developerEmail'):
                email_raw = app['developerEmail'].lower().strip()
                score = app.get('score', 0)
                reviews = app.get('reviews', 0)

                # ফিল্টার: রেটিং এবং রিভিউ জিরো হতে হবে
                if (score == 0 or score is None) and (reviews == 0 or reviews is None):
                    email_key = email_raw.replace('.', '_').replace('@', '_at_')
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

# --- Global Scraper Engine ---
async def scrape_task(base_kw, context, uid):
    keywords = await get_expanded_keywords(base_kw)
    # মার্কেট আরও বাড়ানো হয়েছে
    countries = ['us', 'gb', 'in', 'ca', 'br', 'au', 'de', 'fr', 'es', 'it', 'nl', 'mx', 'ru', 'za']
    
    await context.bot.send_message(uid, f"🚀 গ্লোবাল সুপার-সার্চ শুরু হয়েছে!\n🔍 মূল নিস: {base_kw}\n🌍 টার্গেট: {len(countries)}টি দেশ ও {len(keywords)}টি কিওয়ার্ড।\n⏳ দয়া করে অপেক্ষা করুন...")
    
    new_count = 0
    ref = db.reference('scraped_emails')
    processed_apps = set()
    all_new_leads = []

    for kw in keywords:
        for lang_country in countries:
            try:
                # n_hits=100 করে দেওয়া হয়েছে যাতে আরও গভীর সার্চ হয়
                results = play_search(kw, n_hits=100, lang='en', country=lang_country)
                tasks = []
                for r in results:
                    tasks.append(process_single_app(r['appId'], lang_country, ref, processed_apps))
                
                # একসাথে অনেকগুলো অ্যাপ চেক করা হবে
                batch_results = await asyncio.gather(*tasks)
                
                for data in batch_results:
                    if data:
                        ref.child(data['email'].replace('.', '_').replace('@', '_at_')).set(data)
                        all_new_leads.append(data)
                        new_count += 1
                
                await asyncio.sleep(0.2) # গুগল ব্যান প্রতিরোধে সামান্য বিরতি
            except Exception as e:
                logger.error(f"Search Error: {e}")
                continue

    # কাজ শেষ হলে সরাসরি ফাইল পাঠানো (আপনার রিকোয়েস্ট অনুযায়ী)
    if all_new_leads:
        await send_file(context, uid, all_new_leads, "New_Scraped_Leads")
    
    await context.bot.send_message(uid, f"✅ মিশন সাকসেসফুল!\n🔥 এই সেশনে মোট {new_count}টি ফ্রেশ লিড পাওয়া গেছে।\nপুরো ডাটাবেজ ডাউনলোড করতে /export লিখুন।")

async def send_file(context, uid, data_list, filename_prefix):
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['App Name', 'Email', 'Rating', 'Reviews', 'Installs', 'Country', 'Developer', 'Date'])
    for v in data_list:
        cw.writerow([v.get('app_name'), v.get('email'), 0, 0, v.get('installs'), v.get('country'), v.get('dev'), v.get('timestamp')])
    
    output = io.BytesIO(si.getvalue().encode())
    output.name = f"{filename_prefix}_{datetime.now().strftime('%H%M')}.csv"
    await context.bot.send_document(chat_id=uid, document=output, caption="📧 এইমাত্র পাওয়া নতুন লিডগুলোর লিস্ট।")

# --- Handlers ---
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    btn = [[InlineKeyboardButton("🌍 স্টার্ট গ্লোবাল স্ক্র্যাপিং", callback_data='s')]]
    await u.message.reply_text("🤖 বটের ক্ষমতা বাড়ানো হয়েছে!\nএখন থেকে গ্লোবাল কান্ট্রি এবং প্যারালাল সার্চ কাজ করবে।", reply_markup=InlineKeyboardMarkup(btn))

async def stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    count = len(data) if data else 0
    await u.message.reply_text(f"📊 ডাটাবেজে মোট জমানো লিড সংখ্যা: {count}")

async def export(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    if not data:
        await u.message.reply_text("কোনো ডেটা নেই!")
        return
    await send_file(c, u.effective_user.id, data.values(), "Full_Database_Export")

async def clear_db(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    db.reference('scraped_emails').delete()
    await u.message.reply_text("🗑️ ডাটাবেজ একদম পরিষ্কার করা হয়েছে।")

async def cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    if not is_owner(q.from_user.id): return
    await q.answer()
    if q.data == 's':
        c.user_data['state'] = 'kw'
        await q.edit_message_text("কোন নিশের ইমেল চান? (যেমন: Photo Editor)\nআমি অটোমেটিক এটার ৫০টি ভেরিয়েশন তৈরি করে খুঁজব।")

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    if c.user_data.get('state') == 'kw':
        c.user_data['state'] = None
        # ব্যাকগ্রাউন্ড টাস্ক হিসেবে রান করা
        asyncio.create_task(scrape_task(u.message.text, c, u.effective_user.id))
        await u.message.reply_text(f"🔎 '{u.message.text}' নিয়ে গভীর অনুসন্ধান শুরু হয়েছে। এটি শেষ হতে কয়েক মিনিট সময় লাগতে পারে।")

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
