import os
import random
from datetime import datetime, timezone

import asyncio
import json
from aiohttp import web

import discord
from discord.ext import commands, tasks


# ---------- CONFIG ----------
GUILD_ID = 893504850699116544
ANNOUNCE_CHANNEL_ID = 1446118933278101535

DEVOTED_ROLE_ID = 1446104532638761071
KEEPER_ROLE_ID = 893506590567694356
CULT_LEADER_ROLE_ID = 907661878308786257

# Arcane-matching XP rules (from your screenshot)
MESSAGE_XP_MIN = 10
MESSAGE_XP_MAX = 15
MESSAGE_COOLDOWN = 60          # seconds

VOICE_XP_MIN = 4
VOICE_XP_MAX = 8
VOICE_COOLDOWN = 300           # seconds
VOICE_MIN_MEMBERS = 3

REACTION_XP_MIN = 4
REACTION_XP_MAX = 8
REACTION_COOLDOWN = 300        # seconds

# Weekly roll time (UTC) – Friday 19:00
WEEKLY_ROLL_DAY = 4   # 0=Mon ... 4=Fri
WEEKLY_ROLL_HOUR = 19 # 19:00 UTC

# Optional: occasional random reactions (set 0.0 to disable)
RANDOM_REACTION_CHANCE = 0.0
RANDOM_REACTION_EMOJIS = ["🌲"]


# ---------- BOT SETUP ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True
intents.voice_states = True  # REQUIRED for voice tracking

bot = commands.Bot(command_prefix="!", intents=intents)

# user_id -> xp this week
weekly_xp: dict[int, int] = {}

# cooldown trackers
last_message_xp: dict[int, datetime] = {}
last_reaction_xp: dict[int, datetime] = {}
last_receive_xp: dict[int, datetime] = {}

# voice cooldown tracker
voice_last_award: dict[int, datetime] = {}


# ---------- PERSISTENCE ----------
DATA_FILE = "weekly_xp.json"


def load_weekly_xp():
    """Load weekly_xp from disk if present."""
    global weekly_xp
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        weekly_xp = {int(k): int(v) for k, v in raw.items()}
        print(f"Loaded weekly XP for {len(weekly_xp)} users.")
    except FileNotFoundError:
        print("No existing weekly XP file, starting fresh.")
    except Exception as e:
        print(f"Failed to load weekly XP: {e}")


