from collections import namedtuple
from typing import cast
import sqlite3

import discord
from discord import app_commands

import breadcord
from breadcord.helpers import HTTPModuleCog
from discord.ext import tasks
import aiohttp
from yarl import URL

TIME_RANGES = ["7day", "1month", "3month", "6month", "12month", "overall"]

Credentials = namedtuple("Credentials", ["username", "api_key"])
Track = namedtuple("Track", ["artist", "name"])


class NoCredentialsError(Exception):
    def __init__(self) -> None:
        super().__init__(f"You need to set your Last.fm username and API key.")

class APIError(Exception):
    ...


class DailyMusic(HTTPModuleCog):
    # region Setup and backend
    def __init__(self, module_id: str) -> None:
        super().__init__(module_id)
        self.webhook = discord.Webhook.from_url(cast(str, self.settings.webhook_url.value), client=self.bot)
        @cast(breadcord.config.Setting, self.settings.webhook_url).observe
        def on_webhook_change(_, new: str) -> None:
            self.webhook = discord.Webhook.from_url(new, client=self.bot)

        self.url_base = URL("http://ws.audioscrobbler.com/2.0/")
        self.db = sqlite3.connect(self.storage_path / 'daily_music.db')
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id INTEGER PRIMARY KEY,
                lfm_username TEXT NOT NULL,
                lfm_api_key TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                track_artist TEXT NOT NULL,
                track_name TEXT NOT NULL,
                PRIMARY KEY (user_id, track_artist, track_name)
            )
        """)
        self.db.commit()

        self.daily_task.start()

    def get_credentials(self, user_id: int) -> Credentials | None:
        credentials = self.db.execute(
            "SELECT lfm_username, lfm_api_key FROM users WHERE discord_id = ?",
            (user_id,),
        ).fetchone()
        return Credentials(*credentials) if credentials else None

    async def get_track(
        self,
        user_id: int,
        credentials: Credentials,
        *,
        session: aiohttp.ClientSession,
    ) -> Track | None:
        url = self.url_base % {
            "format": "json",
            "method": "user.gettoptracks",
            "user": credentials.username,
            "api_key": credentials.api_key,
        }
        for time_range in TIME_RANGES:
            async with session.get(url % {"period": time_range}) as response:
                response.raise_for_status()
                data = await response.json()
                if "error" in data:
                    raise APIError(f"Error while fetching top tracks: {data['message']}")

                tracks: list[dict] = data["toptracks"]["track"]
                # First track not in DB
                for track in tracks:
                    artist = track["artist"]["name"]
                    name = track["name"]
                    if not self.db.execute(
                        "SELECT 1 FROM tracks WHERE user_id = ? AND track_artist = ? AND track_name = ?",
                        (user_id, artist, name),
                    ).fetchone():
                        self.db.execute(
                            "INSERT INTO tracks VALUES (?, date('now'), ?, ?)",
                            (user_id, artist, name),
                        )
                        self.db.commit()
                        return Track(artist=artist, name=name)
    # endregion

    group = app_commands.Group(
        name="daily",
        description="Configuration commands for daily music",
    )

    class RegisterModal(discord.ui.Modal):
        title = "Last.fm credentials"
        username = discord.ui.TextInput(
            label="Last.fm username",
            placeholder="Username",
        )
        api_key = discord.ui.TextInput(
            label="Last.fm API key - Only if you trust this bot",
            placeholder="https://www.last.fm/api/accounts",
        )

        def __init__(self, db: sqlite3.Connection) -> None:
            self.db = db
            super().__init__()

        async def on_submit(self, interaction: discord.Interaction) -> None:
            self.db.execute(
                "INSERT OR REPLACE INTO users VALUES (?, ?, ?)",
                (interaction.user.id, self.username.value, self.api_key.value)
            )
            self.db.commit()
            await interaction.response.send_message("Credentials saved", ephemeral=True)
            self.stop()

    @group.command(
        name="register",
        description="Set your Last.fm username and API key",
    )
    async def register_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(self.RegisterModal(self.db))

    @tasks.loop(hours=24)
    async def daily_task(self):
        async with aiohttp.ClientSession() as session:
            for user_id, username, api_key in self.db.execute("SELECT discord_id, lfm_username, lfm_api_key FROM users"):
                if self.db.execute(
                    "SELECT 1 FROM tracks WHERE user_id = ? AND date = date('now')",
                    (user_id,),
                ).fetchone():
                    continue

                try:
                    track = await self.get_track(user_id, Credentials(username, api_key), session=session)
                except Exception as error:
                    self.logger.error(f"Error while fetching track for {user_id}: {error}")
                    continue
                if not track:
                    continue
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                if not user:
                    continue
                await self.webhook.send(
                    username=f"Daily Music - {user.global_name}",
                    avatar_url=user.display_avatar.url,
                    embed=discord.Embed(
                        title=f"{track.name} by {track.artist}",
                        colour=user.accent_color or discord.Colour.random(seed=user.id),
                        url=URL("https://www.youtube.com/results")
                            % dict(search_query=f"{track.name} {track.artist}"),
                    ),
                )


async def setup(bot: breadcord.Bot, module: breadcord.module.Module) -> None:
    await bot.add_cog(DailyMusic(module.id))
