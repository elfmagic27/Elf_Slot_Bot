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

# ══════════════════════════════════════════
#                  CONFIG
# ══════════════════════════════════════════
TOKEN       = os.getenv("TOKEN")
GUILD_ID    = 1471717932362109082
CATEGORY_ID = 1474439067042320528
OWNER_ID    = 1464507268438691891
MONGO_URI   = os.getenv("MONGO_URI")

# Set this to your log channel ID (or set via env var LOG_CHANNEL_ID)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", 0))

# ══════════════════════════════════════════
#             BOT & FLASK SETUP
# ══════════════════════════════════════════
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=".", intents=intents)
app = Flask(__name__)

# ══════════════════════════════════════════
#                  MONGODB
# ══════════════════════════════════════════
client     = MongoClient(MONGO_URI)
db         = client["slotbot"]
slots_col  = db["slots"]
keys_col   = db["keys"]
admins_col = db["admins"]

# ══════════════════════════════════════════
#                  HELPERS
# ══════════════════════════════════════════
COLORS = {
    "success": 0x2ecc71,
    "error":   0xe74c3c,
    "info":    0x3498db,
    "warning": 0xf39c12,
    "log":     0x9b59b6,
}

def generate_key():
    return "SLOT-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

def parse_time(duration: str):
    duration = duration.lower()
    if duration.endswith("m"): return datetime.timedelta(minutes=int(duration[:-1]))
    if duration.endswith("h"): return datetime.timedelta(hours=int(duration[:-1]))
    if duration.endswith("d"): return datetime.timedelta(days=int(duration[:-1]))
    return None

async def is_admin(user_id: int) -> bool:
    return admins_col.find_one({"user_id": user_id}) is not None

def now_str() -> str:
    return datetime.datetime.utcnow().strftime("%d %B %Y — %H:%M UTC")

# ── Log helper ──────────────────────────────────────────────────────────────
async def send_log(embed: discord.Embed):
    """Send a log embed to the configured log channel."""
    if not LOG_CHANNEL_ID:
        return
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        embed.set_footer(text=f"SlotBot Logs  •  {now_str()}")
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

# ── Embed shortcut helpers ──────────────────────────────────────────────────
def err_embed(msg: str) -> discord.Embed:
    return discord.Embed(description=f"❌  {msg}", color=COLORS["error"])

def ok_embed(msg: str) -> discord.Embed:
    return discord.Embed(description=f"✅  {msg}", color=COLORS["success"])

