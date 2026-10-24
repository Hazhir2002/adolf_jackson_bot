import asyncio
import html
import ipaddress
import os
import re
from urllib.parse import urlparse

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    CallbackQuery,
    ForceReply,
    LinkPreviewOptions,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from db import DB, init_db

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # bot creator: can delete any villa
MAX_VOTES = int(os.getenv("MAX_VOTES", "0"))  # votes per person, 0 = unlimited
PROXY = os.getenv("PROXY")  # e.g. socks5://127.0.0.1:1080 (only if running from Iran)
AUTO = (
    os.getenv("AUTO_DETECT") == "1"
)  # offer "add" button on pasted links (privacy mode OFF)

URL_RE = re.compile(r"https?://\S+")
HTML = ParseMode.HTML
NOPREV = LinkPreviewOptions(is_disabled=True)
PAGE_SIZE = 6
router = Router()

# villa columns that users can fill in (whitelist: also protects the SQL f-strings below)
SETTABLE = ("price", "location", "rating", "ratingCount")
ALIASES = {k.lower(): k for k in SETTABLE}

VOTE_RULE = (
    f"هر نفر حداکثر به {MAX_VOTES} ویلا می‌تواند رأی بدهد"
    if MAX_VOTES
    else "هر نفر می‌تواند به چند ویلا رأی بدهد"
)
HELP = (
    "🏡 <b>ربات انتخاب ویلا</b>\n\n"
    "/menu — منوی اصلی\n"
    "/add لینک [نام] — افزودن سریع ویلا\n"
    "/set شماره price|location|rating|ratingCount مقدار — ویرایش مشخصات\n"
    "     (پاک کردن مقدار: -)\n"
    "/villas — نمایش لیست و رأی‌گیری\n"
    "/remove شماره — حذف ویلا (فقط اضافه‌کننده یا صاحب ربات)\n"
    "/close — بستن رأی‌گیری و اعلام برنده (ادمین)\n"
    "/open — باز کردن دوباره (ادمین)\n"
    "/reset — پاک کردن همه چیز (ادمین)\n"
    "/cancel — لغو افزودن مرحله‌ای\n\n"
    f"{VOTE_RULE}؛ دوباره روی همان ویلا بزنید تا رأی پس گرفته شود."
)
MENU_TEXT = "🏡 <b>انتخاب ویلا</b>\n\nیک گزینه را انتخاب کنید:"

DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


# ---------- callback data & states ----------
class Menu(CallbackData, prefix="m"):
    action: str  # home | board | add | delete | help | close
    page: int = 0


class Vote(CallbackData, prefix="v"):  # packs to "v:12:0"
    villa_id: int
    page: int = 0


class Del(CallbackData, prefix="d"):
    villa_id: int
    step: int = 0  # 0 = ask for confirmation, 1 = confirmed


class AddVilla(StatesGroup):
    link = State()
    name = State()
    price = State()
    location = State()
    rating = State()
    rating_count = State()


# ---------- keyboards ----------
def main_menu(admin: bool):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ افزودن ویلا", callback_data=Menu(action="add"))
    kb.button(text="🗳 رأی‌گیری", callback_data=Menu(action="board"))
    kb.button(text="🗑 حذف ویلا", callback_data=Menu(action="delete"))
    kb.button(text="ℹ️ راهنما", callback_data=Menu(action="help"))
    if admin:
        kb.button(text="🔒 بستن رأی‌گیری", callback_data=Menu(action="close"))
    kb.adjust(2)
    return kb.as_markup()


def back_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 منو", callback_data=Menu(action="home"))
    return kb.as_markup()


# ---------- formatting helpers ----------
def esc(v) -> str:
    return html.escape(str(v))


def fmt_price(v) -> str:
    """'4500000' / '۴۵۰۰۰۰۰' / '4,500,000' -> '4,500,000' (anything else stays as typed)"""
    digits = re.sub(r"[,٬،\s]", "", str(v).translate(DIGITS))
    return f"{int(digits):,}" if digits.isdigit() else str(v)


