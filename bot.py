import os
import logging
from datetime import datetime, timezone
from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")
MONGO_URI = os.getenv("MONGO_URI")

logging.basicConfig(level=logging.INFO)

# ============================================================
# DATABASE
# ============================================================
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["mereb_coffee"]
carts_col = db["carts"]
orders_col = db["orders"]

try:
    mongo_client.admin.command("ping")
    print("Connected to MongoDB.")
except Exception as e:
    print("MongoDB connection FAILED:", e)

# ============================================================
# MENU
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

STATUS_LABELS = {
    "payment_submitted": "🧾 Payment Submitted",
    "preparing": "👨‍🍳 Preparing",
    "ready": "✅ Ready",
    "completed": "📦 Completed",
}
STATUS_CUSTOMER_MESSAGE = {
    "preparing": "👨‍🍳 Your order is now being prepared!",
    "ready": "🎉 Your order is ready!",
    "completed": "📦 Your order is complete. Thank you for choosing Mereb Coffee House!",
}


# ============================================================
# CART HELPERS
# ============================================================

def get_cart(chat_id):
    doc = carts_col.find_one({"chat_id": chat_id})
    return doc["items"] if doc else []


def save_cart(chat_id, items):
    carts_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "items": items, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def clear_cart(chat_id):
    carts_col.delete_one({"chat_id": chat_id})


def cart_total(cart):
    return sum(item["price"] * item["qty"] for item in cart)


def menu_keyboard():
    buttons = [
        [InlineKeyboardButton(f'{item["name"]} — {item["price"]} ETB', callback_data=f'add_{item["id"]}')]
        for item in MENU
    ]
    buttons.append([InlineKeyboardButton("🛒 View Cart", callback_data="view_cart")])
    return InlineKeyboardMarkup(buttons)


# Persistent bottom bar — stays visible below the text box at all times,
# so the customer never has to type /start again to get the menu back.
BOTTOM_BAR = ReplyKeyboardMarkup(
    [[KeyboardButton("📋 Menu"), KeyboardButton("🛒 Cart")]],
    resize_keyboard=True,
    is_persistent=True,
)


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


def fulfillment_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏪 Pickup", callback_data="fulfill_pickup")],
        [InlineKeyboardButton("🚚 Delivery", callback_data="fulfill_delivery")],
    ])


def owner_order_text(order):
    order_lines = "\n".join(f'• {i["name"]} x{i["qty"]} — {i["price"] * i["qty"]} ETB' for i in order["items"])
    fulfillment = order.get("fulfillment", "pickup")
    address_line = f'\n📍 Deliver to: {order["address"]}' if fulfillment == "delivery" and order.get("address") else ""
    status_line = f'\n\nStatus: {STATUS_LABELS.get(order.get("status", "new"), "🆕 New")}'
    return (
        f'🔔 New order from {order["customer"]}\n\n'
        f'{order_lines}\n\n'
        f'Total: {order["total"]} ETB\n'
        f'Fulfillment: {"🚚 Delivery" if fulfillment == "delivery" else "🏪 Pickup"}'
        f'{address_line}'
        f'{status_line}'
    )


def owner_status_keyboard(order_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👨‍🍳 Preparing", callback_data=f"status_preparing_{order_id}"),
            InlineKeyboardButton("✅ Ready", callback_data=f"status_ready_{order_id}"),
        ],
        [InlineKeyboardButton("📦 Completed", callback_data=f"status_completed_{order_id}")],
    ])


