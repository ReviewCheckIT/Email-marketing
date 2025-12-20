# -*- coding: utf-8 -*-
import logging
import os
import sys
import json
import asyncio
import csv
import io
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

# Scraper & AI
from google_play_scraper import search as play_search, app as app_details
from google.genai import Client

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore

# --- Environment Variables ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TARGET_CHAT_ID = os.environ.get('TARGET_CHAT_ID')
BOT_OWNER_ID = os.environ.get('BOT_OWNER_ID')
FIREBASE_CREDENTIALS_JSON = os.environ.get('FIREBASE_CREDENTIALS_JSON')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL')
PORT = int(os.environ.get('PORT', '8080'))

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
COLLECTION_EMAILS = 'scraped_app_emails'
COLLECTION_ADMINS = 'admins'

# --- Firebase Initialization ---
db = None
def init_firebase():
    global db
    try:
        if not firebase_admin._apps:
            cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("🔥 Firebase Connected!")
    except Exception as e:
        logger.error(f"Firebase Init Error: {e}")
        sys.exit(1)

init_firebase()

# --- AI Logic ---
async def get_ai_keywords(base_keyword: str):
    if not GEMINI_API_KEY: return [base_keyword]
    try:
        client = Client(api_key=GEMINI_API_KEY)
        prompt = (f"Provide 5-7 unique Play Store search terms for apps related to '{base_keyword}'. "
                  "Target niche, new, or low-download apps. Output as comma-separated only.")
        response = client.models.generate_content(model='gemini-2.0-flash-exp', contents=prompt)
        keywords = [k.strip() for k in response.text.split(',') if k.strip()]
        return list(set([base_keyword] + keywords))
    except:
        return [base_keyword]

# --- Database Helpers ---
async def is_admin(user_id):
    if str(user_id) == str(BOT_OWNER_ID): return True
    doc = db.collection(COLLECTION_ADMINS).document(str(user_id)).get()
    return doc.exists

# --- Core Scraper Engine ---
async def fetch_app_details(app_id, semaphore):
    async with semaphore:
        try:
            # Adding a small delay to prevent rate limit
            await asyncio.sleep(1.5)
            details = app_details(app_id, lang='en', country='us')
            return details
        except Exception:
            return None

async def run_smart_search(base_keyword, context, user_id):
    semaphore = asyncio.Semaphore(3) # Limit concurrent requests
    keywords = await get_ai_keywords(base_keyword)
    await context.bot.send_message(user_id, f"🔎 Keywords: {', '.join(keywords)}")
    
    status_msg = await context.bot.send_message(user_id, "⏳ Scraping started... This may take a minute.")
    new_leads = []

    for kw in keywords:
        try:
            search_results = play_search(kw, n_hits=40)
            tasks = [fetch_app_details(res['appId'], semaphore) for res in search_results]
            detailed_apps = await asyncio.gather(*tasks)

            for app in detailed_apps:
                if not app: continue
                
                email = app.get('developerEmail')
                score = app.get('score', 0)
                installs_str = app.get('installs', '0+')
                
                # Filter: Rating < 3.8 or New apps
                if email and (score is None or score < 3.8):
                    email_clean = email.strip().lower()
                    doc_ref = db.collection(COLLECTION_EMAILS).document(email_clean)
                    
                    if not doc_ref.get().exists:
                        data = {
                            'app_name': app.get('title'),
                            'email': email_clean,
                            'rating': score,
                            'installs': installs_str,
                            'dev_name': app.get('developer'),
                            'scraped_at': firestore.SERVER_TIMESTAMP
                        }
                        doc_ref.set(data)
                        new_leads.append(data)
                        
        except Exception as e:
            logger.error(f"Search Error: {e}")

    # Final Report
    if new_leads:
        msg = f"✅ **Scrape Complete!**\nNew Leads: {len(new_leads)}\n\n"
        for lead in new_leads[:8]:
            msg += f"📦 {lead['app_name']}\n📧 `{lead['email']}`\n⭐ {lead['rating']}\n\n"
        await context.bot.send_message(user_id, msg, parse_mode=ParseMode.MARKDOWN)
        if TARGET_CHAT_ID:
            await context.bot.send_message(TARGET_CHAT_ID, msg, parse_mode=ParseMode.MARKDOWN)
    else:
        await context.bot.send_message(user_id, "❌ No new unique leads found.")

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id): return
    btns = [[InlineKeyboardButton("🚀 AI Search", callback_data='search')],
            [InlineKeyboardButton("📊 Export CSV", callback_data='export')]]
    await update.message.reply_text("🔥 **Play Store Lead Bot**", 
                                   reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.MARKDOWN)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'search':
        context.user_data['step'] = 'ask_kw'
        await query.edit_message_text("⌨️ Enter your niche/keyword (e.g., 'Fitness Tracker'):")
    elif query.data == 'export':
        await export_data(update, context)

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('step') == 'ask_kw':
        context.user_data['step'] = None
        kw = update.message.text
        asyncio.create_task(run_smart_search(kw, context, update.effective_user.id))

async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    docs = db.collection(COLLECTION_EMAILS).order_by('scraped_at', direction=firestore.Query.DESCENDING).limit(5000).get()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Email', 'App Name', 'Rating', 'Installs', 'Developer'])
    for d in docs:
        val = d.to_dict()
        writer.writerow([val.get('email'), val.get('app_name'), val.get('rating'), val.get('installs'), val.get('dev_name')])
    
    output.seek(0)
    await context.bot.send_document(update.effective_user.id, document=io.BytesIO(output.getvalue().encode()), filename="leads.csv")

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    if RENDER_URL:
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TELEGRAM_BOT_TOKEN, 
                        webhook_url=f"{RENDER_URL}/{TELEGRAM_BOT_TOKEN}")
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
