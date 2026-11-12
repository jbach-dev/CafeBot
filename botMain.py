import discord
import datetime
import os
from discord.ext import commands, tasks
from typing import Optional
import DataStorage
from Permissions import (
    is_authorized,
    is_authorized_interaction,
    slash_auth_check,
    check_cmd_permission,
)

import DndModule
import EconomyModule
import FunModule
import ModerationModule
import BotAdminModule
import FaithModule
import TriviaModule
import MusicModule

from dotenv import dotenv_values

class InteractionContext:
    """Adapter that wraps a discord.Interaction to behave like commands.Context.
    Allows all module functions to be reused for slash commands unchanged."""

    def __init__(self, interaction: discord.Interaction):
        self._interaction = interaction
        self._responded = False
        self.bot = interaction.client
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.message = None

    @property
    def author(self):
        # Return the full Member object in guild context so .voice, .guild_permissions, etc. are available
        if self._interaction.guild:
            member = self._interaction.guild.get_member(self._interaction.user.id)
            if member:
                return member
        return self._interaction.user

    async def defer(self, ephemeral: bool = False):
        await self._interaction.response.defer(ephemeral=ephemeral)
        self._responded = True

    async def send(self, content=None, **kwargs):
        if not self._responded:
            self._responded = True
            await self._interaction.response.send_message(content, **kwargs)
            return await self._interaction.original_response()
        else:
            return await self._interaction.followup.send(content, **kwargs)


intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='.', intents=intents)

bot.remove_command('help')

config = dotenv_values(".env")
DataStorage.administrators = config.get("administrators").split(",")


EXTENSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extensions")


async def load_extensions():
    """Load every package in extensions/ (a folder containing __init__.py) with bot.load_extension.
    Does nothing if the folder doesn't exist."""
    if not os.path.isdir(EXTENSIONS_DIR):
        return
    for name in sorted(os.listdir(EXTENSIONS_DIR)):
        if name.startswith((".", "_")):
            continue
        if not os.path.isfile(os.path.join(EXTENSIONS_DIR, name, "__init__.py")):
            continue
        try:
            await bot.load_extension(f"extensions.{name}")
            print(f"Loaded extension: {name}")
        except Exception as e:
            print(f"Failed to load extension {name}: {e!r}")


async def setup_hook():
    await load_extensions()

bot.setup_hook = setup_hook


def _match_member_by_name(guild, argument: str):
    """Case-insensitive scan of guild.members against display_name, global_name, and name."""
    needle = argument.lower()
    for m in guild.members:
        if m.display_name.lower() == needle:
            return m
        global_name = getattr(m, "global_name", None)
        if global_name and global_name.lower() == needle:
            return m
        if m.name.lower() == needle:
            return m
    return None


class FlexibleMember(commands.MemberConverter):
    """MemberConverter with a case-insensitive display_name / global_name / username fallback.
    discord.py's default get_member_named is case-sensitive — this lets `.kick jon` match "Jon" or a "Jon" nickname."""
    async def convert(self, ctx, argument):
        try:
            return await super().convert(ctx, argument)
        except commands.MemberNotFound:
            if ctx.guild is None:
                raise
            match = _match_member_by_name(ctx.guild, argument)
            if match is not None:
                return match
            raise


class FlexibleUser(commands.UserConverter):
    """UserConverter with case-insensitive display_name fallback.
    In DM, scans the invoker's default DM guild so bot-admin commands like `.force_marry Jon Jane` work by nickname."""
    async def convert(self, ctx, argument):
        try:
            return await super().convert(ctx, argument)
        except commands.UserNotFound:
            search_guild = ctx.guild
            if search_guild is None:
                invoker_record = DataStorage.get_or_create_user(ctx.author.id)
                target_gid = invoker_record.default_dm_guild_id
                if target_gid:
                    search_guild = ctx.bot.get_guild(int(target_gid))
            if search_guild is not None:
                match = _match_member_by_name(search_guild, argument)
                if match is not None:
                    return match
            raise


def parse_duration(s):
    """Parse a duration string like '1h', '30m', '2d' into seconds. Returns None for 'none' or '0'."""
    s = s.strip().lower()
    if s in ("none", "0"):
        return None
    if s.endswith("d") and s[:-1].isdigit():
        return int(s[:-1]) * 86400
    if s.endswith("h") and s[:-1].isdigit():
        return int(s[:-1]) * 3600
    if s.endswith("m") and s[:-1].isdigit():
        return int(s[:-1]) * 60
    return None


@tasks.loop(seconds=30)
async def lottery_timer_check():
    now = datetime.datetime.now(datetime.timezone.utc)
    for guild_id in list(DataStorage.lottery_active.keys()):
        active = DataStorage.lottery_active.get(guild_id)
        if not active:
            continue
        end_time = active.get("end_time")
        if not end_time:
            continue
        end_dt = datetime.datetime.fromisoformat(end_time)
        if now >= end_dt:
            entries = DataStorage.get_lottery_entries(guild_id)
            channel = bot.get_channel(int(active["channel_id"]))
            if entries and channel:
                await channel.send("⏰ Time's up! Drawing the lottery winner now...")
                await EconomyModule._execute_lottery_draw(guild_id, channel)
            else:
                DataStorage.lottery_active.pop(guild_id, None)
                DataStorage.lottery_pot.pop(guild_id, None)
                DataStorage.lottery_entries.pop(guild_id, None)
                DataStorage.save_lottery()


# Events:
@bot.event
async def on_ready():
    DataStorage.load_user_data()
    DataStorage.load_all()
    EconomyModule.bot = bot

    now = datetime.datetime.now(datetime.timezone.utc)
    for guild_id in list(DataStorage.lottery_active.keys()):
        active = DataStorage.lottery_active.get(guild_id)
        if not active:
            continue
        end_time = active.get("end_time")
        if end_time:
            end_dt = datetime.datetime.fromisoformat(end_time)
            if now >= end_dt:
                entries = DataStorage.get_lottery_entries(guild_id)
                channel = bot.get_channel(int(active["channel_id"]))
                if entries and channel:
                    await channel.send("⏰ Lottery expired while the bot was offline! Drawing now...")
                    await EconomyModule._execute_lottery_draw(guild_id, channel)
                else:
                    DataStorage.lottery_active.pop(guild_id, None)
                    DataStorage.lottery_pot.pop(guild_id, None)
                    DataStorage.lottery_entries.pop(guild_id, None)
                    DataStorage.save_lottery()

    lottery_timer_check.start()
    synced = await bot.tree.sync()
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print(f'Synced {len(synced)} slash commands.')
    print('------')


# Commands:


# --- HELP MENU CONFIGURATION ---

