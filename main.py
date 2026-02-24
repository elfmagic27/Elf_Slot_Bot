import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask
import os
import random
import string
import datetime
from pymongo import MongoClient
import asyncio
import threading

# ================= CONFIG =================
TOKEN = os.getenv("TOKEN")
GUILD_ID = 1289578124463308840
CATEGORY_ID = 1475141791945592934
OWNER_ID = 584181828420632577
MONGO_URI = os.getenv("MONGO_URI")

# ================= BOT =================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=".", intents=intents)

# ================= FLASK =================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

threading.Thread(target=run_flask).start()

# ================= DATABASE =================
client = MongoClient(MONGO_URI)
db = client["slotbot"]
slots_col = db["slots"]
keys_col = db["keys"]
admins_col = db["admins"]

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

async def is_admin(user_id):
    return admins_col.find_one({"user_id": user_id}) is not None

# ================= PERSISTENT PANEL =================
class KeyModal(discord.ui.Modal, title="Enter Slot Key"):
    key_input = discord.ui.TextInput(label="Enter Your Key")

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip()
        key_data = keys_col.find_one({"key": key, "active": True})

        if not key_data:
            return await interaction.response.send_message("Invalid or used key.", ephemeral=True)

        duration_td = parse_time(key_data["duration"])
        expiry_time = datetime.datetime.utcnow() + duration_td

        category = bot.get_channel(CATEGORY_ID)

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        channel = await interaction.guild.create_text_channel(
            name=f"slot-{interaction.user.name}",
            category=category,
            overwrites=overwrites
        )

        slots_col.insert_one({
            "channel_id": channel.id,
            "owner_id": interaction.user.id,
            "owner_name": interaction.user.name,
            "created_at": datetime.datetime.utcnow(),
            "expiry": expiry_time,
            "everyone_left": key_data["everyone"],
            "here_left": key_data["here"],
            "key_used": key
        })

        keys_col.update_one({"key": key}, {"$set": {"active": False}})

        total_pings = key_data["everyone"] + key_data["here"]

        embed = discord.Embed(title="Slot Activated", color=0x2ecc71)
        embed.add_field(name="Slot Owner", value=interaction.user.mention, inline=False)
        embed.add_field(name="Created", value=datetime.datetime.utcnow().strftime("%d %b %Y | %H:%M UTC"))
        embed.add_field(name="Expires", value=expiry_time.strftime("%d %b %Y | %H:%M UTC"))
        embed.add_field(name="Total Pings Left", value=str(total_pings), inline=False)

        await channel.send(embed=embed)
        await interaction.response.send_message(f"Slot created: {channel.mention}", ephemeral=True)

class PersistentPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter Key", style=discord.ButtonStyle.green, custom_id="persistent_key_button")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeyModal())

# ================= READY =================
@bot.event
async def on_ready():
    bot.add_view(PersistentPanel())
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print("Bot Ready")
    check_expiry.start()

# ================= SEND PANEL =================
@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def sendpanel(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)

    embed = discord.Embed(
        title="Slot Key System",
        description="Click below to activate your slot.",
        color=0x3498db
    )

    await interaction.channel.send(embed=embed, view=PersistentPanel())
    await interaction.response.send_message("Panel sent.", ephemeral=True)

# ================= ADMIN =================
@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def adminadd(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    admins_col.update_one({"user_id": user.id}, {"$set": {"user_id": user.id}}, upsert=True)
    await interaction.response.send_message("Admin added.", ephemeral=True)

@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def removeadmin(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    admins_col.delete_one({"user_id": user.id})
    await interaction.response.send_message("Admin removed.", ephemeral=True)

# ================= CREATE KEY =================
@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def createkey(interaction: discord.Interaction, duration: str, everyone: int, here: int):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)

    key = generate_key()

    keys_col.insert_one({
        "key": key,
        "duration": duration,
        "everyone": everyone,
        "here": here,
        "active": True
    })

    await interaction.response.send_message(f"Key Created: `{key}`", ephemeral=True)

# ================= PING ADD =================
@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def pingsadd(interaction: discord.Interaction, everyone: int, here: int):
    slot = slots_col.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message("Not a slot channel.", ephemeral=True)

    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)

    slots_col.update_one(
        {"channel_id": interaction.channel.id},
        {"$inc": {"everyone_left": everyone, "here_left": here}}
    )

    await interaction.response.send_message("Pings added successfully.", ephemeral=True)

# ================= PING SYSTEM =================
async def handle_ping(interaction, ping_type):
    slot = slots_col.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message("Not a slot channel.", ephemeral=True)

    if interaction.user.id != slot["owner_id"]:
        return await interaction.response.send_message("Only slot owner.", ephemeral=True)

    if slot[f"{ping_type}_left"] <= 0:
        return await interaction.response.send_message("No pings left.", ephemeral=True)

    await interaction.channel.send(
        f"@{ping_type}",
        allowed_mentions=discord.AllowedMentions(everyone=True)
    )

    slots_col.update_one(
        {"channel_id": interaction.channel.id},
        {"$inc": {f"{ping_type}_left": -1}}
    )

    await interaction.response.send_message("Ping sent.", ephemeral=True)

@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def everyone(interaction: discord.Interaction):
    await handle_ping(interaction, "everyone")

@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def here(interaction: discord.Interaction):
    await handle_ping(interaction, "here")

# ================= DELETE ALL =================
@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def deleteall(interaction: discord.Interaction):
    slot = slots_col.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message("Not a slot channel.", ephemeral=True)

    deleted = 0
    async for msg in interaction.channel.history(limit=None):
        try:
            await msg.delete()
            deleted += 1
        except:
            pass

    await interaction.response.send_message(f"Deleted {deleted} messages.", ephemeral=True)

# ================= DELETE KEY =================
@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def deletekey(interaction: discord.Interaction, key: str):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)

    slot = slots_col.find_one({"key_used": key})
    if slot:
        channel = bot.get_channel(slot["channel_id"])
        if channel:
            await channel.delete()

    slots_col.delete_many({"key_used": key})
    keys_col.delete_many({"key": key})

    await interaction.response.send_message("Key deleted.", ephemeral=True)

# ================= EXPIRY CHECK =================
@tasks.loop(minutes=1)
async def check_expiry():
    now = datetime.datetime.utcnow()
    expired = slots_col.find({"expiry": {"$lte": now}})

    for slot in expired:
        channel = bot.get_channel(slot["channel_id"])
        if channel:
            await channel.delete()
        slots_col.delete_one({"channel_id": slot["channel_id"]})

bot.run(TOKEN)
