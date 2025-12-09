#!/usr/bin/env python3
"""
Gmail Marketplace Telegram Bot
Run on Termux: python3 bot.py
"""

import os
import json
import sqlite3
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes
)
from telegram.constants import ParseMode

# ========== CONFIGURATION ==========
# REPLACE THESE WITH YOUR ACTUAL VALUES
BOT_TOKEN = "8563619575:AAFzu7z1niQ7Ot24mEtZFZiC8M6Tub0wh2c"
BOT_USERNAME = "@gmail_v2_bot"  # Without @
CHANNEL_USERNAME = "@YOUR_CHANNEL"  # With @
ADMIN_ID = 8493728889  # Your Telegram ID
ADMIN_USERNAME = "@dravenlocke"

# Database file
DB_FILE = "marketplace.db"
# ===================================

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
(
    AWAITING_EMAIL,
    AWAITING_EWALLET_TYPE,
    AWAITING_EWALLET_NUMBER,
    AWAITING_WITHDRAW_AMOUNT,
    AWAITING_ADMIN_ACTION,
    AWAITING_PAYMENT_PROOF
) = range(6)

class Database:
    def __init__(self, db_file=DB_FILE):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        
        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                balance REAL DEFAULT 0,
                points INTEGER DEFAULT 0,
                total_earned REAL DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                ewallet_type TEXT,
                ewallet_number TEXT,
                is_joined_channel BOOLEAN DEFAULT FALSE,
                is_banned BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # Gmail submissions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                email TEXT,
                password TEXT,
                status TEXT DEFAULT 'pending', -- pending, valid, invalid
                submission_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                earnings REAL DEFAULT 0,
                reviewed_by INTEGER,
                review_date TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Withdrawals table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                status TEXT DEFAULT 'pending', -- pending, approved, rejected
                request_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_date TIMESTAMP,
                processed_by INTEGER,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Referrals table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER UNIQUE,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                earnings_generated REAL DEFAULT 0,
                FOREIGN KEY (referrer_id) REFERENCES users (user_id),
                FOREIGN KEY (referred_id) REFERENCES users (user_id)
            )
        ''')
        
        self.conn.commit()
    
    def get_user(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        columns = [column[0] for column in cursor.description]
        row = cursor.fetchone()
        return dict(zip(columns, row)) if row else None
    
    def create_user(self, user_id, username, referral_code=None):
        cursor = self.conn.cursor()
        
        # Check if user exists
        if self.get_user(user_id):
            return False
        
        # Generate unique referral code
        import random
        import string
        if not referral_code:
            referral_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        
        cursor.execute('''
            INSERT INTO users (user_id, username, referral_code)
            VALUES (?, ?, ?)
        ''', (user_id, username, referral_code))
        self.conn.commit()
        return True
    
    def update_balance(self, user_id, amount):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE users 
            SET balance = balance + ?, 
                total_earned = total_earned + ?
            WHERE user_id = ?
        ''', (amount, max(0, amount), user_id))
        self.conn.commit()
    
    def add_submission(self, user_id, email, password):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO submissions (user_id, email, password)
            VALUES (?, ?, ?)
        ''', (user_id, email, password))
        self.conn.commit()
        return cursor.lastrowid
    
    def update_submission_status(self, submission_id, status, earnings, reviewed_by):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE submissions 
            SET status = ?, 
                earnings = ?,
                reviewed_by = ?,
                review_date = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (status, earnings, reviewed_by, submission_id))
        
        # Update user balance if valid
        if status == 'valid' and earnings > 0:
            submission = cursor.execute(
                'SELECT user_id FROM submissions WHERE id = ?', 
                (submission_id,)
            ).fetchone()
            if submission:
                self.update_balance(submission[0], earnings)
        
        self.conn.commit()
    
    def create_withdrawal(self, user_id, amount):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO withdrawals (user_id, amount)
            VALUES (?, ?)
        ''', (user_id, amount))
        self.conn.commit()
        return cursor.lastrowid
    
    def update_withdrawal_status(self, withdrawal_id, status, processed_by):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE withdrawals 
            SET status = ?,
                processed_by = ?,
                processed_date = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (status, processed_by, withdrawal_id))
        
        # Deduct balance if approved
        if status == 'approved':
            withdrawal = cursor.execute(
                'SELECT user_id, amount FROM withdrawals WHERE id = ?', 
                (withdrawal_id,)
            ).fetchone()
            if withdrawal:
                cursor.execute(
                    'UPDATE users SET balance = balance - ? WHERE user_id = ?',
                    (withdrawal[1], withdrawal[0])
                )
        
        self.conn.commit()
    
    def add_referral(self, referrer_id, referred_id):
        cursor = self.conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO referrals (referrer_id, referred_id)
                VALUES (?, ?)
            ''', (referrer_id, referred_id))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
    
    def update_ewallet(self, user_id, ewallet_type, ewallet_number):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE users 
            SET ewallet_type = ?, ewallet_number = ?
            WHERE user_id = ?
        ''', (ewallet_type, ewallet_number, user_id))
        self.conn.commit()
    
    def set_channel_joined(self, user_id, joined=True):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE users 
            SET is_joined_channel = ?
            WHERE user_id = ?
        ''', (joined, user_id))
        self.conn.commit()

db = Database()

async def check_channel_membership(user_id, context: ContextTypes.DEFAULT_TYPE):
    """Check if user is member of required channel"""
    try:
        member = await context.bot.get_chat_member(
            chat_id=CHANNEL_USERNAME,
            user_id=user_id
        )
        is_member = member.status in ['member', 'administrator', 'creator']
        db.set_channel_joined(user_id, is_member)
        return is_member
    except Exception as e:
        logger.error(f"Error checking channel membership: {e}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    user = update.effective_user
    user_id = user.id
    username = user.username or f"user_{user_id}"
    
    # Check channel membership
    if not await check_channel_membership(user_id, context):
        keyboard = [
            [InlineKeyboardButton("✅ Join Channel", url=f"https://t.me/{CHANNEL_USERNAME.replace('@', '')}")],
            [InlineKeyboardButton("🔁 Check Membership", callback_data="check_membership")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "⚠️ *Please join our channel first to use this bot!*\n\n"
            "Join our channel and then click 'Check Membership' button.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        return
    
    # Create user if not exists
    referral_code = context.args[0] if context.args else None
    referred_by = None
    
    if referral_code:
        # Find referrer by code
        cursor = db.conn.cursor()
        referrer = cursor.execute(
            'SELECT user_id FROM users WHERE referral_code = ?', 
            (referral_code,)
        ).fetchone()
        if referrer:
            referred_by = referrer[0]
    
    db.create_user(user_id, username)
    
    if referred_by:
        db.add_referral(referred_by, user_id)
        # Reward referrer (0.50 pesos per referral)
        db.update_balance(referred_by, 0.50)
    
    # Get user info
    user_data = db.get_user(user_id)
    
    # Welcome message
    welcome_text = f"""
👋 *Welcome to Gmail Marketplace!*

📊 *Your Stats:*
├ Balance: ₱{user_data['balance']:.2f}
├ Points: {user_data['points']}
├ Total Earned: ₱{user_data['total_earned']:.2f}
└ Referral Code: `{user_data['referral_code']}`

💡 *How to Earn:*
1. Submit valid Gmail accounts (format: email:password)
2. Earn from referrals (₱0.50 per valid referral)
3. Reach ₱100 to unlock referral earnings

📋 *Available Commands:*
/submit - Submit Gmail account
/withdraw - Withdraw earnings
/bind - Bind e-wallet account
/referral - Get referral link
/stats - Check your stats
/help - Show help

⚠️ *Minimum withdrawal:* ₱10
"""
    
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

async def submit_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle email submission"""
    user_id = update.effective_user.id
    
    # Check channel membership
    if not await check_channel_membership(user_id, context):
        await update.message.reply_text("Please join our channel first!")
        return
    
    await update.message.reply_text(
        "📧 *Submit Gmail Account*\n\n"
        "Please send the Gmail account in this format:\n"
        "`email@gmail.com:password`\n\n"
        "⚠️ *One submission per message*",
        parse_mode=ParseMode.MARKDOWN
    )
    return AWAITING_EMAIL

async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive and process email submission"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    # Validate format
    if ':' not in text:
        await update.message.reply_text(
            "❌ Invalid format! Please use:\n"
            "`email@gmail.com:password`",
            parse_mode=ParseMode.MARKDOWN
        )
        return AWAITING_EMAIL
    
    email, password = text.split(':', 1)
    email = email.strip()
    password = password.strip()
    
    # Basic validation
    if '@gmail.com' not in email.lower():
        await update.message.reply_text("❌ Please submit only Gmail accounts!")
        return AWAITING_EMAIL
    
    # Save submission
    submission_id = db.add_submission(user_id, email, password)
    
    # Notify admin
    user = db.get_user(user_id)
    admin_text = f"""
🆕 *New Submission*
├ ID: `{submission_id}`
├ User: @{user['username']} ({user_id})
├ Email: `{email}`
└ Status: Pending

/review_{submission_id} - Review this submission
"""
    
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Failed to notify admin: {e}")
    
    await update.message.reply_text(
        "✅ Submission received!\n"
        "We will review your submission within 24 hours.\n"
        "Check /stats for updates."
    )
    
    return ConversationHandler.END

async def bind_ewallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bind e-wallet account"""
    keyboard = [
        [InlineKeyboardButton("GCash", callback_data="ewallet_gcash")],
        [InlineKeyboardButton("PayMaya", callback_data="ewallet_paymaya")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "💳 *Select your e-wallet provider:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    return AWAITING_EWALLET_TYPE

async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate withdrawal"""
    user_id = update.effective_user.id
    user_data = db.get_user(user_id)
    
    # Check if e-wallet is bound
    if not user_data['ewallet_type'] or not user_data['ewallet_number']:
        await update.message.reply_text(
            "⚠️ Please bind your e-wallet first using /bind"
        )
        return
    
    # Check minimum balance
    if user_data['balance'] < 10:
        await update.message.reply_text(
            f"❌ Minimum withdrawal is ₱10\n"
            f"Your balance: ₱{user_data['balance']:.2f}"
        )
        return
    
    await update.message.reply_text(
        f"💸 *Withdrawal Request*\n\n"
        f"Current balance: ₱{user_data['balance']:.2f}\n"
        f"E-wallet: {user_data['ewallet_type']} - {user_data['ewallet_number']}\n\n"
        f"Enter amount to withdraw (minimum ₱10):",
        parse_mode=ParseMode.MARKDOWN
    )
    return AWAITING_WITHDRAW_AMOUNT

async def process_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process withdrawal amount"""
    user_id = update.effective_user.id
    user_data = db.get_user(user_id)
    
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number!")
        return AWAITING_WITHDRAW_AMOUNT
    
    # Validate amount
    if amount < 10:
        await update.message.reply_text("❌ Minimum withdrawal is ₱10!")
        return AWAITING_WITHDRAW_AMOUNT
    
    if amount > user_data['balance']:
        await update.message.reply_text(
            f"❌ Insufficient balance!\n"
            f"Your balance: ₱{user_data['balance']:.2f}"
        )
        return AWAITING_WITHDRAW_AMOUNT
    
    # Create withdrawal request
    withdrawal_id = db.create_withdrawal(user_id, amount)
    
    # Notify admin
    admin_text = f"""
💸 *New Withdrawal Request*
├ ID: `{withdrawal_id}`
├ User: @{user_data['username']} ({user_id})
├ Amount: ₱{amount:.2f}
├ E-wallet: {user_data['ewallet_type']}
└ Number: {user_data['ewallet_number']}

/approve_{withdrawal_id} - Approve
/reject_{withdrawal_id} - Reject
"""
    
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Failed to notify admin: {e}")
    
    await update.message.reply_text(
        f"✅ Withdrawal request submitted!\n"
        f"Amount: ₱{amount:.2f}\n"
        f"Status: Pending admin approval\n\n"
        f"Please wait 24-48 hours for processing."
    )
    
    return ConversationHandler.END

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user statistics"""
    user_id = update.effective_user.id
    user_data = db.get_user(user_id)
    
    if not user_data:
        await update.message.reply_text("Please use /start first!")
        return
    
    # Get submission stats
    cursor = db.conn.cursor()
    cursor.execute('''
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN status = 'valid' THEN 1 ELSE 0 END) as valid,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
            SUM(earnings) as total_earned
        FROM submissions 
        WHERE user_id = ?
    ''', (user_id,))
    
    stats = cursor.fetchone()
    
    # Get referral stats
    cursor.execute('''
        SELECT COUNT(*) as referrals, SUM(earnings_generated) as ref_earnings
        FROM referrals 
        WHERE referrer_id = ?
    ''', (user_id,))
    
    ref_stats = cursor.fetchone()
    
    # Check if referral system is unlocked (₱100 earned)
    referral_unlocked = user_data['total_earned'] >= 100
    
    stats_text = f"""
📊 *Your Statistics*

💰 *Earnings:*
├ Available Balance: ₱{user_data['balance']:.2f}
├ Total Earned: ₱{user_data['total_earned']:.2f}
└ Points: {user_data['points']} (₱{user_data['points'] * 0.5:.2f})

📧 *Submissions:*
├ Total: {stats[0] or 0}
├ Valid: {stats[1] or 0}
├ Pending: {stats[2] or 0}
└ Earnings from subs: ₱{stats[3] or 0:.2f}

👥 *Referrals:*
├ Total: {ref_stats[0] or 0}
├ Referral Earnings: ₱{ref_stats[1] or 0:.2f}
└ System: {'✅ UNLOCKED' if referral_unlocked else f'🔒 LOCKED (need ₱{100 - user_data["total_earned"]:.2f} more)'}

💳 *E-wallet:*
├ Type: {user_data['ewallet_type'] or 'Not set'}
└ Number: {user_data['ewallet_number'] or 'Not set'}

🔗 *Referral Code:* `{user_data['referral_code']}`
📈 *Referral Link:* https://t.me/{BOT_USERNAME}?start={user_data['referral_code']}
"""
    
    await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)

async def referral_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show referral information"""
    user_id = update.effective_user.id
    user_data = db.get_user(user_id)
    
    if not user_data:
        await update.message.reply_text("Please use /start first!")
        return
    
    # Check if referral system is unlocked
    if user_data['total_earned'] < 100:
        await update.message.reply_text(
            f"🔒 *Referral System Locked*\n\n"
            f"You need to earn ₱100 first to unlock referrals.\n"
            f"Current earnings: ₱{user_data['total_earned']:.2f}\n"
            f"Need: ₱{100 - user_data['total_earned']:.2f} more\n\n"
            f"Submit more valid Gmail accounts to unlock!",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    referral_text = f"""
👥 *Referral Program*

💰 *Earnings per referral:* ₱0.50
🔗 *Your referral code:* `{user_data['referral_code']}`
📢 *Your referral link:*
https://t.me/{BOT_USERNAME}?start={user_data['referral_code']}

*How it works:*
1. Share your referral link
2. When someone joins using your link
3. You earn ₱0.50 instantly
4. They need to submit at least 1 valid Gmail

*Your referral stats:*
├ Total referrals: (check /stats)
└ Total earned: (check /stats)

⚠️ *Note:* Referrals must join channel and submit valid emails for you to earn.
"""
    
    await update.message.reply_text(referral_text, parse_mode=ParseMode.MARKDOWN)

# Admin commands
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin statistics"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Admin only command!")
        return
    
    cursor = db.conn.cursor()
    
    # Overall stats
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    
    cursor.execute('SELECT SUM(balance) FROM users')
    total_balance = cursor.fetchone()[0] or 0
    
    cursor.execute('SELECT SUM(total_earned) FROM users')
    total_earned = cursor.fetchone()[0] or 0
    
    cursor.execute('''
        SELECT COUNT(*), SUM(amount) 
        FROM withdrawals 
        WHERE status = 'approved'
    ''')
    withdrawals = cursor.fetchone()
    
    cursor.execute('''
        SELECT COUNT(*), 
               SUM(CASE WHEN status = 'valid' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END),
               SUM(earnings)
        FROM submissions
    ''')
    submissions = cursor.fetchone()
    
    stats_text = f"""
👑 *Admin Statistics*

👥 *Users:*
├ Total Users: {total_users}
├ Total Balance: ₱{total_balance:.2f}
└ Total Earned: ₱{total_earned:.2f}

📧 *Submissions:*
├ Total: {submissions[0] or 0}
├ Valid: {submissions[1] or 0}
├ Pending: {submissions[2] or 0}
└ Total Paid: ₱{submissions[3] or 0:.2f}

💸 *Withdrawals:*
├ Total Approved: {withdrawals[0] or 0}
└ Total Paid: ₱{withdrawals[1] or 0:.2f}

📋 *Pending Actions:*
├ /pending_subs - Pending submissions
└ /pending_wd - Pending withdrawals
"""
    
    await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)

async def pending_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending submissions"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Admin only command!")
        return
    
    cursor = db.conn.cursor()
    cursor.execute('''
        SELECT s.id, s.email, s.submission_date, u.username, u.user_id
        FROM submissions s
        JOIN users u ON s.user_id = u.user_id
        WHERE s.status = 'pending'
        ORDER BY s.submission_date ASC
        LIMIT 10
    ''')
    
    pending = cursor.fetchall()
    
    if not pending:
        await update.message.reply_text("✅ No pending submissions!")
        return
    
    subs_text = "📧 *Pending Submissions*\n\n"
    for sub in pending:
        subs_text += f"""
├ ID: `{sub[0]}`
├ Email: `{sub[1]}`
├ User: @{sub[3]} ({sub[4]})
├ Date: {sub[2]}
└ Actions: /review_{sub[0]}

"""
    
    await update.message.reply_text(subs_text, parse_mode=ParseMode.MARKDOWN)

async def pending_wd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending withdrawals"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Admin only command!")
        return
    
    cursor = db.conn.cursor()
    cursor.execute('''
        SELECT w.id, w.amount, w.request_date, u.username, u.user_id, 
               u.ewallet_type, u.ewallet_number
        FROM withdrawals w
        JOIN users u ON w.user_id = u.user_id
        WHERE w.status = 'pending'
        ORDER BY w.request_date ASC
        LIMIT 10
    ''')
    
    pending = cursor.fetchall()
    
    if not pending:
        await update.message.reply_text("✅ No pending withdrawals!")
        return
    
    wd_text = "💸 *Pending Withdrawals*\n\n"
    for wd in pending:
        wd_text += f"""
├ ID: `{wd[0]}`
├ Amount: ₱{wd[1]:.2f}
├ User: @{wd[3]} ({wd[4]})
├ E-wallet: {wd[5]} - {wd[6]}
├ Date: {wd[2]}
└ Actions: /approve_{wd[0]} | /reject_{wd[0]}

"""
    
    await update.message.reply_text(wd_text, parse_mode=ParseMode.MARKDOWN)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data == "check_membership":
        if await check_channel_membership(user_id, context):
            await query.edit_message_text(
                "✅ Membership verified! Use /start to continue.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_text(
                "❌ Still not a member! Please join the channel first.",
                parse_mode=ParseMode.MARKDOWN
            )
    
    elif data.startswith("ewallet_"):
        ewallet_type = data.replace("ewallet_", "").capitalize()
        context.user_data['ewallet_type'] = ewallet_type
        
        await query.edit_message_text(
            f"💳 *{ewallet_type} Setup*\n\n"
            f"Please enter your {ewallet_type} number:",
            parse_mode=ParseMode.MARKDOWN
        )
        return AWAITING_EWALLET_NUMBER

async def receive_ewallet_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive e-wallet number"""
    user_id = update.effective_user.id
    ewallet_number = update.message.text.strip()
    ewallet_type = context.user_data.get('ewallet_type', 'GCash')
    
    # Basic validation
    if not ewallet_number.isdigit() or len(ewallet_number) != 11:
        await update.message.reply_text(
            "❌ Invalid mobile number! Please enter 11 digits."
        )
        return AWAITING_EWALLET_NUMBER
    
    # Save to database
    db.update_ewallet(user_id, ewallet_type, ewallet_number)
    
    await update.message.reply_text(
        f"✅ {ewallet_type} account bound successfully!\n"
        f"Number: {ewallet_number}\n\n"
        f"You can now use /withdraw when you have enough balance."
    )
    
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command"""
    help_text = """
📋 *Gmail Marketplace Bot Help*

*User Commands:*
/start - Start the bot
/submit - Submit Gmail account (format: email:password)
/withdraw - Withdraw earnings (min ₱10)
/bind - Bind e-wallet account (GCash/PayMaya)
/stats - Check your statistics
/referral - Get referral information
/help - Show this help message

*Admin Commands:* (Admin only)
/admin_stats - Show admin statistics
/pending_subs - Show pending submissions
/pending_wd - Show pending withdrawals

*How to Earn:*
1. Submit valid Gmail accounts
2. Each valid submission earns money
3. Refer friends to earn ₱0.50 each
4. Unlock referrals after earning ₱100

*Rules:*
- Only submit Gmail accounts
- One account per submission
- Minimum withdrawal: ₱10
- Withdrawals processed in 24-48 hours
- Must join channel to use bot

⚠️ *Never share your password with anyone!*
"""
    
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation"""
    await update.message.reply_text(
        "❌ Operation cancelled.",
        reply_markup=None
    )
    return ConversationHandler.END

def main():
    """Start the bot"""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation handler for email submission
    submit_conv = ConversationHandler(
        entry_points=[CommandHandler('submit', submit_email)],
        states={
            AWAITING_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Conversation handler for e-wallet binding
    bind_conv = ConversationHandler(
        entry_points=[CommandHandler('bind', bind_ewallet)],
        states={
            AWAITING_EWALLET_TYPE: [
                CallbackQueryHandler(handle_callback, pattern='^ewallet_')
            ],
            AWAITING_EWALLET_NUMBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ewallet_number)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Conversation handler for withdrawal
    withdraw_conv = ConversationHandler(
        entry_points=[CommandHandler('withdraw', withdraw)],
        states={
            AWAITING_WITHDRAW_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_withdrawal)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Add handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(submit_conv)
    application.add_handler(bind_conv)
    application.add_handler(withdraw_conv)
    application.add_handler(CommandHandler('stats', stats))
    application.add_handler(CommandHandler('referral', referral_info))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('admin_stats', admin_stats))
    application.add_handler(CommandHandler('pending_subs', pending_subs))
    application.add_handler(CommandHandler('pending_wd', pending_wd))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Handle review commands (admin)
    async def review_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle review command from admin"""
        if update.effective_user.id != ADMIN_ID:
            return
        
        # Extract submission ID from command
        command = update.message.text
        if '_' in command:
            try:
                submission_id = int(command.split('_')[1])
                # Here you would implement the actual review logic
                # For now, just acknowledge
                await update.message.reply_text(
                    f"Review submission {submission_id}\n\n"
                    f"Use: /valid_{submission_id}_0.50 (for valid with amount)\n"
                    f"Or: /invalid_{submission_id} (for invalid)"
                )
            except (IndexError, ValueError):
                await update.message.reply_text("Invalid command format!")
    
    # Handle review and withdrawal commands
    application.add_handler(MessageHandler(
        filters.Regex(r'^/(review|valid|invalid|approve|reject)_\d+'), 
        review_submission
    ))
    
    # Start the bot
    print(f"🤖 Bot is running...")
    print(f"📱 Bot username: @{BOT_USERNAME}")
    print(f"📢 Channel: {CHANNEL_USERNAME}")
    print(f"👑 Admin: {ADMIN_USERNAME} (ID: {ADMIN_ID})")
    print(f"💾 Database: {DB_FILE}")
    print(f"🚀 Press Ctrl+C to stop")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
