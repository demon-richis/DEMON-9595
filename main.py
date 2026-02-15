import asyncio
import os
from typing import Dict

import discord
import wavelink

from system import MusicController, SQLiteStore


class DemonBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)

        self.db = SQLiteStore(os.getenv("SQLITE_DB_PATH", "music_bot.db"))
        dj_role = os.getenv("DJ_ROLE_ID", "").strip()
        self.music = MusicController(self, self.db, int(dj_role) if dj_role.isdigit() else None)
        self.user_message_window: Dict[int, float] = {}

    async def setup_hook(self) -> None:
        await self.db.setup()
        host = os.getenv("LAVALINK_HOST", "127.0.0.1")
        port = int(os.getenv("LAVALINK_PORT", "2333"))
        password = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
        await self.music.connect_lavalink(host, port, password)

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        for guild in self.guilds:
            await self.music.restore_for_guild(guild)
        print(f"Logged in as {self.user} ({self.user.id})")

    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        await self.music.handle_track_end(payload)

    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        await self.music.handle_track_exception(payload)

    async def on_wavelink_node_closed(self, payload: wavelink.NodeClosedEventPayload) -> None:
        await self.db.log_error(None, "lavalink_node_closed", str(payload.code))

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        now = asyncio.get_event_loop().time()
        last = self.user_message_window.get(message.author.id, 0)
        if now - last < 0.6:
            return
        self.user_message_window[message.author.id] = now

        content = message.content.strip()
        if not content:
            return

        parts = content.split()
        command = parts[0].lower()
        args = parts[1:]

        known = {
            "play",
            "pause",
            "resume",
            "skip",
            "stop",
            "queue",
            "remove",
            "clear",
            "shuffle",
            "loop",
            "volume",
            "seek",
            "dj",
            "filter",
            "speed",
            "pitch",
        }
        if command not in known:
            return

        if await self.db.is_on_cooldown(message.guild.id, message.author.id, command, 1.2):
            return

        try:
            if command == "play":
                if not args:
                    await message.channel.send("Usage: play <url or search>")
                    return
                response = await self.music.play_query(
                    message.guild,
                    message.author,
                    message.channel,
                    " ".join(args),
                )
                await message.channel.send(response)
            elif command in {"pause", "resume"}:
                ok, text = await self.music.toggle_pause(message.guild.id, message.author)
                await message.channel.send(text)
            elif command == "skip":
                ok, text = await self.music.skip(message.guild.id, message.author)
                await message.channel.send(text)
            elif command == "stop":
                await message.channel.send(await self.music.stop(message.guild.id, message.author))
            elif command == "queue":
                await message.channel.send(self.music.get_queue_text(message.guild.id))
            elif command == "remove":
                if not args or not args[0].isdigit():
                    await message.channel.send("Usage: remove <index>")
                    return
                await message.channel.send(await self.music.remove(message.guild.id, int(args[0])))
            elif command == "clear":
                await message.channel.send(await self.music.clear(message.guild.id))
            elif command == "shuffle":
                await message.channel.send(await self.music.shuffle(message.guild.id))
            elif command == "loop":
                mode = await self.music.toggle_loop(message.guild.id)
                await message.channel.send(f"Loop mode: {mode}")
            elif command == "volume":
                if not args or not args[0].isdigit():
                    await message.channel.send("Usage: volume <0-200>")
                    return
                await message.channel.send(
                    await self.music.set_volume(message.guild.id, message.author, int(args[0]))
                )
            elif command == "seek":
                if not args or not args[0].lstrip("-").isdigit():
                    await message.channel.send("Usage: seek <seconds>")
                    return
                await message.channel.send(
                    await self.music.seek(message.guild.id, message.author, int(args[0]))
                )
            elif command == "dj":
                if not message.author.guild_permissions.administrator:
                    await message.channel.send("Administrator only.")
                    return
                if not args:
                    await message.channel.send("Usage: dj <role_id|off>")
                    return
                if args[0].lower() == "off":
                    await self.db.set_dj_role(message.guild.id, None)
                    await message.channel.send("DJ role disabled.")
                elif args[0].isdigit():
                    await self.db.set_dj_role(message.guild.id, int(args[0]))
                    await message.channel.send(f"DJ role set to {args[0]}")
                else:
                    await message.channel.send("Provide a valid role ID.")
            elif command == "filter":
                if not args:
                    await message.channel.send("Usage: filter <bass|vocal|nightcore|8d|karaoke|tremolo|vibrato|off>")
                    return
                await message.channel.send(await self.music.set_filter(message.guild.id, args[0].lower()))
            elif command in {"speed", "pitch"}:
                if not args:
                    await message.channel.send(f"Usage: {command} <value>")
                    return
                try:
                    value = float(args[0])
                except ValueError:
                    await message.channel.send("Value must be numeric.")
                    return
                speed = value if command == "speed" else 1.0
                pitch = value if command == "pitch" else 1.0
                await message.channel.send(await self.music.set_speed_pitch(message.guild.id, speed, pitch))
        except Exception as exc:
            await self.db.log_error(message.guild.id, f"command:{command}", str(exc))
            await message.channel.send("Command failed. Error logged.")


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is required")

    bot = DemonBot()
    bot.run(token)