# ══════════════════════════════════════════
#           PERSISTENT PANEL (UI)
# ══════════════════════════════════════════
class KeyModal(discord.ui.Modal, title="🔑  Activate Your Slot"):
    key_input = discord.ui.TextInput(
        label="Enter Your Slot Key",
        placeholder="SLOT-XXXXXXXXXX",
        style=discord.TextStyle.short,
        max_length=20
    )

    async def on_submit(self, interaction: discord.Interaction):
        key      = self.key_input.value.strip()
        key_data = keys_col.find_one({"key": key, "active": True})

        if not key_data:
            return await interaction.response.send_message(
                embed=err_embed("Invalid or already used key."), ephemeral=True
            )

        duration_td = parse_time(key_data["duration"])
        if not duration_td:
            return await interaction.response.send_message(
                embed=err_embed("This key has an invalid duration. Contact an admin."), ephemeral=True
            )

        expiry_time = datetime.datetime.utcnow() + duration_td
        category    = bot.get_channel(CATEGORY_ID)

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            interaction.user:               discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

        channel = await interaction.guild.create_text_channel(
            name=f"slot-{interaction.user.name}",
            category=category,
            overwrites=overwrites,
            topic=f"Slot owned by {interaction.user.name} | Expires: {expiry_time.strftime('%d %b %Y %H:%M UTC')}"
        )

        total_pings = key_data["everyone"] + key_data["here"]

        slots_col.insert_one({
            "channel_id":    channel.id,
            "owner_id":      interaction.user.id,
            "owner_name":    interaction.user.name,
            "created_at":    datetime.datetime.utcnow(),
            "expiry":        expiry_time,
            "everyone_left": key_data["everyone"],
            "here_left":     key_data["here"],
            "key_used":      key
        })
        keys_col.update_one({"key": key}, {"$set": {"active": False}})

        # Slot info embed
        info_embed = discord.Embed(
            title="🟢  Slot Activated",
            color=COLORS["success"]
        )
        info_embed.add_field(name="👤  Owner",         value=interaction.user.mention,                        inline=True)
        info_embed.add_field(name="🔑  Key",           value=f"`{key}`",                                      inline=True)
        info_embed.add_field(name="\u200b",             value="\u200b",                                        inline=False)
        info_embed.add_field(name="🕐  Created At",    value=now_str(),                                       inline=True)
        info_embed.add_field(name="⏳  Expires At",    value=expiry_time.strftime("%d %B %Y — %H:%M UTC"),    inline=True)
        info_embed.add_field(name="\u200b",             value="\u200b",                                        inline=False)
        info_embed.add_field(name="🔔  @everyone",     value=f"`{key_data['everyone']}` left",                inline=True)
        info_embed.add_field(name="🔔  @here",         value=f"`{key_data['here']}` left",                    inline=True)
        info_embed.add_field(name="📊  Total Pings",   value=f"`{total_pings}` left",                         inline=True)
        info_embed.set_footer(text="Use /everyone or /here  •  .everyone or .here to send pings")

        await channel.send(embed=info_embed)
        await interaction.response.send_message(
            embed=ok_embed(f"Your slot is ready — {channel.mention}"), ephemeral=True
        )

        # Log
        log = discord.Embed(title="📋  Slot Created", color=COLORS["log"])
        log.add_field(name="User",     value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
        log.add_field(name="Channel",  value=channel.mention,                                 inline=True)
        log.add_field(name="Key",      value=f"`{key}`",                                      inline=True)
        log.add_field(name="Expires",  value=expiry_time.strftime("%d %b %Y %H:%M UTC"),      inline=True)
        log.add_field(name="Pings",    value=f"@everyone: {key_data['everyone']} | @here: {key_data['here']}", inline=False)
        await send_log(log)


class KeyPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔑  Activate Slot",
        style=discord.ButtonStyle.green,
        custom_id="persistent_enter_key_button"
    )
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeyModal())


# ══════════════════════════════════════════
#                  ON READY
# ══════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"[SlotBot] Logged in as {bot.user}  (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"[SlotBot] Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"[SlotBot] Command sync failed: {e}")

    if not check_expiry.is_running():
        check_expiry.start()

    bot.add_view(KeyPanel())
    print("[SlotBot] Ready.")


