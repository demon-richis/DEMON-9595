import asyncio
import json
import logging
from typing import List

import discord
from discord.ext import commands


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("anti_nuke_bot")


###########################
# Configuration variables #
###########################

# Replace this token with your bot's token obtained from the Discord Developer Portal.
BOT_TOKEN = "discord_token"

# List of user IDs (integers) that are trusted and exempt from anti‑nuke actions.
# Populate this list with the IDs of server owners, administrators or bots
# you trust.  If someone in this list deletes a channel/role, no action
# will be taken.
TRUSTED_USER_IDS: List[int] = []

# A simple prefix for commands – change if you prefer something else.
COMMAND_PREFIX = "!"


#########################################
# Helper functions for audit log lookups #
#########################################

async def get_audit_log_entry_for_channel_delete(
    guild: discord.Guild, channel_id: int
) -> discord.AuditLogEntry | None:
    """Return the most recent audit log entry for a deleted channel.

    This function fetches the last audit log entry for a channel deletion and
    returns it if the target's ID matches the deleted channel.  Because
    Discord audit logs are rate limited, this fetch is restricted to a
    single entry.

    Args:
        guild: The guild (server) where the deletion occurred.
        channel_id: The ID of the deleted channel.

    Returns:
        A `discord.AuditLogEntry` if found, else `None`.
    """
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
        if entry.target.id == channel_id:
            return entry
    return None


async def get_audit_log_entry_for_role_delete(
    guild: discord.Guild, role_id: int
) -> discord.AuditLogEntry | None:
    """Return the most recent audit log entry for a deleted role.

    Args:
        guild: The guild (server) where the deletion occurred.
        role_id: The ID of the deleted role.

    Returns:
        A `discord.AuditLogEntry` if found, else `None`.
    """
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_delete):
        if entry.target.id == role_id:
            return entry
    return None


########################################
# Bot setup and event definitions      #
########################################

# Intents specify which gateway events your bot receives.  Audit logs
# require the `guilds` and `guild_messages` intents; channel and role
# deletion events require `guilds`.  We also enable members and
# messages for commands.
intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True
intents.members = True


bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


@bot.event
async def on_ready() -> None:
    """Log a message when the bot is ready."""
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info("Anti‑nuke bot is ready to protect your server.")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    """Handle the deletion of a guild channel.

    When a channel is deleted, the bot checks the audit log to determine
    who performed the deletion.  If the user is not trusted, it will
    recreate the channel and ban the user.
    """
    guild = channel.guild
    logger.warning(f"Channel deleted: {channel.name} (ID: {channel.id}) in guild {guild.name}")
    # Fetch the audit log entry to identify the deleter
    entry = await get_audit_log_entry_for_channel_delete(guild, channel.id)
    if entry is None:
        logger.warning("Could not find audit log entry for channel deletion.")
        return

    user = entry.user  # type: ignore[attr-defined]
    if user.id in TRUSTED_USER_IDS:
        logger.info(f"Trusted user {user} deleted channel; no action taken.")
        return

    # Recreate the channel with the same attributes where possible
    try:
        category = channel.category
        overwrites = channel.overwrites
        position = channel.position
        # Determine the type of channel and recreate accordingly
        if isinstance(channel, discord.TextChannel):
            new_channel = await guild.create_text_channel(
                name=channel.name,
                topic=channel.topic,
                category=category,
                overwrites=overwrites,
                position=position,
                slowmode_delay=channel.slowmode_delay,
                nsfw=channel.is_nsfw()
            )
        elif isinstance(channel, discord.VoiceChannel):
            new_channel = await guild.create_voice_channel(
                name=channel.name,
                category=category,
                overwrites=overwrites,
                position=position,
                bitrate=channel.bitrate,
                user_limit=channel.user_limit
            )
        elif isinstance(channel, discord.CategoryChannel):
            new_channel = await guild.create_category(
                name=channel.name,
                overwrites=overwrites,
                position=position
            )
        else:
            # Other channel types (e.g. stage channels, forums) can be handled similarly
            new_channel = await guild.create_text_channel(name=channel.name)
        logger.info(f"Recreated channel {new_channel.name} (ID: {new_channel.id})")
    except Exception as e:
        logger.exception(f"Failed to recreate channel {channel.name}: {e}")

    # Punish the user who deleted the channel by banning them
    try:
        await guild.ban(user, reason="Anti‑nuke: unauthorized channel deletion")
        logger.info(f"Banned user {user} for deleting channel {channel.name}")
    except Exception as e:
        logger.exception(f"Failed to ban user {user}: {e}")