def norm_num(text: str, decimal: bool) -> str | None:
    """validate rating (decimal=True) or rating count (decimal=False). None if invalid."""
    s = text.strip().translate(DIGITS)
    if decimal:
        s = s.replace("٫", ".").replace(",", ".")
        return s if re.fullmatch(r"\d+(\.\d+)?", s) else None
    s = re.sub(r"[,٬،\s]", "", s)
    return s if s.isdigit() else None


def details_lines(x: dict, show_empty: bool = True) -> list[str]:
    """all villa properties; missing ones show '—' (hidden when show_empty=False)"""
    rating, count = x.get("rating"), x.get("ratingCount")
    rating_txt = None
    if rating:
        rating_txt = f"<b>{esc(rating)}</b>" + (f" ({esc(count)} نظر)" if count else "")
    elif count:
        rating_txt = f"({esc(count)} نظر)"
    rows = [
        ("📍 موقعیت", esc(x["location"]) if x.get("location") else None),
        ("⭐ امتیاز", rating_txt),
        ("💰 قیمت", esc(fmt_price(x["price"])) if x.get("price") else None),
    ]
    return [f"{label}: {val or '—'}" for label, val in rows if val or show_empty]


# ---------- helpers ----------
def can_delete(user_id: int, added_by: int) -> bool:
    """only the bot's creator or the user who added the villa"""
    return user_id == added_by or (OWNER_ID != 0 and user_id == OWNER_ID)


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        mem = await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest:
        return False
    return mem.status in ("creator", "administrator")


async def is_public_host(host: str) -> bool:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


async def fetch_title(url: str) -> str | None:
    try:
        host = urlparse(url).hostname
        if not host or not await is_public_host(host):
            return None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                page = (await r.content.read(300_000)).decode("utf-8", "ignore")
        m = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
            page,
            re.I | re.S,
        ) or re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        return html.unescape(m.group(1)).strip()[:60] if m else None
    except Exception:
        return None


async def add_villa(chat_id, user_id, url, title, **extra):
    extra = {k: v for k, v in extra.items() if k in SETTABLE and v}
    cols = ["chat_id", "url", "title", "added_by", *extra]
    try:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                f"INSERT INTO villas({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                (chat_id, url, title, user_id, *extra.values()),
            )
            await db.commit()
            return cur.lastrowid
    except aiosqlite.IntegrityError:
        return None


