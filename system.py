import asyncio
import random
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

import discord
import wavelink


@dataclass
class StoredTrack:
    title: str
    uri: str
    author: str
    length: int
    artwork: Optional[str]
    requester_id: int


class SQLiteStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: Optional[sqlite3.Connection] = None
        self.lock = asyncio.Lock()

    async def setup(self) -> None:
        await asyncio.to_thread(self._setup_sync)

    def _setup_sync(self) -> None:
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.cursor()
        cur.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                dj_role_id INTEGER,
                volume INTEGER DEFAULT 100,
                idle_timeout INTEGER DEFAULT 120,
                last_text_channel INTEGER,
                player_message_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS persistent_queue (
                guild_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                title TEXT NOT NULL,
                uri TEXT NOT NULL,
                author TEXT,
                length INTEGER,
                artwork TEXT,
                requester_id INTEGER,
                PRIMARY KEY (guild_id, position)
            );

            CREATE TABLE IF NOT EXISTS playback_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                uri TEXT NOT NULL,
                requester_id INTEGER,
                played_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                context TEXT NOT NULL,
                error TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS command_cooldowns (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                command TEXT NOT NULL,
                last_used REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id, command)
            );
            """
        )
        self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await asyncio.to_thread(self.conn.close)

    async def _execute(self, query: str, params: Tuple = ()) -> List[sqlite3.Row]:
        async with self.lock:
            return await asyncio.to_thread(self._execute_sync, query, params)

    def _execute_sync(self, query: str, params: Tuple = ()) -> List[sqlite3.Row]:
        assert self.conn is not None
        cur = self.conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        self.conn.commit()
        return rows

    async def get_guild_settings(self, guild_id: int) -> sqlite3.Row:
        await self._execute("INSERT OR IGNORE INTO guild_settings(guild_id) VALUES (?)", (guild_id,))
        rows = await self._execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return rows[0]

    async def set_dj_role(self, guild_id: int, role_id: Optional[int]) -> None:
        await self._execute(
            "UPDATE guild_settings SET dj_role_id = ? WHERE guild_id = ?", (role_id, guild_id)
        )

    async def set_volume(self, guild_id: int, volume: int) -> None:
        await self._execute("UPDATE guild_settings SET volume = ? WHERE guild_id = ?", (volume, guild_id))

    async def set_player_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        await self._execute(
            "UPDATE guild_settings SET last_text_channel = ?, player_message_id = ? WHERE guild_id = ?",
            (channel_id, message_id, guild_id),
        )

    async def save_queue(self, guild_id: int, tracks: List[StoredTrack]) -> None:
        await self._execute("DELETE FROM persistent_queue WHERE guild_id = ?", (guild_id,))
        for pos, track in enumerate(tracks):
            await self._execute(
                """
                INSERT INTO persistent_queue(guild_id, position, title, uri, author, length, artwork, requester_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, pos, track.title, track.uri, track.author, track.length, track.artwork, track.requester_id),
            )

    async def load_queue(self, guild_id: int) -> List[StoredTrack]:
        rows = await self._execute(
            "SELECT * FROM persistent_queue WHERE guild_id = ? ORDER BY position ASC", (guild_id,)
        )
        return [
            StoredTrack(
                title=row["title"],
                uri=row["uri"],
                author=row["author"] or "Unknown",
                length=row["length"] or 0,
                artwork=row["artwork"],
                requester_id=row["requester_id"] or 0,
            )
            for row in rows
        ]

    async def add_history(self, guild_id: int, track: StoredTrack) -> None:
        await self._execute(
            """
            INSERT INTO playback_history(guild_id, title, uri, requester_id, played_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, track.title, track.uri, track.requester_id, datetime.now(timezone.utc).isoformat()),
        )

    async def log_error(self, guild_id: Optional[int], context: str, error: str) -> None:
        await self._execute(
            "INSERT INTO error_logs(guild_id, context, error, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, context, error[:1900], datetime.now(timezone.utc).isoformat()),
        )

    async def is_on_cooldown(self, guild_id: int, user_id: int, command: str, cooldown_s: float) -> bool:
        rows = await self._execute(
            "SELECT last_used FROM command_cooldowns WHERE guild_id = ? AND user_id = ? AND command = ?",
            (guild_id, user_id, command),
        )
        now = time.time()
        if rows and now - rows[0]["last_used"] < cooldown_s:
            return True
        await self._execute(
            """
            INSERT INTO command_cooldowns(guild_id, user_id, command, last_used)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, command) DO UPDATE SET last_used = excluded.last_used
            """,
            (guild_id, user_id, command, now),
        )
        return False


class GuildSession:
    def __init__(self) -> None:
        self.queue: Deque[wavelink.Playable] = deque()
        self.loop_mode: str = "off"
        self.default_volume: int = 100
        self.last_track: Optional[wavelink.Playable] = None
        self.idle_task: Optional[asyncio.Task] = None
        self.retry_attempts: int = 0


class PlayerControls(discord.ui.View):
    def __init__(self, controller: "MusicController", guild_id: int) -> None:
        super().__init__(timeout=None)
        self.controller = controller
        self.guild_id = guild_id

    @discord.ui.button(label="Pause/Resume", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        ok, msg = await self.controller.toggle_pause(self.guild_id, interaction.user)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        ok, msg = await self.controller.skip(self.guild_id, interaction.user)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        mode = await self.controller.toggle_loop(self.guild_id)
        await interaction.response.send_message(f"Loop mode: {mode}", ephemeral=True)

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.success)
    async def queue(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        text = self.controller.get_queue_text(self.guild_id)
        await interaction.response.send_message(text, ephemeral=True)


class MusicController:
    def __init__(self, bot: discord.Client, store: SQLiteStore, dj_role_id: Optional[int]) -> None:
        self.bot = bot
        self.store = store
        self.dj_role_id = dj_role_id
        self.sessions: Dict[int, GuildSession] = {}

    async def connect_lavalink(self, host: str, port: int, password: str) -> None:
        node = wavelink.Node(uri=f"http://{host}:{port}", password=password)
        await wavelink.Pool.connect(nodes=[node], client=self.bot)

    def _session(self, guild_id: int) -> GuildSession:
        if guild_id not in self.sessions:
            self.sessions[guild_id] = GuildSession()
        return self.sessions[guild_id]

    async def ensure_player(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> wavelink.Player:
        player: Optional[wavelink.Player] = guild.voice_client  # type: ignore[assignment]
        if player and player.channel.id != voice_channel.id:
            await player.move_to(voice_channel)
        if player is None:
            player = await voice_channel.connect(cls=wavelink.Player, self_deaf=True)
        return player

    async def restore_for_guild(self, guild: discord.Guild) -> None:
        tracks = await self.store.load_queue(guild.id)
        if not tracks:
            return
        session = self._session(guild.id)
        for item in tracks:
            rebuilt = wavelink.Playable(
                {
                    "info": {
                        "title": item.title,
                        "author": item.author,
                        "uri": item.uri,
                        "length": item.length,
                        "artworkUrl": item.artwork,
                        "sourceName": "youtube",
                    }
                }
            )
            rebuilt.extras = {"requester_id": item.requester_id}
            session.queue.append(rebuilt)

    async def play_query(
        self,
        guild: discord.Guild,
        user: discord.Member,
        text_channel: discord.TextChannel,
        query: str,
    ) -> str:
        if not user.voice or not user.voice.channel:
            return "Join a voice channel first."

        settings = await self.store.get_guild_settings(guild.id)
        session = self._session(guild.id)
        session.default_volume = settings["volume"] or 100
        player = await self.ensure_player(guild, user.voice.channel)

        if not wavelink.Pool.nodes:
            return "Lavalink is not connected."

        results = await wavelink.Playable.search(query)
        if not results:
            return "No tracks found."

        added = 0
        incoming: List[wavelink.Playable] = []
        if isinstance(results, wavelink.Playlist):
            incoming.extend(results.tracks)
        else:
            incoming.append(results[0])

        existing_uris = {t.uri for t in session.queue}
        if player.current:
            existing_uris.add(player.current.uri)

        for track in incoming:
            if track.uri in existing_uris:
                continue
            track.extras = {"requester_id": user.id}
            session.queue.append(track)
            existing_uris.add(track.uri)
            added += 1

        await self._persist_queue(guild.id)
        if not player.playing and not player.paused:
            await self.play_next(guild.id, text_channel)
        return f"Queued {added} track(s)."

    async def play_next(self, guild_id: int, text_channel: discord.TextChannel) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        player: Optional[wavelink.Player] = guild.voice_client  # type: ignore[assignment]
        if not player:
            return
        session = self._session(guild_id)
        if player.current and session.loop_mode == "track":
            await player.play(player.current)
            return
        if player.current and session.loop_mode == "queue":
            session.queue.append(player.current)

        if not session.queue:
            if session.idle_task and not session.idle_task.done():
                session.idle_task.cancel()
            session.idle_task = asyncio.create_task(self._idle_disconnect(guild_id, 120))
            return

        next_track = session.queue.popleft()
        session.last_track = next_track
        await player.play(next_track)
        await player.set_volume(session.default_volume)
        await self._persist_queue(guild_id)
        await self.store.add_history(guild_id, self._stored(next_track))
        await self.send_or_update_player_embed(guild_id, text_channel)

    async def _idle_disconnect(self, guild_id: int, timeout_s: int) -> None:
        await asyncio.sleep(timeout_s)
        guild = self.bot.get_guild(guild_id)
        if guild and guild.voice_client:
            await guild.voice_client.disconnect(force=True)

    async def _persist_queue(self, guild_id: int) -> None:
        session = self._session(guild_id)
        await self.store.save_queue(guild_id, [self._stored(t) for t in session.queue])

    def _stored(self, t: wavelink.Playable) -> StoredTrack:
        return StoredTrack(
            title=t.title,
            uri=t.uri,
            author=t.author,
            length=t.length,
            artwork=getattr(t, "artwork", None),
            requester_id=int(getattr(t, "extras", {}).get("requester_id", 0)),
        )

    async def handle_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        if not payload.player:
            return
        guild_id = payload.player.guild.id
        settings = await self.store.get_guild_settings(guild_id)
        channel_id = settings["last_text_channel"]
        if channel_id:
            channel = self.bot.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                await self.play_next(guild_id, channel)

    async def handle_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        guild_id = payload.player.guild.id if payload.player else None
        await self.store.log_error(guild_id, "track_exception", str(payload.exception))
        if payload.player and payload.player.current:
            session = self._session(payload.player.guild.id)
            if session.retry_attempts < 2:
                session.retry_attempts += 1
                await payload.player.play(payload.player.current)
            else:
                session.retry_attempts = 0
                await payload.player.skip(force=True)

    async def toggle_pause(self, guild_id: int, user: discord.abc.User) -> Tuple[bool, str]:
        guild = self.bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            return False, "Player is not active."
        if not await self.has_dj(guild, user):
            return False, "You need DJ permissions."
        player: wavelink.Player = guild.voice_client  # type: ignore[assignment]
        await player.pause(not player.paused)
        return True, "Toggled pause."

    async def skip(self, guild_id: int, user: discord.abc.User) -> Tuple[bool, str]:
        guild = self.bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            return False, "Player is not active."
        if not await self.has_dj(guild, user):
            return False, "You need DJ permissions."
        player: wavelink.Player = guild.voice_client  # type: ignore[assignment]
        await player.skip(force=True)
        return True, "Skipped."

    async def stop(self, guild_id: int, user: discord.abc.User) -> str:
        guild = self.bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            return "Player is not active."
        if not await self.has_dj(guild, user):
            return "You need DJ permissions."
        player: wavelink.Player = guild.voice_client  # type: ignore[assignment]
        self._session(guild_id).queue.clear()
        await self._persist_queue(guild_id)
        await player.disconnect(force=True)
        return "Stopped and disconnected."

    async def set_volume(self, guild_id: int, user: discord.abc.User, volume: int) -> str:
        guild = self.bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            return "Player is not active."
        if not await self.has_dj(guild, user):
            return "You need DJ permissions."
        volume = max(0, min(200, volume))
        session = self._session(guild_id)
        session.default_volume = volume
        await self.store.set_volume(guild_id, volume)
        player: wavelink.Player = guild.voice_client  # type: ignore[assignment]
        await player.set_volume(volume)
        return f"Volume set to {volume}."

    async def seek(self, guild_id: int, user: discord.abc.User, seconds: int) -> str:
        guild = self.bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            return "Player is not active."
        if not await self.has_dj(guild, user):
            return "You need DJ permissions."
        player: wavelink.Player = guild.voice_client  # type: ignore[assignment]
        await player.seek(max(0, seconds * 1000))
        return "Seeked."

    async def remove(self, guild_id: int, index: int) -> str:
        session = self._session(guild_id)
        if index < 1 or index > len(session.queue):
            return "Invalid queue index."
        del session.queue[index - 1]
        await self._persist_queue(guild_id)
        return "Removed track."

    async def clear(self, guild_id: int) -> str:
        self._session(guild_id).queue.clear()
        await self._persist_queue(guild_id)
        return "Queue cleared."

    async def shuffle(self, guild_id: int) -> str:
        session = self._session(guild_id)
        items = list(session.queue)
        random.shuffle(items)
        session.queue = deque(items)
        await self._persist_queue(guild_id)
        return "Queue shuffled."

    async def toggle_loop(self, guild_id: int) -> str:
        session = self._session(guild_id)
        session.loop_mode = {"off": "track", "track": "queue", "queue": "off"}[session.loop_mode]
        return session.loop_mode

    async def set_filter(self, guild_id: int, filter_name: str) -> str:
        guild = self.bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            return "Player is not active."
        player: wavelink.Player = guild.voice_client  # type: ignore[assignment]
        payload = {
            "bass": {"equalizer": [{"band": i, "gain": 0.15 if i < 4 else 0.0} for i in range(15)]},
            "vocal": {"equalizer": [{"band": i, "gain": 0.1 if 4 <= i <= 10 else 0.0} for i in range(15)]},
            "nightcore": {"timescale": {"speed": 1.15, "pitch": 1.2, "rate": 1.0}},
            "8d": {"rotation": {"rotationHz": 0.2}},
            "karaoke": {"karaoke": {"level": 1.0, "monoLevel": 1.0, "filterBand": 220.0, "filterWidth": 100.0}},
            "tremolo": {"tremolo": {"frequency": 4.0, "depth": 0.8}},
            "vibrato": {"vibrato": {"frequency": 4.0, "depth": 0.7}},
            "off": {},
        }.get(filter_name, {})

        try:
            await player.set_filters(wavelink.Filters(data=payload))
        except Exception:
            await player.set_filters(wavelink.Filters(**payload))
        return f"Filter set: {filter_name}"

    async def set_speed_pitch(self, guild_id: int, speed: float, pitch: float) -> str:
        guild = self.bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            return "Player is not active."
        player: wavelink.Player = guild.voice_client  # type: ignore[assignment]
        speed = max(0.5, min(2.0, speed))
        pitch = max(0.5, min(2.0, pitch))
        payload = {"timescale": {"speed": speed, "pitch": pitch, "rate": 1.0}}
        try:
            await player.set_filters(wavelink.Filters(data=payload))
        except Exception:
            await player.set_filters(wavelink.Filters(**payload))
        return f"Speed={speed:.2f}, Pitch={pitch:.2f}"

    def get_queue_text(self, guild_id: int, limit: int = 12) -> str:
        session = self._session(guild_id)
        if not session.queue:
            return "Queue is empty."
        lines = [f"{i}. {track.title}" for i, track in enumerate(list(session.queue)[:limit], start=1)]
        return "\n".join(lines)

    async def send_or_update_player_embed(self, guild_id: int, text_channel: discord.TextChannel) -> None:
        guild = self.bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            return
        player: wavelink.Player = guild.voice_client  # type: ignore[assignment]
        current = player.current
        if not current:
            return

        position = int((player.position or 0) / 1000)
        length = max(1, int(current.length / 1000))
        filled = int((position / length) * 20)
        progress = "█" * filled + "░" * (20 - filled)

        embed = discord.Embed(title="Now Playing", description=f"[{current.title}]({current.uri})", color=0x8B0000)
        embed.add_field(name="Duration", value=f"{position}s / {length}s")
        embed.add_field(name="Progress", value=progress, inline=False)
        if getattr(current, "artwork", None):
            embed.set_thumbnail(url=current.artwork)

        controls = PlayerControls(self, guild_id)
        msg = await text_channel.send(embed=embed, view=controls)
        await self.store.set_player_message(guild_id, text_channel.id, msg.id)

    async def has_dj(self, guild: discord.Guild, user: discord.abc.User) -> bool:
        if not isinstance(user, discord.Member):
            return False
        if user.guild_permissions.administrator:
            return True
        settings = await self.store.get_guild_settings(guild.id)
        db_role_id = settings["dj_role_id"]
        role_id = db_role_id or self.dj_role_id
        if not role_id:
            return True
        return any(role.id == role_id for role in user.roles)
