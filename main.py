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

# ========== CONFIG ==========
TOKEN = os.getenv("TOKEN")
GUILD_ID = 1289578124463308840
CATEGORY_ID = 1475141791945592934
OWNER_ID = 584181828420632577
MONGO_URI = os.getenv("MONGO_URI")  # your MongoDB connection string

# ========== BOT & FLASK ==========
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=".", intents=intents)
app = Flask(__name__)

# ========== MONGO ==========
client = MongoClient(MONGO_URI)
db = client["slotbot"]
slots_col = db["slots"]
keys_col = db["keys"]
admins_col = db["admins"]

# ========== HELPERS ==========
def generate_key():
    return "SLOT-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

def parse_time(duration):
    duration = duration.lower()
    if duration.endswith("m"): return datetime.timedelta(minutes=int(duration[:-1]))
    if duration.endswith("h"): return datetime.timedelta(hours=int(duration[:-1]))
    if duration.endswith("d"): return datetime.timedelta(days=int(duration[:-1]))
    return None

async def is_admin(user_id):
    return admins_col.find_one({"user_id": user_id}) is not None

# ========== READY ==========
@bot.event
async def on_ready():
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print("Bot Ready")
    check_expiry.start()

# ========== SLASH COMMANDS ==========
# ---- Create Key ----
@bot.tree.command(name="createkey", description="Create slot key", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(duration="Example: 30m, 2h, 7d", everyone="Everyone pings", here="Here pings")
async def createkey(interaction: discord.Interaction, duration: str, everyone: int, here: int):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)
    key = generate_key()
    keys_col.insert_one({"key": key, "duration": duration, "everyone": everyone, "here": here, "active": True})
    await interaction.response.send_message(f"Key Created: `{key}`", ephemeral=True)

# ---- Send Panel ----
class KeyModal(discord.ui.Modal, title="Enter Slot Key"):
    key_input = discord.ui.TextInput(label="Enter Your Key")
    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip()
        key_data = keys_col.find_one({"key": key, "active": True})
        if not key_data:
            return await interaction.response.send_message("Invalid or used key.", ephemeral=True)

        duration_td = parse_time(key_data["duration"])
        if not duration_td:
            return await interaction.response.send_message("Invalid duration.", ephemeral=True)
        expiry_time = datetime.datetime.utcnow() + duration_td
        expiry_str = expiry_time.strftime("%d %B %Y | %H:%M UTC")

        category = bot.get_channel(CATEGORY_ID)
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        channel = await interaction.guild.create_text_channel(
            name=f"slot-{interaction.user.name}", category=category, overwrites=overwrites
        )

        slots_col.insert_one({
            "channel_id": channel.id,
            "owner_id": interaction.user.id,
            "expiry": expiry_time,
            "everyone_left": key_data["everyone"],
            "here_left": key_data["here"],
            "key_used": key
        })
        keys_col.update_one({"key": key}, {"$set": {"active": False}})

        embed = discord.Embed(title="Slot Activated", description=f"Expires: {expiry_str}", color=0x2ecc71)
        await channel.send(embed=embed)
        await interaction.response.send_message(f"Slot created: {channel.mention}", ephemeral=True)

class KeyPanel(discord.ui.View):
    @discord.ui.button(label="Enter Key", style=discord.ButtonStyle.green)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeyModal())

@bot.tree.command(name="sendpanel", description="Send key panel", guild=discord.Object(id=GUILD_ID))
async def sendpanel(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)
    embed = discord.Embed(title="Slot Key System", description="Click below to activate your slot", color=0x3498db)
    await interaction.channel.send(embed=embed, view=KeyPanel())
    await interaction.response.send_message("Panel sent.", ephemeral=True)

