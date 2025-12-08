import os
import random
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

# ---------- CONFIG ----------
# REPLACE THESE WITH YOUR REAL IDS

GUILD_ID = 893504850699116544          # your server ID
ANNOUNCE_CHANNEL_ID = 1446118933278101535  # channel where winner is announced

DEVOTED_ROLE_ID = 1446104532638761071       # @The Devoted
KEEPER_ROLE_ID = 893506590567694356        # @Grimwood Keeper
CULT_LEADER_ROLE_ID = 907661878308786257   # @Cult Leader

# Arcane-matching XP rules
MESSAGE_XP = 10
MESSAGE_COOLDOWN = 90          # seconds

REACTION_XP = 4
REACTION_COOLDOWN = 300        # seconds

# Weekly roll time (UTC) – Friday 19:00
WEEKLY_ROLL_DAY = 4            # 0=Mon ... 4=Fri
WEEKLY_ROLL_HOUR = 19          # 19:00 UTC


# ---------- BOT SETUP ----------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# user_id -> xp this week
weekly_xp: dict[int, int] = {}
# cooldown trackers
last_message_xp: dict[int, datetime] = {}
last_reaction_xp: dict[int, datetime] = {}
last_receive_xp: dict[int, datetime] = {}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_leadership(member: discord.Member) -> bool:
    role_ids = {r.id for r in member.roles}
    return KEEPER_ROLE_ID in role_ids or CULT_LEADER_ROLE_ID in role_ids


def add_xp(user_id: int, amount: int):
    weekly_xp[user_id] = weekly_xp.get(user_id, 0) + amount


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    weekly_devoted_roll.start()


@bot.event
async def on_message(message: discord.Message):
    # ignore DMs, bots, other guilds
    if message.author.bot or message.guild is None or message.guild.id != GUILD_ID:
        return

    user_id = message.author.id
    now = now_utc()
    last = last_message_xp.get(user_id)

    if last is None or (now - last).total_seconds() >= MESSAGE_COOLDOWN:
        add_xp(user_id, MESSAGE_XP)
        last_message_xp[user_id] = now

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
        add_xp(reactor_id, REACTION_XP)
        last_reaction_xp[reactor_id] = now

    # Message author XP (receiver)
    if not message.author.bot:
        receiver_id = message.author.id
        last_rec = last_receive_xp.get(receiver_id)
        if last_rec is None or (now - last_rec).total_seconds() >= REACTION_COOLDOWN:
            add_xp(receiver_id, REACTION_XP)
            last_receive_xp[receiver_id] = now


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
        await announce_channel.send(
            "💠 No eligible walkers gained XP this week. The Path watches in silence."
        )
        reset_week()
        return

    # Sort by XP desc, take top 3
    candidates.sort(key=lambda t: t[1], reverse=True)
    top_three = candidates[:3]

    # Randomly choose one of the top three
    winner, winner_xp = random.choice(top_three)

    # Remove Devoted from everyone
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

    # Build top 3 listing
    lines = [f"- {m.mention} — **{xp} XP**" for m, xp in top_three]

    await announce_channel.send(
        "💠 **The Path has rolled the bones.**\n"
        "From this week’s three most devoted walkers, one name was drawn.\n\n"
        f"**The Devoted** is {winner.mention} with **{winner_xp} XP**.\n\n"
        "**Top three this week:**\n" + "\n".join(lines)
    )

    reset_week()


def reset_week():
    weekly_xp.clear()
    last_message_xp.clear()
    last_reaction_xp.clear()
    last_receive_xp.clear()


@weekly_devoted_roll.before_loop
async def before_weekly_roll():
    await bot.wait_until_ready()


if __name__ == "__main__":
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN not set")

bot.run(TOKEN)

