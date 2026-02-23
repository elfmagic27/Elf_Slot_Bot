import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import pymongo
import datetime
import os
import random
import string
from flask import Flask
from threading import Thread

# ================= FLASK SERVER FOR 24/7 =================
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_flask).start()  # Run Flask in a separate thread

# ================= DISCORD SETUP =================
TOKEN = os.getenv("TOKEN")
GUILD_ID = 1289578124463308840
CATEGORY_ID = 1475141791945592934
OWNER_ID = 584181828420632577

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=".", intents=intents)

# ================= MONGO =================
MONGO_URI = os.getenv("MONGO_URI")
mongo_client = pymongo.MongoClient(MONGO_URI)
db = mongo_client["slots_db"]
slots_col = db["slots"]
keys_col = db["keys"]
admins_col = db["admins"]

# ================= HELPERS =================
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

# ================= READY =================
@bot.event
async def on_ready():
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print("Bot Ready")
    check_expiry.start()

# ================= COMMANDS =================

# /adminadd
@bot.tree.command(name="adminadd", description="Add admin", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to make admin")
async def adminadd(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Only owner can add admin.", ephemeral=True)
    admins_col.update_one({"user_id": user.id}, {"$set": {"user_id": user.id}}, upsert=True)
    await interaction.response.send_message(f"{user.mention} is now admin.", ephemeral=True)

# /removeadmin
@bot.tree.command(name="removeadmin", description="Remove admin", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to remove")
async def removeadmin(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Only owner can remove admin.", ephemeral=True)
    admins_col.delete_one({"user_id": user.id})
    await interaction.response.send_message(f"{user.mention} removed from admin.", ephemeral=True)

# /createkey
@bot.tree.command(name="createkey", description="Create slot key", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(duration="Example: 30m, 2h, 7d", everyone="Everyone pings", here="Here pings")
async def createkey(interaction: discord.Interaction, duration: str, everyone: int, here: int):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)
    key = generate_key()
    keys_col.insert_one({"key_code": key, "duration": duration, "everyone": everyone, "here": here, "active": True})
    await interaction.response.send_message(f"Key Created: `{key}`", ephemeral=True)

# /sendpanel
class KeyModal(discord.ui.Modal, title="Enter Slot Key"):
    key_input = discord.ui.TextInput(label="Enter Your Key")
    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip()
        data = keys_col.find_one({"key_code": key, "active": True})
        if not data:
            return await interaction.response.send_message("Invalid or used key.", ephemeral=True)
        duration = parse_time(data["duration"])
        if not duration:
            return await interaction.response.send_message("Invalid duration.", ephemeral=True)
        expiry_time = datetime.datetime.utcnow() + duration
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
            "expiry": expiry_str,
            "everyone_left": data["everyone"],
            "here_left": data["here"],
            "key_used": key
        })
        keys_col.update_one({"key_code": key}, {"$set": {"active": False}})
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

# ================= EXPIRY CHECK =================
@tasks.loop(minutes=1)
async def check_expiry():
    for slot in slots_col.find():
        try:
            expiry_time = datetime.datetime.strptime(slot["expiry"], "%d %B %Y | %H:%M UTC")
            if datetime.datetime.utcnow() > expiry_time:
                channel = bot.get_channel(slot["channel_id"])
                if channel:
                    await channel.delete()
                slots_col.delete_one({"channel_id": slot["channel_id"]})
        except Exception as e:
            print("Expiry check error:", e)

# ================= RUN BOT =================
bot.run(TOKEN)