async def delete_villa(chat_id: int, vid: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM villas WHERE id=? AND chat_id=?", (vid, chat_id))
        await db.execute(
            "DELETE FROM votes WHERE villa_id=? AND chat_id=?", (vid, chat_id)
        )
        await db.commit()


async def board(chat_id: int):
    """returns (closed, [(id, url, title, [voter names], {properties})] sorted by votes)"""
    async with aiosqlite.connect(DB) as db:
        row = await (
            await db.execute("SELECT closed FROM chats WHERE chat_id=?", (chat_id,))
        ).fetchone()
        villas = await (
            await db.execute(
                "SELECT id,url,title,price,location,rating,ratingCount "
                "FROM villas WHERE chat_id=?",
                (chat_id,),
            )
        ).fetchall()
        votes = await (
            await db.execute(
                "SELECT villa_id,name FROM votes WHERE chat_id=?", (chat_id,)
            )
        ).fetchall()
    names = {}
    for vid, n in votes:
        names.setdefault(vid, []).append(n)
    rows = [
        (
            v,
            u,
            t,
            names.get(v, []),
            {"price": p, "location": loc, "rating": r, "ratingCount": c},
        )
        for v, u, t, p, loc, r, c in villas
    ]
    rows.sort(key=lambda r: (-len(r[3]), r[0]))
    return bool(row and row[0]), rows


async def render(chat_id: int, page: int = 0):
    closed, rows = await board(chat_id)
    kb = InlineKeyboardBuilder()

    if not rows:
        kb.button(text="➕ افزودن ویلا", callback_data=Menu(action="add"))
        kb.button(text="🏠 منو", callback_data=Menu(action="home"))
        kb.adjust(1)
        return "هنوز ویلایی اضافه نشده.", kb.as_markup()

    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    top = len(rows[0][3])
    total = sum(len(r[3]) for r in rows)
    head = "🔒 <b>رأی‌گیری بسته شد</b>" if closed else "🗳 <b>رأی‌گیری ویلا</b>"

    blocks, vote_btns = [], 0
    for vid, url, title, voters, extra in rows[
        page * PAGE_SIZE : (page + 1) * PAGE_SIZE
    ]:
        n = len(voters)
        medal = "🥇 " if n and n == top else "🏡 "
        block = [f"{medal}<b>#{vid}</b> <a href='{esc(url)}'>{esc(title)}</a>"]
        block += [f"   {line}" for line in details_lines(extra)]
        block.append(f"   🗳 <b>{n}</b> رأی")
        if voters:
            block.append("   👤 " + "، ".join(esc(v) for v in voters))
        blocks.append("\n".join(block))
        if not closed:
            kb.button(
                text=f"#{vid} {title[:25]} ({n})",
                callback_data=Vote(villa_id=vid, page=page),
            )
            vote_btns += 1

    nav = 0
    if page > 0:
        kb.button(text="◀️ قبلی", callback_data=Menu(action="board", page=page - 1))
        nav += 1
    if page < pages - 1:
        kb.button(text="بعدی ▶️", callback_data=Menu(action="board", page=page + 1))
        nav += 1
    kb.button(text="🏠 منو", callback_data=Menu(action="home"))
    kb.adjust(*([1] * vote_btns), *([nav] if nav else []), 1)

    out = f"{head}\n\n" + "\n\n".join(blocks) + f"\n\n📊 مجموع رأی‌ها: {total}"
    if pages > 1:
        out += f"  ·  📄 صفحه {page + 1} از {pages}"
    return out, kb.as_markup()


async def safe_edit(msg: Message, text: str, kb=None):
    try:
        await msg.edit_text(
            text, reply_markup=kb, parse_mode=HTML, link_preview_options=NOPREV
        )
    except TelegramBadRequest:
        pass  # message is not modified


async def refresh(msg: Message, page: int = 0):
    text, kb = await render(msg.chat.id, page)
    await safe_edit(msg, text, kb)


async def show_delete_list(msg: Message, user_id: int):
    async with aiosqlite.connect(DB) as db:
        rows = await (
            await db.execute(
                "SELECT id,title,added_by FROM villas WHERE chat_id=? ORDER BY id",
                (msg.chat.id,),
            )
        ).fetchall()
    mine = [r for r in rows if can_delete(user_id, r[2])]
    kb = InlineKeyboardBuilder()
    for vid, title, _ in mine:
        kb.button(text=f"🗑 #{vid} {title[:28]}", callback_data=Del(villa_id=vid))
    kb.button(text="🏠 منو", callback_data=Menu(action="home"))
    kb.adjust(1)
    text = (
        "🗑 کدام ویلا حذف شود؟"
        if mine
        else "ویلایی برای حذف وجود ندارد.\n(فقط ویلاهایی که خودتان اضافه کرده‌اید قابل حذف‌اند.)"
    )
    await safe_edit(msg, text, kb.as_markup())


async def close_poll(chat_id: int) -> str:
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO chats VALUES(?,1) ON CONFLICT(chat_id) DO UPDATE SET closed=1",
            (chat_id,),
        )
        await db.commit()
    _, rows = await board(chat_id)
    top = len(rows[0][3]) if rows else 0
    if top == 0:
        return "🔒 رأی‌گیری بسته شد؛ اما هیچ رأیی ثبت نشده بود."
    w = [r for r in rows if len(r[3]) == top]
    link = lambda r: f"<a href='{esc(r[1])}'>{esc(r[2])}</a>"
    if len(w) == 1:
        lines = [f"🏆 <b>برنده:</b> {link(w[0])}"] + details_lines(
            w[0][4], show_empty=False
        )
        lines.append(f"🗳 با {top} رأی 🎉")
        msg = "\n".join(lines)
    else:
        msg = (
            f"🤝 <b>مساوی!</b> هر کدام {top} رأی:\n"
            + "\n".join(f"• {link(r)}" for r in w)
            + "\n\nبا /open دوباره باز کنید و تصمیم بگیرید."
        )
    return "🔒 رأی‌گیری بسته شد.\n\n" + msg


