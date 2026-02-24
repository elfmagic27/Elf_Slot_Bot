import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask
import os
import random
import string
import datetime
from pymongo import MongoClient
import threading

# ========== CONFIG ==========
TOKEN = os.getenv("TOKEN")
GUILD_ID = 1289578124463308840
CATEGORY_ID = 1475141791945592934
OWNER_ID = 584181828420632577
MONGO_URI = os.getenv("MONGO_URI")

# ========== BOT & FLASK ==========
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents)
app = Flask(__name__)

# ========== DATABASE ==========
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
    if duration.endswith("m"):
        return datetime.timedelta(minutes=int(duration[:-1]))
    if duration.endswith("h"):
        return datetime.timedelta(hours=int(duration[:-1]))
    if duration.endswith("d"):
        return datetime.timedelta(days=int(duration[:-1]))
    return None

async def is_admin(user_id):
    return admins_col.find_one({"user_id": user_id}) is not None

# ========== READY ==========
@bot.event
async def on_ready():
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print("Bot Ready - CP6")
    check_expiry.start()

# ========== PANEL ==========
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
        embed.add_field(name="Slot Owner", value=interaction.user.name, inline=False)
        embed.add_field(name="Created At", value=datetime.datetime.utcnow().strftime("%d %B %Y | %H:%M UTC"), inline=True)
        embed.add_field(name="Expires At", value=expiry_time.strftime("%d %B %Y | %H:%M UTC"), inline=True)
        embed.add_field(name="Total Pings Left", value=str(total_pings), inline=False)

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

    embed = discord.Embed(title="Slot Key System",
                          description="Click below to activate your slot",
                          color=0x3498db)

    await interaction.channel.send(embed=embed, view=KeyPanel())
    await interaction.response.send_message("Panel sent.", ephemeral=True)

# ========== PINGS ADD ==========
@bot.tree.command(name="pingsadd", description="Add extra pings", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(everyone="Number of @everyone pings", here="Number of @here pings")
async def pingsadd(interaction: discord.Interaction, everyone: int = 0, here: int = 0):

    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)

    slot = slots_col.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message("This is not a slot channel.", ephemeral=True)

    new_everyone = slot.get("everyone_left", 0) + max(everyone, 0)
    new_here = slot.get("here_left", 0) + max(here, 0)

    slots_col.update_one(
        {"channel_id": interaction.channel.id},
        {"$set": {"everyone_left": new_everyone, "here_left": new_here}}
    )

    total_left = new_everyone + new_here

    async for msg in interaction.channel.history(limit=50):
        if msg.embeds and msg.embeds[0].title == "Slot Activated":
            embed = msg.embeds[0]
            embed.set_field_at(3, name="Total Pings Left", value=str(total_left), inline=False)
            await msg.edit(embed=embed)
            break

    await interaction.response.send_message(
        f"Added {everyone} @everyone and {here} @here pings.\nTotal now: {total_left}",
        ephemeral=True
    )

# ========== EXPIRY ==========
@tasks.loop(minutes=1)
async def check_expiry():
    now = datetime.datetime.utcnow()
    expired = slots_col.find({"expiry": {"$lte": now}})

    for slot in expired:
        channel = bot.get_channel(slot["channel_id"])
        if channel:
            await channel.delete()
        slots_col.delete_one({"channel_id": slot["channel_id"]})

# ========== FLASK KEEP ALIVE ==========
@app.route("/")
def home():
    return "Bot is running 24/7!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

threading.Thread(target=run_flask).start()

bot.run(TOKEN)