def save_weekly_xp():
    """Persist weekly_xp to disk."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(weekly_xp, f)
    except Exception as e:
        print(f"Failed to save weekly XP: {e}")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_leadership(member: discord.Member) -> bool:
    role_ids = {r.id for r in member.roles}
    return KEEPER_ROLE_ID in role_ids or CULT_LEADER_ROLE_ID in role_ids


def add_xp(user_id: int, amount: int):
    weekly_xp[user_id] = weekly_xp.get(user_id, 0) + amount
    save_weekly_xp()


def reset_week():
    weekly_xp.clear()
    last_message_xp.clear()
    last_reaction_xp.clear()
    last_receive_xp.clear()
    voice_last_award.clear()
    save_weekly_xp()


def voice_member_eligible(member: discord.Member, guild: discord.Guild) -> bool:
    """Eligible for voice XP: in a voice channel, not muted/deafened, not AFK, not bot."""
    if member.bot:
        return False

    vs = member.voice
    if vs is None or vs.channel is None:
        return False

    # Anti-AFK: ignore AFK channel
    if guild.afk_channel and vs.channel.id == guild.afk_channel.id:
        return False

    # If muted/deafened in any way, do not award
    if vs.self_mute or vs.self_deaf:
        return False
    if vs.mute or vs.deaf:  # server mute/deaf
        return False

    return True


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    load_weekly_xp()

    # Start loops once
    if not weekly_devoted_roll.is_running():
        weekly_devoted_roll.start()
    if not voice_xp_tick.is_running():
        voice_xp_tick.start()


@bot.event
async def on_message(message: discord.Message):
    # ignore DMs, bots, other guilds
    if message.guild is None or message.guild.id != GUILD_ID:
        return
    if message.author.bot:
        return

    user_id = message.author.id
    now = now_utc()
    last = last_message_xp.get(user_id)

    if last is None or (now - last).total_seconds() >= MESSAGE_COOLDOWN:
        add_xp(user_id, random.randint(MESSAGE_XP_MIN, MESSAGE_XP_MAX))
        last_message_xp[user_id] = now

    # Optional random reaction "forest wink"
    if RANDOM_REACTION_CHANCE > 0:
        try:
            if random.random() < RANDOM_REACTION_CHANCE:
                emoji = random.choice(RANDOM_REACTION_EMOJIS)
                await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    await bot.process_commands(message)


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.abc.User):
    if user.bot:
        return

    message = reaction.message
    if message.guild is None or message.guild.id != GUILD_ID:
        return

    now = now_utc()

    # Reactor XP
    reactor_id = user.id
    last_r = last_reaction_xp.get(reactor_id)
    if last_r is None or (now - last_r).total_seconds() >= REACTION_COOLDOWN:
        add_xp(reactor_id, random.randint(REACTION_XP_MIN, REACTION_XP_MAX))
        last_reaction_xp[reactor_id] = now

    # Message author XP (receiver)
    if not message.author.bot:
        receiver_id = message.author.id
        last_rec = last_receive_xp.get(receiver_id)
        if last_rec is None or (now - last_rec).total_seconds() >= REACTION_COOLDOWN:
            add_xp(receiver_id, random.randint(REACTION_XP_MIN, REACTION_XP_MAX))
            last_receive_xp[receiver_id] = now


@tasks.loop(seconds=30)
async def voice_xp_tick():
    """Every 30s, award voice XP if eligible and VOICE_COOLDOWN has elapsed."""
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    now = now_utc()

    # Build eligible members per voice channel
    channel_to_members: dict[int, list[discord.Member]] = {}

    for vc in guild.voice_channels:
        eligible = [m for m in vc.members if voice_member_eligible(m, guild)]
        if eligible:
            channel_to_members[vc.id] = eligible

    # Award only in channels meeting minimum eligible members
    for members in channel_to_members.values():
        if len(members) < VOICE_MIN_MEMBERS:
            continue

        for member in members:
            last = voice_last_award.get(member.id)
            if last is None or (now - last).total_seconds() >= VOICE_COOLDOWN:
                add_xp(member.id, random.randint(VOICE_XP_MIN, VOICE_XP_MAX))
                voice_last_award[member.id] = now


@tasks.loop(minutes=5)
async def weekly_devoted_roll():
    """Run every 5 minutes; when it's Friday ~19:00 UTC, pick a winner."""
    now = now_utc()
    if not (now.weekday() == WEEKLY_ROLL_DAY and now.hour == WEEKLY_ROLL_HOUR and now.minute < 5):
        return

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print("Guild not found")
        return

    devoted_role = guild.get_role(DEVOTED_ROLE_ID)
    if devoted_role is None:
        print("Devoted role not found")
        return

    announce_channel = guild.get_channel(ANNOUNCE_CHANNEL_ID)
    if not isinstance(announce_channel, discord.TextChannel):
        print("Announce channel not found")
        return

    # Build candidate list: in guild, not bot, not leadership, xp > 0
    candidates: list[tuple[discord.Member, int]] = []
    for user_id, xp in weekly_xp.items():
        member = guild.get_member(user_id)
        if member is None or member.bot:
            continue
        if is_leadership(member):
            continue
        if xp <= 0:
            continue
        candidates.append((member, xp))

    if not candidates:
        await announce_channel.send("💠 No eligible walkers gained XP this week. The Path watches in silence.")
        reset_week()
        return

    # Sort by XP desc, take top 3
    candidates.sort(key=lambda t: t[1], reverse=True)
    top_three = candidates[:3]

    # Randomly choose one of the top three
    winner, winner_xp = random.choice(top_three)

    # Remove Devoted from everyone who has it
    for member in guild.members:
        if devoted_role in member.roles:
            try:
                await member.remove_roles(devoted_role, reason="Weekly Devoted rotation")
            except discord.HTTPException:
                pass

    # Give Devoted to winner
    try:
        await winner.add_roles(devoted_role, reason="Weekly Devoted winner")
    except discord.HTTPException:
        pass

    lines = [f"- {m.mention} — **{xp} XP**" for m, xp in top_three]

    announcement = (
        "💠 **The Bestower has rolled the bones, The Devoted has been chosen.**\n\n"
        "From this week’s three most devoted walkers, one name was drawn.\n"
        f"{winner.mention} is now **The Devoted**.\n"
        f"They walked with **{winner_xp} XP** worth of footsteps this week.\n\n"
        "__This week’s contenders:__\n"
        + "\n".join(lines)
        + "\n\n"
        "_When the next Friday falls, the bones will be rolled again._"
    )

    await announce_channel.send(announcement)
    reset_week()


@weekly_devoted_roll.before_loop
async def before_weekly_roll():
    await bot.wait_until_ready()


async def health(request):
    return web.Response(text="OK")


async def main():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set")

    # Tiny web server for Render
    port = int(os.getenv("PORT", "10000"))
    app = web.Application()
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    # Start Discord bot
    await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