# ---------- commands ----------
@router.message(Command("start", "menu"))
async def menu(m: Message):
    admin = await is_admin(m.bot, m.chat.id, m.from_user.id)
    await m.answer(MENU_TEXT, reply_markup=main_menu(admin), parse_mode=HTML)


@router.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(HELP, parse_mode=HTML)


@router.message(Command("add"))
async def add(m: Message, command: CommandObject):
    args = command.args or ""
    mt = URL_RE.search(args)
    if not mt:
        return await m.reply("استفاده: /add لینک [نام ویلا]")
    url = mt.group()
    title = (
        args.replace(url, "").strip()
        or await fetch_title(url)
        or urlparse(url).hostname
        or url[:40]
    )
    vid = await add_villa(m.chat.id, m.from_user.id, url, title)
    if not vid:
        return await m.reply("⚠️ این لینک قبلاً اضافه شده.")
    await m.reply(
        f"✅ ویلا <b>#{vid}</b> اضافه شد: {esc(title)}\n"
        f"برای ثبت مشخصات: /set {vid} price 4,500,000\nبرای رأی دادن: /villas",
        parse_mode=HTML,
        link_preview_options=NOPREV,
    )


@router.message(Command("set"))
async def set_field(m: Message, command: CommandObject):
    parts = (command.args or "").split(maxsplit=2)
    field = ALIASES.get(parts[1].lower()) if len(parts) > 1 else None
    if len(parts) < 3 or not parts[0].isdigit() or not field:
        return await m.reply(
            "استفاده: /set شماره price|location|rating|ratingCount مقدار\n"
            "مثال: /set 3 rating 4.8\n(پاک کردن مقدار: -)"
        )
    raw = parts[2].strip()
    if raw == "-":
        value = None
    elif field == "rating":
        value = norm_num(raw, decimal=True)
    elif field == "ratingCount":
        value = norm_num(raw, decimal=False)
    else:
        value = raw
    if raw != "-" and value is None:
        return await m.reply("❌ عدد معتبر نیست.")
    async with aiosqlite.connect(DB) as db:  # field is whitelisted -> f-string is safe
        cur = await db.execute(
            f"UPDATE villas SET {field}=? WHERE id=? AND chat_id=?",
            (value, int(parts[0]), m.chat.id),
        )
        await db.commit()
    await m.reply("✅ ثبت شد" if cur.rowcount else "ویلا پیدا نشد.")


@router.message(Command("villas", "vote"))
async def show(m: Message):
    text, kb = await render(m.chat.id)
    await m.answer(text, reply_markup=kb, parse_mode=HTML, link_preview_options=NOPREV)


