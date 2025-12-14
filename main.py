# -*- coding: utf-8 -*-
# উন্নত প্লে স্টোর স্ক্র্যাপার বট: বাটন-ভিত্তিক ইন্টারফেস, অ্যাডমিন ম্যানেজমেন্ট, এবং ফায়ারবেস ইন্টিগ্রেশন।

import logging
import os
import sys
import json
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from google_play_scraper import search as play_search
from google_play_scraper.exceptions import GooglePlayScraperException

# --- Firebase ইম্পোর্ট এবং ইনিশিয়ালাইজেশন ---
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_INITIALIZED = False
except ImportError:
    logging.error("Firebase libraries not found. Please install 'firebase-admin'.")
    sys.exit(1)

# --- কনফিগারেশন লোডিং ---
PRODUCTION = os.environ.get('RENDER') == 'true'
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')

# টার্গেট চ্যাট আইডি যেখানে ফলাফল পাঠানো হবে (গ্রুপ/চ্যানেল আইডি)
TARGET_CHAT_ID = os.environ.get('TARGET_CHAT_ID')

# প্রাথমিক মালিক আইডি (সুপার অ্যাডমিন)
BOT_OWNER_ID = os.environ.get('BOT_OWNER_ID') 

# ফায়ারবেস সার্ভিস অ্যাকাউন্ট JSON স্ট্রিং হিসাবে লোড করা হচ্ছে
FIREBASE_CREDENTIALS_JSON = os.environ.get('FIREBASE_CREDENTIALS_JSON')

# স্টার্টআপ মেসেজ
STARTUP_MESSAGE = '🤖 বস আমি প্রস্তুত! আমাকে কমান্ড দিন কাজ শুরু করি।'

# লগিং সেটআপ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Firestore কালেকশন নাম
COLLECTION_EMAILS = 'scraped_app_emails'
COLLECTION_ADMINS = 'admins'

# --- ফায়ারবেস ইনিশিয়ালাইজেশন ও ইউটিলিটি ---

def initialize_firebase():
    """এনভায়রনমেন্ট ভেরিয়েবল থেকে ক্রেডেনশিয়াল ব্যবহার করে Firebase অ্যাপ ইনিশিয়ালাইজ করা।"""
    global FIREBASE_INITIALIZED
    if FIREBASE_INITIALIZED:
        return firestore.client()
        
    if not FIREBASE_CREDENTIALS_JSON:
        logger.error("FIREBASE_CREDENTIALS_JSON এনভায়রনমেন্ট ভেরিয়েবল সেট করা নেই।")
        sys.exit(1)
        
    try:
        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
        cred = credentials.Certificate(cred_dict)
        # ফিক্স: initializeApp -> initialize_app
        firebase_admin.initialize_app(cred)
        FIREBASE_INITIALIZED = True
        logger.info("Firebase সফলভাবে ইনিশিয়ালাইজ করা হয়েছে।")
        return firestore.client()
    except Exception as e:
        logger.error(f"Firebase ইনিশিয়ালাইজেশন ত্রুটি: {e}", exc_info=True)
        sys.exit(1)

try:
    # ফায়ারবেস ক্লায়েন্ট ইনিশিয়ালাইজেশন
    db = initialize_firebase()
except Exception:
    logger.warning("Firebase ক্লায়েন্ট লোড হতে পারেনি। ডিপ্লয়মেন্টের সময় এটি স্বয়ংক্রিয়ভাবে লোড হবে।")


# --- অ্যাডমিন ম্যানেজমেন্ট লজিক (Firebase await ফিক্সড, অপরিবর্তিত) ---

