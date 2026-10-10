import os
import re
import io
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands
from PIL import Image

try:
    import pytesseract
    OCR_AVAILABLE = True
except Exception:
    pytesseract = None
    OCR_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bonus-bot")

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None
APPROVAL_CHANNEL_ID = 1525131055412154368
EXTRA_CHANNEL_ID = 1548222840023678976
APPROVER_ROLE_ID = 1525107899381186709

EVENTS = {
    1525477519451619349: ("State-Win", 50_000),
    1525134079605997678: ("Informal-Win", 50_000),
    1525131417854410772: ("Weapon-Factory", 50_000),
    1525131463492636714: ("Foundry", 50_000),
    1525131518039818350: ("Biz-War", 50_000),
    1525131566802665552: ("Rating-Battle", 50_000),
}
DB_PATH = os.getenv("DATABASE_PATH", "bonus_bot.sqlite3")
MAX_IMAGE_BYTES = 8 * 1024 * 1024

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL UNIQUE,
        author_id INTEGER NOT NULL,
        event_name TEXT NOT NULL,
        rate INTEGER NOT NULL,
        custom_rate INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        parsed_json TEXT NOT NULL,
        ocr_text TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        approved_by INTEGER,
        approval_message_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS rate_approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        event_name TEXT NOT NULL,
        requested_rate INTEGER NOT NULL,
        parsed_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        decided_by INTEGER,
        UNIQUE(message_id)
    );
    """)
    db.commit()

def fmt_money(amount: int) -> str:
    if amount >= 1_000_000:
        val = amount / 1_000_000
        return f"{val:g}m"
    if amount >= 1_000:
        val = amount / 1_000
        return f"{val:g}k"
    return str(amount)

def parse_rate(text: str) -> Optional[int]:
    # Only treat an explicitly stated "per kill" rate as a custom rate.
    m = re.search(r'(?i)(\d+(?:\.\d+)?)\s*([km])?\s*(?:per\s*kill|/\s*kill|each\s*kill)', text)
    if not m:
        return None
    value = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)

def parse_entries(message: discord.Message) -> list[dict]:
    """
    Accepts lines like:
      @DiscordUser 2k
      @DiscordUser 1 kill
    The number after each mention is interpreted as kill count, not money.
    A "200k per kill" declaration is handled separately as a rate request.
    """
    text = message.content or ""
    entries = []
    seen = set()
    # Mentions are authoritative and avoid accidentally paying arbitrary names.
    for mention in message.mentions:
        if mention.id in seen:
            continue
        seen.add(mention.id)
        # Find the mention's raw token in content, then parse a nearby count.
        patterns = [
            rf'<@!?{mention.id}>\s*[:\-]?\s*(\d+)\s*k\b',
            rf'<@!?{mention.id}>\s*[:\-]?\s*(\d+)\s*(?:kills?|ks?)\b',
        ]
        kills = None
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                kills = int(m.group(1))
                break
        if kills is None:
            # Discord can render a mention in content as a token; allow a count
            # after the mention even if punctuation/formatting intervenes.
            token = rf'<@!?{mention.id}>'
            m = re.search(token + r'[^\n]{0,20}?(\d+)\s*k\b', text, re.IGNORECASE)
            if m:
                kills = int(m.group(1))
        if kills is not None and 0 <= kills <= 100:
            entries.append({
                "user_id": mention.id,
                "display": mention.display_name,
                "kills": kills,
            })
    return entries

def get_images(message: discord.Message):
    return [a for a in message.attachments if (a.content_type or "").startswith("image/") or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]

async def run_ocr(attachment: discord.Attachment) -> str:
    if not OCR_AVAILABLE:
        return ""
    if attachment.size > MAX_IMAGE_BYTES:
        return "[OCR skipped: image exceeds 8 MB]"
    try:
        raw = await attachment.read()
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        # Screenshots are often dark/tinted; Tesseract can still provide a useful
        # candidate transcription, but it is never used to auto-pay by itself.
        text = pytesseract.image_to_string(image, config="--psm 6")
        return text[:5000].strip()
    except Exception as exc:
        log.warning("OCR failed: %s", exc)
        return ""

def money_line(event_name: str, rate: int, entries: list[dict]) -> str:
    if not entries:
        return "No member kill counts were parsed. Please add lines like `@member 2k` (meaning 2 kills) or ask staff to verify the screenshot."
    lines = [f"**{event_name} — bonus calculation**", f"Rate: **{fmt_money(rate)} per kill**"]
    total = 0
    for e in entries:
        amount = e["kills"] * rate
        total += amount
        lines.append(f"<@{e['user_id']}> — {e['kills']} kill(s) × {fmt_money(rate)} = **{fmt_money(amount)}**")
    lines.append(f"**Total: {fmt_money(total)}**")
    return "\n".join(lines)

def create_submission(message, event_name, rate, custom_rate, entries, ocr_text, status="pending"):
    cur = db.execute("""
        INSERT INTO submissions
        (guild_id, channel_id, message_id, author_id, event_name, rate, custom_rate,
         status, parsed_json, ocr_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        message.guild.id, message.channel.id, message.id, message.author.id,
        event_name, rate, int(custom_rate), status,
        json.dumps(entries), ocr_text, datetime.now(timezone.utc).isoformat()
    ))
    db.commit()
    return cur.lastrowid