# 2. Format: ("Command Usage", "Description", "Required Permission")
COMMAND_MODULES = {
    "Misc": {
        "description": "General bot utilities.",
        "emoji": "🔧",
        "commands": [
            ("`.ping`", "Check if the bot is awake", "any"),
            ("`.help [module]`", "Show all modules, or use `.help <module>` to list the commands inside it", "any"),
            ("`.dm_server [clear]`", "Pick which server your DM-invoked economy/trivia commands route to (`.shift`, `.beans`, `.daily`, `.quick_trivia`, etc.). Run with no args to open the picker, or `.dm_server clear` to unset", "any")
        ]
    },
    "Moderation": {
        "description": "Server management and moderation tools.",
        "emoji": "🛡️",
        "commands": [
            ("`.purge <amount>`", "Delete the last `<amount>` messages in the current channel", "server_admin"),
            ("`.lockdown [True/False] [all]`", "Lock or unlock the current channel (`True` = lock, `False` = unlock). Pass `True` as the second argument to apply to every text channel in the server at once", "server_admin"),
            ("`.slowmode [True/False] [seconds]`", "Set a per-message cooldown in the current channel. Defaults to 500s if no time is given. Pass `False` to disable. Maximum is 6 hours (21600s)", "server_admin"),
            ("`.kick <user> [reason]`", "Kick a member from the server. They can rejoin using a new invite", "kick"),
            ("`.ban <user> [reason]`", "Permanently ban a member from the server", "ban"),
            ("`.softban <user> [days] [reason]`", "Ban a member to wipe their recent messages, then immediately unban them so they can rejoin. `[days]` sets how many days of messages to delete (default: 1)", "server_admin"),
            ("`.unban <user_id>`", "Unban a user by their Discord user ID (right-click → Copy ID with Developer Mode on)", "ban"),
            ("`.mute <user> [mins] [reason]`", "Put a user in Discord timeout for `[mins]` minutes (default: 10). They cannot send messages or speak in voice while timed out", "server_admin"),
            ("`.unmute <user>`", "Remove an active timeout from a user early", "server_admin"),
            ("`.whois <user>`", "View a detailed profile of a server member: their ID, nickname, account creation date, server join date, and all their roles", "server_admin"),
            ("`.warn <@user> [reason]`", "Log a formal warning against a server member", "moderator"),
            ("`.warnings <@user>`", "View all logged warnings for a member", "moderator")
        ]
    },
    "DnD": {
        "description": "Tabletop RPG dice rolling and character management.",
        "emoji": "🎲",
        "commands": [
            ("`.roll <NdX> [modifier]`", "Roll dice using standard notation (e.g. `.roll 2d20`, `.roll 1d6 3`). Supported dice: d4, d6, d8, d10, d12, d20, d100. An optional modifier is added to the final total. Max 100 dice per roll", "any"),
            ("`.roll_multiple <input>`", "Roll multiple sets of dice in one command, separated by commas (e.g. `.roll_multiple 2d20 3, 1d8`). Each group is rolled and reported separately", "any"),
            ("`.create_character <class> <name>`", "Create and save a new D&D character. Valid classes: Artificer, Barbarian, Bard, Cleric, Druid, Fighter, Monk, Paladin, Ranger, Rogue, Sorcerer, Warlock, Wizard. Each name must be unique per user", "any"),
            ("`.view_characters`", "List all D&D characters you currently have saved", "any"),
            ("`.view_character <name>`", "View a single character's full stat sheet", "any"),
            ("`.character_delete <name>`", "Permanently delete one of your saved characters", "any")
        ]
    },
    "Fun": {
        "description": "Social commands, marriage, quotes, duels, trivia, and games.",
        "emoji": "💕",
        "commands": [
            ("`.marry <user>`", "Send a marriage proposal to another user. If they also use `.marry` on you, you are automatically wed. Both users must currently be single", "any"),
            ("`.divorce <user>`", "Divorce one of your partners. This immediately severs the bond for both of you", "any"),
            ("`.partner`", "View your marriage certificate, showing your partner and how long you've been together. Also shows any children you and your partner have both adopted", "any"),
            ("`.adopt <user>`", "Send an adoption request to another user. If they also use `.adopt` on you, the adoption is confirmed. You can adopt as many people as you want", "any"),
            ("`.unadopt <user>`", "Dissolve an adoption relationship. Either the parent or child can run this", "any"),
            ("`.family`", "View your adopted family: who adopted you (if anyone) and all your adopted children", "any"),
            ("`.marriage_top`", "See the top 10 longest-running marriages in the server, sorted by how long ago they were formed", "any"),
            ("`.duel <user>`", "Start a turn-based duel against another user. Both combatants begin with 100 HP. Each round, both roll a d20 and deal that much damage — last one standing wins. You can also challenge the bot (good luck)", "any"),
            ("`.quote`", "Display a single random quote from the quote database", "any"),
            ("`.quotes <amount>`", "Display multiple random quotes at once (maximum 5)", "any"),
            ("`.quote_list <user> <amount>`", "Display random quotes from a specific person by their author name in the database (maximum 5 at a time)", "any"),
            ("`.quote_count <user>`", "Check how many quotes a specific person has saved in the database", "any"),
            ("`.quote_top`", "See the top 10 people with the most quotes in the database", "any"),
            ("`.quote_search <keyword>`", "Search all quotes for ones containing a keyword or phrase. Shows up to 10 matches", "any"),
            ("`.quote_stats`", "Show overall quote database stats: total quotes, authors, average per author, and most quoted", "any"),
            ("`.profile`", "View your personal profile: beans, partner, D&D characters, daily streak, trivia wins, and bookmarked verses", "any"),
            ("`.eight_ball <question>`", "Consult the Magic 8-Ball with a yes/no question for a cryptic answer", "any"),
            ("`.coinflip`", "Flip a coin and get Heads or Tails", "any"),
            ("`.trivia <rounds>`", "Start a multiplayer trivia session in the current channel. Questions are drawn from your enabled categories (set with `.trivia_config`). The first person to type the correct answer wins the round. Regular users are limited to 10 rounds max. The winner earns 25 Coffee Beans per correct answer", "any"),
            ("`.trivia_config`", "Open an interactive dropdown menu to choose which trivia categories appear in your games", "any"),
            ("`.quick_trivia [category]`", "Ask a single trivia question — no session required. First correct answer wins 10 beans. Optionally specify a category (e.g. `animals`, `history`)", "any"),
            ("`.trivia_stats`", "View your personal trivia stats: total correct answers and your enabled categories", "any"),
            ("`Emotes (optional @target):`", "**Aggressive:** `.punch` `.slap` `.bonk` `.bite` `.kill` `.purge` `.throw` `.mock` `.yoink` `.grip`\n**Affectionate:** `.kiss` `.hug` `.cuddle` `.pat`\n**Social:** `.wave` `.cheer` `.tickle` `.spill` `.wink` `.salute` `.snap` `.thanks`\n**Reactions:** `.stare` `.shocked` `.popcorn` `.frog`\n**Self:** `.happy` `.cry` `.sleep` `.sip` `.explode` `.stub_toe`", "any")
        ]
    },
    "Economy": {
        "description": "Work shifts, earn Coffee Beans, and tip friends.",
        "emoji": "☕",
        "commands": [
            ("`.shift`", "Work a shift at the cafe to earn between 10–50 Coffee Beans. Has a 30-minute cooldown between uses", "any"),
            ("`.beans`", "Check your current Coffee Bean balance", "any"),
            ("`.tip <@user> <amount>`", "Send some of your Coffee Beans to another user. You cannot tip yourself or bots, and you must have enough beans to cover the amount", "any"),
            ("`.bean_top`", "See the top 10 richest users in the server by Coffee Bean balance", "any"),
            ("`.daily`", "Claim your daily Coffee Bean reward (base: 100 beans). Your streak grows by 1 each consecutive day you claim, adding +2% to your reward per streak day. Missing more than 48 hours resets your streak. 24-hour cooldown", "any"),
            ("`.cafe_status`", "Show a server-wide snapshot: beans in circulation, registered users, active marriages, and total quotes", "any"),
            ("`.slots <bet>`", "Spin a 3-reel slot machine. Minimum bet 50 beans. Triple 7s pays 230×, three of a kind pays 25×, two of a kind returns your bet. No match loses your bet", "any"),
            ("`.blackjack <bet>`", "Play blackjack against the dealer. Minimum bet 20 beans. Use the Hit and Stand buttons to play. Blackjack on deal pays 1.5×, a win pays 1×, push returns your bet", "any"),
            ("`.hilo <bet>`", "Press-your-luck card game. A card 2–A is drawn; bet whether the next is higher or lower. Each correct guess multiplies your pot by ×1.4. Cash out anytime after the first correct guess. Wrong guess loses your bet. Min bet 50", "any"),
            ("`.lottery`", "Check the current lottery pot and your ticket count", "any"),
            ("`.lottery_buy <amount>`", "Buy lottery tickets (50 beans each, max 10 per round). More tickets = better odds. Winner takes the whole pot", "any"),
            ("`.bank`", "View your bank balance, current storage cap, and upgrade cost", "any"),
            ("`.deposit <amount>`", "Move beans from your wallet into the bank (safe from robbers)", "any"),
            ("`.withdraw <amount>`", "Move beans from your bank back to your wallet", "any"),
            ("`.bank_upgrade`", "Spend beans to increase your bank's storage cap. Tiers: 1,000 → 2,000 → 5,000 → 10,000 → 20,000", "any"),
            ("`.rob <@user>`", "Attempt to steal 10–25% of a user's wallet (45% success). Failure costs you 150 beans paid to the target. 60-min cooldown", "any"),
            ("`.bet <user> <amount> [reason]`", "Offer a peer-to-peer bet, or accept an incoming offer by matching their exact amount. Beans are escrowed immediately on both sides. Optional `reason` is shown on the offer and carries through to resolution", "any"),
            ("`.betwinner <winner> [opponent]`", "Vote who won an active bet. Match = winner takes the pot, mismatch = bet nulls and both are refunded. Pass `opponent` only when claiming victory yourself with multiple active bets in flight", "any"),
            ("`.cancelbet [user]`", "Cancel a pending offer (refunds you), decline an incoming offer (refunds them), or forfeit an active bet (opponent wins the pot). Omit `user` to auto-resolve when you only have one bet in flight", "any")
        ]
    },
    "Faith": {
        "description": "Send testimonies, get Bible verses, and more! (WIP)",
        "emoji": "✝️",
        "commands": [
            ("`.testimony_role`", "Claim the testimony role", "any"),
            ("`.send_anonymous_testimony <message>`", "Send a testimony to the server's testimony channel with no name attached. **Must be used in DMs with the bot.** The bot will show you a preview and ask you to confirm before sending", "any"),
            ("`.random_verse [version]`", "Display a random Bible verse. Optionally specify a version (e.g. `ASV`) to pull from that translation only", "any"),
            ("`.verse_context`", "Show the 2 verses before and after the last randomly generated verse (if they exist)", "any"),
            ("`.lookup_verse <version> <book> <chapter:verse>`", "Look up a Bible verse or range of verses (e.g. `.lookup_verse ASV John 3:16` or `.lookup_verse ASV John 3:14-18`). Non-admins are limited to 8 verses per range", "any"),
            ("`.list_versions`", "List all Bible versions currently loaded", "any"),
            ("`.verse_search <max_results> <query>`", "Search the Bible index for verses containing a keyword or phrase. Prefix with `version:<VERSION>` to filter by translation (e.g. `.verse_search 5 version:ASV love one another`)", "any"),
            ("`.verse_compare <version1> <version2> <book> <chapter:verse>`", "Show the same verse in two translations side by side (e.g. `.verse_compare KJV NIV John 3:16`)", "any"),
            ("`.verse_bookmark`", "Save the last randomly generated verse to your personal bookmarks", "any"),
            ("`.verse_bookmarks`", "List all of your bookmarked Bible verses", "any")
        ]
    },
    "Music": {
        "description": "Play music in voice channels from YouTube.",
        "emoji": "🎵",
        "commands": [
            ("`.play <song name>`", "Search YouTube and add a song to the queue. Joins your voice channel if not already connected", "any"),
            ("`.skip`", "Skip the currently playing song and move to the next in the queue", "any"),
            ("`.pause`", "Pause the current song. Use again to resume", "any"),
            ("`.loop`", "Toggle loop mode — the current song will repeat until loop is turned off", "any"),
            ("`.queue`", "Show the current music queue", "any"),
            ("`.leave`", "Clear the queue and disconnect the bot from the voice channel", "any")
        ]
    },
    "Admin": {
        "description": "Bot configuration and database management.",
        "emoji": "⚙️",
        "commands": [
            ("`.smite [user]`", "Strike a target (or no one) with divine judgement. Admin-only emote", "bot_admin"),
            ("`.add_gif <type> <link>`", "Add a new GIF URL to a specific emote category (e.g. `punch`, `hug`)", "bot_admin"),
            ("`.remove_gif <type> <link>`", "Remove a GIF URL from an emote category by its exact link", "bot_admin"),
            ("`.add_gif_message <type> <message>`", "Add a new message template to an emote category. Use `{author}` and `{target}` as placeholders", "bot_admin"),
            ("`.remove_gif_message <type> <message>`", "Remove a message template from an emote category by its exact text", "bot_admin"),
            ("`.add_quote <author> <quote>`", "Add a new quote to the database under the given author name", "bot_admin"),
            ("`.remove_quote <quote>`", "Remove a quote from the database by its exact text content", "bot_admin"),
            ("`.add_eight_ball <response>`", "Add a new response to the Magic 8-Ball's answer pool", "bot_admin"),
            ("`.remove_eight_ball <response>`", "Remove a response from the Magic 8-Ball's answer pool by its exact text", "bot_admin"),
            ("`.add_trivia <category> <sub_category> <question> <answers>`", "Add a new question to the trivia bank. Wrap fields containing spaces in quotes. Answers should be a comma-separated list of all acceptable answers (e.g. `\"coffee, java, beans\"`)", "bot_admin"),
            ("`.remove_trivia <category> [sub_category] <question>`", "Remove a question from the trivia bank by fuzzy search. Sub-category is optional; omit it to search the whole category. Wrap fields containing spaces in quotes.", "bot_admin"),
            ("`.admin_tip <user> <amount>`", "Grant a user beans without requiring the admin to have funds.", "bot_admin"),
            ("`.admin_lottery_start [ticket_cap] [duration] [max_per_user]`", "Start a new lottery. Ticket cap, duration (e.g. `30m`, `1h`, `2d`), or both can end the round; use `0`/`none` to omit either. Defaults to 10 tickets per user.", "bot_admin"),
            ("`.admin_lottery_cancel`", "Cancel the active lottery and refund all ticket buyers.", "bot_admin"),
            ("`.admin_lottery_add <amount>`", "Add beans directly to the lottery pot to seed prize pool.", "bot_admin"),
            ("`.admin_lottery_give <user> <amount>`", "Grant lottery tickets to a user without requiring bean payment.", "bot_admin"),
            ("`.force_lottery_draw`", "Draw a lottery winner and reset the pool", "bot_admin"),
            ("`.admin_jackpot_set <amount>`", "Set the per-server slots jackpot pool to an exact bean amount (use `0` to clear).", "bot_admin"),
            ("`.force_marry <user1> <user2>`", "Force two users into a marriage without mutual consent", "bot_admin"),
            ("`.force_divorce <user1> <user2>`", "Force dissolve a marriage between two users", "bot_admin"),
            ("`.force_adopt <parent> <child>`", "Force an adoption relationship — first user becomes the parent", "bot_admin"),
            ("`.force_unadopt <user1> <user2>`", "Force dissolve an adoption relationship between two users (order doesn't matter)", "bot_admin"),
            ("`.admin_user_info <user>`", "View a full summary of a user's economy, social, stats, and moderation data", "bot_admin")
        ]
    },
    "Testing": {
        "description": "Commands currently in testing — bot admins only.",
        "emoji": "🧪",
        "commands": [
            ("`.family_tree [user]`", "Render a Pillow family-tree image around yourself or another user. Shows nearby parents, children, and partners with cycle protection", "bot_admin"),
            ("`.host_check`", "Diagnose the host machine's basic platform and architecture details", "bot_admin"),
            ("`.debug_music`", "Inspect local music-runtime dependencies like Node, FFmpeg, and cookies setup", "bot_admin"),
            ("`.debug_node`", "Test whether Node can run and report the installed yt-dlp version", "bot_admin"),
            ("`.roulette <bet>`", "Open the Roulette bet picker. Pick a bet type from the embed buttons (Red/Black/Even/Odd/Low/High/dozens/columns) or click 🔢 Pick Number to enter a single number 0-36. Numbers pay 35:1, dozens/columns pay 2:1, outside bets pay 1:1. Min bet 25", "bot_admin"),
            ("`.trivia_review`", "Replay gray-zone trivia guesses one at a time and label each ✅ correct / ❌ incorrect / ⏭ skip to build the matcher training set", "bot_admin"),
        ]
    }
}