async def is_admin(user_id: int) -> bool:
    """ব্যবহারকারী অ্যাডমিন কিনা তা Firestore থেকে পরীক্ষা করা। BOT_OWNER_ID-কে সুপার অ্যাডমিন হিসেবে গণ্য করা হয়।"""
    try:
        if str(user_id) == BOT_OWNER_ID:
            await add_admin(user_id, added_by='System/Owner') 
            return True
            
        doc = db.collection(COLLECTION_ADMINS).document(str(user_id)).get()
        return doc.exists
    except Exception as e:
        logger.error(f"অ্যাডমিন চেকিং ত্রুটি: {e}")
        return False

async def add_admin(user_id: int, added_by: str = None) -> bool:
    """Firestore-এ একজন অ্যাডমিন যুক্ত করা।"""
    try:
        admin_ref = db.collection(COLLECTION_ADMINS).document(str(user_id))
        admin_ref.set({'user_id': user_id, 'added_by': added_by or 'Admin', 'added_at': datetime.now().isoformat()})
        return True
    except Exception as e:
        logger.error(f"অ্যাডমিন যুক্ত করার ত্রুটি: {e}")
        return False

async def remove_admin(user_id: int) -> bool:
    """Firestore থেকে একজন অ্যাডমিন অপসারণ করা।"""
    try:
        if str(user_id) == BOT_OWNER_ID:
            return False
        db.collection(COLLECTION_ADMINS).document(str(user_id)).delete()
        return True
    except Exception as e:
        logger.error(f"অ্যাডমিন অপসারণের ত্রুটি: {e}")
        return False


