import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import os
import random
import string
import asyncio
import certifi
from pymongo import MongoClient

TOKEN = os.getenv("TOKEN")
MONGO_URI = os.getenv("MONGO_URI")  # Add this in Render environment variables
GUILD_ID = 1289578124463308840
CATEGORY_ID = 1475141791945592934
OWNER_ID = 584181828420632577

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents)

# ================= MONGODB =================
try:
    client = MongoClient(MONGO_URI, tls=True, tlsCAFile=certifi.where())
    db = client.slotbot  # your database
    slots_col = db.slots
    keys_col = db.keys
    admins_col = db.admins
    print("✅ MongoDB connected successfully")
except Exception as e:
    print("❌ MongoDB connection failed:", e)
    raise e