class RateApprovalView(discord.ui.View):
    def __init__(self, approval_id: int):
        super().__init__(timeout=None)
        self.approval_id = approval_id

        approve_button = discord.ui.Button(
            label="Approve custom rate",
            style=discord.ButtonStyle.success,
            custom_id=f"bonus:approve:{approval_id}"
        )
        reject_button = discord.ui.Button(
            label="Reject custom rate",
            style=discord.ButtonStyle.danger,
            custom_id=f"bonus:reject:{approval_id}"
        )
        approve_button.callback = self._approve_callback
        reject_button.callback = self._reject_callback
        self.add_item(approve_button)
        self.add_item(reject_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
        if APPROVER_ROLE_ID not in role_ids and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "You do not have permission to approve custom bonus rates.", ephemeral=True
            )
            return False
        return True

    async def _approve_callback(self, interaction: discord.Interaction):
        await decide_rate(interaction, self.approval_id, True)

    async def _reject_callback(self, interaction: discord.Interaction):
        await decide_rate(interaction, self.approval_id, False)

async def decide_rate(interaction: discord.Interaction, approval_id: int, approved: bool):
    row = db.execute("SELECT * FROM rate_approvals WHERE id=?", (approval_id,)).fetchone()
    if not row:
        await interaction.response.send_message("Approval request not found.", ephemeral=True)
        return
    if row["status"] != "pending":
        await interaction.response.send_message(f"This request is already {row['status']}.", ephemeral=True)
        return
    status = "approved" if approved else "rejected"
    db.execute("UPDATE rate_approvals SET status=?, decided_by=? WHERE id=?",
               (status, interaction.user.id, approval_id))
    db.commit()
    event_channel = bot.get_channel(row["channel_id"])
    entries = json.loads(row["parsed_json"])
    rate = row["requested_rate"] if approved else EVENTS.get(row["channel_id"], (row["event_name"], 50_000))[1]
    result = money_line(row["event_name"], rate, entries)
    if approved:
        result = "✅ **Custom rate approved**\n" + result
    else:
        result = "❌ **Custom rate rejected. Default rate used.**\n" + result
    if event_channel:
        try:
            await event_channel.send(result, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.HTTPException:
            log.exception("Could not send calculation to event channel")
    for item in interaction.message.embeds:
        item.color = discord.Color.green() if approved else discord.Color.red()
        item.add_field(name="Decision", value=f"{status.title()} by {interaction.user.mention}", inline=False)
    for child in interaction.message.components:
        pass
    await interaction.response.edit_message(
        content=f"Request **{status}** by {interaction.user.mention}. The result was sent to the event channel.",
        embed=interaction.message.embeds[0] if interaction.message.embeds else None,
        view=None
    )

async def create_rate_request(message, event_name, requested_rate, entries, ocr_text):
    cur = db.execute("""
        INSERT OR IGNORE INTO rate_approvals
        (guild_id, channel_id, message_id, author_id, event_name, requested_rate,
         parsed_json, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (
        message.guild.id, message.channel.id, message.id, message.author.id,
        event_name, requested_rate, json.dumps(entries),
        datetime.now(timezone.utc).isoformat()
    ))
    db.commit()
    row = db.execute("SELECT id FROM rate_approvals WHERE message_id=?", (message.id,)).fetchone()
    approval_id = row["id"]
    approval_channel = bot.get_channel(APPROVAL_CHANNEL_ID)
    if approval_channel is None:
        try:
            approval_channel = await bot.fetch_channel(APPROVAL_CHANNEL_ID)
        except discord.HTTPException:
            approval_channel = None
    if approval_channel is None:
        await message.reply("⚠️ Custom rate detected, but I cannot access the configured approval channel. Please contact an administrator.")
        return
    embed = discord.Embed(
        title="Custom bonus rate approval",
        description=f"**Event:** {event_name}\n**Requested rate:** {fmt_money(requested_rate)} per kill\n**Submitted by:** {message.author.mention}\n**Source:** {message.jump_url}",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Parsed member counts", value=money_line(event_name, requested_rate, entries)[:1024], inline=False)
    if ocr_text:
        embed.add_field(name="Screenshot OCR (review only)", value=ocr_text[:1024], inline=False)
    embed.set_footer(text=f"Approval ID: {approval_id} • Only the configured approver role can decide")
    view = RateApprovalView(approval_id)
    approval_msg = await approval_channel.send(
        content=f"<@&{APPROVER_ROLE_ID}> Custom rate needs review.",
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False)
    )
    await message.reply(f"⏳ Your custom rate of **{fmt_money(requested_rate)} per kill** was sent for approval in <#{APPROVAL_CHANNEL_ID}>. Bonuses will be calculated after a decision.")

@bot.event
async def on_ready():
    init_db()
    # Re-register persistent views for pending requests after a restart.
    pending_rows = db.execute(
        "SELECT id FROM rate_approvals WHERE status='pending'"
    ).fetchall()
    for row in pending_rows:
        bot.add_view(RateApprovalView(row["id"]))
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)
    if message.guild is None or message.channel.id not in EVENTS:
        return
    event_name, default_rate = EVENTS[message.channel.id]
    images = get_images(message)
    if not images:
        return

    text = message.content or ""
    entries = parse_entries(message)
    requested_rate = parse_rate(text)
    ocr_text = ""
    # OCR helps staff inspect a screenshot, but never automatically invents
    # Discord-to-game identity matches or awards bonuses without a clear count.
    for attachment in images[:3]:
        found = await run_ocr(attachment)
        if found:
            ocr_text += (("\n--- image ---\n" if ocr_text else "") + found)
    if requested_rate is not None and requested_rate != default_rate:
        await create_rate_request(message, event_name, requested_rate, entries, ocr_text)
        return

    create_submission(message, event_name, default_rate, False, entries, ocr_text, "processed")
    if entries:
        await message.reply(
            money_line(event_name, default_rate, entries),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )
    else:
        review_channel = bot.get_channel(EXTRA_CHANNEL_ID)
        if review_channel is None:
            try:
                review_channel = await bot.fetch_channel(EXTRA_CHANNEL_ID)
            except discord.HTTPException:
                review_channel = None
        notice = (
            f"🟡 **Manual bonus verification needed**\n"
            f"Event: **{event_name}** • Default rate: **{fmt_money(default_rate)} per kill**\n"
            f"Submitted by: {message.author.mention}\n"
            f"Screenshot: {message.jump_url}\n"
            "I could not safely match screenshot leaderboard names to Discord members. "
            "Please verify the kill counts and post the member mentions with counts in the event channel, e.g. `@member 2k`."
        )
        if ocr_text:
            notice += "\n\n**OCR text (unverified):**\n```text\n" + ocr_text[:2500] + "\n```"
        if review_channel:
            await review_channel.send(notice, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        await message.reply(
            "📷 Screenshot received. I couldn't reliably match the game leaderboard to Discord members, so I sent it for manual verification. Add member mentions with counts like `@member 2k` to calculate automatically."
        )

@bot.command(name="bonushelp")
async def bonushelp(ctx):
    await ctx.send(
        "**Bonus Bot Help**\n"
        "• Post a screenshot in an event channel.\n"
        "• Include member mentions and kill counts, e.g. `@member 2k` (2 kills).\n"
        "• Default rate is 50k per kill for all configured events.\n"
        "• To request a different rate, write it explicitly, e.g. `200k per kill`, with the screenshot. "
        "The request must be approved by the configured approver role.\n"
        "• OCR is advisory only; unreadable/mismatched screenshots go for manual verification."
    )

@bot.command(name="bonusstatus")
@commands.has_guild_permissions(administrator=True)
async def bonusstatus(ctx):
    pending = db.execute("SELECT COUNT(*) AS n FROM rate_approvals WHERE status='pending'").fetchone()["n"]
    await ctx.send(f"Pending custom-rate approvals: **{pending}**")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need administrator permissions to use that command.")
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        log.error("Command error", exc_info=error)

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

bot.run(TOKEN)
