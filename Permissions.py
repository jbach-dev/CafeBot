import discord
from discord.ext import commands

import DataStorage

# Permission checks used by prefix commands, slash commands and the help menu.
# Lives outside botMain.py so any module can import it.

_DM_REJECT_MESSAGE = "⚠️ This command must be used in a server, not a DM."
_DM_FALLBACK_UNSET_MESSAGE = "⚠️ Set a default server first with `.dm_server` to use this command in DMs."
_DM_FALLBACK_STALE_MESSAGE = "⚠️ Your DM default server is no longer valid. Run `.dm_server` to pick a new one."


# Extra permission levels added with register_permission_level().
# name -> (check, admin_bypass), where check(user_id, guild) returns True or False.
PERMISSION_LEVELS = {}


def register_permission_level(name, check, admin_bypass=True):
    # admin_bypass=False: being in the .env administrators list does not pass this level.
    PERMISSION_LEVELS[name] = (check, admin_bypass)


def has_permission(user, guild, required_type):
    is_admin = str(user.id) in DataStorage.administrators

    # Extra levels are checked first
    if required_type in PERMISSION_LEVELS:
        check, admin_bypass = PERMISSION_LEVELS[required_type]
        if admin_bypass and is_admin:
            return True
        return check(user.id, guild)

    if is_admin:
        return True

    if required_type == "any":
        # Anyone can use this command
        return True

    if guild is None:
        return False
    member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
    if member is None:
        return False

    if required_type == "server_admin":
        return member.guild_permissions.administrator
    elif required_type == "moderator":
        return member.guild_permissions.manage_messages
    elif required_type == "kick":
        return member.guild_permissions.kick_members
    elif required_type == "ban":
        return member.guild_permissions.ban_members
    elif required_type == "mute":
        return member.guild_permissions.moderate_members
    elif required_type == "bot_admin":
        # Commands for only bot administrators
        return False

    # Default: deny access
    return False


def _needs_guild(required_type, guild_only):
    # The built-in levels other than "any" are server permissions, so they need a server.
    # Extra levels decide for themselves (their check gets guild=None in DMs).
    return guild_only or (required_type != "any" and required_type not in PERMISSION_LEVELS)


def _resolve_dm_fallback(bot, user_id: int):
    """For a DM invocation with dm_fallback=True, return (ok, error_message).
    ok=True means the gate should let the command through; the resolved guild id is on the User."""
    user_data = DataStorage.get_or_create_user(user_id)
    target = user_data.default_dm_guild_id
    if not target:
        return False, _DM_FALLBACK_UNSET_MESSAGE
    guild = bot.get_guild(int(target))
    if guild is None or guild.get_member(user_id) is None:
        return False, _DM_FALLBACK_STALE_MESSAGE
    return True, None


def is_authorized(required_type: str = "any", guild_only: bool = False, dm_fallback: bool = False):
    async def predicate(ctx):
        # Guild gate — applies to all non-"any" auth types implicitly, plus any "any" command
        # that opts in. Even bot-admin DMs are blocked because per-guild commands need guild context.
        if _needs_guild(required_type, guild_only) and ctx.guild is None:
            if dm_fallback:
                ok, err = _resolve_dm_fallback(ctx.bot, ctx.author.id)
                if not ok:
                    await ctx.send(err)
                    return False
            else:
                await ctx.send(_DM_REJECT_MESSAGE)
                return False

        # Returning False here makes on_command_error log the denial (nothing is sent)
        return has_permission(ctx.author, ctx.guild, required_type)

    return commands.check(predicate)


async def is_authorized_interaction(interaction: discord.Interaction, required_type: str) -> bool:
    return has_permission(interaction.user, interaction.guild, required_type)


async def slash_auth_check(interaction: discord.Interaction, required_type: str, guild_only: bool = False, dm_fallback: bool = False) -> bool:
    if _needs_guild(required_type, guild_only) and interaction.guild is None:
        if dm_fallback:
            ok, err = _resolve_dm_fallback(interaction.client, interaction.user.id)
            if not ok:
                await interaction.response.send_message(err, ephemeral=True)
                return False
        else:
            await interaction.response.send_message(_DM_REJECT_MESSAGE, ephemeral=True)
            return False
    if not await is_authorized_interaction(interaction, required_type):
        await interaction.response.send_message("🚫 You don't have permission to use this command.", ephemeral=True)
        return False
    return True


# Helper function to check if a user can see a command in the help menu
def check_cmd_permission(ctx, required_type):
    if required_type not in PERMISSION_LEVELS:
        # Bot admins can see everything
        if str(ctx.author.id) in DataStorage.administrators:
            return True

        if isinstance(ctx.channel, discord.DMChannel):
            return True  # Temporary fix, users will be able to see commands they normally cant

    return has_permission(ctx.author, ctx.guild, required_type)