@bot.event
async def on_guild_role_delete(role: discord.Role) -> None:
    """Handle the deletion of a guild role.

    When a role is deleted, the bot checks the audit log to determine
    who performed the deletion.  If the user is not trusted, it will
    recreate the role and ban the user.
    """
    guild = role.guild
    logger.warning(f"Role deleted: {role.name} (ID: {role.id}) in guild {guild.name}")
    # Fetch the audit log entry to identify the deleter
    entry = await get_audit_log_entry_for_role_delete(guild, role.id)
    if entry is None:
        logger.warning("Could not find audit log entry for role deletion.")
        return
    user = entry.user  # type: ignore[attr-defined]
    if user.id in TRUSTED_USER_IDS:
        logger.info(f"Trusted user {user} deleted role; no action taken.")
        return

    # Recreate the role with the same attributes where possible
    try:
        new_role = await guild.create_role(
            name=role.name,
            colour=role.colour,
            hoist=role.hoist,
            mentionable=role.mentionable,
            permissions=role.permissions,
            position=role.position
        )
        logger.info(f"Recreated role {new_role.name} (ID: {new_role.id})")
    except Exception as e:
        logger.exception(f"Failed to recreate role {role.name}: {e}")

    # Punish the user who deleted the role by banning them
    try:
        await guild.ban(user, reason="Anti‑nuke: unauthorized role deletion")
        logger.info(f"Banned user {user} for deleting role {role.name}")
    except Exception as e:
        logger.exception(f"Failed to ban user {user}: {e}")


#########################
# Command implementations
#########################

@bot.command(name="trust")
@commands.has_permissions(administrator=True)
async def add_trusted(ctx: commands.Context, user: discord.User) -> None:
    """Add a user to the trusted list.

    Only administrators can run this command.  The user specified will
    be exempt from anti‑nuke punishments.  You must reload or restart
    the bot to persist changes across sessions or implement your own
    storage mechanism (JSON/DB) here.

    Usage: `!trust @User` where `@User` is a mention or ID.
    """
    if user.id not in TRUSTED_USER_IDS:
        TRUSTED_USER_IDS.append(user.id)
        await ctx.send(f"Added {user.mention} to the trusted user list.")
        logger.info(f"Added {user} to trusted users.")
    else:
        await ctx.send(f"{user.mention} is already trusted.")


@bot.command(name="untrust")
@commands.has_permissions(administrator=True)
async def remove_trusted(ctx: commands.Context, user: discord.User) -> None:
    """Remove a user from the trusted list.

    Only administrators can run this command.  The user specified will
    no longer be exempt from anti‑nuke punishments.

    Usage: `!untrust @User` where `@User` is a mention or ID.
    """
    if user.id in TRUSTED_USER_IDS:
        TRUSTED_USER_IDS.remove(user.id)
        await ctx.send(f"Removed {user.mention} from the trusted user list.")
        logger.info(f"Removed {user} from trusted users.")
    else:
        await ctx.send(f"{user.mention} is not currently trusted.")


@bot.command(name="trusted")
@commands.has_permissions(administrator=True)
async def list_trusted(ctx: commands.Context) -> None:
    """List all trusted users.

    This command lists the IDs of all users who are currently exempt from
    anti‑nuke actions.  Administrators can use this to verify who is
    trusted.
    """
    if not TRUSTED_USER_IDS:
        await ctx.send("No users are currently trusted.")
        return
    trusted_mentions = []
    for user_id in TRUSTED_USER_IDS:
        user = ctx.guild.get_member(user_id)
        trusted_mentions.append(user.mention if user else str(user_id))
    await ctx.send("Trusted users: " + ", ".join(trusted_mentions))


def main() -> None:
    """Entry point to run the bot."""
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Please replace 'YOUR_BOT_TOKEN_HERE' with your actual Discord bot token.")
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
