# ================= IMPORT =================

import os
import io
import uuid
import json
import time
import random
import asyncio
import concurrent.futures

from dotenv import load_dotenv
load_dotenv()

from PIL import Image, ImageFilter

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)

from telegram.request import HTTPXRequest
from telegram.helpers import escape_markdown

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

MAX_MB = 10
BASE_WIDTH = 4200

KEYS_FILE = "keys.json"
USERS_FILE = "users.json"
ADMINS_FILE = "admins.json"
CHATS_FILE = "chats.json"
FREE_FILE = "free.json"

# ================= JSON =================

def load_json(f):

    if not os.path.exists(f):
        return {}

    with open(f) as x:
        return json.load(x)

def save_json(f, d):

    with open(f, "w") as x:
        json.dump(d, x)

keys = load_json(KEYS_FILE)
users = load_json(USERS_FILE)
admins = load_json(ADMINS_FILE)
chats = load_json(CHATS_FILE)
free_mode = load_json(FREE_FILE)

# ================= SAVE CHAT =================

def save_chat(chat_id):

    chats[str(chat_id)] = True

    save_json(CHATS_FILE, chats)

# ================= TIME =================

def parse_duration(s):

    s = s.lower()

    if s.endswith("d"):
        return int(s[:-1]) * 86400

    if s.endswith("m"):
        return int(s[:-1]) * 30 * 86400

    if s.endswith("y"):
        return int(s[:-1]) * 365 * 86400

    return 0

# ================= PREMIUM =================

def is_admin(uid):

    return uid == ADMIN_ID or str(uid) in admins


def is_free_mode():

    exp = free_mode.get("exp", 0)

    if exp == 0:
        return False

    return time.time() < exp


def is_premium(uid):

    if is_free_mode():
        return True

    user = users.get(str(uid))

    if not user:
        return False

    exp = user.get("exp", -1)

    if exp == 0:
        return True

    if exp < time.time():
        return False

    return True

# ================= COLLAGE =================