# ══════════════════════════════════════════
#               /sendpanel
# ══════════════════════════════════════════
@bot.tree.command(name="sendpanel", description="Send the slot activation panel", guild=discord.Object(id=GUILD_ID))
async def sendpanel(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message(embed=err_embed("Owner / Admin only."), ephemeral=True)

    panel_embed = discord.Embed(
        title="🎰  Slot Key System",
        description=(
            "Welcome to the Slot System.\n\n"
            "**Click the button below** to activate your slot using your key.\n"
            "Once activated, you will receive a private channel with your allocated pings."
        ),
        color=COLORS["info"]
    )
    panel_embed.set_footer(text="Each key is single-use  •  Contact an admin for support")

    await interaction.channel.send(embed=panel_embed, view=KeyPanel())
    await interaction.response.send_message(embed=ok_embed("Panel sent successfully."), ephemeral=True)


# ══════════════════════════════════════════
#                  /help
# ══════════════════════════════════════════
@bot.tree.command(name="help", description="Show all available commands", guild=discord.Object(id=GUILD_ID))
async def help_cmd(interaction: discord.Interaction):
    admin = interaction.user.id == OWNER_ID or await is_admin(interaction.user.id)

    embed = discord.Embed(
        title="📖  SlotBot — Command Reference",
        color=COLORS["info"]
    )

    embed.add_field(
        name="🔔  Ping Commands  *(slot channels only)*",
        value=(
            "`/everyone` or `.everyone` — Send an @everyone ping\n"
            "`/here` or `.here` — Send an @here ping\n"
            "> *Prefix commands (`.`) auto-delete your message.*"
        ),
        inline=False
    )
    embed.add_field(
        name="🗑️  Slot Tools  *(slot channels only)*",
        value="`/deleteall` — Clear all messages except the slot info embed",
        inline=False
    )

    if admin:
        embed.add_field(
            name="🔑  Key Management  *(Admin / Owner)*",
            value=(
                "`/createkey <duration> <everyone> <here>` — Generate a new key\n"
                "> Duration format: `30m`, `2h`, `7d`\n"
                "`/deletekey <key>` — Delete a key and its slot channel\n"
                "`/pingsadd <everyone> <here>` — Add pings to the current slot"
            ),
            inline=False
        )
        embed.add_field(
            name="👑  Admin Management  *(Owner only)*",
            value=(
                "`/adminadd <user>` — Grant admin access\n"
                "`/removeadmin <user>` — Revoke admin access"
            ),
            inline=False
        )
        embed.add_field(
            name="🛠️  Panel  *(Admin / Owner)*",
            value="`/sendpanel` — Post the slot activation panel in the current channel",
            inline=False
        )

    embed.set_footer(text="SlotBot  •  Prefix: .  •  Slash: /")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════
#           ADMIN MANAGEMENT
# ══════════════════════════════════════════
@bot.tree.command(name="adminadd", description="Grant a user admin access", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to promote to admin")
async def adminadd(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message(embed=err_embed("Owner only."), ephemeral=True)
    admins_col.update_one({"user_id": user.id}, {"$set": {"user_id": user.id}}, upsert=True)
    await interaction.response.send_message(embed=ok_embed(f"{user.mention} has been granted admin access."), ephemeral=True)

    log = discord.Embed(title="🛡️  Admin Added", color=COLORS["log"])
    log.add_field(name="Promoted By", value=str(interaction.user), inline=True)
    log.add_field(name="New Admin",   value=str(user),             inline=True)
    await send_log(log)


@bot.tree.command(name="removeadmin", description="Revoke admin access from a user", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to demote")
async def removeadmin(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message(embed=err_embed("Owner only."), ephemeral=True)
    admins_col.delete_one({"user_id": user.id})
    await interaction.response.send_message(embed=ok_embed(f"{user.mention} has been removed from admin."), ephemeral=True)

    log = discord.Embed(title="🚫  Admin Removed", color=COLORS["log"])
    log.add_field(name="Removed By", value=str(interaction.user), inline=True)
    log.add_field(name="Ex-Admin",   value=str(user),             inline=True)
    await send_log(log)


# ══════════════════════════════════════════
#           KEY MANAGEMENT
# ══════════════════════════════════════════
@bot.tree.command(name="createkey", description="Create a new slot key", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(duration="Duration: 30m, 2h, 7d", everyone="@everyone ping count", here="@here ping count")
async def createkey(interaction: discord.Interaction, duration: str, everyone: int, here: int):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message(embed=err_embed("Owner / Admin only."), ephemeral=True)

    if parse_time(duration) is None:
        return await interaction.response.send_message(
            embed=err_embed("Invalid duration format. Use `30m`, `2h`, or `7d`."), ephemeral=True
        )

    key = generate_key()
    keys_col.insert_one({"key": key, "duration": duration, "everyone": everyone, "here": here, "active": True})

    embed = discord.Embed(title="🔑  Key Created", color=COLORS["success"])
    embed.add_field(name="Key",        value=f"`{key}`",   inline=False)
    embed.add_field(name="Duration",   value=duration,     inline=True)
    embed.add_field(name="@everyone",  value=str(everyone), inline=True)
    embed.add_field(name="@here",      value=str(here),    inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

    log = discord.Embed(title="🔑  Key Generated", color=COLORS["log"])
    log.add_field(name="Created By", value=str(interaction.user), inline=True)
    log.add_field(name="Key",        value=f"`{key}`",             inline=True)
    log.add_field(name="Duration",   value=duration,               inline=True)
    log.add_field(name="@everyone",  value=str(everyone),          inline=True)
    log.add_field(name="@here",      value=str(here),              inline=True)
    await send_log(log)


@bot.tree.command(name="deletekey", description="Delete a key and its associated slot", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(key="The key to delete")
async def deletekey(interaction: discord.Interaction, key: str):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message(embed=err_embed("Owner / Admin only."), ephemeral=True)

    slot = slots_col.find_one({"key_used": key})
    if slot:
        channel = bot.get_channel(slot["channel_id"])
        if channel:
            await channel.delete()
    slots_col.delete_many({"key_used": key})
    keys_col.delete_many({"key": key})
    await interaction.response.send_message(embed=ok_embed("Key and its slot have been deleted."), ephemeral=True)

    log = discord.Embed(title="🗑️  Key Deleted", color=COLORS["warning"])
    log.add_field(name="Deleted By", value=str(interaction.user), inline=True)
    log.add_field(name="Key",        value=f"`{key}`",             inline=True)
    await send_log(log)


# ══════════════════════════════════════════
#         PING — SHARED CORE LOGIC
# ══════════════════════════════════════════
async def _do_ping(channel_id: int, user: discord.Member, guild: discord.Guild,
                   channel: discord.TextChannel, ping_type: str,
                   respond_ephemeral, delete_trigger_msg=None):
    """
    Core ping logic shared between slash commands and prefix commands.
    respond_ephemeral : async callable(**kw) that sends an ephemeral-style reply.
    delete_trigger_msg: prefix message to delete before sending the ping.
    """
    slot = slots_col.find_one({"channel_id": channel_id})
    if not slot:
        return await respond_ephemeral(embed=err_embed("This is not a slot channel."))
    if user.id != slot["owner_id"] and not await is_admin(user.id):
        return await respond_ephemeral(embed=err_embed("Only the slot owner or an admin can use this."))

    field_key  = "everyone_left" if ping_type == "everyone" else "here_left"
    pings_left = slot[field_key]

    if pings_left <= 0:
        return await respond_ephemeral(embed=err_embed(f"No `@{ping_type}` pings remaining."))

    # Delete prefix trigger message first so ping appears clean
    if delete_trigger_msg:
        try:
            await delete_trigger_msg.delete()
        except Exception:
            pass

    mention = "@everyone" if ping_type == "everyone" else "@here"
    await channel.send(mention, allowed_mentions=discord.AllowedMentions(everyone=True))

    remaining = pings_left - 1
    slots_col.update_one({"channel_id": channel_id}, {"$set": {field_key: remaining}})

    # Recalculate totals from updated DB document
    updated    = slots_col.find_one({"channel_id": channel_id})
    total_left = updated["everyone_left"] + updated["here_left"]

    # Update the slot info embed (fields at indices 6, 7, 8)
    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == "🟢  Slot Activated":
            embed = msg.embeds[0]
            try:
                embed.set_field_at(6, name="🔔  @everyone",   value=f"`{updated['everyone_left']}` left", inline=True)
                embed.set_field_at(7, name="🔔  @here",       value=f"`{updated['here_left']}` left",     inline=True)
                embed.set_field_at(8, name="📊  Total Pings", value=f"`{total_left}` left",               inline=True)
                await msg.edit(embed=embed)
            except Exception:
                pass
            break

    # DM owner with remaining count
    try:
        owner = await bot.fetch_user(slot["owner_id"])
        if owner:
            dm_embed = discord.Embed(
                description=(
                    f"🔔 You used an `@{ping_type}` ping in **{channel.name}**.\n"
                    f"`{remaining}` `@{ping_type}` ping(s) remaining."
                ),
                color=COLORS["info"]
            )
            await owner.send(embed=dm_embed)
    except Exception:
        pass

    await respond_ephemeral(embed=ok_embed(f"`@{ping_type}` sent — `{remaining}` left."))

    # Log
    log = discord.Embed(title=f"🔔  @{ping_type} Ping Sent", color=COLORS["log"])
    log.add_field(name="By",        value=str(user),       inline=True)
    log.add_field(name="Channel",   value=channel.mention, inline=True)
    log.add_field(name="Remaining", value=f"`{remaining}` @{ping_type}", inline=True)
    await send_log(log)


# ══════════════════════════════════════════
#       SLASH COMMANDS: /everyone  /here
# ══════════════════════════════════════════
@bot.tree.command(name="everyone", description="Send @everyone ping in your slot channel", guild=discord.Object(id=GUILD_ID))
async def slash_everyone(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await _do_ping(
        interaction.channel.id, interaction.user, interaction.guild,
        interaction.channel, "everyone",
        respond_ephemeral=lambda **kw: interaction.followup.send(ephemeral=True, **kw)
    )

@bot.tree.command(name="here", description="Send @here ping in your slot channel", guild=discord.Object(id=GUILD_ID))
async def slash_here(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await _do_ping(
        interaction.channel.id, interaction.user, interaction.guild,
        interaction.channel, "here",
        respond_ephemeral=lambda **kw: interaction.followup.send(ephemeral=True, **kw)
    )


# ══════════════════════════════════════════
#     PREFIX COMMANDS: .everyone  .here
#     (trigger message auto-deleted)
# ══════════════════════════════════════════
@bot.command(name="everyone")
async def prefix_everyone(ctx: commands.Context):
    async def reply(**kw):
        try:
            await ctx.author.send(**kw)   # DM the result; ephemeral not available in prefix
        except Exception:
            pass

    await _do_ping(
        ctx.channel.id, ctx.author, ctx.guild,
        ctx.channel, "everyone",
        respond_ephemeral=reply,
        delete_trigger_msg=ctx.message
    )

@bot.command(name="here")
async def prefix_here(ctx: commands.Context):
    async def reply(**kw):
        try:
            await ctx.author.send(**kw)
        except Exception:
            pass

    await _do_ping(
        ctx.channel.id, ctx.author, ctx.guild,
        ctx.channel, "here",
        respond_ephemeral=reply,
        delete_trigger_msg=ctx.message
    )


# ══════════════════════════════════════════
#               /deleteall
# ══════════════════════════════════════════
@bot.tree.command(name="deleteall", description="Delete all messages in slot except the info embed", guild=discord.Object(id=GUILD_ID))
async def deleteall(interaction: discord.Interaction):
    slot = slots_col.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message(embed=err_embed("This is not a slot channel."), ephemeral=True)
    if interaction.user.id != slot["owner_id"] and not await is_admin(interaction.user.id):
        return await interaction.response.send_message(embed=err_embed("Only the slot owner or an admin can use this."), ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    deleted = 0
    async for msg in interaction.channel.history(limit=None):
        if msg.author.id == bot.user.id and msg.embeds:
            continue
        try:
            await msg.delete()
            deleted += 1
        except Exception:
            pass

    await interaction.followup.send(embed=ok_embed(f"Cleared `{deleted}` message(s)."), ephemeral=True)

    log = discord.Embed(title="🗑️  Slot Cleared", color=COLORS["log"])
    log.add_field(name="By",      value=str(interaction.user),       inline=True)
    log.add_field(name="Channel", value=interaction.channel.mention, inline=True)
    log.add_field(name="Deleted", value=f"`{deleted}` messages",     inline=True)
    await send_log(log)


# ══════════════════════════════════════════
#               /pingsadd
# ══════════════════════════════════════════
@bot.tree.command(name="pingsadd", description="Add extra pings to the current slot", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(everyone="@everyone pings to add", here="@here pings to add")
async def pingsadd(interaction: discord.Interaction, everyone: int = 0, here: int = 0):
    if interaction.user.id != OWNER_ID and not await is_admin(interaction.user.id):
        return await interaction.response.send_message(embed=err_embed("Owner / Admin only."), ephemeral=True)

    slot = slots_col.find_one({"channel_id": interaction.channel.id})
    if not slot:
        return await interaction.response.send_message(embed=err_embed("This is not a slot channel."), ephemeral=True)

    new_everyone = slot.get("everyone_left", 0) + max(everyone, 0)
    new_here     = slot.get("here_left",     0) + max(here, 0)
    total_left   = new_everyone + new_here

    slots_col.update_one(
        {"channel_id": interaction.channel.id},
        {"$set": {"everyone_left": new_everyone, "here_left": new_here}}
    )

    # Update slot info embed
    channel = bot.get_channel(slot["channel_id"])
    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == "🟢  Slot Activated":
            embed = msg.embeds[0]
            try:
                embed.set_field_at(6, name="🔔  @everyone",   value=f"`{new_everyone}` left", inline=True)
                embed.set_field_at(7, name="🔔  @here",       value=f"`{new_here}` left",     inline=True)
                embed.set_field_at(8, name="📊  Total Pings", value=f"`{total_left}` left",   inline=True)
                await msg.edit(embed=embed)
            except Exception:
                pass
            break

    result_embed = discord.Embed(title="✅  Pings Added", color=COLORS["success"])
    result_embed.add_field(name="@everyone Added", value=str(everyone),   inline=True)
    result_embed.add_field(name="@here Added",     value=str(here),       inline=True)
    result_embed.add_field(name="Total Now",        value=str(total_left), inline=True)
    await interaction.response.send_message(embed=result_embed, ephemeral=True)

    log = discord.Embed(title="➕  Pings Added to Slot", color=COLORS["log"])
    log.add_field(name="By",         value=str(interaction.user),       inline=True)
    log.add_field(name="Channel",    value=interaction.channel.mention, inline=True)
    log.add_field(name="+@everyone", value=str(everyone),               inline=True)
    log.add_field(name="+@here",     value=str(here),                   inline=True)
    log.add_field(name="New Total",  value=str(total_left),             inline=True)
    await send_log(log)


# ══════════════════════════════════════════
#           AUTO EXPIRY LOOP (every 1 min)
# ══════════════════════════════════════════
@tasks.loop(minutes=1)
async def check_expiry():
    now     = datetime.datetime.utcnow()
    expired = list(slots_col.find({"expiry": {"$lte": now}}))
    for slot in expired:
        channel = bot.get_channel(slot["channel_id"])
        if channel:
            try:
                await channel.delete()
            except Exception:
                pass
        slots_col.delete_one({"channel_id": slot["channel_id"]})

        log = discord.Embed(title="⏰  Slot Expired & Deleted", color=COLORS["warning"])
        log.add_field(name="Owner",   value=slot.get("owner_name", "Unknown"),  inline=True)
        log.add_field(name="Channel", value=f"#{slot.get('owner_name','?')}'s slot", inline=True)
        log.add_field(name="Key",     value=f"`{slot.get('key_used','?')}`",     inline=True)
        await send_log(log)


# ══════════════════════════════════════════
#           FLASK KEEP-ALIVE SERVER
# ══════════════════════════════════════════
@app.route("/")
def home():
    return "SlotBot is online. 🟢"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

threading.Thread(target=run_flask, daemon=True).start()

bot.run(TOKEN)