@router.message(Command("remove"))
async def remove(m: Message, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        return await m.reply("استفاده: /remove شماره")
    vid = int(command.args)
    async with aiosqlite.connect(DB) as db:
        row = await (
            await db.execute(
                "SELECT added_by FROM villas WHERE id=? AND chat_id=?", (vid, m.chat.id)
            )
        ).fetchone()
    if not row:
        return await m.reply("چنین ویلایی پیدا نشد.")
    if not can_delete(m.from_user.id, row[0]):
        return await m.reply("⛔ فقط اضافه‌کننده یا صاحب ربات می‌تواند حذف کند.")
    await delete_villa(m.chat.id, vid)
    await m.reply(f"🗑 ویلا #{vid} حذف شد.")


@router.message(Command("close"))
async def close(m: Message):
    if not await is_admin(m.bot, m.chat.id, m.from_user.id):
        return await m.reply("⛔ فقط ادمین‌ها می‌توانند رأی‌گیری را ببندند.")
    await m.answer(
        await close_poll(m.chat.id), parse_mode=HTML, link_preview_options=NOPREV
    )


@router.message(Command("open"))
async def reopen(m: Message):
    if not await is_admin(m.bot, m.chat.id, m.from_user.id):
        return await m.reply("⛔ فقط ادمین‌ها.")
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO chats VALUES(?,0) ON CONFLICT(chat_id) DO UPDATE SET closed=0",
            (m.chat.id,),
        )
        await db.commit()
    await m.answer("🔓 رأی‌گیری دوباره باز شد. /villas")


@router.message(Command("reset"))
async def reset(m: Message):
    if not await is_admin(m.bot, m.chat.id, m.from_user.id):
        return await m.reply("⛔ فقط ادمین‌ها.")
    async with aiosqlite.connect(DB) as db:
        for t in ("villas", "votes", "chats"):
            await db.execute(f"DELETE FROM {t} WHERE chat_id=?", (m.chat.id,))
        await db.commit()
    await m.answer("♻️ همه چیز پاک شد.")


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.reply("❌ لغو شد.")


# ---------- callbacks ----------
@router.callback_query(Vote.filter())
async def vote(cb: CallbackQuery, callback_data: Vote):
    vid, uid, cid = callback_data.villa_id, cb.from_user.id, cb.message.chat.id
    async with aiosqlite.connect(DB) as db:
        closed = await (
            await db.execute("SELECT closed FROM chats WHERE chat_id=?", (cid,))
        ).fetchone()
        if closed and closed[0]:
            return await cb.answer("رأی‌گیری بسته شده است 🔒", show_alert=True)
        if not await (
            await db.execute(
                "SELECT 1 FROM villas WHERE id=? AND chat_id=?", (vid, cid)
            )
        ).fetchone():
            await cb.answer("این ویلا دیگر وجود ندارد", show_alert=True)
            return await refresh(cb.message, callback_data.page)

        exists = await (
            await db.execute(
                "SELECT 1 FROM votes WHERE chat_id=? AND user_id=? AND villa_id=?",
                (cid, uid, vid),
            )
        ).fetchone()
        if exists:  # tap again -> remove this vote
            await db.execute(
                "DELETE FROM votes WHERE chat_id=? AND user_id=? AND villa_id=?",
                (cid, uid, vid),
            )
            note = f"رأی شما برای ویلا #{vid} پس گرفته شد"
        else:
            if MAX_VOTES:
                count = (
                    await (
                        await db.execute(
                            "SELECT COUNT(*) FROM votes WHERE chat_id=? AND user_id=?",
                            (cid, uid),
                        )
                    ).fetchone()
                )[0]
                if count >= MAX_VOTES:
                    return await cb.answer(
                        f"حداکثر {MAX_VOTES} رأی می‌توانید بدهید. "
                        "ابتدا یکی از رأی‌هایتان را پس بگیرید.",
                        show_alert=True,
                    )
            await db.execute(
                "INSERT INTO votes VALUES(?,?,?,?)",
                (cid, uid, vid, cb.from_user.full_name),
            )
            note = f"به ویلا #{vid} رأی دادید ✅"
        await db.commit()
    await refresh(cb.message, callback_data.page)
    await cb.answer(note)


