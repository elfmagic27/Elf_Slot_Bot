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
MONGO_URI = os.getenv("MONGO_URI")

GUILD_ID = 1289578124463308840
CATEGORY_ID = 1475141791945592934
OWNER_ID = 584181828420632577

# ================= BOT =================
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)
app = Flask(__name__)

# ================= DATABASE =================
client = MongoClient(MONGO_URI)
db = client["slotbot"]
slots = db["slots"]
keys = db["keys"]
admins = db["admins"]

# ================= UTIL =================
def gen_key():
    return "SLOT-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

def parse_time(t):
    if t.endswith("m"): return datetime.timedelta(minutes=int(t[:-1]))
    if t.endswith("h"): return datetime.timedelta(hours=int(t[:-1]))
    if t.endswith("d"): return datetime.timedelta(days=int(t[:-1]))
    return None

def utcnow():
    return datetime.datetime.now(datetime.UTC)

async def is_admin(user_id):
    return admins.find_one({"user_id": user_id}) is not None

# ================= READY =================
@bot.event
async def on_ready():
    bot.add_view(KeyPanel())  # persistent button
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    check_expiry.start()
    print("CP6 FULL READY")

# ================= PANEL =================
class KeyModal(discord.ui.Modal, title="Activate Slot"):
    key = discord.ui.TextInput(label="Enter Key")

    async def on_submit(self, interaction: discord.Interaction):
        data = keys.find_one({"key": self.key.value, "active": True})
        if not data:
            return await interaction.response.send_message("Invalid key.", ephemeral=True)

        duration = parse_time(data["duration"])
        expiry = utcnow() + duration

        category = bot.get_channel(CATEGORY_ID)
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        channel = await interaction.guild.create_text_channel(
            f"slot-{interaction.user.name}", category=category, overwrites=overwrites
        )

        slots.insert_one({
            "channel_id": channel.id,
            "owner_id": interaction.user.id,
            "owner_name": interaction.user.name,
            "created": utcnow(),
            "expiry": expiry,
            "everyone": data["everyone"],
            "here": data["here"],
            "key": self.key.value
        })

        keys.update_one({"key": self.key.value}, {"$set": {"active": False}})

        total = data["everyone"] + data["here"]

        embed = discord.Embed(title="Slot Activated", color=0x2ecc71)
        embed.add_field(name="Owner", value=interaction.user.name, inline=False)
        embed.add_field(name="Created", value=utcnow().strftime("%d %b %Y | %H:%M UTC"))
        embed.add_field(name="Expires", value=expiry.strftime("%d %b %Y | %H:%M UTC"))
        embed.add_field(name="Total Pings Left", value=str(total), inline=False)

        await channel.send(embed=embed)
        await interaction.response.send_message(f"Slot created: {channel.mention}", ephemeral=True)

class KeyPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter Key", style=discord.ButtonStyle.green, custom_id="persistent_key_button")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeyModal())

# ================= ADMIN =================
@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def adminadd(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    admins.update_one({"user_id": user.id}, {"$set": {"user_id": user.id}}, upsert=True)
    await interaction.response.send_message("Admin added.", ephemeral=True)

@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def removeadmin(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    admins.delete_one({"user_id": user.id})
    await interaction.response.send_message("Admin removed.", ephemeral=True)

# ================= KEY =================
@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def createkey(interaction: discord.Interaction, duration: str, everyone: int, here: int):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    key = gen_key()
    keys.insert_one({"key": key, "duration": duration, "everyone": everyone, "here": here, "active": True})
    await interaction.response.send_message(f"Key: `{key}`", ephemeral=True)

@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def deletekey(interaction: discord.Interaction, key: str):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    slot = slots.find_one({"key": key})
    if slot:
        ch = bot.get_channel(slot["channel_id"])
        if ch: await ch.delete()
        slots.delete_one({"key": key})
    keys.delete_one({"key": key})
    await interaction.response.send_message("Deleted.", ephemeral=True)

# ================= PINGS =================
async def send_ping(interaction, type):
    slot = slots.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message("Not a slot.", ephemeral=True)

    if interaction.user.id != slot["owner_id"] and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    if slot[type] <= 0:
        return await interaction.response.send_message("No pings left.", ephemeral=True)

    await interaction.channel.send(
        "@everyone" if type=="everyone" else "@here",
        allowed_mentions=discord.AllowedMentions(everyone=True)
    )

    slots.update_one({"channel_id": interaction.channel.id}, {"$inc": {type: -1}})

    new_slot = slots.find_one({"channel_id": interaction.channel.id})
    total = new_slot["everyone"] + new_slot["here"]

    async for msg in interaction.channel.history(limit=20):
        if msg.embeds and msg.embeds[0].title == "Slot Activated":
            embed = msg.embeds[0]
            embed.set_field_at(3, name="Total Pings Left", value=str(total), inline=False)
            await msg.edit(embed=embed)
            break

    try:
        user = await bot.fetch_user(slot["owner_id"])
        await user.send(f"{type} ping used. {new_slot[type]} left.")
    except:
        pass

    await interaction.response.send_message("Ping sent.", ephemeral=True)

@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def everyone(interaction: discord.Interaction):
    await send_ping(interaction, "everyone")

@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def here(interaction: discord.Interaction):
    await send_ping(interaction, "here")

@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def pingsadd(interaction: discord.Interaction, everyone: int = 0, here: int = 0):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    slots.update_one({"channel_id": interaction.channel.id}, {"$inc": {"everyone": everyone, "here": here}})
    await interaction.response.send_message("Pings added.", ephemeral=True)

# ================= DELETE ALL =================
@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def deleteall(interaction: discord.Interaction):
    slot = slots.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message("Not slot.", ephemeral=True)

    deleted = 0
    async for msg in interaction.channel.history(limit=None):
        if msg.embeds:
            continue
        try:
            await msg.delete()
            deleted += 1
        except:
            pass

    await interaction.response.send_message(f"Deleted {deleted} messages.", ephemeral=True)

# ================= HELP =================
@bot.tree.command(guild=discord.Object(id=GUILD_ID))
async def help(interaction: discord.Interaction):
    embed = discord.Embed(title="Slot Bot Commands", color=0x3498db)
    embed.description = """
/createkey – Owner/Admin  
/deletekey – Owner/Admin  
/adminadd – Owner only  
/removeadmin – Owner only  
/sendpanel – Owner/Admin  
/everyone – Slot owner  
/here – Slot owner  
/pingsadd – Owner/Admin  
/deleteall – Slot owner  
"""
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= EXPIRY =================
@tasks.loop(minutes=1)
async def check_expiry():
    for slot in slots.find({"expiry": {"$lte": utcnow()}}):
        ch = bot.get_channel(slot["channel_id"])
        if ch: await ch.delete()
        slots.delete_one({"channel_id": slot["channel_id"]})

# ================= FLASK =================
@app.route("/")
def home():
    return "CP6 Running"

def run():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

threading.Thread(target=run).start()

bot.run(TOKEN)
