import discord
from discord.ext import commands, tasks
from discord import app_commands
import pymongo
import datetime
import os
import random
import string
import asyncio

TOKEN = os.getenv("TOKEN")
GUILD_ID = 1289578124463308840
CATEGORY_ID = 1475141791945592934
OWNER_ID = 584181828420632577
MONGO_URL = os.getenv("MONGO_URL")  # Your MongoDB URI

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents)

# ================= MONGO =================

client = pymongo.MongoClient(MONGO_URL)
db = client['slotbot']
slots_col = db['slots']
keys_col = db['keys']
admins_col = db['admins']

# ================= HELPERS =================

def generate_key():
    return "SLOT-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

def parse_time(duration: str):
    duration = duration.lower()
    if duration.endswith("m"):
        return datetime.timedelta(minutes=int(duration[:-1]))
    if duration.endswith("h"):
        return datetime.timedelta(hours=int(duration[:-1]))
    if duration.endswith("d"):
        return datetime.timedelta(days=int(duration[:-1]))
    return None

async def is_admin_or_owner(user_id: int, owner_id: int):
    if user_id == OWNER_ID or user_id == owner_id:
        return True
    if admins_col.find_one({"user_id": user_id}):
        return True
    return False

# ================= READY =================

@bot.event
async def on_ready():
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print("Bot Ready")
    check_expiry.start()

# ================= CREATE KEY =================

@bot.tree.command(name="createkey", description="Create slot key", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(duration="30m,2h,7d", everyone="Everyone pings", here="Here pings")
async def createkey(interaction: discord.Interaction, duration: str, everyone: int, here: int):
    if not await is_admin_or_owner(interaction.user.id, OWNER_ID):
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

        duration = parse_time(data['duration'])
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
            name=f"slot-{interaction.user.name}",
            category=category,
            overwrites=overwrites
        )

        slots_col.insert_one({
            "channel_id": channel.id,
            "owner_id": interaction.user.id,
            "expiry": expiry_str,
            "everyone_left": data['everyone'],
            "here_left": data['here'],
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
    if not await is_admin_or_owner(interaction.user.id, OWNER_ID):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)

    embed = discord.Embed(title="Slot Key System",
                          description="Click below to activate your slot",
                          color=0x3498db)
    await interaction.channel.send(embed=embed, view=KeyPanel())
    await interaction.response.send_message("Panel sent.", ephemeral=True)

# ================= DELETE ALL =================

@bot.tree.command(name="deleteall", description="Delete all messages except slot info", guild=discord.Object(id=GUILD_ID))
async def deleteall(interaction: discord.Interaction):
    slot = slots_col.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message("This is not a slot channel.", ephemeral=True)
    if interaction.user.id != slot['owner_id']:
        return await interaction.response.send_message("Only slot owner can use this.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)  # Avoid timeout

    # Get slot info message (first message)
    first_msg = None
    async for msg in interaction.channel.history(limit=1, oldest_first=True):
        first_msg = msg

    # Delete other messages
    async for msg in interaction.channel.history(limit=None):
        if msg.id != first_msg.id:
            try: await msg.delete()
            except: pass

    await interaction.followup.send("All messages deleted (except slot info).", ephemeral=True)

# ================= DELETE KEY =================

@bot.tree.command(name="deletekey", description="Delete key and slot", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(key="Enter key")
async def deletekey(interaction: discord.Interaction, key: str):
    slot = slots_col.find_one({"key_used": key})
    if not await is_admin_or_owner(interaction.user.id, OWNER_ID if slot is None else slot['owner_id']):
        return await interaction.response.send_message("Owner/Admin only.", ephemeral=True)

    if slot:
        channel = bot.get_channel(slot['channel_id'])
        if channel:
            await channel.delete()
        slots_col.delete_one({"key_used": key})
    keys_col.delete_one({"key_code": key})
    await interaction.response.send_message("Key and slot deleted.", ephemeral=True)

# ================= PING SYSTEM =================

async def handle_ping(ctx, ping_type):
    slot = slots_col.find_one({"channel_id": ctx.channel.id})
    if not slot:
        return await ctx.send("This is not a slot channel.")
    if not await is_admin_or_owner(ctx.author.id, slot['owner_id']):
        return await ctx.send("Only slot owner or admin can use pings.")

    remaining = 0
    if ping_type == "everyone":
        if slot['everyone_left'] <= 0:
            return await ctx.send("❌ No everyone pings left.")
        await ctx.send("@everyone", allowed_mentions=discord.AllowedMentions(everyone=True))
        remaining = slot['everyone_left'] - 1
        slots_col.update_one({"channel_id": ctx.channel.id}, {"$set": {"everyone_left": remaining}})
    else:
        if slot['here_left'] <= 0:
            return await ctx.send("❌ No here pings left.")
        await ctx.send("@here", allowed_mentions=discord.AllowedMentions(everyone=True))
        remaining = slot['here_left'] - 1
        slots_col.update_one({"channel_id": ctx.channel.id}, {"$set": {"here_left": remaining}})

    # Send embed DM to owner
    owner = ctx.guild.get_member(slot['owner_id'])
    if owner:
        embed = discord.Embed(
            title="Ping Reminder",
            description=f"You have **{remaining} {ping_type} ping(s) left** in {ctx.channel.mention}.",
            color=0x3498db
        )
        try:
            await owner.send(embed=embed)
        except:
            await ctx.send(f"Couldn't DM <@{slot['owner_id']}>.")

@bot.command()
async def everyone(ctx): await handle_ping(ctx, "everyone")
@bot.command()
async def here(ctx): await handle_ping(ctx, "here")

# ================= ADMIN =================

@bot.tree.command(name="adminadd", description="Add admin", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to make admin")
async def adminadd(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    admins_col.insert_one({"user_id": user.id})
    await interaction.response.send_message(f"{user.mention} is now an admin.", ephemeral=True)

@bot.tree.command(name="removeadmin", description="Remove admin", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to remove admin")
async def removeadmin(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    admins_col.delete_one({"user_id": user.id})
    await interaction.response.send_message(f"{user.mention} removed from admin.", ephemeral=True)

# ================= EXPIRY CHECK =================

@tasks.loop(minutes=1)
async def check_expiry():
    for slot in slots_col.find():
        expiry_time = datetime.datetime.strptime(slot['expiry'], "%d %B %Y | %H:%M UTC")
        if datetime.datetime.utcnow() > expiry_time:
            channel = bot.get_channel(slot['channel_id'])
            if channel:
                await channel.delete()
            slots_col.delete_one({"channel_id": slot['channel_id']})

# ================= RUN BOT =================

bot.run(TOKEN)