# 3. The New Dynamic Help Command
@bot.command()
@is_authorized("any")
async def help(ctx, module_name: str = None):
    # If they just type .help (no module specified)
    if module_name is None:
        embed = discord.Embed(
            title="☕ CafeBot Modules",
            description="Use `.help <module>` to see the commands inside it!",
            color=discord.Color.gold()
        )

        for mod_name, mod_data in COMMAND_MODULES.items():
            # Check if user has access to at least ONE command in this module
            can_see_module = any(check_cmd_permission(ctx, req) for _, _, req in mod_data["commands"])

            if can_see_module:
                embed.add_field(
                    name=f"{mod_data['emoji']} {mod_name}",
                    value=mod_data["description"],
                    inline=False
                )

        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        await ctx.send(embed=embed)

    # If they typed .help <module>
    else:
        # Find the module, ignoring capitalization
        target_module = None
        for mod_name in COMMAND_MODULES:
            if mod_name.lower() == module_name.lower():
                target_module = mod_name
                break

        if not target_module:
            await ctx.send(f"❌ Could not find a module named `{module_name}`.")
            return

        mod_data = COMMAND_MODULES[target_module]

        embed = discord.Embed(
            title=f"{mod_data['emoji']} {target_module} Commands",
            description=mod_data["description"],
            color=discord.Color.gold()
        )

        # Loop through commands and only add the ones they have permission for
        visible_commands = 0
        for cmd_usage, cmd_desc, req_permission in mod_data["commands"]:
            if check_cmd_permission(ctx, req_permission):
                embed.add_field(name=cmd_usage, value=cmd_desc, inline=False)
                visible_commands += 1

        # If they found the module but don't have access to any commands inside it
        if visible_commands == 0:
            await ctx.send("🚫 You do not have permission to view commands in this module.")
            return

        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        await ctx.send(embed=embed)


@bot.command()
@is_authorized("any")
async def ping(ctx):
    await ctx.send('Pong!')


@bot.command()
@is_authorized("server_admin")
async def m_purge(ctx, amount: int):
    await ModerationModule.purge(ctx, amount)


@bot.command()
@is_authorized("server_admin")
async def lockdown(ctx, state: bool = True, all_channels: bool = False):
    await ModerationModule.lockdown(ctx, state, all_channels)


@bot.command()
@is_authorized("kick")
async def kick(ctx, member: FlexibleMember, reason="No reason provided"):
    await ModerationModule.kick_user(ctx, member, reason)


@bot.command()
@is_authorized("ban")
async def ban(ctx, member: FlexibleMember, reason="No reason provided"):
    await ModerationModule.ban_user(ctx, member, reason)


@bot.command()
@is_authorized("ban")
async def unban(ctx, user_id: int):
    await ModerationModule.unban_user(ctx, user_id)


@bot.command()
@is_authorized("server_admin")
async def slowmode(ctx, boolean: bool = True, seconds: int = 500):
    await ModerationModule.slowmode(ctx, boolean, seconds)


@bot.command()
@is_authorized("server_admin")
async def mute(ctx, member: FlexibleMember, minutes: int = 10, *, reason="No reason given"):
    await ModerationModule.timeout_user(ctx, member, minutes, reason)


@bot.command()
@is_authorized("server_admin")
async def unmute(ctx, member: FlexibleMember):
    await ModerationModule.remove_timeout(ctx, member)


@bot.command()
@is_authorized("server_admin")
async def softban(ctx, member: FlexibleMember, amount_of_days: int = 1, reason="No reason given"):
    await ModerationModule.softban_user(ctx, member, amount_of_days, reason)


@bot.command()
@is_authorized("server_admin")
async def whois(ctx, member: FlexibleMember):
    await ModerationModule.whois(ctx, member)


@bot.command()
@is_authorized("moderator")
async def warn(ctx, member: FlexibleMember, *, reason: str = "No reason provided"):
    await ModerationModule.warn_user(ctx, member, reason)


@bot.command()
@is_authorized("moderator")
async def warnings(ctx, member: FlexibleMember):
    await ModerationModule.view_warnings(ctx, member)


@bot.command()
@is_authorized("any")
async def roll(ctx, dice_type_and_amount: str, modifier: int = 0):
    await DndModule.roll_dice(ctx, dice_type_and_amount, modifier)