@router.callback_query(Del.filter())
async def delete_cb(cb: CallbackQuery, callback_data: Del):
    msg, vid = cb.message, callback_data.villa_id
    async with aiosqlite.connect(DB) as db:
        row = await (
            await db.execute(
                "SELECT title,added_by FROM villas WHERE id=? AND chat_id=?",
                (vid, msg.chat.id),
            )
        ).fetchone()
    if not row:
        await cb.answer("این ویلا دیگر وجود ندارد", show_alert=True)
        return await show_delete_list(msg, cb.from_user.id)
    title, added_by = row
    if not can_delete(cb.from_user.id, added_by):
        return await cb.answer(
            "⛔ فقط اضافه‌کننده یا صاحب ربات می‌تواند حذف کند.", show_alert=True
        )

    if callback_data.step == 0:  # ask for confirmation
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ بله، حذف شود", callback_data=Del(villa_id=vid, step=1))
        kb.button(text="↩️ انصراف", callback_data=Menu(action="delete"))
        kb.adjust(1)
        await safe_edit(
            msg, f"⚠️ ویلا <b>#{vid}</b> «{esc(title)}» حذف شود؟", kb.as_markup()
        )
        return await cb.answer()

    await delete_villa(msg.chat.id, vid)
    await cb.answer(f"🗑 ویلا #{vid} حذف شد")
    await show_delete_list(msg, cb.from_user.id)


@router.callback_query(Menu.filter())
async def menu_cb(cb: CallbackQuery, callback_data: Menu, state: FSMContext):
    msg, a = cb.message, callback_data.action
    admin = await is_admin(cb.bot, msg.chat.id, cb.from_user.id)

    if a == "home":
        await state.clear()
        await safe_edit(msg, MENU_TEXT, main_menu(admin))
    elif a == "board":
        await refresh(msg, callback_data.page)
    elif a == "delete":
        await show_delete_list(msg, cb.from_user.id)
    elif a == "help":
        await safe_edit(msg, HELP, back_kb())
    elif a == "add":
        await state.set_state(AddVilla.link)
        mention = f"<a href='tg://user?id={cb.from_user.id}'>{esc(cb.from_user.full_name)}</a>"
        await msg.answer(
            f"{mention}\n🔗 لینک ویلا را <b>در پاسخ به همین پیام</b> بفرستید.\n(لغو: /cancel)",
            reply_markup=ForceReply(selective=True),
            parse_mode=HTML,
        )
    elif a == "close":
        if not admin:
            return await cb.answer("⛔ فقط ادمین‌ها", show_alert=True)
        await msg.answer(
            await close_poll(msg.chat.id), parse_mode=HTML, link_preview_options=NOPREV
        )
    await cb.answer()


# ---------- add-villa flow (replies to the bot, so privacy mode ON is fine) ----------
# link -> name -> price -> location -> rating -> ratingCount   ("-" skips a step)
TXT = F.text & ~F.text.startswith("/")
ASK = ForceReply(selective=True)


def clean(t: str):
    t = t.strip()
    return None if t == "-" else t