# ---- Delete Key ----
@bot.tree.command(name="deletekey", description="Delete key and slot", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(key="Enter key")
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
    await interaction.response.send_message("Key and slot deleted.", ephemeral=True)

# ---- Admin Add/Remove ----
@bot.tree.command(name="adminadd", description="Add admin", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to make admin")
async def adminadd(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    admins_col.update_one({"user_id": user.id}, {"$set": {"user_id": user.id}}, upsert=True)
    await interaction.response.send_message(f"{user.mention} is now an admin.", ephemeral=True)

@bot.tree.command(name="removeadmin", description="Remove admin", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to remove")
async def removeadmin(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    admins_col.delete_one({"user_id": user.id})
    await interaction.response.send_message(f"{user.mention} removed from admin.", ephemeral=True)

# ---- Key Ping Add ----
@bot.tree.command(name="keypingadd", description="Add pings to a slot", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(channel="Slot channel", type="everyone/here", amount="Number of pings to add")
async def keypingadd(interaction: discord.Interaction, channel: discord.TextChannel, type: str, amount: int):
    slot = slots_col.find_one({"channel_id": channel.id})
    if not slot:
        return await interaction.response.send_message("This is not a slot.", ephemeral=True)
    if interaction.user.id != slot["owner_id"] and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Only slot owner or admin.", ephemeral=True)
    type = type.lower()
    if type == "everyone":
        slots_col.update_one({"channel_id": channel.id}, {"$inc": {"everyone_left": amount}})
    elif type == "here":
        slots_col.update_one({"channel_id": channel.id}, {"$inc": {"here_left": amount}})
    else:
        return await interaction.response.send_message("Type must be 'everyone' or 'here'.", ephemeral=True)
    await interaction.response.send_message(f"Added {amount} {type} ping(s) to {channel.mention}.", ephemeral=True)

# ---- Delete All ----
@bot.tree.command(name="deleteall", description="Delete all messages except slot info", guild=discord.Object(id=GUILD_ID))
async def deleteall(interaction: discord.Interaction):
    slot = slots_col.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message("This is not a slot channel.", ephemeral=True)
    if interaction.user.id != slot["owner_id"] and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Only slot owner or admin can use this.", ephemeral=True)

    async for msg in interaction.channel.history(limit=None):
        if msg.author.id != bot.user.id or msg.embeds:
            continue
        await msg.delete()
    await interaction.response.send_message("✅ All messages deleted except slot info.", ephemeral=True)

# ---- Ping Commands ----
async def handle_ping(interaction: discord.Interaction, ping_type: str):
    slot = slots_col.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message("This is not a slot channel.", ephemeral=True)
    owner_id = slot["owner_id"]
    if interaction.user.id != owner_id and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Only slot owner or admin can use this.", ephemeral=True)

    remaining = 0
    if ping_type == "everyone":
        if slot["everyone_left"] <= 0:
            return await interaction.response.send_message("❌ No @everyone pings left.", ephemeral=True)
        await interaction.channel.send("@everyone", allowed_mentions=discord.AllowedMentions(everyone=True))
        remaining = slot["everyone_left"] - 1
        slots_col.update_one({"channel_id": interaction.channel.id}, {"$set": {"everyone_left": remaining}})
    else:
        if slot["here_left"] <= 0:
            return await interaction.response.send_message("❌ No @here pings left.", ephemeral=True)
        await interaction.channel.send("@here", allowed_mentions=discord.AllowedMentions(everyone=True))
        remaining = slot["here_left"] - 1
        slots_col.update_one({"channel_id": interaction.channel.id}, {"$set": {"here_left": remaining}})

    # DM reminder
    try:
        owner = await bot.fetch_user(owner_id)
        if owner:
            await owner.send(f"You have **{remaining} {ping_type} ping(s) left** in {interaction.channel.name}")
    except: pass
    await interaction.response.send_message(f"{ping_type} ping sent! {remaining} left.", ephemeral=True)

@bot.tree.command(name="everyone", description="Send @everyone ping in slot", guild=discord.Object(id=GUILD_ID))
async def everyone(interaction: discord.Interaction):
    await handle_ping(interaction, "everyone")

@bot.tree.command(name="here", description="Send @here ping in slot", guild=discord.Object(id=GUILD_ID))
async def here(interaction: discord.Interaction):
    await handle_ping(interaction, "here")

# ========== CHECK EXPIRY ==========
@tasks.loop(minutes=1)
async def check_expiry():
    now = datetime.datetime.utcnow()
    expired = slots_col.find({"expiry": {"$lte": now}})
    for slot in expired:
        channel = bot.get_channel(slot["channel_id"])
        if channel:
            await channel.delete()
        slots_col.delete_one({"channel_id": slot["channel_id"]})

# ========== FLASK 24/7 ==========
@app.route("/")
def home():
    return "Bot is running 24/7!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

# ========== RUN ==========
import threading
threading.Thread(target=run_flask).start()
bot.run(TOKEN)