@bot.command()
@is_authorized("any")
async def roll_multiple(ctx, *,  dice_input):
    await DndModule.roll_multiple(ctx, dice_input)


@bot.command()
@is_authorized("any")
async def create_character(ctx, dnd_class: str, name: str):
    await DndModule.create_character(ctx, name, dnd_class)


@bot.command()
@is_authorized("any")
async def view_characters(ctx):
    await DndModule.view_characters(ctx)


@bot.command()
@is_authorized("any")
async def view_character(ctx, *, name: str):
    await DndModule.view_character(ctx, name)


@bot.command()
@is_authorized("any")
async def character_delete(ctx, *, name: str):
    await DndModule.character_delete(ctx, name)


@bot.command()
@is_authorized("any", guild_only=True)
async def marry(ctx, target_user: FlexibleMember):
    await FunModule.marry(ctx, target_user)


@bot.command()
@is_authorized("any", guild_only=True)
async def divorce(ctx, target_user: FlexibleMember):
    await FunModule.divorce(ctx, target_user)


@bot.command()
@is_authorized("any", guild_only=True)
async def adopt(ctx, target_user: FlexibleMember):
    await FunModule.adopt(ctx, target_user)


@bot.command()
@is_authorized("any", guild_only=True)
async def unadopt(ctx, target_user: FlexibleMember):
    await FunModule.unadopt(ctx, target_user)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def family(ctx):
    await FunModule.family(ctx)


@bot.command()
@is_authorized("bot_admin")
async def family_tree(ctx, member: FlexibleMember = None):
    await FunModule.family_tree(ctx, member)


@bot.command()
@is_authorized("any")
async def duel(ctx, target: FlexibleMember):
    await FunModule.duel(ctx, target)


@bot.command()
@is_authorized("any", guild_only=True)
async def quote(ctx):
    await FunModule.quote(ctx)


@bot.command()
@is_authorized("bot_admin")
async def add_quote(ctx, authors, *, quote_input: str):
    await BotAdminModule.add_quote(ctx, authors, quote_input)


@bot.command()
@is_authorized("bot_admin")
async def remove_quote(ctx, *, quote_input: str):
    await BotAdminModule.remove_quote(ctx, quote_input)


@bot.command()
@is_authorized("bot_admin")
async def add_gif(ctx, type: str, link: str):
    await BotAdminModule.add_gif(ctx, type, link)


@bot.command()
@is_authorized("bot_admin")
async def remove_gif(ctx, type: str, link: str):
    await BotAdminModule.remove_gif(ctx, type, link)


@bot.command()
@is_authorized("bot_admin")
async def add_gif_message(ctx, type: str, *, message: str):
    await BotAdminModule.add_gif_message(ctx, type, message)


@bot.command()
@is_authorized("bot_admin")
async def remove_gif_message(ctx, type: str, *, message: str):
    await BotAdminModule.remove_gif_message(ctx, type, message)


@bot.command()
@is_authorized("any")
async def punch(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "punch", target)


@bot.command()
@is_authorized("any")
async def kill(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "kill", target)


@bot.command()
@is_authorized("any")
async def slap(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "slap", target)


@bot.command()
@is_authorized("any")
async def tickle(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "tickle", target)


@bot.command()
@is_authorized("any")
async def wave(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "wave", target)


@bot.command()
@is_authorized("any")
async def happy(ctx):
    await FunModule.gif(ctx, "happy")


@bot.command()
@is_authorized("any")
async def cry(ctx):
    await FunModule.gif(ctx, "cry")


@bot.command(aliases=["smooch"])
@is_authorized("any")
async def kiss(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "kiss", target)


@bot.command()
@is_authorized("any")
async def hug(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "hug", target)


@bot.command()
@is_authorized("any")
async def sip(ctx):
    # No target needed!
    await FunModule.gif(ctx, "sip")


@bot.command()
@is_authorized("any")
async def spill(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "spill", target)


@bot.command()
@is_authorized("any")
async def shocked(ctx):
    await FunModule.gif(ctx, "shocked")


@bot.command()
@is_authorized("any")
async def pat(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "pat", target)


@bot.command()
@is_authorized("any")
async def cuddle(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "cuddle", target)


@bot.command()
@is_authorized("any")
async def cheer(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "cheer", target)


@bot.command()
@is_authorized("any")
async def bonk(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "bonk", target)


@bot.command()
@is_authorized("any")
async def bite(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "bite", target)


@bot.command()
@is_authorized("any")
async def stare(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "stare", target)


@bot.command()
@is_authorized("any")
async def explode(ctx):
    await FunModule.gif(ctx, "explode")


@bot.command()
@is_authorized("any")
async def sleep(ctx):
    await FunModule.gif(ctx, "sleep")


@bot.command()
@is_authorized("any")
async def purge(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "purge", target)


@bot.command()
@is_authorized("any")
async def stub_toe(ctx):
    await FunModule.gif(ctx, "stub_toe")


@bot.command()
@is_authorized("any")
async def grip(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "grip", target)


@bot.command()
@is_authorized("any")
async def throw(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "throw", target)


@bot.command()
@is_authorized("any")
async def wink(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "wink", target)


@bot.command()
@is_authorized("any")
async def salute(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "salute", target)


@bot.command()
@is_authorized("any")
async def snap(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "snap", target)


@bot.command()
@is_authorized("any")
async def mock(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "mock", target)


@bot.command()
@is_authorized("any")
async def yoink(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "yoink", target)


@bot.command()
@is_authorized("any")
async def popcorn(ctx):
    await FunModule.gif(ctx, "popcorn")


@bot.command()
@is_authorized("any")
async def frog(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "frog", target)


@bot.command()
@is_authorized("any")
async def thanks(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "thanks", target)


@bot.command()
@is_authorized("any")
async def absolutecinema(ctx):
    await FunModule.gif(ctx, "absolutecinema")


@bot.command()
@is_authorized("bot_admin")
async def smite(ctx, target: FlexibleMember = None):
    await FunModule.gif(ctx, "smite", target)


@bot.command()
@is_authorized("any")
async def eight_ball(ctx, *, question: str = "No question asked"):
    await FunModule.magic_eight_ball(ctx, question)


@bot.command()
@is_authorized("bot_admin")
async def add_eight_ball(ctx, *, response: str):
    await BotAdminModule.add_eight_ball(ctx, response)


