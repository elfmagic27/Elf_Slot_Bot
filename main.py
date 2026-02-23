import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask
import os, random, string, datetime, asyncio, threading
from pymongo import MongoClient

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
bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

# ================= FLASK 24/7 =================
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot running 24/7!"
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))).start()

# ================= MONGO =================
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

# ================= COMMAND INFO =================
commands_info = [
    {"name": "createkey", "usage": ".createkey <duration> <everyone> <here>", "who": "Owner/Admin"},
    {"name": "sendpanel", "usage": ".sendpanel", "who": "Owner/Admin"},
    {"name": "deletekey", "usage": ".deletekey <key>", "who": "Owner/Admin"},
    {"name": "adminadd", "usage": ".adminadd <user>", "who": "Owner"},
    {"name": "removeadmin", "usage": ".removeadmin <user>", "who": "Owner"},
    {"name": "keypingadd", "usage": ".keypingadd <channel> <everyone/here> <amount>", "who": "Slot Owner/Admin"},
    {"name": "deleteall", "usage": ".deleteall", "who": "Slot Owner/Admin"},
    {"name": "everyone", "usage": ".everyone", "who": "Slot Owner/Admin"},
    {"name": "here", "usage": ".here", "who": "Slot Owner/Admin"},
    {"name": "help", "usage": ".help", "who": "Everyone"}
]

async def send_help(ctx_or_inter):
    embed = discord.Embed(title="Bot Commands", color=0x1abc9c)
    for cmd in commands_info:
        embed.add_field(name=cmd["name"], value=f"Usage: `{cmd['usage']}`\nWho: {cmd['who']}", inline=False)
    if isinstance(ctx_or_inter, discord.Interaction):
        await ctx_or_inter.response.send_message(embed=embed, ephemeral=True)
    else:
        await ctx_or_inter.send(embed=embed)

# ================= HELP =================
@bot.tree.command(name="help", description="Show all commands", guild=discord.Object(id=GUILD_ID))
async def slash_help(interaction: discord.Interaction):
    await send_help(interaction)

@bot.command()
async def help(ctx):
    await send_help(ctx)

# ================= CREATEKEY =================
@bot.tree.command(name="createkey", description="Create slot key", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(duration="30m/1h/1d", everyone="Everyone pings", here="Here pings")
async def slash_createkey(interaction: discord.Interaction, duration: str, everyone: int, here: int):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)
    key = generate_key()
    keys_col.insert_one({"key": key, "duration": duration, "everyone": everyone, "here": here, "active": True})
    await interaction.response.send_message(f"Key Created: `{key}`", ephemeral=True)

@bot.command()
async def createkey(ctx, duration: str, everyone: int, here: int):
    fake = type("obj", (), {"user": ctx.author, "response": ctx.send, "guild": ctx.guild})()
    await slash_createkey.callback(fake, duration, everyone, here)

# ================= SENDPANEL =================
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
            name=f"slot-{interaction.user.name}", category=category, overwrites=overwrites
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
        embed.add_field(name="Slot Owner", value=interaction.user.name)
        embed.add_field(name="Created At", value=datetime.datetime.utcnow().strftime("%d %B %Y | %H:%M UTC"))
        embed.add_field(name="Expires At", value=expiry_time.strftime("%d %B %Y | %H:%M UTC"))
        embed.add_field(name="Total Pings Left", value=str(total_pings))
        await channel.send(embed=embed)
        await interaction.response.send_message(f"Slot created: {channel.mention}", ephemeral=True)

class KeyPanel(discord.ui.View):
    @discord.ui.button(label="Enter Key", style=discord.ButtonStyle.green)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeyModal())

@bot.tree.command(name="sendpanel", description="Send key panel", guild=discord.Object(id=GUILD_ID))
async def slash_sendpanel(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)
    embed = discord.Embed(title="Slot Key System", description="Click below to activate your slot", color=0x3498db)
    await interaction.channel.send(embed=embed, view=KeyPanel())
    await interaction.response.send_message("Panel sent.", ephemeral=True)

@bot.command()
async def sendpanel(ctx):
    fake = type("obj", (), {"user": ctx.author, "response": ctx.send, "channel": ctx.channel, "guild": ctx.guild})()
    await slash_sendpanel.callback(fake)

# ================= RUN BOT =================
bot.run(TOKEN)
