import os
import logging
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")

logging.basicConfig(level=logging.INFO)

# ============================================================
# MENU — edit this to change items, prices, or descriptions
# ============================================================
MENU = [
    {"id": "buna", "name": "Buna Special", "price": 120, "desc": "Traditional ceremony, roasted to order"},
    {"id": "macchiato", "name": "Macchiato", "price": 90, "desc": "Double shot, steamed milk"},
    {"id": "coldbrew", "name": "Cold Brew", "price": 140, "desc": "Steeped 18 hours, served over ice"},
    {"id": "toast", "name": "Avocado Toast", "price": 180, "desc": "Sourdough, chili, lime"},
    {"id": "shakshuka", "name": "Shakshuka", "price": 220, "desc": "Two eggs, house tomato sauce"},
    {"id": "croissant", "name": "Honey Croissant", "price": 95, "desc": "Baked daily, local honey"},
]
MENU_BY_ID = {item["id"]: item for item in MENU}

# In-memory cart storage: { chat_id: [{id, name, price, qty}] }
# NOTE: this resets whenever the bot restarts — we'll swap this for
# MongoDB once the Atlas cluster is ready, same as we discussed.
carts = {}


def get_cart(chat_id):
    return carts.setdefault(chat_id, [])


def cart_total(cart):
    return sum(item["price"] * item["qty"] for item in cart)


def menu_keyboard():
    buttons = [
        [InlineKeyboardButton(f'{item["name"]} — {item["price"]} ETB', callback_data=f'add_{item["id"]}')]
        for item in MENU
    ]
    buttons.append([InlineKeyboardButton("🛒 View Cart", callback_data="view_cart")])
    return InlineKeyboardMarkup(buttons)


def cart_keyboard(cart):
    buttons = [
        [InlineKeyboardButton(f'{item["name"]} x{item["qty"]} — {item["price"] * item["qty"]} ETB', callback_data="noop")]
        for item in cart
    ]
    if cart:
        buttons.append([InlineKeyboardButton("✅ Checkout", callback_data="checkout")])
        buttons.append([InlineKeyboardButton("🗑 Clear Cart", callback_data="clear_cart")])
    buttons.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_menu")])
    return InlineKeyboardMarkup(buttons)


# ============================================================
# COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "☕ Welcome to Mereb Coffee House!\n\nTap an item to add it to your order:",
        reply_markup=menu_keyboard(),
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your chat ID is: {update.effective_chat.id}")


# ============================================================
# BUTTON ACTIONS
# ============================================================

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat.id
    data = query.data

    if data == "noop":
        await query.answer()
        return

    if data.startswith("add_"):
        item_id = data[4:]
        item = MENU_BY_ID.get(item_id)
        if not item:
            await query.answer("Item not found")
            return
        cart = get_cart(chat_id)
        existing = next((c for c in cart if c["id"] == item_id), None)
        if existing:
            existing["qty"] += 1
        else:
            cart.append({**item, "qty": 1})
        await query.answer(f'Added {item["name"]}')
        return

    if data == "view_cart":
        cart = get_cart(chat_id)
        await query.answer()
        if not cart:
            await query.edit_message_text("Your cart is empty. Add something from the menu:", reply_markup=menu_keyboard())
            return
        total = cart_total(cart)
        await query.edit_message_text(f"🛒 Your Cart\n\nTotal: {total} ETB", reply_markup=cart_keyboard(cart))
        return

    if data == "back_menu":
        await query.answer()
        await query.edit_message_text("☕ Mereb Coffee House Menu\n\nTap an item to add it:", reply_markup=menu_keyboard())
        return

    if data == "clear_cart":
        carts[chat_id] = []
        await query.answer("Cart cleared")
        await query.edit_message_text("Cart cleared. Back to the menu:", reply_markup=menu_keyboard())
        return

    if data == "checkout":
        cart = get_cart(chat_id)
        if not cart:
            await query.answer("Your cart is empty")
            return
        total = cart_total(cart)
        order_lines = "\n".join(f'• {i["name"]} x{i["qty"]} — {i["price"] * i["qty"]} ETB' for i in cart)
        user = query.from_user
        customer = f"@{user.username}" if user.username else user.first_name

        await query.answer("Order placed!")
        await query.edit_message_text(
            f"✅ Order confirmed!\n\n{order_lines}\n\nTotal: {total} ETB\n\nWe'll message you here when it's ready."
        )

        if OWNER_CHAT_ID:
            await context.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=f"🔔 New order from {customer}\n\n{order_lines}\n\nTotal: {total} ETB",
            )

        carts[chat_id] = []
        return


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CallbackQueryHandler(on_button))
    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()