@bot.command()
@is_authorized("bot_admin")
async def remove_eight_ball(ctx, *, response: str):
    await BotAdminModule.remove_eight_ball(ctx, response)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def shift(ctx):
    await EconomyModule.shift(ctx)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def beans(ctx):
    await EconomyModule.beans(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def tip(ctx, target: FlexibleMember, amount: float):
    await EconomyModule.tip(ctx, target, amount)


@bot.command()
@is_authorized("any", guild_only=True)
async def bet(ctx, target: FlexibleMember, amount: int, *, reason: str = None):
    await EconomyModule.bet(ctx, target, amount, reason)


@bot.command()
@is_authorized("any", guild_only=True)
async def betwinner(ctx, winner: FlexibleMember, opponent: Optional[FlexibleMember] = None):
    await EconomyModule.betwinner(ctx, winner, opponent)


@bot.command()
@is_authorized("any", guild_only=True)
async def cancelbet(ctx, target: Optional[FlexibleMember] = None):
    await EconomyModule.cancelbet(ctx, target)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def partner(ctx):
    await FunModule.partner(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def marriage_top(ctx):
    await FunModule.marriage_top(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def bean_top(ctx):
    await EconomyModule.bean_top(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def cafe_status(ctx):
    await EconomyModule.cafe_status(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def quote_list(ctx, user: str, amount: int = 1):
    if amount > 5 and not check_cmd_permission(ctx, "server_admin"):
        await ctx.send("You can only send five quotes at a time.")
        return
    await FunModule.quote_list(ctx, user, amount)


@bot.command()
@is_authorized("any", guild_only=True)
async def quote_count(ctx, user: str):
    await FunModule.quote_count(ctx, user)


@bot.command()
@is_authorized("any", guild_only=True)
async def quote_top(ctx):
    await FunModule.quote_top(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def quote_search(ctx, *, keyword: str):
    await FunModule.quote_search(ctx, keyword)


@bot.command()
@is_authorized("any", guild_only=True)
async def quote_stats(ctx):
    await FunModule.quote_stats(ctx)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def profile(ctx):
    await FunModule.profile(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def quotes(ctx, amount: int):
    if amount > 5 and not check_cmd_permission(ctx, "server_admin"):
        await ctx.send("You can only list 5 quotes at a time.")
        return
    await FunModule.quotes(ctx, amount)


@bot.command()
@is_authorized("any")
async def coinflip(ctx):
    await FunModule.coinflip(ctx)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def daily(ctx):
    await EconomyModule.daily(ctx)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def slots(ctx, bet: int):
    await EconomyModule.slots(ctx, bet)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def blackjack(ctx, bet: int):
    await EconomyModule.blackjack(ctx, bet)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def hilo(ctx, bet: int):
    await EconomyModule.hilo(ctx, bet)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def roulette(ctx, bet: int):
    await EconomyModule.roulette(ctx, bet)


@bot.command()
@is_authorized("any", guild_only=True)
async def lottery(ctx):
    await EconomyModule.lottery(ctx)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def lottery_buy(ctx, amount: int):
    await EconomyModule.lottery_buy(ctx, amount)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def bank(ctx):
    await EconomyModule.bank(ctx)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def deposit(ctx, amount: str):
    if amount.lower() == "all":
        amount = int(DataStorage.get_or_create_user(ctx.author.id).get_beans(str(ctx.guild.id)))
    else:
        try:
            amount = int(amount)
        except ValueError:
            await ctx.send("Please specify a valid amount or `all`.")
            return
    await EconomyModule.deposit(ctx, amount)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def withdraw(ctx, amount: int):
    await EconomyModule.withdraw(ctx, amount)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def bank_upgrade(ctx):
    await EconomyModule.bank_upgrade(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def rob(ctx, target: FlexibleMember):
    await EconomyModule.rob(ctx, target)


@bot.command(aliases=["testimony", "claim_testimony"])
@is_authorized("any", guild_only=True)
async def testimony_role(ctx):
    await FaithModule.testimony_role(ctx)


@bot.command()
@is_authorized("any")
async def send_anonymous_testimony(ctx, *, message: str):
    await FaithModule.send_testimony(ctx, message)


@bot.command()
@is_authorized("any")
async def verse(ctx):
    await FaithModule.random_verse(ctx, None)


@bot.command()
@is_authorized("any")
async def random_verse(ctx, version: str = None):
    await FaithModule.random_verse(ctx, version)


@bot.command()
@is_authorized("any")
async def verse_context(ctx):
    await FaithModule.verse_context(ctx)


@bot.command()
@is_authorized("any")
async def lookup_verse(ctx, version: str, book: str, reference: str):
    if ":" not in reference:
        await ctx.send("❌ Invalid format. Use `<chapter>:<verse>` (e.g. `.lookup_verse ASV John 3:16` or `.lookup_verse ASV John 3:14-18`).")
        return
    chapter, verse_num = reference.split(":", 1)
    if "-" in verse_num:
        try:
            start, end = verse_num.split("-", 1)
            count = int(end) - int(start) + 1
            if count > 8 and not check_cmd_permission(ctx, "server_admin"):
                await ctx.send("❌ Non-admins can only look up 8 verses at a time.")
                return
        except ValueError:
            await ctx.send("❌ Invalid verse range. Use `<chapter>:<start>-<end>` (e.g. `.lookup_verse ASV John 3:14-18`).")
            return
    await FaithModule.lookup_verse(ctx, version, book, chapter, verse_num)


@bot.command()
@is_authorized("any")
async def list_versions(ctx):
   await FaithModule.list_versions(ctx)


@bot.command()
@is_authorized("any")
async def verse_compare(ctx, version1: str, version2: str, book: str, reference: str):
    if ":" not in reference:
        await ctx.send("❌ Invalid format. Use `<chapter>:<verse>` (e.g. `.verse_compare KJV NIV John 3:16`).")
        return
    chapter, verse_num = reference.split(":", 1)
    await FaithModule.verse_compare(ctx, version1, version2, book, chapter, verse_num)


@bot.command()
@is_authorized("any")
async def verse_bookmark(ctx):
    await FaithModule.verse_bookmark(ctx)


@bot.command()
@is_authorized("any")
async def verse_bookmarks(ctx):
    await FaithModule.verse_bookmarks(ctx)


@bot.command()
@is_authorized("any")
async def verse_search(ctx, max_results: int, *, query: str):
    """
    Search the Bible index for a keyword or phrase.
    Optionally prefix with version:<VERSION> to filter.
    Example: .verse_search love one another
    Example: .verse_search version:NIV faith without works
    """
    if max_results > 5 and not check_cmd_permission(ctx, "server_admin"):
        await ctx.send("Sorry, you cannot request more than 5 results.")
        return
    await FaithModule.search_verses(ctx, max_results, query=query)


@bot.command()
@is_authorized("any", guild_only=True)
async def trivia(ctx, rounds: int):
    """Starts a trivia session using the user's config."""
    user_data = DataStorage.get_or_create_user(ctx.author.id)

    # Check if the user is an Admin
    is_admin = str(ctx.author.id) in DataStorage.administrators or ctx.author.guild_permissions.administrator

    # Apply limits to non-admins
    if not is_admin and rounds > 10:
        await ctx.send("🚫 Regular users can only start games with up to 10 rounds!")
        return

    if rounds < 1:
        await ctx.send("You need to play at least 1 round!")
        return

    await TriviaModule.start_session(ctx, rounds, user_data)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def quick_trivia(ctx, category: str = None):
    user_data = DataStorage.get_or_create_user(ctx.author.id)
    await TriviaModule.quick_trivia(ctx, user_data, category)


@bot.command()
@is_authorized("any", guild_only=True, dm_fallback=True)
async def trivia_stats(ctx):
    user_data = DataStorage.get_or_create_user(ctx.author.id)
    await TriviaModule.trivia_stats(ctx, user_data)


@bot.command(name="trivia_review")
@is_authorized("bot_admin")
async def trivia_review(ctx):
    await TriviaModule.review(ctx)


@bot.command()
@is_authorized("bot_admin")
async def add_trivia(ctx, category: str, sub_category: str, question: str, *, answers: str):
    """
    Usage: .add_trivia "category" "sub-category" "Question?" "answer 1, answer 2"
    Use quotes around each section if they contain spaces!
    """
    await BotAdminModule.add_trivia(ctx, category, sub_category, question, answers)


@bot.command()
@is_authorized("bot_admin")
async def remove_trivia(ctx, category: str, *args):
    """
    Usage: .remove_trivia <category> [sub_category] <question>
    Sub-category is optional. Wrap fields containing spaces in quotes.
    """
    if not args:
        await ctx.send("Usage: `.remove_trivia <category> [sub_category] <question>`")
        return
    if len(args) == 1:
        sub_category = None
        question = args[0]
    else:
        sub_category = args[0]
        question = " ".join(args[1:])
    await BotAdminModule.remove_trivia(ctx, category, sub_category, question)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def admin_tip(ctx, target: FlexibleUser, amount: float):
    """
    Usage: .admin_tip @user <amount>
    Grants beans to a user without requiring the admin to have funds.
    """
    await BotAdminModule.admin_tip(ctx, target, amount)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def admin_lottery_start(ctx, ticket_cap: str = "0", duration: str = "none", max_per_user: int = 10):
    """
    Usage: .admin_lottery_start [ticket_cap] [duration] [max_per_user]
    Examples:
      .admin_lottery_start 100 none 5   → cap at 100 tickets, 5 per user
      .admin_lottery_start 0 2h 10      → runs for 2 hours, 10 per user
      .admin_lottery_start 50 1h 3      → cap OR time, whichever first
    Durations: 30m, 1h, 2d, etc.  Use 0 or none to omit.
    """
    try:
        cap = None if ticket_cap in ("0", "none") else int(ticket_cap)
    except ValueError:
        await ctx.send("Invalid ticket cap. Use a number or 0 for no cap.")
        return
    secs = parse_duration(duration)
    if cap is None and secs is None:
        await ctx.send("Specify at least a ticket cap (non-zero) or a duration (e.g. `1h`). Both can be set.")
        return
    await BotAdminModule.admin_lottery_start(ctx, cap, secs, max_per_user)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def admin_lottery_cancel(ctx):
    """Cancel the active lottery and refund all ticket buyers."""
    await BotAdminModule.admin_lottery_cancel(ctx)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def admin_lottery_add(ctx, amount: int):
    await BotAdminModule.admin_lottery_add(ctx, amount)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def admin_jackpot_set(ctx, amount: int):
    await BotAdminModule.admin_jackpot_set(ctx, amount)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def admin_lottery_give(ctx, target: FlexibleUser, amount: int):
    await BotAdminModule.admin_lottery_give(ctx, target, amount)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def force_lottery_draw(ctx):
    await BotAdminModule.force_lottery_draw(ctx)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def force_marry(ctx, user1: FlexibleUser, user2: FlexibleUser):
    await BotAdminModule.force_marry(ctx, user1, user2)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def force_divorce(ctx, user1: FlexibleUser, user2: FlexibleUser):
    await BotAdminModule.force_divorce(ctx, user1, user2)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def force_adopt(ctx, parent_user: FlexibleUser, child_user: FlexibleUser):
    await BotAdminModule.force_adopt(ctx, parent_user, child_user)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def force_unadopt(ctx, user1: FlexibleUser, user2: FlexibleUser):
    await BotAdminModule.force_unadopt(ctx, user1, user2)


@bot.command()
@is_authorized("bot_admin", dm_fallback=True)
async def admin_user_info(ctx, target: FlexibleUser):
    await BotAdminModule.admin_user_info(ctx, target)


@bot.command()
@is_authorized("any")
async def trivia_config(ctx):
    """Opens the trivia configuration menu."""
    user_data = DataStorage.get_or_create_user(ctx.author.id)
    await TriviaModule.open_config(ctx, user_data)


# --- DM DEFAULT SERVER ---

class DmServerView(discord.ui.View):
    def __init__(self, user_data, shared_guilds):
        super().__init__(timeout=120)
        self.user_data = user_data
        options = []
        for g in shared_guilds:
            is_current = str(g.id) == (user_data.default_dm_guild_id or "")
            options.append(discord.SelectOption(label=g.name[:100], value=str(g.id), default=is_current))
        self.select_menu = discord.ui.Select(
            placeholder="Pick the server to route your DM commands to...",
            min_values=1, max_values=1, options=options,
        )
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != int(self.user_data.discord_id):
            await interaction.response.send_message("❌ This is not your menu!", ephemeral=True)
            return
        chosen = self.select_menu.values[0]
        self.user_data.default_dm_guild_id = chosen
        DataStorage.save_user_data()
        guild = bot.get_guild(int(chosen))
        name = guild.name if guild else chosen
        await interaction.response.send_message(f"✅ DM commands will now route to **{name}**.", ephemeral=True)


def _shared_guilds_for(user_id: int):
    return [g for g in bot.guilds if g.get_member(user_id) is not None]


async def _open_dm_server_picker(ctx, user_data):
    shared = _shared_guilds_for(int(user_data.discord_id))
    if not shared:
        await ctx.send("⚠️ You don't share any servers with me, so there's nothing to set as your DM default.")
        return
    current = user_data.default_dm_guild_id
    if current:
        cur_guild = bot.get_guild(int(current))
        cur_label = cur_guild.name if cur_guild else f"unknown ({current})"
        desc = f"**Current default:** {cur_label}\n\nPick a server below to change where DM commands like `.shift` and `.quick_trivia` send beans."
    else:
        desc = "Pick a server below to set where DM commands like `.shift` and `.quick_trivia` send beans."
    embed = discord.Embed(title="📬 DM Default Server", description=desc, color=discord.Color.blurple())
    await ctx.send(embed=embed, view=DmServerView(user_data, shared))


@bot.command(name="dm_server")
@is_authorized("any")
async def dm_server(ctx, action: Optional[str] = None):
    """Pick which server your DM-invoked commands route to. Use `.dm_server clear` to unset."""
    user_data = DataStorage.get_or_create_user(ctx.author.id)
    if action and action.lower() == "clear":
        user_data.default_dm_guild_id = None
        DataStorage.save_user_data()
        await ctx.send("✅ DM default server cleared.")
        return
    await _open_dm_server_picker(ctx, user_data)


# --- MUSIC COMMANDS ---

@bot.command()
@is_authorized("any", guild_only=True)
async def play(ctx, *, search: str):
    """Searches YouTube and plays a song! Usage: .play <song name>"""
    await MusicModule.play_song(ctx, search)


@bot.command()
@is_authorized("any", guild_only=True)
async def skip(ctx):
    """Skips the currently playing song."""
    await MusicModule.skip_song(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def queue(ctx):
    """Shows the currently playing song and upcoming queue."""
    await MusicModule.show_queue(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def pause(ctx):
    """Pauses or resumes the currently playing song."""
    await MusicModule.pause_song(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def loop(ctx):
    """Toggles looping for the current song."""
    await MusicModule.toggle_loop(ctx)


@bot.command()
@is_authorized("any", guild_only=True)
async def leave(ctx):
    """Clears the queue and makes the bot leave the voice channel."""
    await MusicModule.leave_channel(ctx)


@bot.command()
@is_authorized("bot_admin")  # Or "any" if you want to test it easily
async def host_check(ctx):
    """Diagnoses the cloud host's exact hardware."""
    import platform

    info = (
        f"**OS System:** {platform.system()}\n"
        f"**Machine (CPU):** {platform.machine()}\n"
        f"**Architecture:** {platform.architecture()[0]}\n"
        f"**Linux Release:** {platform.release()}"
    )

    await ctx.send(f"🖥️ **Host Diagnostic Report:**\n```text\n{info}\n```")


@bot.command()
@is_authorized("bot_admin")
async def debug_music(ctx):
    """Check if node and cookies are set up correctly."""
    import shutil
    import os

    bot_dir = os.path.dirname(os.path.abspath(__file__))
    node_path = shutil.which("node")
    cookies_exists = os.path.isfile(os.path.join(bot_dir, "cookies.txt"))
    ffmpeg_path = shutil.which("ffmpeg")

    # Check if 'node' file exists in bot dir even if not on PATH
    local_node = os.path.join(bot_dir, "node")
    local_node_exists = os.path.isfile(local_node)
    local_node_executable = os.access(local_node, os.X_OK) if local_node_exists else False

    # List files in bot dir that might be node-related
    node_files = [f for f in os.listdir(bot_dir) if 'node' in f.lower()]

    info = (
        f"**Bot directory:** {bot_dir}\n"
        f"**node on PATH:** {node_path or '❌ NOT FOUND'}\n"
        f"**node file in bot dir:** {local_node_exists}\n"
        f"**node is executable:** {local_node_executable}\n"
        f"**Node-related files:** {node_files or 'None'}\n"
        f"**cookies.txt exists:** {cookies_exists}\n"
        f"**ffmpeg on PATH:** {ffmpeg_path or '❌ NOT FOUND'}\n"
    )
    await ctx.send(f"🔧 **Music Debug Report:**\n{info}")


@bot.command()
@is_authorized("bot_admin")
async def debug_node(ctx):
    """Test if node actually runs and check yt-dlp version."""
    import subprocess
    import yt_dlp

    # Test node
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=5
        )
        node_info = f"✅ {result.stdout.strip()}" if result.returncode == 0 else f"❌ Exit code {result.returncode}: {result.stderr.strip()}"
    except Exception as e:
        node_info = f"❌ Failed to run: {e}"

    # Test node can actually execute JS
    try:
        result = subprocess.run(
            ["node", "-e", "console.log('ok')"],
            capture_output=True, text=True, timeout=5
        )
        js_info = f"✅ {result.stdout.strip()}" if result.returncode == 0 else f"❌ Exit code {result.returncode}: {result.stderr.strip()}"
    except Exception as e:
        js_info = f"❌ {e}"

    info = (
        f"**Node version:** {node_info}\n"
        f"**Node runs JS:** {js_info}\n"
        f"**yt-dlp version:** {yt_dlp.version.__version__}\n"
    )
    await ctx.send(f"🔧 **Node & yt-dlp Debug:**\n{info}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        # Prefix denials are silent in chat, same as an unknown command; console only.
        print(f"Permission denied: {ctx.author} ({ctx.author.id}) used .{ctx.invoked_with}")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument provided.")
    else:
        # Log unexpected errors
        print(f"Unexpected error: {error}")


config = dotenv_values(".env")
token = config.get("token")


# ===== SLASH COMMANDS =====

# --- Misc ---


@bot.tree.command(name="help", description="Show all modules, or list commands in a specific module")
async def slash_help(interaction: discord.Interaction, module: Optional[str] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    # Reuse the same help logic; check_cmd_permission works with InteractionContext
    # because it only needs ctx.author (which the adapter provides)
    if module is None:
        embed = discord.Embed(
            title="☕ CafeBot Modules",
            description="Use `/help module:<module>` to see the commands inside it!",
            color=discord.Color.gold()
        )
        for mod_name, mod_data in COMMAND_MODULES.items():
            can_see_module = any(check_cmd_permission(ctx, req) for _, _, req in mod_data["commands"])
            if can_see_module:
                embed.add_field(name=f"{mod_data['emoji']} {mod_name}", value=mod_data["description"], inline=False)
        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        await ctx.send(embed=embed)
    else:
        target_module = None
        for mod_name in COMMAND_MODULES:
            if mod_name.lower() == module.lower():
                target_module = mod_name
                break
        if not target_module:
            await ctx.send(f"❌ Could not find a module named `{module}`.")
            return
        mod_data = COMMAND_MODULES[target_module]
        embed = discord.Embed(
            title=f"{mod_data['emoji']} {target_module} Commands",
            description=mod_data["description"],
            color=discord.Color.gold()
        )
        visible_commands = 0
        for cmd_usage, cmd_desc, req_permission in mod_data["commands"]:
            if check_cmd_permission(ctx, req_permission):
                embed.add_field(name=cmd_usage, value=cmd_desc, inline=False)
                visible_commands += 1
        if visible_commands == 0:
            await ctx.send("🚫 You do not have permission to view commands in this module.")
            return
        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        await ctx.send(embed=embed)



# --- DnD ---

@bot.tree.command(name="roll", description="Roll dice using standard notation (e.g. 2d20, 1d6)")
async def slash_roll(interaction: discord.Interaction, dice_type_and_amount: str, modifier: int = 0):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await DndModule.roll_dice(ctx, dice_type_and_amount, modifier)


@bot.tree.command(name="roll_multiple", description="Roll multiple sets of dice separated by commas (e.g. 2d20 3, 1d8)")
async def slash_roll_multiple(interaction: discord.Interaction, dice_input: str):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await DndModule.roll_multiple(ctx, dice_input)


@bot.tree.command(name="create_character", description="Create and save a new D&D character")
async def slash_create_character(interaction: discord.Interaction, dnd_class: str, name: str):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await DndModule.create_character(ctx, name, dnd_class)


@bot.tree.command(name="view_characters", description="List all your saved D&D characters")
async def slash_view_characters(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await DndModule.view_characters(ctx)


@bot.tree.command(name="view_character", description="View a single D&D character's full sheet")
async def slash_view_character(interaction: discord.Interaction, name: str):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await DndModule.view_character(ctx, name)


@bot.tree.command(name="character_delete", description="Delete one of your saved D&D characters")
async def slash_character_delete(interaction: discord.Interaction, name: str):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await DndModule.character_delete(ctx, name)


# --- Fun ---

@bot.tree.command(name="marry", description="Send a marriage proposal to another user")
async def slash_marry(interaction: discord.Interaction, target_user: discord.Member):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await FunModule.marry(ctx, target_user)


@bot.tree.command(name="divorce", description="Divorce one of your partners")
async def slash_divorce(interaction: discord.Interaction, target_user: discord.Member):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await FunModule.divorce(ctx, target_user)


@bot.tree.command(name="partner", description="View your marriage certificate")
async def slash_partner(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await FunModule.partner(ctx)


@bot.tree.command(name="marriage_top", description="See the top 10 longest-running marriages")
async def slash_marriage_top(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await FunModule.marriage_top(ctx)


@bot.tree.command(name="adopt", description="Send or confirm an adoption request")
async def slash_adopt(interaction: discord.Interaction, target_user: discord.Member):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await FunModule.adopt(ctx, target_user)


@bot.tree.command(name="unadopt", description="Dissolve an adoption relationship")
async def slash_unadopt(interaction: discord.Interaction, target_user: discord.Member):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await FunModule.unadopt(ctx, target_user)


@bot.tree.command(name="family", description="View your adopted family")
async def slash_family(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await FunModule.family(ctx)


@bot.tree.command(name="family_tree", description="Render a bounded family tree for yourself or another user")
async def slash_family_tree(interaction: discord.Interaction, target_user: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "bot_admin"): return
    ctx = InteractionContext(interaction)
    await FunModule.family_tree(ctx, target_user)


@bot.tree.command(name="duel", description="Start a turn-based duel against another user")
async def slash_duel(interaction: discord.Interaction, target: discord.Member):
    if not await slash_auth_check(interaction, "any"): return
    await interaction.response.defer()
    ctx = InteractionContext(interaction)
    ctx._responded = True  # already deferred; all sends must go through followup
    await FunModule.duel(ctx, target)


@bot.tree.command(name="quote", description="Display a random quote from the database")
async def slash_quote(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await FunModule.quote(ctx)


@bot.tree.command(name="quotes", description="Display multiple random quotes at once (max 5)")
async def slash_quotes(interaction: discord.Interaction, amount: int):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    if amount > 5 and not await is_authorized_interaction(interaction, "server_admin"):
        await interaction.response.send_message("You can only list 5 quotes at a time.", ephemeral=True)
        return
    ctx = InteractionContext(interaction)
    await FunModule.quotes(ctx, amount)


@bot.tree.command(name="quote_list", description="Display random quotes from a specific person")
async def slash_quote_list(interaction: discord.Interaction, user: str, amount: int = 1):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    if amount > 5 and not await is_authorized_interaction(interaction, "server_admin"):
        await interaction.response.send_message("You can only send five quotes at a time.", ephemeral=True)
        return
    ctx = InteractionContext(interaction)
    await FunModule.quote_list(ctx, user, amount)


@bot.tree.command(name="quote_count", description="Check how many quotes a specific person has saved")
async def slash_quote_count(interaction: discord.Interaction, user: str):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await FunModule.quote_count(ctx, user)


@bot.tree.command(name="quote_top", description="See the top 10 people with the most quotes")
async def slash_quote_top(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await FunModule.quote_top(ctx)


@bot.tree.command(name="quote_search", description="Search quotes by keyword or phrase")
async def slash_quote_search(interaction: discord.Interaction, keyword: str):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await FunModule.quote_search(ctx, keyword)


@bot.tree.command(name="quote_stats", description="Show quote database statistics")
async def slash_quote_stats(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await FunModule.quote_stats(ctx)


@bot.tree.command(name="profile", description="View your personal profile dashboard")
async def slash_profile(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await FunModule.profile(ctx)


@bot.tree.command(name="eight_ball", description="Consult the Magic 8-Ball with a yes/no question")
async def slash_eight_ball(interaction: discord.Interaction, question: str = "No question asked"):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.magic_eight_ball(ctx, question)


@bot.tree.command(name="coinflip", description="Flip a coin and get Heads or Tails")
async def slash_coinflip(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.coinflip(ctx)


@bot.tree.command(name="trivia", description="Start a multiplayer trivia session in this channel")
async def slash_trivia(interaction: discord.Interaction, rounds: int):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    user_data = DataStorage.get_or_create_user(interaction.user.id)
    is_admin = str(interaction.user.id) in DataStorage.administrators or (
        interaction.guild and interaction.guild.get_member(interaction.user.id) and
        interaction.guild.get_member(interaction.user.id).guild_permissions.administrator
    )
    if not is_admin and rounds > 10:
        await interaction.response.send_message("🚫 Regular users can only start games with up to 10 rounds!", ephemeral=True)
        return
    if rounds < 1:
        await interaction.response.send_message("You need to play at least 1 round!", ephemeral=True)
        return
    # Defer so all trivia messages route through followup
    await interaction.response.defer()
    ctx = InteractionContext(interaction)
    await TriviaModule.start_session(ctx, rounds, user_data)


@bot.tree.command(name="trivia_config", description="Choose which trivia categories appear in your games")
async def slash_trivia_config(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    user_data = DataStorage.get_or_create_user(interaction.user.id)
    ctx = InteractionContext(interaction)
    await TriviaModule.open_config(ctx, user_data)


@bot.tree.command(name="quick_trivia", description="Single trivia question — first correct answer wins 10 beans")
async def slash_quick_trivia(interaction: discord.Interaction, category: Optional[str] = None):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    user_data = DataStorage.get_or_create_user(interaction.user.id)
    await interaction.response.defer()
    ctx = InteractionContext(interaction)
    await TriviaModule.quick_trivia(ctx, user_data, category)


@bot.tree.command(name="trivia_stats", description="View your personal trivia statistics")
async def slash_trivia_stats(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    user_data = DataStorage.get_or_create_user(interaction.user.id)
    ctx = InteractionContext(interaction)
    await TriviaModule.trivia_stats(ctx, user_data)


@bot.tree.command(name="trivia_review", description="[testing] Label gray-zone trivia guesses to train the matcher")
async def slash_trivia_review(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "bot_admin"): return
    ctx = InteractionContext(interaction)
    await interaction.response.defer()
    ctx._responded = True  # already deferred; all sends must go through followup
    await TriviaModule.review(ctx)


@bot.tree.command(name="dm_server", description="Pick which server your DM-invoked commands route to")
async def slash_dm_server(interaction: discord.Interaction, action: Optional[str] = None):
    if not await slash_auth_check(interaction, "any"): return
    user_data = DataStorage.get_or_create_user(interaction.user.id)
    ctx = InteractionContext(interaction)
    if action and action.lower() == "clear":
        user_data.default_dm_guild_id = None
        DataStorage.save_user_data()
        await ctx.send("✅ DM default server cleared.")
        return
    await _open_dm_server_picker(ctx, user_data)


# --- Emotes ---

@bot.tree.command(name="punch", description="Punch someone!")
async def slash_punch(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "punch", target)


@bot.tree.command(name="slap", description="Slap someone!")
async def slash_slap(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "slap", target)


@bot.tree.command(name="bonk", description="Bonk someone!")
async def slash_bonk(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "bonk", target)


@bot.tree.command(name="bite", description="Bite someone!")
async def slash_bite(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "bite", target)


@bot.tree.command(name="kill", description="Kill someone!")
async def slash_kill(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "kill", target)


@bot.tree.command(name="kiss", description="Kiss someone!")
async def slash_kiss(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "kiss", target)



@bot.tree.command(name="hug", description="Hug someone!")
async def slash_hug(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "hug", target)


@bot.tree.command(name="cuddle", description="Cuddle someone!")
async def slash_cuddle(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "cuddle", target)


@bot.tree.command(name="pat", description="Pat someone!")
async def slash_pat(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "pat", target)


@bot.tree.command(name="tickle", description="Tickle someone!")
async def slash_tickle(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "tickle", target)


@bot.tree.command(name="wave", description="Wave at someone!")
async def slash_wave(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "wave", target)


@bot.tree.command(name="cheer", description="Cheer for someone!")
async def slash_cheer(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "cheer", target)


@bot.tree.command(name="spill", description="Spill the tea!")
async def slash_spill(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "spill", target)


@bot.tree.command(name="stare", description="Stare at someone!")
async def slash_stare(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "stare", target)


@bot.tree.command(name="happy", description="Express happiness!")
async def slash_happy(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "happy")


@bot.tree.command(name="cry", description="Have a cry")
async def slash_cry(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "cry")


@bot.tree.command(name="sip", description="Take a sip")
async def slash_sip(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "sip")


@bot.tree.command(name="shocked", description="Express shock")
async def slash_shocked(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "shocked")


@bot.tree.command(name="explode", description="Explode!")
async def slash_explode(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "explode")


@bot.tree.command(name="sleep", description="Go to sleep")
async def slash_sleep(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "sleep")


@bot.tree.command(name="purge", description="Purge someone!")
async def slash_purge(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "purge", target)


@bot.tree.command(name="stub_toe", description="Stub your toe painfully")
async def slash_stub_toe(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "stub_toe")


@bot.tree.command(name="grip", description="Grab someone in an iron grip")
async def slash_grip(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "grip", target)


@bot.tree.command(name="throw", description="Throw someone across the room")
async def slash_throw(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "throw", target)


@bot.tree.command(name="wink", description="Wink at someone")
async def slash_wink(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "wink", target)


@bot.tree.command(name="salute", description="Salute someone")
async def slash_salute(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "salute", target)


@bot.tree.command(name="snap", description="Snap your fingers at someone")
async def slash_snap(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "snap", target)


@bot.tree.command(name="mock", description="Mock someone")
async def slash_mock(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "mock", target)


@bot.tree.command(name="yoink", description="Yoink something from someone")
async def slash_yoink(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "yoink", target)


@bot.tree.command(name="popcorn", description="Pull out popcorn and watch the chaos")
async def slash_popcorn(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "popcorn")


@bot.tree.command(name="frog", description="Become a frog")
async def slash_frog(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "frog", target)


@bot.tree.command(name="thanks", description="Thank someone")
async def slash_thanks(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FunModule.gif(ctx, "thanks", target)



# --- Economy ---

@bot.tree.command(name="shift", description="Work a shift at the cafe to earn Coffee Beans")
async def slash_shift(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.shift(ctx)


@bot.tree.command(name="beans", description="Check your current Coffee Bean balance")
async def slash_beans(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.beans(ctx)


@bot.tree.command(name="tip", description="Send some of your Coffee Beans to another user")
async def slash_tip(interaction: discord.Interaction, target: discord.Member, amount: float):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.tip(ctx, target, amount)


@bot.tree.command(name="bet", description="Offer or accept a peer-to-peer bet with another user")
async def slash_bet(interaction: discord.Interaction, target: discord.Member, amount: int, reason: str = None):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.bet(ctx, target, amount, reason)


@bot.tree.command(name="betwinner", description="Vote the winner of an active bet")
async def slash_betwinner(interaction: discord.Interaction, winner: discord.Member, opponent: discord.Member = None):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.betwinner(ctx, winner, opponent)


@bot.tree.command(name="cancelbet", description="Cancel a pending bet offer or forfeit an active bet")
async def slash_cancelbet(interaction: discord.Interaction, target: discord.Member = None):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.cancelbet(ctx, target)


@bot.tree.command(name="bean_top", description="See the top 10 richest users by Coffee Bean balance")
async def slash_bean_top(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.bean_top(ctx)


@bot.tree.command(name="daily", description="Claim your daily Coffee Bean reward")
async def slash_daily(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.daily(ctx)


@bot.tree.command(name="cafe_status", description="Show a server-wide snapshot of cafe activity")
async def slash_cafe_status(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.cafe_status(ctx)


@bot.tree.command(name="slots", description="Spin the slot machine and bet your Coffee Beans")
async def slash_slots(interaction: discord.Interaction, bet: int):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.slots(ctx, bet)


@bot.tree.command(name="blackjack", description="Play blackjack against the dealer and bet your Coffee Beans")
async def slash_blackjack(interaction: discord.Interaction, bet: int):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.blackjack(ctx, bet)


@bot.tree.command(name="hilo", description="Hi-Lo card game — guess if the next card is higher or lower")
async def slash_hilo(interaction: discord.Interaction, bet: int):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await interaction.response.defer()
    await EconomyModule.hilo(ctx, bet)


@bot.tree.command(name="roulette", description="[testing] Open the Roulette bet picker")
async def slash_roulette(interaction: discord.Interaction, bet: int):
    if not await slash_auth_check(interaction, "bot_admin", dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.roulette(ctx, bet)


@bot.tree.command(name="lottery", description="Check the current lottery pot and your ticket count")
async def slash_lottery(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.lottery(ctx)


@bot.tree.command(name="lottery_buy", description="Buy lottery tickets (50 beans each, max 10 per round)")
async def slash_lottery_buy(interaction: discord.Interaction, amount: int):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.lottery_buy(ctx, amount)


@bot.tree.command(name="bank", description="View your bank balance, cap, and upgrade info")
async def slash_bank(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.bank(ctx)


@bot.tree.command(name="deposit", description="Move beans from your wallet into your bank")
async def slash_deposit(interaction: discord.Interaction, amount: int):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.deposit(ctx, amount)


@bot.tree.command(name="withdraw", description="Move beans from your bank back to your wallet")
async def slash_withdraw(interaction: discord.Interaction, amount: int):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.withdraw(ctx, amount)


@bot.tree.command(name="bank_upgrade", description="Purchase the next bank tier to increase your storage cap")
async def slash_bank_upgrade(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True, dm_fallback=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.bank_upgrade(ctx)


@bot.tree.command(name="rob", description="Attempt to steal beans from another user's wallet")
async def slash_rob(interaction: discord.Interaction, target: discord.Member):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await EconomyModule.rob(ctx, target)


# --- Faith ---

@bot.tree.command(name="testimony_role", description="Claim the testimony role")
async def slash_testimony_role(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await FaithModule.testimony_role(ctx)


@bot.tree.command(name="send_anonymous_testimony", description="Send a testimony anonymously (use in DMs with the bot)")
async def slash_send_anonymous_testimony(interaction: discord.Interaction, message: str):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FaithModule.send_testimony(ctx, message)


@bot.tree.command(name="random_verse", description="Display a random Bible verse")
async def slash_random_verse(interaction: discord.Interaction, version: Optional[str] = None):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FaithModule.random_verse(ctx, version)


@bot.tree.command(name="verse_context", description="Show the 2 verses before and after the last random verse")
async def slash_verse_context(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FaithModule.verse_context(ctx)


@bot.tree.command(name="lookup_verse", description="Look up a Bible verse or range (e.g. ASV John 3:16 or ASV John 3:14-18)")
async def slash_lookup_verse(interaction: discord.Interaction, version: str, book: str, reference: str):
    if not await slash_auth_check(interaction, "any"): return
    if ":" not in reference:
        await interaction.response.send_message("❌ Invalid format. Use `<chapter>:<verse>` (e.g. `3:16` or `3:14-18`).", ephemeral=True)
        return
    chapter, verse_num = reference.split(":", 1)
    if "-" in verse_num:
        try:
            start, end = verse_num.split("-", 1)
            count = int(end) - int(start) + 1
            if count > 8 and not await is_authorized_interaction(interaction, "server_admin"):
                await interaction.response.send_message("❌ Non-admins can only look up 8 verses at a time.", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("❌ Invalid verse range. Use `<chapter>:<start>-<end>` (e.g. `3:14-18`).", ephemeral=True)
            return
    ctx = InteractionContext(interaction)
    await FaithModule.lookup_verse(ctx, version, book, chapter, verse_num)


@bot.tree.command(name="list_versions", description="List all Bible versions currently loaded")
async def slash_list_versions(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FaithModule.list_versions(ctx)


@bot.tree.command(name="verse_search", description="Search the Bible for verses containing a keyword or phrase")
async def slash_verse_search(interaction: discord.Interaction, max_results: int, query: str):
    if not await slash_auth_check(interaction, "any"): return
    if max_results > 5 and not await is_authorized_interaction(interaction, "server_admin"):
        await interaction.response.send_message("Sorry, you cannot request more than 5 results.", ephemeral=True)
        return
    ctx = InteractionContext(interaction)
    await FaithModule.search_verses(ctx, max_results, query=query)


@bot.tree.command(name="verse_compare", description="Compare the same verse in two translations side by side")
async def slash_verse_compare(interaction: discord.Interaction, version1: str, version2: str, book: str, reference: str):
    if not await slash_auth_check(interaction, "any"): return
    if ":" not in reference:
        await interaction.response.send_message("❌ Invalid format. Use `<chapter>:<verse>` (e.g. `3:16`).", ephemeral=True)
        return
    chapter, verse_num = reference.split(":", 1)
    ctx = InteractionContext(interaction)
    await FaithModule.verse_compare(ctx, version1, version2, book, chapter, verse_num)


@bot.tree.command(name="verse_bookmark", description="Bookmark the last randomly generated verse")
async def slash_verse_bookmark(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FaithModule.verse_bookmark(ctx)


@bot.tree.command(name="verse_bookmarks", description="List all your bookmarked Bible verses")
async def slash_verse_bookmarks(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any"): return
    ctx = InteractionContext(interaction)
    await FaithModule.verse_bookmarks(ctx)


# --- Music ---

@bot.tree.command(name="play", description="Search YouTube and add a song to the queue")
async def slash_play(interaction: discord.Interaction, search: str):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    await interaction.response.defer()
    ctx = InteractionContext(interaction)
    await MusicModule.play_song(ctx, search)


@bot.tree.command(name="skip", description="Skip the currently playing song")
async def slash_skip(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await MusicModule.skip_song(ctx)


@bot.tree.command(name="queue", description="Show the current music queue")
async def slash_queue(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await MusicModule.show_queue(ctx)


@bot.tree.command(name="pause", description="Pause or resume the currently playing song")
async def slash_pause(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await MusicModule.pause_song(ctx)


@bot.tree.command(name="loop", description="Toggle loop mode for the current song")
async def slash_loop(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await MusicModule.toggle_loop(ctx)


@bot.tree.command(name="leave", description="Clear the queue and disconnect from the voice channel")
async def slash_leave(interaction: discord.Interaction):
    if not await slash_auth_check(interaction, "any", guild_only=True): return
    ctx = InteractionContext(interaction)
    await MusicModule.leave_channel(ctx)


bot.run(token)
