import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
from google_play_scraper import Sort, reviews, app
import schedule
import time
import threading

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment Variables
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID'))
CHAT_ID = int(os.environ.get('CHAT_ID'))

# Global variables
current_keyword = None  # Default no keyword
searched_apps = set()  # To avoid duplicate emails

async def start(update: Update, context: CallbackContext):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("You are not authorized.")
        return
    await update.message.reply_text("Bot started! Use /search <keyword> to set search term.")

async def search(update: Update, context: CallbackContext):
    global current_keyword
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("You are not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Please provide a keyword, e.g., /search games")
        return
    current_keyword = ' '.join(context.args)
    await update.message.reply_text(f"Keyword set to: {current_keyword}. Searching now...")
    await perform_search(context.bot)

async def perform_search(bot):
    if not current_keyword:
        return
    try:
        # Search Play Store for apps matching keyword, sorted by new
        result = reviews(
            'com.android.vending',  # Dummy, but we use search function separately
            lang='en', country='us', sort=Sort.NEWEST, count=50
        )
        # Actually, google-play-scraper has no direct search, so we simulate with app details loop.
        # For real search, we need to use a list of app IDs or external search, but to keep free, we'll assume keyword in title/description.
        # Better: Use search function if available, but library has app() for details.
        # To search: We can hardcode or use a list, but for simplicity, assume user provides keyword, and we fetch top new apps and filter.
        
        # Improved: Fetch top new apps and filter by keyword and rating <4
        # Note: google-play-scraper doesn't have direct 'search', so we'll fetch collections or use external.
        # For free, let's use a workaround: Fetch similar apps or predefined.
        # Actual implementation: Use the 'search' function from library if exists, but it doesn't. So use web scrape or limit.
        # Library has 'search' function! Yes, from google_play_scraper import search
        
        from google_play_scraper import search
        search_results = search(current_keyword, lang="en", country="us", n_hits=20)
        
        emails = []
        for res in search_results:
            app_id = res['appId']
            if app_id in searched_apps:
                continue
            try:
                app_details = app(app_id, lang='en', country='us')
                rating = app_details['score'] or 5.0
                if rating >= 4.0:
                    continue  # Skip 4-star and above
                email = app_details.get('email')
                if email:
                    emails.append(f"App: {app_details['title']}\nEmail: {email}\nURL: {app_details['url']}")
                    searched_apps.add(app_id)
            except Exception as e:
                logger.error(f"Error fetching {app_id}: {e}")
        
        if emails:
            message = "New support emails found:\n\n" + "\n\n".join(emails)
            await bot.send_message(chat_id=CHAT_ID, text=message)
        else:
            await bot.send_message(chat_id=CHAT_ID, text="No new emails found for keyword.")
    except Exception as e:
        logger.error(f"Search error: {e}")
        await bot.send_message(chat_id=CHAT_ID, text="Error during search.")

def run_scheduler(bot):
    schedule.every(1).hours.do(lambda: perform_search(bot))
    while True:
        schedule.run_pending()
        time.sleep(1)

def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("search", search))
    
    # Run scheduler in background
    scheduler_thread = threading.Thread(target=run_scheduler, args=(application.bot,))
    scheduler_thread.start()
    
    application.run_polling()

if __name__ == '__main__':
    main()