async def finish_add(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    url = d["url"]
    title = (
        d.get("name") or await fetch_title(url) or urlparse(url).hostname or url[:40]
    )
    vid = await add_villa(
        m.chat.id, m.from_user.id, url, title, **{k: d.get(k) for k in SETTABLE}
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🗳 رأی‌گیری", callback_data=Menu(action="board"))
    await m.reply(
        f"✅ ویلا <b>#{vid}</b> اضافه شد: {esc(title)}"
        if vid
        else "⚠️ این لینک قبلاً اضافه شده.",
        reply_markup=kb.as_markup(),
        parse_mode=HTML,
    )


@router.message(AddVilla.link, TXT)
async def got_link(m: Message, state: FSMContext):
    mt = URL_RE.search(m.text)
    if not mt:
        return await m.reply(
            "❌ لینک معتبر نیست. دوباره بفرستید (یا /cancel)", reply_markup=ASK
        )
    await state.update_data(url=mt.group())
    await state.set_state(AddVilla.name)
    await m.reply("🏷 نام ویلا؟\n(انتخاب خودکار از روی لینک: -)", reply_markup=ASK)


@router.message(AddVilla.name, TXT)
async def got_name(m: Message, state: FSMContext):
    await state.update_data(name=clean(m.text))
    await state.set_state(AddVilla.price)
    await m.reply("💰 قیمت؟ (مثلاً 4,500,000 — رد کردن: -)", reply_markup=ASK)


@router.message(AddVilla.price, TXT)
async def got_price(m: Message, state: FSMContext):
    await state.update_data(price=clean(m.text))
    await state.set_state(AddVilla.location)
    await m.reply("📍 موقعیت یا شهر؟ (رد کردن: -)", reply_markup=ASK)


@router.message(AddVilla.location, TXT)
async def got_location(m: Message, state: FSMContext):
    await state.update_data(location=clean(m.text))
    await state.set_state(AddVilla.rating)
    await m.reply("⭐ امتیاز؟ (مثلاً 4.8 — رد کردن: -)", reply_markup=ASK)


@router.message(AddVilla.rating, TXT)
async def got_rating(m: Message, state: FSMContext):
    if clean(m.text) is None:  # no rating -> no need to ask for the count
        return await finish_add(m, state)
    rating = norm_num(m.text, decimal=True)
    if rating is None:
        return await m.reply(
            "❌ عدد معتبر نیست (مثلاً 4.8). دوباره بفرستید:", reply_markup=ASK
        )
    await state.update_data(rating=rating)
    await state.set_state(AddVilla.rating_count)
    await m.reply("💬 تعداد نظرات؟ (مثلاً 5 — رد کردن: -)", reply_markup=ASK)


@router.message(AddVilla.rating_count, TXT)
async def got_rating_count(m: Message, state: FSMContext):
    if clean(m.text) is not None:
        count = norm_num(m.text, decimal=False)
        if count is None:
            return await m.reply(
                "❌ عدد معتبر نیست (مثلاً 5). دوباره بفرستید:", reply_markup=ASK
            )
        await state.update_data(ratingCount=count)
    await finish_add(m, state)


# ---------- optional: auto-detect pasted links (keep AFTER the FSM handlers) ----------
async def offer(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ افزودن به لیست ویلاها", callback_data="a")
    await m.reply("این لینک ویلاست؟", reply_markup=kb.as_markup())


@router.callback_query(F.data == "a")
async def auto_add(cb: CallbackQuery):
    src = cb.message.reply_to_message
    mt = src and src.text and URL_RE.search(src.text)
    if not mt:
        return await cb.answer("لینک پیدا نشد", show_alert=True)
    url = mt.group()
    title = await fetch_title(url) or urlparse(url).hostname or url[:40]
    vid = await add_villa(cb.message.chat.id, src.from_user.id, url, title)
    await cb.message.edit_text(
        f"✅ ویلا #{vid} اضافه شد: {esc(title)}\nبرای ثبت مشخصات: /set {vid} price 4,500,000"
        if vid
        else "⚠️ این لینک قبلاً اضافه شده.",
        parse_mode=HTML,
    )
    await cb.answer()


if AUTO:
    router.message.register(offer, F.text.regexp(URL_RE), ~F.text.startswith("/"))


# ---------- main ----------
async def main():
    await init_db()
    if not OWNER_ID:
        print(
            "⚠️  OWNER_ID is not set in .env: only the user who added a villa can delete it."
        )
    bot = Bot(TOKEN, session=AiohttpSession(proxy=PROXY) if PROXY else None)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="menu", description="🏠 منوی اصلی"),
            BotCommand(command="villas", description="🗳 لیست و رأی‌گیری"),
            BotCommand(command="add", description="➕ افزودن سریع ویلا"),
            BotCommand(command="set", description="✏️ ویرایش مشخصات ویلا"),
            BotCommand(command="cancel", description="❌ لغو"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