# --- ইন্টারফেস বাটন এবং মেনু (অপরিবর্তিত) ---

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """প্রধান মেনুর বাটন তৈরি করা।"""
    keyboard = [
        [InlineKeyboardButton("🔍 কীওয়ার্ড সার্চ করুন", callback_data='action_start_search')],
        [InlineKeyboardButton("🗄️ সংগৃহীত ডেটা এক্সপোর্ট করুন", callback_data='action_export')],
        [InlineKeyboardButton("👤 অ্যাডমিন ম্যানেজমেন্ট", callback_data='action_admin_menu')],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_menu_keyboard() -> InlineKeyboardMarkup:
    """অ্যাডমিন মেনুর বাটন তৈরি করা।"""
    keyboard = [
        [InlineKeyboardButton("➕ অ্যাডমিন যুক্ত করুন (ID দিয়ে)", callback_data='admin_add')],
        [InlineKeyboardButton("➖ অ্যাডমিন অপসারণ করুন (ID দিয়ে)", callback_data='admin_remove')],
        [InlineKeyboardButton("📜 অ্যাডমিন তালিকা দেখুন", callback_data='admin_list')],
        [InlineKeyboardButton("⬅️ প্রধান মেনু", callback_data='main_menu')],
    ]
    return InlineKeyboardMarkup(keyboard)


# --- টেলিগ্রাম হ্যান্ডলার্স (অপরিবর্তিত) ---

async def post_init_callback(application: Application) -> None:
    """বট ইনিশিয়ালাইজেশনের পর টার্গেট চ্যাটে স্টার্টআপ মেসেজ পাঠায়।"""
    target_chat = TARGET_CHAT_ID
    if not target_chat:
        logger.warning("TARGET_CHAT_ID সেট করা নেই। স্টার্টআপ মেসেজ পাঠানো যাচ্ছে না।")
        return
        
    try:
        await application.bot.send_message(
            chat_id=target_chat,
            text=STARTUP_MESSAGE
        )
        logger.info(f"Startup message sent to target chat: {target_chat}")
    except Exception as e:
        logger.error(f"Failed to send startup message to chat {target_chat}: {e}")

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start কমান্ডের হ্যান্ডলার।"""
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("দুঃখিত, আপনি এই বটের অ্যাডমিন নন।")
        return
        
    await update.message.reply_text(
        "স্বাগতম, অ্যাডমিন! আপনি এখন প্রধান মেনু থেকে কমান্ড নির্বাচন করতে পারেন।",
        reply_markup=get_main_menu_keyboard()
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ইনলাইন বাটন ক্লিক হ্যান্ডলার।"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not await is_admin(user_id):
        await query.edit_message_text("দুঃখিত, আপনার অ্যাডমিন অনুমতি নেই।")
        return

    data = query.data
    
    if data == 'main_menu':
        await query.edit_message_text("প্রধান মেনু:", reply_markup=get_main_menu_keyboard())
    
    elif data == 'action_start_search':
        context.user_data['state'] = 'await_keyword'
        await query.edit_message_text(
            "অনুগ্রহ করে এখন আপনার **সার্চ কীওয়ার্ডটি** টাইপ করে মেসেজ হিসেবে পাঠান।\n"
            "উদাহরণ: `স্বাস্থ্য অ্যাপ`"
        )
        
    elif data == 'action_export':
        await export_data_logic(user_id, context)
        
    elif data == 'action_admin_menu':
        await query.edit_message_text("অ্যাডমিন ম্যানেজমেন্ট মেনু:", reply_markup=get_admin_menu_keyboard())

    elif data == 'admin_add':
        context.user_data['state'] = 'await_admin_id_add'
        await query.edit_message_text(
            "➕ অনুগ্রহ করে নতুন অ্যাডমিন এর **Telegram User ID** টি টাইপ করে মেসেজ হিসেবে পাঠান।\n"
            "উদাহরণ: `1234567890`"
        )

    elif data == 'admin_remove':
        context.user_data['state'] = 'await_admin_id_remove'
        await query.edit_message_text(
            "➖ অনুগ্রহ করে যে অ্যাডমিনকে অপসারণ করতে চান, তার **Telegram User ID** টি টাইপ করে মেসেজ হিসেবে পাঠান।"
        )
        
    elif data == 'admin_list':
        await list_admins_logic(context, user_id)
        await query.edit_message_text("📜 অ্যাডমিন তালিকা পাঠানো হয়েছে।", reply_markup=get_admin_menu_keyboard())
        

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """টেক্সট মেসেজ হ্যান্ডলার (কীওয়ার্ড বা আইডি ইনপুট পাওয়ার জন্য)।"""
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        return

    text = update.message.text.strip()
    state = context.user_data.get('state')
    
    context.user_data['user_id'] = user_id

    if state == 'await_keyword':
        context.user_data['keyword'] = text
        context.user_data['state'] = 'await_limit'
        await update.message.reply_text(
            f"কীওয়ার্ড সেভ হয়েছে: **{text}**।\n"
            "এখন **কতগুলো অ্যাপ পরীক্ষা করতে চান (সংখ্যা)** তা টাইপ করে পাঠান।\n"
            "উদাহরণ: `150`", 
            parse_mode=ParseMode.MARKDOWN
        )

    elif state == 'await_limit':
        try:
            limit = int(text)
            keyword = context.user_data.pop('keyword')
            context.user_data.pop('state') 
            
            await update.message.reply_text(f"🚀 সার্চ শুরু হচ্ছে: কীওয়ার্ড='{keyword}', সংখ্যা='{limit}'...")
            await search_apps_logic(keyword, limit, context)
            
            await update.message.reply_text("সার্চ সম্পন্ন হয়েছে। প্রধান মেনু:", reply_markup=get_main_menu_keyboard())

        except ValueError:
            await update.message.reply_text("অনুগ্রহ করে একটি বৈধ সংখ্যা লিখুন।")
        except Exception as e:
            logger.error(f"সার্চ চালানোর ত্রুটি: {e}")
            await update.message.reply_text("সার্চ করতে গিয়ে একটি অপ্রত্যাশিত ত্রুটি ঘটেছে।")

    elif state == 'await_admin_id_add':
        try:
            new_admin_id = int(text)
            if await add_admin(new_admin_id, added_by=str(user_id)):
                await update.message.reply_text(f"✅ সফলভাবে User ID `{new_admin_id}`-কে অ্যাডমিন হিসেবে যুক্ত করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text("❌ অ্যাডমিন যুক্ত করতে ব্যর্থ।")
        except ValueError:
            await update.message.reply_text("অনুগ্রহ করে একটি বৈধ সংখ্যা (User ID) লিখুন।")
        finally:
            context.user_data.pop('state')
            await update.message.reply_text("অ্যাডমিন মেনু:", reply_markup=get_admin_menu_keyboard())

    elif state == 'await_admin_id_remove':
        try:
            target_id = int(text)
            if str(target_id) == BOT_OWNER_ID:
                 await update.message.reply_text("❌ আপনি বটের প্রাথমিক মালিককে অপসারণ করতে পারবেন না।")
            elif await remove_admin(target_id):
                await update.message.reply_text(f"✅ সফলভাবে User ID `{target_id}`-কে অ্যাডমিন তালিকা থেকে অপসারণ করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text("❌ অ্যাডমিন অপসারণ করতে ব্যর্থ।")
        except ValueError:
            await update.message.reply_text("অনুগ্রহ করে একটি বৈধ সংখ্যা (User ID) লিখুন।")
        finally:
            context.user_data.pop('state')
            await update.message.reply_text("অ্যাডমিন মেনু:", reply_markup=get_admin_menu_keyboard())
    
    else:
        await start_handler(update, context)


# --- কোর লজিক ফাংশন (সংরক্ষণ ও সার্চ) ---

async def check_if_email_exists(email: str) -> bool:
    """ফায়ারবেসে ইমেইলটি আগে থেকে আছে কিনা তা পরীক্ষা করা।"""
    try:
        doc = db.collection(COLLECTION_EMAILS).document(email.lower()).get()
        return doc.exists
    except Exception as e:
        logger.error(f"Firebase চেকিং ত্রুটি: {e}")
        return False 

async def save_app_data(app_data: dict) -> bool:
    """ফায়ারবেসে নতুন অ্যাপের ডেটা সংরক্ষণ করা।"""
    try:
        email_key = app_data.get('email', '').lower()
        if not email_key: return False
        
        db.collection(COLLECTION_EMAILS).document(email_key).set(app_data)
        return True
    except Exception as e:
        logger.error(f"Firebase সেভিং ত্রুটি: {e}")
        return False

async def search_apps_logic(keyword: str, limit: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """অ্যাপস খোঁজা, ফিল্টারিং করা এবং টার্গেট চ্যাটে ফলাফল পাঠানো।"""
    
    target_chat = TARGET_CHAT_ID
    if not target_chat:
        logger.error("TARGET_CHAT_ID সেট করা নেই। ফলাফল পাঠানো যাবে না।")
        await context.bot.send_message(context.user_data['user_id'], "❌ TARGET_CHAT_ID সেট করা নেই। ফলাফল পাঠানো সম্ভব নয়।")
        return

    try:
        # প্লে স্টোর সার্চ: কীওয়ার্ড এবং সংখ্যার ওপর ভিত্তি করে ডেটা আনা হবে
        search_results = play_search(keyword, lang='bn', country='us', n_hits=limit)
    except GooglePlayScraperException:
        await context.bot.send_message(context.user_data['user_id'], f"❌ স্ক্র্যাপিং ত্রুটি: Play Store থেকে ডেটা আনা যায়নি। কীওয়ার্ড: **{keyword}**", parse_mode=ParseMode.MARKDOWN)
        return

    newly_scraped_apps = []
    
    # --- নতুন অ্যাপের একমাত্র কঠোর শর্ত: খুবই সম্প্রতি আপডেট (৯০ দিন) ---
    max_days_old_for_new = 90 # ৯০ দিন (৩ মাসের মধ্যে আপডেট)

    for app in search_results:
        # ১. মৌলিক শর্ত: ডেভেলপার ইমেইল ঠিকানা অবশ্যই থাকতে হবে।
        # দ্রষ্টব্য: এই ইমেইলটিই প্লে স্টোরে ডেভেলপার কন্টাক্ট হিসেবে তালিকাভুক্ত হয়।
        if not app.get('developerEmail'):
            continue
        
        # ২. নতুনত্বের শর্ত: খুবই সাম্প্রতিক আপডেট (৯০ দিনের মধ্যে)
        is_recently_updated = False
        try:
            # updated তারিখটি YY-MM-DDT... ফরম্যাটে থাকে, শুধু তারিখ অংশ নেওয়া হচ্ছে
            updated_date_str = app.get('updated', '1970-01-01T00:00:00.000Z').split('T')[0]
            updated_date = datetime.strptime(updated_date_str, '%Y-%m-%d')
            # বর্তমান সময় থেকে আপডেটের সময়ের ব্যবধান ৯০ দিনের কম হতে হবে
            if datetime.now() - updated_date <= timedelta(days=max_days_old_for_new):
                is_recently_updated = True
        except Exception:
            # যদি তারিখ পড়তে না পারে, তবে সেটি বাদ যাবে (নিরাপত্তার জন্য)
            pass

        if not is_recently_updated:
            continue

        app_email = app['developerEmail'].strip()
        
        # ৩. ডুপ্লিকেট চেক: ইমেইলটি আগে থেকে ডেটাবেসে থাকলে বাদ যাবে।
        if await check_if_email_exists(app_email):
            continue

        # ৪. ডেটা সংরক্ষণ
        data_to_save = {
            'name': app['title'],
            'email': app_email,
            'score': app.get('score', 0.0), 
            'installs': app.get('installs', 'N/A'),
            'updated': app.get('updated', 'N/A'),
            'keyword': keyword,
            'scraped_at': datetime.now().isoformat()
        }
        
        if await save_app_data(data_to_save):
            newly_scraped_apps.append(data_to_save)

    # টার্গেট চ্যাটে ফলাফল পাঠানো
    if newly_scraped_apps:
        message_parts = [
            f'✨ **নতুন ফলাফল: {keyword}** (🔍 পরীক্ষা করা হয়েছে: {limit}টি অ্যাপ)',
            f'✅ **{len(newly_scraped_apps)}টি** নতুন (অনন্য ডেভেলপার ইমেইল সহ) অ্যাপের ইমেইল সংগৃহীত ও ডেটাবেসে সংরক্ষণ করা হয়েছে।',
            '---'
        ]
        
        for app in newly_scraped_apps:
            message_parts.append(
                f'🔗 নাম: **{app["name"]}**\n'
                f'⭐ রেটিং: {app["score"]:.2f} | ⬇️ ইনস্টল: {app["installs"]}\n'
                f'📧 ডেভেলপার ইমেইল: `{app["email"]}`\n'
                '---'
            )
        
        final_message = "\n".join(message_parts)
        
        try:
            # নিশ্চিত করা হলো: ফলাফল টার্গেট চ্যাটে (গ্রুপ/চ্যানেল) যাচ্ছে।
            await context.bot.send_message(chat_id=target_chat, text=final_message, parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(context.user_data['user_id'], f'✅ ফলাফল সফলভাবে টার্গেট চ্যাটে পাঠানো হয়েছে।')
        except Exception as e:
            logger.error(f"টার্গেট চ্যাটে মেসেজ পাঠানোর ত্রুটি: {e}")
            await context.bot.send_message(context.user_data['user_id'], f"❌ ফলাফল প্রাইভেট গ্রুপে পাঠানো যায়নি। অনুগ্রহ করে `TARGET_CHAT_ID` এবং বটের পারমিশন যাচাই করুন। ত্রুটি: {e}")

    else:
        await context.bot.send_message(
            context.user_data['user_id'], # ব্যক্তিগত চ্যাটে ফলাফল পাঠানো
            f'❌ **{keyword}** কীওয়ার্ডের জন্য শর্তাবলী পূরণ করে এমন কোনো নতুন (অনন্য ডেভেলপার ইমেইল সহ) অ্যাপ খুঁজে পাওয়া যায়নি।\n(একমাত্র শর্তাবলী: ইমেইল থাকতে হবে এবং গত ৯০ দিনের মধ্যে আপডেট হতে হবে)।'
        )

async def export_data_logic(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ফায়ারবেস থেকে সংগৃহীত সকল ডেটা এক্সপোর্ট করে CSV ফাইল আকারে অ্যাডমিনের কাছে পাঠানো।"""
    
    await context.bot.send_message(user_id, "🗄️ ডেটাবেস থেকে সংগৃহীত সকল ইমেইল এক্সপোর্ট করা হচ্ছে...")
    
    try:
        docs = db.collection(COLLECTION_EMAILS).stream()
        
        all_data = [doc.to_dict() for doc in docs]

        if all_data:
            total_count = len(all_data)
            csv_data = "Email Address,App Name,Rating,Installs,Keyword,Scraped Date\n"
            
            for data in all_data:
                row = (
                    f'"{data.get("email", "")}", '
                    f'"{data.get("name", "")}", '
                    f'{data.get("score", 0):.2f}, '
                    f'"{data.get("installs", "")}", '
                    f'"{data.get("keyword", "")}", '
                    f'"{data.get("scraped_at", "")[:10]}"\n'
                )
                csv_data += row
            
            await context.bot.send_document(
                chat_id=user_id,
                document=csv_data.encode('utf-8'),
                filename=f"scraped_app_emails_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                caption=f'✅ মোট **{total_count}টি** অনন্য ইমেইল CSV ফরম্যাটে এক্সপোর্ট করা হয়েছে।'
            )
        else:
            await context.bot.send_message(user_id, "❌ ডেটাবেসে কোনো ডেটা সংরক্ষণ করা নেই।")

    except Exception as e:
        logger.error(f"এক্সপোর্ট ত্রুটি: {e}")
        await context.bot.send_message(user_id, f"দুঃখিত, ডেটা এক্সপোর্ট করতে একটি গুরুতর ত্রুটি হয়েছে। ত্রুটি: {e}")


async def list_admins_logic(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """সকল অ্যাডমিনের তালিকা দেখানো।"""
    try:
        docs = db.collection(COLLECTION_ADMINS).stream()
        
        admin_list = [doc.id for doc in docs]
            
        if admin_list:
            message = "📜 **বর্তমান অ্যাডমিন তালিকা:**\n"
            for admin_id in admin_list:
                tag = "(মালিক)" if admin_id == BOT_OWNER_ID else ""
                message += f"- `{admin_id}` {tag}\n"
        else:
            message = "❌ কোনো অ্যাডমিন খুঁজে পাওয়া যায়নি।"
            
        await context.bot.send_message(user_id, message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"অ্যাডমিন তালিকা প্রদর্শনের ত্রুটি: {e}")
        await context.bot.send_message(user_id, "অ্যাডমিন তালিকা দেখাতে সমস্যা হয়েছে।")

# --- মূল রান ফাংশন ---

def main() -> None:
    """বট শুরু করার মূল ফাংশন।"""
    if not all([TELEGRAM_BOT_TOKEN, WEBHOOK_URL, BOT_OWNER_ID, TARGET_CHAT_ID, FIREBASE_CREDENTIALS_JSON]):
        logger.error("গুরুত্বপূর্ণ এনভায়রনমেন্ট ভেরিয়েবল সেট করা নেই। অনুগ্রহ করে README_SETUP.md দেখুন।")
        sys.exit(1)
        
    initialize_firebase() 

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init_callback).build()

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    if PRODUCTION and WEBHOOK_URL:
        port = int(os.environ.get('PORT', '8080'))
        
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}",
        )
        logger.info(f"Webhook মোডে শুরু হয়েছে: {WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN} পোর্ট {port}-এ শুনছে।")
    else:
        logger.info("Polling মোডে শুরু হয়েছে।")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
