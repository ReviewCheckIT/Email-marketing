import os
import logging
import schedule
import time
import threading
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from google_play_scraper import search, app, Sort

# Environment Variables থেকে লোড করো (Render-এ সেট করবো)
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID'))  # তোমার user ID
CHAT_ID = os.getenv('CHAT_ID')  # প্রাইভেট গ্রুপ ID (string হিসেবে রাখো, negative সহ)
KEYWORDS = []  # অ্যাডমিন কমান্ড দিয়ে এড করবে
CHECKED_APPS = set()  # ডুপ্লিকেট এড়ানোর জন্য (memory-তে, restart-এ হারাবে, কিন্তু সিম্পল)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# সাপোর্ট ইমেইল + অন্যান্য ডিটেইলস বের করা
def get_support_email(app_id):
    try:
        details = app(app_id, lang='en', country='us')
        email = details.get('developerEmail', 'No email found')
        title = details['title']
        url = details['url']
        return f"App: {title}\nSupport Email: {email}\nLink: {url}"
    except Exception as e:
        return f"Error fetching {app_id}: {str(e)}"

# নতুন অ্যাপস চেক করা
def check_new_apps():
    global KEYWORDS
    if not KEYWORDS:
        return

    found_emails = []
    for keyword in KEYWORDS:
        try:
            results = search(
                keyword,
                lang="en",
                country="us",
                n_hits=20  # প্রথম 20টা
            )
            for result in results:
                app_id = result['appId']
                if app_id not in CHECKED_APPS:
                    CHECKED_APPS.add(app_id)
                    info = get_support_email(app_id)
                    found_emails.append(f"Keyword: {keyword}\n{info}\n{'-'*30}")
        except Exception as e:
            logger.error(f"Search error for {keyword}: {e}")

    # ফিচার্ড/টপ ফ্রি অ্যাপস থেকেও চেক (অতিরিক্ত)
    try:
        top_free = search("", collection="topselling_free", n_hits=20)  # টপ ফ্রি
        for result in top_free:
            app_id = result['appId']
            if app_id not in CHECKED_APPS:
                CHECKED_APPS.add(app_id)
                info = get_support_email(app_id)
                found_emails.append(f"Top Free App\n{info}\n{'-'*30}")
    except Exception as e:
        logger.error(f"Top free error: {e}")

    if found_emails:
        message = "\n\n".join(found_emails)
        # গ্রুপে পাঠাও
        bot = Application.builder().token(BOT_TOKEN).build().bot
        bot.send_message(chat_id=CHAT_ID, text=message)

# পিরিয়ডিক চেক (প্রতি ৩০ মিনিটে)
def schedule_checker():
    schedule.every(30).minutes.do(check_new_apps)
    while True:
        schedule.run_pending()
        time.sleep(1)

# কমান্ডস
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("You are not authorized!")
        return
    await update.message.reply_text("Bot started! Use /addkeyword <keyword> to add search keywords.")

async def add_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized!")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addkeyword <keyword>")
        return
    keyword = " ".join(context.args)
    if keyword not in KEYWORDS:
        KEYWORDS.append(keyword)
        await update.message.reply_text(f"Added keyword: {keyword}")
    else:
        await update.message.reply_text("Already added!")

async def list_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if KEYWORDS:
        await update.message.reply_text("Keywords: " + ", ".join(KEYWORDS))
    else:
        await update.message.reply_text("No keywords yet.")

async def manual_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("Checking now...")
    check_new_apps()
    await update.message.reply_text("Check complete!")

# মেইন ফাংশন
async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addkeyword", add_keyword))
    application.add_handler(CommandHandler("keywords", list_keywords))
    application.add_handler(CommandHandler("checknow", manual_check))

    # কমান্ড লিস্ট সেট করো
    await application.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("addkeyword", "Add a keyword to search"),
        BotCommand("keywords", "List current keywords"),
        BotCommand("checknow", "Manual check for new apps")
    ])

    # Background-এ scheduler চালাও
    threading.Thread(target=schedule_checker, daemon=True).start()

    # Polling শুরু করো
    await application.start()
    await application.updater.start_polling()
    await asyncio.sleep(float('inf'))  # চিরকাল চালু রাখো

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
