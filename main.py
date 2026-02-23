import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import random
import string
import datetime
import asyncio
from pymongo import MongoClient

# ================= CONFIG =================
TOKEN = os.getenv("TOKEN")
GUILD_ID = 1289578124463308840
CATEGORY_ID = 1475141791945592934
OWNER_ID = 584181828420632577
MONGO_URI = os.getenv("MONGO_URI")  # MongoDB connection string

# ================= INTENTS & BOT =================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=".", intents=intents)

# ================= MONGO DB =================
client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
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
    if user_id == OWNER_ID:
        return True
    return admins_col.find_one({"user_id": user_id}) is not None

# ================= BOT READY =================
@bot.event
async def on_ready():
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    check_expiry.start()
    print(f"Bot ready as {bot.user}")

# ================= CREATE KEY =================
@bot.tree.command(name="createkey", description="Create slot key", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(duration="Example: 30m, 2h, 7d", everyone="Everyone pings", here="Here pings")
async def createkey(interaction: discord.Interaction, duration: str, everyone: int, here: int):
    if not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)
    key = generate_key()
    keys_col.insert_one({
        "key_code": key,
        "duration": duration,
        "everyone": everyone,
        "here": here,
        "active": True
    })
    await interaction.response.send_message(f"Key Created: `{key}`", ephemeral=True)

# ================= PANEL =================
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
            "expiry": expiry_time,
            "everyone_left": data["everyone"],
            "here_left": data["here"],
            "key_used": key
        })
        keys_col.update_one({"key_code": key}, {"$set": {"active": False}})

        embed = discord.Embed(title="Slot Activated",
                              description=f"Expires: {expiry_time.strftime('%d %B %Y | %H:%M UTC')}",
                              color=0x2ecc71)
        await channel.send(embed=embed)
        await interaction.response.send_message(f"Slot created: {channel.mention}", ephemeral=True)

class KeyPanel(discord.ui.View):
    @discord.ui.button(label="Enter Key", style=discord.ButtonStyle.green)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeyModal())

@bot.tree.command(name="sendpanel", description="Send key panel", guild=discord.Object(id=GUILD_ID))
async def sendpanel(interaction: discord.Interaction):
    if not await is_admin(interaction.user.id):
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
    if not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)
    slot = slots_col.find_one({"key_used": key})
    if slot:
        channel = bot.get_channel(slot["channel_id"])
        if channel:
            await channel.delete()
    slots_col.delete_many({"key_used": key})
    keys_col.delete_one({"key_code": key})
    await interaction.response.send_message("Key and slot deleted.", ephemeral=True)

# ================= PINGS =================
async def handle_ping(ctx, ping_type):
    slot = slots_col.find_one({"channel_id": ctx.channel.id})
    if not slot:
        return await ctx.send("This is not a slot channel.")

    if ctx.author.id != slot["owner_id"] and not await is_admin(ctx.author.id):
        return await ctx.send("Only slot owner or admin can use pings.")

    remaining = 0
    if ping_type == "everyone":
        if slot["everyone_left"] <= 0:
            return await ctx.send("❌ No everyone pings left.")
        await ctx.send("@everyone", allowed_mentions=discord.AllowedMentions(everyone=True))
        remaining = slot["everyone_left"] - 1
        slots_col.update_one({"channel_id": ctx.channel.id}, {"$set": {"everyone_left": remaining}})
    else:
        if slot["here_left"] <= 0:
            return await ctx.send("❌ No here pings left.")
        await ctx.send("@here", allowed_mentions=discord.AllowedMentions(everyone=True))
        remaining = slot["here_left"] - 1
        slots_col.update_one({"channel_id": ctx.channel.id}, {"$set": {"here_left": remaining}})

    try:
        user = await bot.fetch_user(slot["owner_id"])
        await user.send(f"Reminder: You have **{remaining} {ping_type} pings left** in `{ctx.channel.name}`.")
    except:
        pass

@bot.command()
async def everyone(ctx): await handle_ping(ctx, "everyone")
@bot.command()
async def here(ctx): await handle_ping(ctx, "here")

# ================= ADMIN MANAGEMENT =================
@bot.tree.command(name="adminadd", description="Add admin", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to make admin")
async def adminadd(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    admins_col.update_one({"user_id": user.id}, {"$set": {"user_id": user.id}}, upsert=True)
    await interaction.response.send_message(f"{user.mention} is now an admin.", ephemeral=True)

@bot.tree.command(name="removeadmin", description="Remove admin", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to remove from admin")
async def removeadmin(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    admins_col.delete_one({"user_id": user.id})
    await interaction.response.send_message(f"{user.mention} removed from admin.", ephemeral=True)

# ================= KEYPING ADD =================
@bot.tree.command(name="keypingadd", description="Add pings to a slot", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(channel="Slot channel", type="everyone/here", amount="Number of pings to add")
async def keypingadd(interaction: discord.Interaction, channel: discord.TextChannel, type: str, amount: int):
    slot = slots_col.find_one({"channel_id": channel.id})
    if not slot:
        return await interaction.response.send_message("This channel is not a slot.", ephemeral=True)
    if interaction.user.id != slot["owner_id"] and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Only slot owner/admin can add pings.", ephemeral=True)
    type = type.lower()
    if type == "everyone":
        slots_col.update_one({"channel_id": channel.id}, {"$inc": {"everyone_left": amount}})
    elif type == "here":
        slots_col.update_one({"channel_id": channel.id}, {"$inc": {"here_left": amount}})
    else:
        return await interaction.response.send_message("Type must be 'everyone' or 'here'.", ephemeral=True)
    await interaction.response.send_message(f"Added {amount} {type} ping(s) to {channel.mention}.", ephemeral=True)

# ================= DELETE ALL MESSAGES =================
@bot.tree.command(name="deleteall", description="Delete all messages except slot info", guild=discord.Object(id=GUILD_ID))
async def deleteall(interaction: discord.Interaction):
    slot = slots_col.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message("This is not a slot channel.", ephemeral=True)
    if interaction.user.id != slot["owner_id"] and not await is_admin(interaction.user.id):
        return await interaction.response.send_message("Only slot owner/admin can delete messages.", ephemeral=True)

    messages = [m async for m in interaction.channel.history(limit=None)]
    first_message = messages[-1] if messages else None
    to_delete = [m for m in messages if m.id != first_message.id] if first_message else messages
    await interaction.channel.delete_messages(to_delete)
    await interaction.response.send_message("Deleted all messages except slot info.", ephemeral=True)

# ================= EXPIRY CHECK =================
@tasks.loop(minutes=1)
async def check_expiry():
    for slot in slots_col.find():
        if datetime.datetime.utcnow() > slot["expiry"]:
            channel = bot.get_channel(slot["channel_id"])
            if channel:
                try:
                    await channel.delete()
                except: pass
            slots_col.delete_one({"channel_id": slot["channel_id"]})

# ================= RUN BOT =================
bot.run(TOKEN)