# ============================================================
# COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_address"] = False
    await update.message.reply_text(
        "☕ Welcome to Mereb Coffee House!",
        reply_markup=BOTTOM_BAR,
    )
    await update.message.reply_text(
        "Tap an item to add it to your order:",
        reply_markup=menu_keyboard(),
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your chat ID is: {update.effective_chat.id}")


# ============================================================
# ORDER FINALIZATION — shared by pickup and delivery paths
# ============================================================

async def finalize_order(context, chat_id, user, fulfillment, address=None):
    cart = get_cart(chat_id)
    total = cart_total(cart)
    customer = f"@{user.username}" if user.username else user.first_name

    order = {
        "chat_id": chat_id,
        "customer": customer,
        "items": cart,
        "total": total,
        "fulfillment": fulfillment,
        "address": address,
        "created_at": datetime.now(timezone.utc),
        "status": "new",
    }
    result = orders_col.insert_one(order)
    order_id = str(result.inserted_id)

    order_lines = "\n".join(f'• {i["name"]} x{i["qty"]} — {i["price"] * i["qty"]} ETB' for i in cart)
    confirm_text = (
        f"✅ Order recorded!\n\n{order_lines}\n\nTotal: {total} ETB\n"
        f'{"📍 Delivering to: " + address if fulfillment == "delivery" else "🏪 Ready for pickup"}\n\n'
        "📸 Please send a screenshot of your payment slip to confirm this order."
    )

    context.user_data["awaiting_payment_order_id"] = order_id
    clear_cart(chat_id)
    return confirm_text


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
        save_cart(chat_id, cart)
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
        clear_cart(chat_id)
        await query.answer("Cart cleared")
        await query.edit_message_text("Cart cleared. Back to the menu:", reply_markup=menu_keyboard())
        return

    if data == "checkout":
        cart = get_cart(chat_id)
        if not cart:
            await query.answer("Your cart is empty")
            return
        await query.answer()
        await query.edit_message_text("How would you like to get your order?", reply_markup=fulfillment_keyboard())
        return

    if data == "fulfill_pickup":
        await query.answer()
        confirm_text = await finalize_order(context, chat_id, query.from_user, "pickup")
        await query.edit_message_text(confirm_text)
        return

    if data == "fulfill_delivery":
        await query.answer()
        context.user_data["awaiting_address"] = True
        await query.edit_message_text("📍 Please type your delivery address as a message below:")
        return

    if data.startswith("status_"):
        try:
            remainder = data[len("status_"):]  # e.g. "preparing_507f1f77bcf86cd799439011"
            status_code, order_id = remainder.rsplit("_", 1)

            order = orders_col.find_one({"_id": ObjectId(order_id)})
            if not order:
                await query.answer("⚠️ Order not found in database")
                return

            orders_col.update_one({"_id": ObjectId(order_id)}, {"$set": {"status": status_code}})
            order["status"] = status_code

            await query.answer(f"Marked as {STATUS_LABELS.get(status_code, status_code)}")

            # The status buttons can be attached to either a text message
            # (old flow) or a photo message (payment screenshot flow) —
            # edit the right part depending on which one this is.
            if query.message.photo:
                await query.edit_message_caption(caption=owner_order_text(order), reply_markup=owner_status_keyboard(order_id))
            else:
                await query.edit_message_text(owner_order_text(order), reply_markup=owner_status_keyboard(order_id))

            customer_msg = STATUS_CUSTOMER_MESSAGE.get(status_code)
            if customer_msg:
                await context.bot.send_message(chat_id=order["chat_id"], text=customer_msg)

        except Exception as e:
            logging.exception("Failed to process status button")
            await query.answer(f"⚠️ Error: {e}", show_alert=True)
        return


# ============================================================
# TEXT MESSAGES — only used to capture a delivery address
# ============================================================

# ============================================================
# PHOTOS — used to capture the payment screenshot
# ============================================================

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = context.user_data.get("awaiting_payment_order_id")
    if not order_id:
        return  # not currently expecting a payment screenshot, ignore

    order = orders_col.find_one({"_id": ObjectId(order_id)})
    if not order:
        await update.message.reply_text("Sorry, we couldn't find that order. Please try /start again.")
        context.user_data["awaiting_payment_order_id"] = None
        return

    orders_col.update_one({"_id": ObjectId(order_id)}, {"$set": {"status": "payment_submitted"}})
    order["status"] = "payment_submitted"

    photo_file_id = update.message.photo[-1].file_id  # largest available size

    if OWNER_CHAT_ID:
        await context.bot.send_photo(
            chat_id=OWNER_CHAT_ID,
            photo=photo_file_id,
            caption=owner_order_text(order),
            reply_markup=owner_status_keyboard(order_id),
        )

    context.user_data["awaiting_payment_order_id"] = None
    await update.message.reply_text(
        "📸 Payment screenshot received! Your order is being reviewed — we'll message you here as it progresses.",
        reply_markup=BOTTOM_BAR,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Persistent bottom bar buttons
    if text == "📋 Menu":
        await update.message.reply_text(
            "☕ Mereb Coffee House Menu\n\nTap an item to add it:", reply_markup=menu_keyboard()
        )
        return

    if text == "🛒 Cart":
        cart = get_cart(update.effective_chat.id)
        if not cart:
            await update.message.reply_text("Your cart is empty. Add something from the menu:", reply_markup=menu_keyboard())
            return
        total = cart_total(cart)
        await update.message.reply_text(f"🛒 Your Cart\n\nTotal: {total} ETB", reply_markup=cart_keyboard(cart))
        return

    if not context.user_data.get("awaiting_address"):
        return  # ignore random text outside the checkout flow

    context.user_data["awaiting_address"] = False
    address = text
    chat_id = update.effective_chat.id

    confirm_text = await finalize_order(context, chat_id, update.effective_user, "delivery", address=address)
    await update.message.reply_text(confirm_text)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()