def generate_layout_variants(n):

    layouts = []

    layouts.append([
        max(1, n // 2),
        n - max(1, n // 2)
    ])

    rows = 3

    base = n // rows
    extra = n % rows

    l2 = []

    for i in range(rows):
        l2.append(base + (1 if i < extra else 0))

    layouts.append(l2)

    rows = 4

    base = n // rows
    extra = n % rows

    l3 = []

    for i in range(rows):
        l3.append(base + (1 if i < extra else 0))

    layouts.append(l3)

    return layouts

def build_collage(imgs, layout):

    canvas_width = BASE_WIDTH

    idx = 0

    rows_data = []

    for count in layout:

        row_imgs = imgs[idx:idx + count]

        idx += count

        heights = [i.height for i in row_imgs]

        target_h = min(heights)

        # Pre-compute width each image would have at target_h
        widths_at_target = [
            int(img.width * target_h / img.height)
            for img in row_imgs
        ]

        total_w = sum(widths_at_target)

        # Combine both resize steps into one: scale directly to final size
        final_scale = canvas_width / total_w

        final_h = int(target_h * final_scale)

        final_row = []

        for img, w in zip(row_imgs, widths_at_target):

            final_w = int(w * final_scale)

            r = img.resize(
                (final_w, final_h),
                Image.BILINEAR
            )

            final_row.append(r)

        rows_data.append((final_row, final_h))

    total_height = sum(h for _, h in rows_data)

    canvas = Image.new(
        "RGB",
        (canvas_width, total_height),
        (255, 255, 255)
    )

    y = 0

    for row, h in rows_data:

        x = 0

        for img in row:

            canvas.paste(img, (x, y))

            x += img.width

        y += h

    return canvas

def smart_collages(imgs):

    layouts = generate_layout_variants(len(imgs))

    imgs2 = imgs.copy()
    imgs3 = imgs.copy()

    random.shuffle(imgs2)
    random.shuffle(imgs3)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f1 = ex.submit(build_collage, imgs, layouts[0])
        f2 = ex.submit(build_collage, imgs2, layouts[1])
        f3 = ex.submit(build_collage, imgs3, layouts[2])
        return [f1.result(), f2.result(), f3.result()]

def open_imgs(data):

    return [
        Image.open(io.BytesIO(b)).convert("RGB")
        for b in data
    ]

# ================= CROP =================

def crop_status_bar(img):

    w, h = img.size

    crop_top = int(h * 0.055)

    return img.crop((0, crop_top, w, h))

# ================= COMPRESS =================

def compress(collages, limit):

    if limit == 0:
        limit = MAX_MB

    if limit is None:
        limit = 2

    target = limit * 1024 * 1024

    out = []

    for img in collages:

        img = img.filter(ImageFilter.SHARPEN)

        # Downscale loop: keep shrinking until quality=20 strictly fits
        while True:

            bio = io.BytesIO()

            img.save(
                bio,
                "JPEG",
                quality=20,
                subsampling=2
            )

            if bio.tell() <= target:
                break

            scale = (target / bio.tell()) ** 0.5 * 0.90

            img = img.resize(
                (
                    int(img.width * scale),
                    int(img.height * scale)
                ),
                Image.LANCZOS
            )

        # Binary search for highest quality that still fits
        low = 20
        high = 95
        best = low

        while low <= high:

            mid = (low + high) // 2

            bio = io.BytesIO()

            img.save(
                bio,
                "JPEG",
                quality=mid,
                subsampling=2
            )

            if bio.tell() <= target:
                best = mid
                low = mid + 1
            else:
                high = mid - 1

        # Final save with optimize + progressive
        bio = io.BytesIO()

        img.save(
            bio,
            "JPEG",
            quality=best,
            optimize=True,
            progressive=True,
            subsampling=2
        )

        # Safety: optimize/progressive can occasionally produce a larger file;
        # fall back to plain save if it went over
        if bio.tell() > target:

            bio = io.BytesIO()

            img.save(
                bio,
                "JPEG",
                quality=best,
                subsampling=2
            )

        bio.seek(0)

        out.append(bio)

    return out

# ================= HELP =================

async def help_cmd(u, c):

    uid = u.effective_user.id

    txt = (
        "📸 𝗦𝗺𝗮𝗿𝘁 𝗖𝗼𝗹𝗹𝗮𝗴𝗲 𝗘𝗻𝗴𝗶𝗻𝗲\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "📌 𝗛𝗼𝘄 𝘁𝗼 𝘂𝘀𝗲:\n"
        "1️⃣ Send photos to the bot\n"
        "2️⃣ Tap 'Crop Status Bar' or 'Make Collage'\n"
        "3️⃣ Bot generates 3 layout variants\n\n"

        "⚙ 𝗨𝘀𝗲𝗿 𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀:\n"
        "/start — Bot info & your status\n"
        "/help — This guide\n"
        "/limit <MB> — Set max file size (e.g., 2MB). Use /limit 0 for max quality\n"
        "/redeem <key> — Redeem a premium key\n\n"

        "💡 𝗧𝗶𝗽𝘀:\n"
        "• /limit 0 → max quality (10MB)\n"
        "• /limit 2 → default (2MB per collage)\n"
    )

    if is_admin(uid):

        txt += (
            "\n🔐 𝗔𝗱𝗺𝗶𝗻 𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀:\n"
            "/approve <uid> <duration> — Give premium\n"
            "/revoke <uid> — Remove premium\n"
            "/free <duration> — Open bot to all (e.g. /free 1d)\n"
            "/free off — Disable free mode\n"
            "/generate <qty> <duration> — Generate keys\n"
            "/user_premium — List premium users\n"
            "/admin <uid> — Add sub-admin\n"
            "/broadcast <msg> — Message all users\n"
            "/admincmds — Quick admin reference\n"
        )

    await u.message.reply_text(txt)

# ================= ADMIN CMDS =================

async def admincmds(u, c):

    uid = u.effective_user.id

    if not is_admin(uid):
        return

    txt = (
        "🔐 𝗔𝗱𝗺𝗶𝗻 𝗥𝗲𝗳𝗲𝗿𝗲𝗻𝗰𝗲\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "👤 𝗨𝘀𝗲𝗿 𝗠𝗮𝗻𝗮𝗴𝗲𝗺𝗲𝗻𝘁:\n"
        "/approve 123456 3d — Premium for 3 days\n"
        "/approve 123456 1m — Premium for 1 month\n"
        "/approve 123456 1y — Premium for 1 year\n"
        "/approve 123456 — Unlimited premium\n"
        "/revoke 123456 — Remove user premium\n\n"

        "🆓 𝗙𝗿𝗲𝗲 𝗠𝗼𝗱𝗲:\n"
        "/free 1d — Open bot to everyone for 1 day\n"
        "/free 1m — Open bot to everyone for 1 month\n"
        "/free off — Disable free mode\n"
        "/free — Check free mode status\n\n"

        "🔑 𝗞𝗲𝘆 𝗚𝗲𝗻𝗲𝗿𝗮𝘁𝗶𝗼𝗻:\n"
        "/generate 5 3d — 5 keys for 3 days\n"
        "/generate 10 1m — 10 keys for 1 month\n\n"

        "📊 𝗠𝗼𝗻𝗶𝘁𝗼𝗿𝗶𝗻𝗴:\n"
        "/user_premium — List all premium users\n\n"

        "👑 𝗦𝘂𝗽𝗲𝗿 𝗔𝗱𝗺𝗶𝗻 𝗢𝗻𝗹𝘆:\n"
        "/admin <uid> — Add sub-admin\n"
        "/broadcast <msg> — Message all users\n\n"

        "⏱ 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻 𝗙𝗼𝗿𝗺𝗮𝘁𝘀:\n"
        "3d = 3 days | 1m = 1 month | 1y = 1 year\n"
    )

    await u.message.reply_text(txt)

# ================= START =================

async def start(u, c):

    save_chat(u.effective_chat.id)

    uid = str(u.effective_user.id)

    if uid not in users:

        users[uid] = {
            "limit": 2
        }

        save_json(USERS_FILE, users)

    premium = is_premium(u.effective_user.id)

    if premium:

        exp = users.get(uid, {}).get("exp", 0)

        if exp == 0:

            status = "✅ PREMIUM (Unlimited)"

        else:

            days = int((exp - time.time()) / 86400)

            if days < 0:
                days = 0

            status = f"✅ PREMIUM ({days}d left)"

    else:

        status = "⛔ FREE"

    admin_badge = "  👑 Admin" if is_admin(u.effective_user.id) else ""

    await u.message.reply_text(

        f"📸 𝗦𝗺𝗮𝗿𝘁 𝗖𝗼𝗹𝗹𝗮𝗴𝗲 𝗘𝗻𝗴𝗶𝗻𝗲\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"

        f"🆔 ID: {uid}{admin_badge}\n"
        f"📊 Status: {status}\n\n"

        f"Ultra high quality smart collages\n"
        f"in low MB. 3 layouts per request.\n\n"

        f"✨ 𝗙𝗲𝗮𝘁𝘂𝗿𝗲𝘀:\n"
        f"• Smart multi-layout collages\n"
        f"• Status bar crop\n"
        f"• Adjustable file size limit\n\n"

        f"📖 /help — Full command guide"
    )

# ================= LIMIT =================

async def limit(u, c):

    if not c.args:

        current = users.get(
            str(u.effective_user.id),
            {}
        ).get("limit", 2)

        return await u.message.reply_text(
            f"Current limit: {current}MB"
        )

    try:

        v = int(c.args[0])

        if v < 0 or v > MAX_MB:

            return await u.message.reply_text(
                "Use: /limit 0-10"
            )

        uid = str(u.effective_user.id)

        user = users.get(uid, {})

        user["limit"] = v

        users[uid] = user

        save_json(USERS_FILE, users)

        if v == 0:
            await u.message.reply_text(
                "Limit set to max quality (10MB)"
            )
        else:
            await u.message.reply_text(
                f"Limit set to {v}MB"
            )

    except:

        await u.message.reply_text(
            "Use: /limit 0-10"
        )



# ================= FREE MODE =================

async def free_cmd(u, c):

    if not is_admin(u.effective_user.id):
        return

    if not c.args:
        if is_free_mode():
            left = int((free_mode.get("exp", 0) - time.time()) / 3600)
            return await u.message.reply_text(
                f"🆓 Free mode is ON — {left}h remaining\n"
                f"Use /free off to disable."
            )
        else:
            return await u.message.reply_text(
                "Free mode is OFF.\n"
                "Usage: /free 1d  or  /free off"
            )

    arg = c.args[0].lower()

    if arg == "off":
        free_mode["exp"] = 0
        save_json(FREE_FILE, free_mode)
        return await u.message.reply_text("🔴 Free mode disabled.")

    seconds = parse_duration(arg)

    if seconds <= 0:
        return await u.message.reply_text(
            "Invalid duration.\nUse: /free 1d / 1m / 1y  or  /free off"
        )

    free_mode["exp"] = time.time() + seconds
    save_json(FREE_FILE, free_mode)

    days = int(seconds / 86400)

    await u.message.reply_text(
        f"🟢 Free mode ON for {days}d\n"
        f"All users can now use the bot freely."
    )


# ================= APPROVE =================

async def approve(u, c):

    if u.effective_user.id != ADMIN_ID:
        return

    if not c.args:
        return await u.message.reply_text(
            "Usage:\n/approve userid 3d"
        )

    uid = c.args[0]

    exp = 0

    if len(c.args) > 1:

        seconds = parse_duration(c.args[1])

        if seconds > 0:
            exp = time.time() + seconds

    old = users.get(uid, {})

    users[uid] = {
        "limit": old.get("limit", 2),
        "exp": exp
    }

    save_json(USERS_FILE, users)

    await u.message.reply_text("Approved")

# ================= PREMIUM USERS =================

async def user_premium(u, c):

    if u.effective_user.id != ADMIN_ID:
        return

    out = []

    for uid, data in users.items():

        exp = data.get("exp", -1)

        if exp == -1:
            continue

        try:

            chat = await c.bot.get_chat(int(uid))

            name = chat.first_name or "Unknown"

            if chat.username:
                name += f" (@{chat.username})"

        except:

            name = "Unknown"

        if exp == 0:

            status = "Unlimited"

        else:

            left = int((exp - time.time()) / 86400)

            if left <= 0:
                continue

            status = f"{left}d left"

        safe_uid = escape_markdown(uid, version=2)
        safe_name = escape_markdown(name, version=2)
        safe_status = escape_markdown(status, version=2)

        out.append(
            f"`{safe_uid}`\n{safe_name}\n{safe_status}\n"
        )

    if not out:

        return await u.message.reply_text(
            "No premium users"
        )

    await u.message.reply_text(
        "\n".join(out),
        parse_mode="MarkdownV2"
    )

# ================= REVOKE =================

async def revoke(u, c):

    if u.effective_user.id != ADMIN_ID:
        return

    if not c.args:
        return

    uid = c.args[0]

    if uid in users:

        del users[uid]

        save_json(USERS_FILE, users)

    await u.message.reply_text("Revoked")

# ================= GENERATE =================

async def generate(u, c):

    uid = u.effective_user.id

    if not is_admin(uid):
        return

    if len(c.args) < 2:

        return await u.message.reply_text(
            "Usage:\n/generate amount duration\nExample:\n/generate 5 3d"
        )

    try:

        qty = int(c.args[0])
        duration_text = c.args[1].lower()

    except:

        return await u.message.reply_text(
            "Invalid format"
        )

    seconds = parse_duration(duration_text)

    if seconds <= 0:

        return await u.message.reply_text(
            "Invalid duration.\nUse: 3d / 1m / 1y"
        )

    if uid != ADMIN_ID:

        if seconds > 30 * 86400:

            return await u.message.reply_text(
                "Sub-admins can only generate keys upto 30d"
            )

    out = []

    for _ in range(qty):

        k = uuid.uuid4().hex[:10]

        keys[k] = {
            "duration": seconds,
            "used": False
        }

        out.append(k)

    save_json(KEYS_FILE, keys)

    days = int(seconds / 86400)

    lines = "\n".join(f"<code>{k}</code>" for k in out)

    text = (
        f"🔑 <b>{qty} Keys Generated</b> — {duration_text} ({days}d)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{lines}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📲 <b>How to redeem:</b>\n"
        f"1. Open @Leotempbot\n"
        f"2. Tap the key above to copy it\n"
        f"3. Send: <code>/redeem YOUR_KEY</code>"
    )

    await u.message.reply_text(
        text,
        parse_mode="HTML"
    )

# ================= REDEEM =================

async def redeem(u, c):

    if not c.args:
        return

    k = c.args[0]

    if k not in keys:

        return await u.message.reply_text(
            "Invalid key"
        )

    if keys[k]["used"]:

        return await u.message.reply_text(
            "Key already used"
        )

    duration = keys[k]["duration"]

    exp = time.time() + duration

    old = users.get(str(u.effective_user.id), {})

    users[str(u.effective_user.id)] = {
        "limit": old.get("limit", 2),
        "exp": exp
    }

    keys[k]["used"] = True

    save_json(KEYS_FILE, keys)
    save_json(USERS_FILE, users)

    days = int(duration / 86400)

    await u.message.reply_text(
        f"You successfully redeemed premium for {days} days."
    )

# ================= ADMIN =================

async def admin_cmd(u, c):

    if u.effective_user.id != ADMIN_ID:
        return

    if not c.args:
        return

    uid = c.args[0]

    admins[uid] = True

    save_json(ADMINS_FILE, admins)

    await u.message.reply_text("Admin added")

# ================= BROADCAST =================

async def broadcast(u, c):

    if u.effective_user.id != ADMIN_ID:
        return

    if not c.args:
        return

    msg = " ".join(c.args)

    sent = 0
    failed = 0

    for cid in list(chats.keys()):

        try:

            await c.bot.send_message(
                chat_id=int(cid),
                text=msg
            )

            sent += 1

        except:

            failed += 1

    await u.message.reply_text(
        f"Broadcast completed\n\nSent: {sent}\nFailed: {failed}"
    )

# ================= PHOTO =================

async def photo(u, c):

    if not is_premium(u.effective_user.id):

        return await u.message.reply_text(
            "Your premium expired.\nRedeem a new key or contact admin."
        )

    save_chat(u.effective_chat.id)

    if "imgs" not in c.user_data:
        c.user_data["imgs"] = []

    if c.user_data.get("panel_msg"):

        try:
            await c.user_data["panel_msg"].delete()
        except:
            pass

    f = await u.message.photo[-1].get_file()

    b = await f.download_as_bytearray()

    c.user_data["imgs"].append(b)

    total = len(c.user_data["imgs"])

    kb = [
        [
            InlineKeyboardButton(
                f"Crop Status Bar ({total})",
                callback_data="crop"
            ),
            InlineKeyboardButton(
                "Make Collage",
                callback_data="done_now"
            )
        ]
    ]

    msg = await u.message.reply_text(
        f"{total} images loaded",
        reply_markup=InlineKeyboardMarkup(kb)
    )

    c.user_data["panel_msg"] = msg

# ================= TEXT HANDLER =================

async def text_handler(u, c):
    pass

# ================= BUTTONS =================

async def buttons(update: Update, c):

    q = update.callback_query

    save_chat(q.message.chat.id)

    uid = q.from_user.id

    await q.answer()

    imgs_data = c.user_data.get("imgs", [])
    c.user_data["imgs"] = []

    if not imgs_data:
        return

    try:
        await q.message.delete()
    except:
        pass

    imgs = open_imgs(imgs_data)

    if q.data == "crop":

        imgs = [crop_status_bar(i) for i in imgs]

    msg = await q.message.reply_text(
        "Generating..."
    )

    collages = await asyncio.to_thread(
        smart_collages,
        imgs
    )

    uid_str = str(uid)

    lim = users.get(
        uid_str,
        {}
    ).get("limit", 2)

    bios = compress(collages, lim)

    for b in bios:

        b.seek(0)

        await q.message.reply_document(
            document=b,
            filename=f"collage_{uuid.uuid4().hex}.jpg"
        )

    await msg.delete()

    c.user_data["imgs"] = []

# ================= MAIN =================

request = HTTPXRequest(
    connect_timeout=30.0,
    read_timeout=120.0,
    write_timeout=120.0,
    pool_timeout=120.0,
)

app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .request(request)
    .build()
)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_cmd))
app.add_handler(CommandHandler("limit", limit))

app.add_handler(CommandHandler("approve", approve))
app.add_handler(CommandHandler("free", free_cmd))
app.add_handler(CommandHandler("user_premium", user_premium))
app.add_handler(CommandHandler("revoke", revoke))
app.add_handler(CommandHandler("generate", generate))
app.add_handler(CommandHandler("redeem", redeem))
app.add_handler(CommandHandler("admin", admin_cmd))
app.add_handler(CommandHandler("broadcast", broadcast))
app.add_handler(CommandHandler("admincmds", admincmds))

app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_handler
    )
)

app.add_handler(
    MessageHandler(
        filters.PHOTO,
        photo
    )
)

app.add_handler(
    CallbackQueryHandler(buttons)
)

app.run_polling(drop_pending_updates=True)
