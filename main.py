import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import datetime
import os
import random
import string

TOKEN = os.getenv("TOKEN")
GUILD_ID = 1289578124463308840
CATEGORY_ID = 1475141791945592934
OWNER_ID = 584181828420632577

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents)

# ================= DATABASE =================

conn = sqlite3.connect("slots.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS slots (
    channel_id INTEGER PRIMARY KEY,
    owner_id INTEGER,
    expiry TEXT,
    everyone_left INTEGER,
    here_left INTEGER,
    key_used TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS keys (
    key_code TEXT PRIMARY KEY,
    duration TEXT,
    everyone INTEGER,
    here INTEGER,
    active INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY
)
""")

conn.commit()

# ================= HELPERS =================

def generate_key():
    return "SLOT-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

def parse_time(duration):
    duration = duration.lower()
    if duration.endswith("m"):
        return datetime.timedelta(minutes=int(duration[:-1]))
    if duration.endswith("h"):
        return datetime.timedelta(hours=int(duration[:-1]))
    if duration.endswith("d"):
        return datetime.timedelta(days=int(duration[:-1]))
    return None

# ================= READY =================

@bot.event
async def on_ready():
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print("Bot Ready")
    check_expiry.start()

# ================= CREATE KEY =================

@bot.tree.command(name="createkey", description="Create slot key", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(duration="Example: 30m, 2h, 7d", everyone="Everyone pings", here="Here pings")
async def createkey(interaction: discord.Interaction, duration: str, everyone: int, here: int):
    cursor.execute("SELECT user_id FROM admins WHERE user_id=?", (interaction.user.id,))
    is_admin = cursor.fetchone()
    if interaction.user.id != OWNER_ID and not is_admin:
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)

    key = generate_key()
    cursor.execute("INSERT INTO keys VALUES (?, ?, ?, ?, 1)",
                   (key, duration, everyone, here))
    conn.commit()
    await interaction.response.send_message(f"Key Created: `{key}`", ephemeral=True)

# ================= PANEL =================

class KeyModal(discord.ui.Modal, title="Enter Slot Key"):
    key_input = discord.ui.TextInput(label="Enter Your Key")
    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip()
        cursor.execute("SELECT * FROM keys WHERE key_code=? AND active=1", (key,))
        data = cursor.fetchone()
        if not data:
            return await interaction.response.send_message("Invalid or used key.", ephemeral=True)

        duration = parse_time(data[1])
        if not duration:
            return await interaction.response.send_message("Invalid duration.", ephemeral=True)

        expiry_time = datetime.datetime.utcnow() + duration
        expiry_str = expiry_time.strftime("%d %B %Y | %H:%M UTC")

        category = bot.get_channel(CATEGORY_ID)
        # Public view but only owner can send
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        channel = await interaction.guild.create_text_channel(
            name=f"slot-{interaction.user.name}",
            category=category,
            overwrites=overwrites
        )

        cursor.execute("INSERT INTO slots VALUES (?, ?, ?, ?, ?, ?)",
                       (channel.id, interaction.user.id, expiry_str, data[2], data[3], key))
        cursor.execute("UPDATE keys SET active=0 WHERE key_code=?", (key,))
        conn.commit()

        embed = discord.Embed(title="Slot Activated",
                              description=f"Expires: {expiry_str}",
                              color=0x2ecc71)
        await channel.send(embed=embed)
        await interaction.response.send_message(f"Slot created: {channel.mention}", ephemeral=True)

class KeyPanel(discord.ui.View):
    @discord.ui.button(label="Enter Key", style=discord.ButtonStyle.green)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeyModal())

@bot.tree.command(name="sendpanel", description="Send key panel", guild=discord.Object(id=GUILD_ID))
async def sendpanel(interaction: discord.Interaction):
    cursor.execute("SELECT user_id FROM admins WHERE user_id=?", (interaction.user.id,))
    is_admin = cursor.fetchone()
    if interaction.user.id != OWNER_ID and not is_admin:
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)

    embed = discord.Embed(title="Slot Key System",
                          description="Click below to activate your slot",
                          color=0x3498db)
    await interaction.channel.send(embed=embed, view=KeyPanel())
    await interaction.response.send_message("Panel sent.", ephemeral=True)

# ================= DELETE KEY =================

@bot.tree.command(name="deletekey", description="Delete key and slot", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(key="Enter key")
async def deletekey(interaction: discord.Interaction, key: str):
    cursor.execute("SELECT user_id FROM admins WHERE user_id=?", (interaction.user.id,))
    is_admin = cursor.fetchone()
    if interaction.user.id != OWNER_ID and not is_admin:
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)

    cursor.execute("SELECT channel_id FROM slots WHERE key_used=?", (key,))
    slot = cursor.fetchone()
    if slot:
        channel = bot.get_channel(slot[0])
        if channel:
            await channel.delete()
    cursor.execute("DELETE FROM slots WHERE key_used=?", (key,))
    cursor.execute("DELETE FROM keys WHERE key_code=?", (key,))
    conn.commit()
    await interaction.response.send_message("Key and slot deleted.", ephemeral=True)

# ================= PING SYSTEM =================

async def handle_ping(ctx, ping_type):
    cursor.execute("SELECT owner_id, everyone_left, here_left FROM slots WHERE channel_id=?", (ctx.channel.id,))
    data = cursor.fetchone()
    if not data:
        return await ctx.send("This is not a slot channel.")

    owner_id, everyone_left, here_left = data
    if ctx.author.id != owner_id:
        cursor.execute("SELECT user_id FROM admins WHERE user_id=?", (ctx.author.id,))
        if not cursor.fetchone():
            return await ctx.send("Only slot owner or admin can use pings.")

    remaining = 0
    if ping_type == "everyone":
        if everyone_left <= 0:
            return await ctx.send("❌ No everyone pings left.")
        await ctx.send("@everyone", allowed_mentions=discord.AllowedMentions(everyone=True))
        remaining = everyone_left - 1
        cursor.execute("UPDATE slots SET everyone_left=? WHERE channel_id=?",
                       (remaining, ctx.channel.id))
    else:
        if here_left <= 0:
            return await ctx.send("❌ No here pings left.")
        await ctx.send("@here", allowed_mentions=discord.AllowedMentions(everyone=True))
        remaining = here_left - 1
        cursor.execute("UPDATE slots SET here_left=? WHERE channel_id=?",
                       (remaining, ctx.channel.id))
    conn.commit()

    # Thread reminder
    thread = await ctx.channel.create_thread(
        name="Ping Reminder",
        type=discord.ChannelType.public_thread,
        auto_archive_duration=60
    )
    msg = await thread.send(f"<@{owner_id}> You have **{remaining} {ping_type} pings left.**")
    await asyncio.sleep(60)
    try:
        await thread.delete()
        await msg.delete()
    except: pass

@bot.command()
async def everyone(ctx): await handle_ping(ctx, "everyone")
@bot.command()
async def here(ctx): await handle_ping(ctx, "here")

# ================= ADMIN SYSTEM =================

@bot.tree.command(name="adminadd", description="Add admin", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to make admin")
async def adminadd(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Only bot owner can add admin.", ephemeral=True)

    cursor.execute("INSERT OR IGNORE INTO admins VALUES (?)", (user.id,))
    conn.commit()
    await interaction.response.send_message(f"{user.mention} is now an admin.", ephemeral=True)

@bot.tree.command(name="removeadmin", description="Remove admin", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to remove admin")
async def removeadmin(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Only bot owner can remove admin.", ephemeral=True)

    cursor.execute("DELETE FROM admins WHERE user_id=?", (user.id,))
    conn.commit()
    await interaction.response.send_message(f"{user.mention} removed from admins.", ephemeral=True)

# ================= KEYPING ADD =================

@bot.tree.command(name="keypingadd", description="Add pings to a slot", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(channel="Slot channel", type="everyone/here", amount="Number of pings to add")
async def keypingadd(interaction: discord.Interaction, channel: discord.TextChannel, type: str, amount: int):
    cursor.execute("SELECT owner_id FROM slots WHERE channel_id=?", (channel.id,))
    slot = cursor.fetchone()
    if not slot:
        return await interaction.response.send_message("This channel is not a slot.", ephemeral=True)

    owner_id = slot[0]
    cursor.execute("SELECT user_id FROM admins WHERE user_id=?", (interaction.user.id,))
    is_admin = cursor.fetchone()
    if interaction.user.id != owner_id and not is_admin:
        return await interaction.response.send_message("Only slot owner or admin can add pings.", ephemeral=True)

    type = type.lower()
    if type == "everyone":
        cursor.execute("SELECT everyone_left FROM slots WHERE channel_id=?", (channel.id,))
        current = cursor.fetchone()[0]
        cursor.execute("UPDATE slots SET everyone_left=? WHERE channel_id=?", (current + amount, channel.id))
    elif type == "here":
        cursor.execute("SELECT here_left FROM slots WHERE channel_id=?", (channel.id,))
        current = cursor.fetchone()[0]
        cursor.execute("UPDATE slots SET here_left=? WHERE channel_id=?", (current + amount, channel.id))
    else:
        return await interaction.response.send_message("Type must be 'everyone' or 'here'.", ephemeral=True)

    conn.commit()
    await interaction.response.send_message(f"Added {amount} {type} ping(s) to {channel.mention}.", ephemeral=True)

# ================= DELETE ALL MESSAGES =================

@bot.tree.command(name="deleteall", description="Delete all messages except first info", guild=discord.Object(id=GUILD_ID))
async def deleteall(interaction: discord.Interaction):
    cursor.execute("SELECT owner_id FROM slots WHERE channel_id=?", (interaction.channel.id,))
    slot = cursor.fetchone()
    if not slot:
        return await interaction.response.send_message("This is not a slot channel.", ephemeral=True)

    owner_id = slot[0]
    cursor.execute("SELECT user_id FROM admins WHERE user_id=?", (interaction.user.id,))
    is_admin = cursor.fetchone()
    if interaction.user.id != owner_id and not is_admin:
        return await interaction.response.send_message("Only slot owner or admin can delete messages.", ephemeral=True)

    # fetch all messages
    messages = []
    async for msg in interaction.channel.history(limit=None):
        messages.append(msg)
    if not messages:
        return await interaction.response.send_message("No messages found.", ephemeral=True)

    # keep first message
    first_msg = messages[-1]
    for msg in messages[:-1]:
        try:
            await msg.delete()
        except: pass

    await interaction.response.send_message("All messages deleted except slot info.", ephemeral=True)

# ================= EXPIRY CHECK =================

@tasks.loop(minutes=1)
async def check_expiry():
    cursor.execute("SELECT channel_id, expiry FROM slots")
    rows = cursor.fetchall()
    for channel_id, expiry_str in rows:
        expiry_time = datetime.datetime.strptime(expiry_str, "%d %B %Y | %H:%M UTC")
        if datetime.datetime.utcnow() > expiry_time:
            channel = bot.get_channel(channel_id)
            if channel:
                await channel.delete()
            cursor.execute("DELETE FROM slots WHERE channel_id=?", (channel_id,))
            conn.commit()

# ================= RUN BOT =================

bot.run(TOKEN)
