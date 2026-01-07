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

# --- AI Deep Keyword Expansion (30 Keywords) ---
async def get_expanded_keywords(base_kw):
    if not GEMINI_KEY: return [base_kw]
    try:
        client = Client(api_key=GEMINI_KEY)
        prompt = f"Generate 30 unique, specific search phrases for Google Play Store to find brand new, unrated apps related to '{base_kw}'. Focus on long-tail and niche terms. Provide only comma-separated values."
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        kws = [k.strip() for k in response.text.split(',') if k.strip()]
        return list(set([base_kw] + kws))[:30] # সর্বোচ্চ ৩০টি কিওয়ার্ড
    except:
        return [base_kw]

# --- Global Scraper Engine ---
async def scrape_task(base_kw, context, uid):
    keywords = await get_expanded_keywords(base_kw)
    countries = ['us', 'gb', 'in', 'ca', 'br', 'au', 'de'] # ইন্টারন্যাশনাল মার্কেট
    await context.bot.send_message(uid, f"🌍 ইন্টারন্যাশনাল সার্চ শুরু! \n🔍 মূল বিষয়: {base_kw}\n🎯 মোট ৩০টি কিওয়ার্ড জেনারেট করা হয়েছে।")
    
    new_count = 0
    ref = db.reference('scraped_emails')

    for kw in keywords:
        for lang_country in countries: # প্রতিটি কি-ওয়ার্ড আলাদা আলাদা দেশে সার্চ হবে
            try:
                # n_hits বাড়ানো হয়েছে যাতে রেজাল্ট বেশি আসে
                results = play_search(kw, n_hits=50, lang='en', country=lang_country) 
                for r in results:
                    try:
                        app = app_details(r['appId'], lang='en', country=lang_country)
                        if app and app.get('developerEmail'):
                            email_raw = app['developerEmail'].lower().strip()
                            email_key = email_raw.replace('.', '_').replace('@', '_at_')
                            
                            score = app.get('score', 0)
                            reviews = app.get('reviews', 0)

                            # টার্গেট: শুধুমাত্র জিরো রেটিং এবং জিরো রিভিউ অ্যাপ
                            if (score == 0 or score is None) and (reviews == 0 or reviews is None):
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
                                    new_count += 1
                    except: continue
                await asyncio.sleep(0.5) # রেট লিমিট এড়াতে বিরতি
            except: continue
        
        # প্রতি ৩০টি ইমেল পাওয়ার পর আপডেট
        if new_count > 0 and new_count % 30 == 0:
            logger.info(f"Found {new_count} leads so far...")

    await context.bot.send_message(uid, f"✅ কাজ শেষ!\n🔥 গ্লোবাল সার্চে মোট {new_count}টি রেটিংবিহীন অ্যাপের ইমেল পাওয়া গেছে।\n/export লিখে ফাইলটি নামিয়ে নিন।")

# --- Handlers ---
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    btn = [[InlineKeyboardButton("🌍 স্টার্ট গ্লোবাল স্ক্র্যাপিং", callback_data='s')]]
    await u.message.reply_text("বট অনলাইন! এই মোডটি বিশ্বজুড়ে রেটিংবিহীন অ্যাপ খুঁজবে।", reply_markup=InlineKeyboardMarkup(btn))

async def stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    data = db.reference('scraped_emails').get()
    count = len(data) if data else 0
    await u.message.reply_text(f"📊 বর্তমানে মোট লিড সংখ্যা: {count}")

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
    output.name = f"Global_Unrated_Leads_{datetime.now().strftime('%d_%m')}.csv"
    await u.message.reply_document(document=output, caption="✅ ইন্টারন্যাশনাল রেটিংবিহীন লিড লিস্ট।")

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
        await q.edit_message_text("কোন নিশের ইমেল চান? কিওয়ার্ড দিন (যেমন: Video Player):")

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u.effective_user.id): return
    if c.user_data.get('state') == 'kw':
        c.user_data['state'] = None
        asyncio.create_task(scrape_task(u.message.text, c, u.effective_user.id))
        await u.message.reply_text(f"🔍 '{u.message.text}' নিয়ে ৩০টি কিওয়ার্ড তৈরি করে গ্লোবাল সার্চ চলছে...")

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
