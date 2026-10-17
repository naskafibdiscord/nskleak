import os
import json
import re
import unicodedata
import asyncio
import time
from pathlib import Path
from collections import deque, defaultdict
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = Path("ticket_data.json")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# ANTI-SPAM (liens Discord + @everyone / @here)
# ---------------------------------------------------------------------------

# Seuil commun : 3 actions en moins de 5 secondes → ban auto
SPAM_THRESHOLD = 3
SPAM_WINDOW = 5.0  # secondes

INVITE_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.(?:gg|io|me|li)|discord(?:app)?\.com/invite)/[a-zA-Z0-9\-]+",
    re.IGNORECASE,
)

# user_id → liste des timestamps
invite_spam_tracker: dict[int, list[float]] = defaultdict(list)
everyone_spam_tracker: dict[int, list[float]] = defaultdict(list)

# ---------------------------------------------------------------------------
# LEAKS → LEAK DU JOUR
# ---------------------------------------------------------------------------

LEAKS_CATEGORY_NAME = "💎・LEAKS"
LEAK_DU_JOUR_CHANNEL_NAME = "🏠・leak-du-jour"

# Mots-clés pour détecter le type de leak
LEAK_TYPE_KEYWORDS = {
    "MLO": ["mlo", "map", "interior", "intérieur", "mapping"],
    "Script": ["script", "lua", "js", "resource", "ressource", "esx", "qbcore", "qb-"],
    "Clothes": ["clothes", "clothing", "tenue", "outfit", "vêtement", "vetement", "ped", "addon clothing"],
    "Véhicule": ["vehicle", "véhicule", "vehicule", "car", "voiture", "moto", "bike", "addon car", "handling"],
    "Armes": ["weapon", "arme", "armes", "gun", "addon weapon"],
    "Base": ["base", "server base", "framework", "template serveur"],
}

LEAK_TYPE_EMOJI = {
    "MLO": "🗺️",
    "Script": "📜",
    "Clothes": "👕",
    "Véhicule": "🚗",
    "Armes": "🔫",
    "Base": "🏗️",
    "Autre": "📦",
}


def detect_leak_type(text: str) -> str:
    """Détecte le type de leak à partir du texte."""
    lower = (text or "").lower()
    for leak_type, keywords in LEAK_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return leak_type
    return "Autre"


def find_channel_by_name(guild: discord.Guild, name: str):
    """Trouve un salon textuel par nom (insensible aux variations d'emoji)."""
    name_clean = name.lower().strip()
    for ch in guild.text_channels:
        if ch.name.lower().strip() == name_clean:
            return ch
        # Fallback : contient le nom sans emoji
        if name_clean.replace("💎・", "").replace("🏠・", "") in ch.name.lower():
            return ch
    return None


def find_category_by_name(guild: discord.Guild, name: str):
    name_clean = name.lower().strip()
    for cat in guild.categories:
        if cat.name.lower().strip() == name_clean:
            return cat
        if "leaks" in cat.name.lower() and "💎" in cat.name:
            return cat
    return None


async def forward_leak_to_daily(message: discord.Message):
    """Prend un message d'un salon LEAKS et le republie dans leak-du-jour."""
    guild = message.guild
    if not guild:
        return

    # Vérifie que le salon est bien dans la catégorie LEAKS
    category = message.channel.category
    if not category:
        return
    cat_name = category.name.lower()
    if "leaks" not in cat_name:
        return

    target = find_channel_by_name(guild, LEAK_DU_JOUR_CHANNEL_NAME)
    if not target:
        # Fallback plus large
        for ch in guild.text_channels:
            if "leak-du-jour" in ch.name.lower() or "leak du jour" in ch.name.lower():
                target = ch
                break
    if not target:
        return

    # Évite de republier ce qui vient déjà de leak-du-jour
    if message.channel.id == target.id:
        return

    # Construit le texte complet pour la détection
    full_text = message.content or ""
    if message.embeds:
        for emb in message.embeds:
            if emb.title:
                full_text += " " + emb.title
            if emb.description:
                full_text += " " + emb.description
            for field in emb.fields:
                full_text += f" {field.name} {field.value}"

    leak_type = detect_leak_type(full_text)
    emoji = LEAK_TYPE_EMOJI.get(leak_type, "📦")

    # Date du jour
    today = datetime.now().strftime("%d/%m/%Y")

    # Construit un bel embed dans le style de l'exemple
    embed = discord.Embed(
        title=f"{emoji} Nouvel ajout — {leak_type}",
        description="Une ressource a été ajoutée dans les leaks.",
        color=discord.Color.dark_purple(),
        timestamp=datetime.now(),
    )

    # Contenu principal
    if message.content:
        # Limite la longueur
        desc = message.content[:1500]
        embed.add_field(name="📄 Contenu", value=desc, inline=False)

    # Si le message d'origine a déjà des embeds, on reprend les infos utiles
    if message.embeds:
        original = message.embeds[0]
        if original.title and not message.content:
            embed.description = original.title
        if original.description:
            embed.add_field(
                name="Description",
                value=original.description[:1000],
                inline=False,
            )
        for field in original.fields[:5]:
            embed.add_field(
                name=field.name[:256],
                value=field.value[:1024],
                inline=field.inline,
            )
        if original.image and original.image.url:
            embed.set_image(url=original.image.url)
        elif original.thumbnail and original.thumbnail.url:
            embed.set_image(url=original.thumbnail.url)

    # Image jointe au message
    if message.attachments:
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                embed.set_image(url=att.url)
                break

    embed.add_field(name="📂 Type", value=f"{emoji} **{leak_type}**", inline=True)
    embed.add_field(name="📅 Date", value=f"**{today}**", inline=True)
    embed.add_field(
        name="📍 Source",
        value=f"{message.channel.mention}",
        inline=True,
    )
    embed.set_footer(text=f"Leak du jour • {today}")
    if message.author:
        embed.set_author(
            name=str(message.author.display_name),
            icon_url=message.author.display_avatar.url if message.author.display_avatar else None,
        )

    try:
        await target.send(embed=embed)
    except discord.Forbidden:
        print(f"[Leak] Pas la permission d'écrire dans {target.name}")
    except Exception as e:
        print(f"[Leak] Erreur envoi : {e}")

# ---------------------------------------------------------------------------
# Données persistantes (tickets / rules)
# ---------------------------------------------------------------------------

def load_data():
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

data = load_data()

def save_data():
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def get_config(guild_id: int):
    key = str(guild_id)
    if key not in data:
        data[key] = {
            "title": "Support — Ouvre un ticket",
            "description": "Choisis le type de ticket dans le menu ci-dessous.",
            "category_id": None,
            "staff_role_id": None,
            "rules_role_id": None,
            "types": [
                {"name": "Aide", "description": "J'ai besoin d'aide", "emoji": "🛟"},
                {"name": "Question", "description": "J'ai une question", "emoji": "❓"}
            ]
        }
        save_data()
    data[key].setdefault("rules_role_id", None)
    return data[key]

def clean_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9-]+", "-", value.lower())
    value = re.sub(r"-+", "-", value).strip("-")
    return (value or "ticket")[:70]

def has_config_permission(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.guild_permissions.manage_guild)

# ---------------------------------------------------------------------------
# MUSIQUE / VOIX
# ---------------------------------------------------------------------------

# Playlist de titres qui plaisent aux ados (rap FR + hits d'été)
# Utilise la recherche YouTube (ytsearch:)
DEFAULT_PLAYLIST = [
    "ytsearch1:Aya Nakamura Djadja",
    "ytsearch1:Ninho Jefe",
    "ytsearch1:Gazo Die",
    "ytsearch1:Tiakola Méchant",
    "ytsearch1:Werenoi Louvre",
    "ytsearch1:SCH Manoir",
    "ytsearch1:Jul Bandana SCH",
    "ytsearch1:PNL Au DD",
    "ytsearch1:Booba Validé",
    "ytsearch1:Niska Méchant",
    "ytsearch1:Aya Nakamura Pookie",
    "ytsearch1:Gazo Drill FR",
    "ytsearch1:Tiakola Manon",
    "ytsearch1:Werenoi Solitaire",
    "ytsearch1:Orelsan L'odeur de l'essence",
    "ytsearch1:Damso Ipséité",
    "ytsearch1:Ninho Tout en haut",
    "ytsearch1:Aya Nakamura Copines",
    "ytsearch1:Gazo Plateforme",
    "ytsearch1:SCH A7",
]

VOICE_CHANNEL_NAME = "🔊・Vocal 1"

ytdl_format_options = {
    "format": "bestaudio/best",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    # Contourne le blocage "Sign in to confirm you're not a bot" de YouTube
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "tv_embedded"],
        }
    },
}

ffmpeg_options = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options) if yt_dlp else None


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title", "Titre inconnu")
        self.url = data.get("webpage_url") or data.get("url", "")

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True, volume=0.5):
        if ytdl is None:
            raise RuntimeError("yt-dlp n'est pas installé. Fais : pip install yt-dlp")
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=not stream)
        )
        if "entries" in data:
            data = data["entries"][0]
        filename = data["url"] if stream else ytdl.prepare_filename(data)
        return cls(
            discord.FFmpegPCMAudio(filename, **ffmpeg_options),
            data=data,
            volume=volume,
        )


class GuildMusicPlayer:
    """État de lecture par serveur."""

    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: deque = deque()
        self.voice_client: discord.VoiceClient | None = None
        self.volume: float = 0.5  # 0.0 → 1.0
        self.current: YTDLSource | None = None
        self.playing = False
        self._lock = asyncio.Lock()

    async def ensure_voice(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.channel.id != channel.id:
                await self.voice_client.move_to(channel)
            return self.voice_client
        self.voice_client = await channel.connect(self_deaf=True)
        return self.voice_client

    def set_volume(self, value: float):
        self.volume = max(0.0, min(1.0, value))
        if self.current:
            self.current.volume = self.volume
        if self.voice_client and self.voice_client.source:
            if isinstance(self.voice_client.source, discord.PCMVolumeTransformer):
                self.voice_client.source.volume = self.volume

    async def play_next(self):
        async with self._lock:
            if not self.voice_client or not self.voice_client.is_connected():
                self.playing = False
                return

            if not self.queue:
                # Recharge la playlist par défaut pour boucler
                self.queue.extend(DEFAULT_PLAYLIST)

            query = self.queue.popleft()
            try:
                source = await YTDLSource.from_url(
                    query, loop=bot.loop, stream=True, volume=self.volume
                )
                self.current = source
                self.playing = True

                def after_play(error):
                    if error:
                        print(f"[Musique] Erreur lecture : {error}")
                    fut = asyncio.run_coroutine_threadsafe(self.play_next(), bot.loop)
                    try:
                        fut.result()
                    except Exception as e:
                        print(f"[Musique] Erreur after_play : {e}")

                self.voice_client.play(source, after=after_play)
            except Exception as e:
                print(f"[Musique] Impossible de jouer {query} : {e}")
                # Passe au suivant
                await self.play_next()

    async def start_playlist(self, channel: discord.VoiceChannel):
        await self.ensure_voice(channel)
        if not self.queue:
            self.queue.extend(DEFAULT_PLAYLIST)
        if not self.playing or not (self.voice_client and self.voice_client.is_playing()):
            await self.play_next()

    async def stop(self):
        self.playing = False
        self.queue.clear()
        if self.voice_client and self.voice_client.is_connected():
            self.voice_client.stop()
            await self.voice_client.disconnect()
        self.voice_client = None
        self.current = None


# État global des players
music_players: dict[int, GuildMusicPlayer] = {}


def get_player(guild_id: int) -> GuildMusicPlayer:
    if guild_id not in music_players:
        music_players[guild_id] = GuildMusicPlayer(guild_id)
    return music_players[guild_id]


# ---------------------------------------------------------------------------
# VUES TICKETS (code original)
# ---------------------------------------------------------------------------

class TicketPanelSelect(discord.ui.Select):
    def __init__(self, guild_id: int, config: dict):
        self.guild_id = guild_id
        options = []
        for index, item in enumerate(config.get("types", [])[:25]):
            kwargs = {
                "label": item["name"][:100],
                "description": item["description"][:100],
                "value": str(index)
            }
            if item.get("emoji"):
                try:
                    kwargs["emoji"] = item["emoji"]
                except Exception:
                    pass
            options.append(discord.SelectOption(**kwargs))

        super().__init__(
            placeholder="Choisis le type de ticket…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"ticket_create_select:{guild_id}"
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message(
                "Cette fonction doit être utilisée dans un serveur.", ephemeral=True
            )

        config = get_config(interaction.guild.id)
        try:
            ticket_type = config["types"][int(self.values[0])]
        except (IndexError, ValueError):
            return await interaction.response.send_message(
                "Ce type n'existe plus. Demande au staff de renvoyer le panneau.",
                ephemeral=True
            )

        category_id = config.get("category_id")
        category = interaction.guild.get_channel(category_id) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message(
                "Le système n'est pas configuré : définis d'abord une catégorie avec "
                "`/ticket_config categorie`.",
                ephemeral=True
            )

        for channel in category.text_channels:
            if channel.topic == f"ticket-owner:{interaction.user.id}":
                return await interaction.response.send_message(
                    f"Tu as déjà un ticket ouvert : {channel.mention}", ephemeral=True
                )

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True
            )
        }

        staff_id = config.get("staff_role_id")
        staff_role = interaction.guild.get_role(staff_id) if staff_id else None
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )

        try:
            channel = await interaction.guild.create_text_channel(
                name=f"{clean_name(ticket_type['name'])}-{clean_name(interaction.user.name)}",
                category=category,
                topic=f"ticket-owner:{interaction.user.id}",
                overwrites=overwrites,
                reason=f"Ticket ouvert par {interaction.user}"
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Je n'ai pas les permissions nécessaires pour créer un salon privé. "
                "Vérifie les permissions du bot.", ephemeral=True
            )

        embed = discord.Embed(
            title=f"{ticket_type.get('emoji', '')} {ticket_type['name']}".strip(),
            description=(
                f"Bonjour {interaction.user.mention} !\n\n"
                "Décris ta demande ici, un membre du staff te répondra dès que possible."
            ),
            color=discord.Color.green()
        )
        embed.add_field(name="Créé par", value=interaction.user.mention, inline=True)
        view = TicketOpenView(interaction.guild.id)
        mentions = f"{staff_role.mention} " if staff_role else ""
        await channel.send(content=f"{mentions}{interaction.user.mention}", embed=embed, view=view)
        await interaction.response.send_message(
            f"✅ Ton ticket a été créé : {channel.mention}", ephemeral=True
        )


class TicketPanelView(discord.ui.View):
    def __init__(self, guild_id: int, config: dict):
        super().__init__(timeout=None)
        if config.get("types"):
            self.add_item(TicketPanelSelect(guild_id, config))


class TicketOpenView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label="Fermer le ticket",
            style=discord.ButtonStyle.danger,
            emoji="🔒",
            custom_id=f"ticket_close:{guild_id}"
        )
        button.callback = self.close_ticket
        self.add_item(button)

    async def close_ticket(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not channel.topic:
            return await interaction.response.send_message("Ticket invalide.", ephemeral=True)
        if not channel.topic.startswith("ticket-owner:"):
            return await interaction.response.send_message("Ce salon n'est pas un ticket.", ephemeral=True)

        owner_id = int(channel.topic.split(":", 1)[1])
        config = get_config(interaction.guild.id)
        staff_id = config.get("staff_role_id")
        is_staff = staff_id and any(role.id == staff_id for role in interaction.user.roles)
        is_admin = interaction.user.guild_permissions.manage_channels
        if interaction.user.id != owner_id and not is_staff and not is_admin:
            return await interaction.response.send_message(
                "Seul le créateur du ticket ou le staff peut le fermer.", ephemeral=True
            )

        owner = interaction.guild.get_member(owner_id)
        if owner:
            await channel.set_permissions(
                owner, view_channel=False, send_messages=False, reason="Ticket fermé"
            )

        await interaction.response.send_message(
            "🔒 Ticket fermé. Le staff peut le supprimer ou le rouvrir.",
            view=TicketClosedView(interaction.guild.id)
        )


class TicketClosedView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)

        delete_button = discord.ui.Button(
            label="Supprimer le ticket",
            style=discord.ButtonStyle.danger,
            custom_id=f"ticket_delete:{guild_id}"
        )
        reopen_button = discord.ui.Button(
            label="Rouvrir",
            style=discord.ButtonStyle.secondary,
            custom_id=f"ticket_reopen:{guild_id}"
        )
        delete_button.callback = self.delete_ticket
        reopen_button.callback = self.reopen_ticket
        self.add_item(delete_button)
        self.add_item(reopen_button)

    async def check_staff(self, interaction: discord.Interaction) -> bool:
        config = get_config(interaction.guild.id)
        staff_id = config.get("staff_role_id")
        is_staff = staff_id and any(role.id == staff_id for role in interaction.user.roles)
        is_admin = interaction.user.guild_permissions.manage_channels
        if not is_staff and not is_admin:
            await interaction.response.send_message(
                "Seul le staff peut effectuer cette action.", ephemeral=True
            )
            return False
        return True

    async def delete_ticket(self, interaction: discord.Interaction):
        if not await self.check_staff(interaction):
            return
        channel = interaction.channel
        await interaction.response.send_message("🗑️ Suppression du ticket dans 3 secondes.")
        await asyncio.sleep(3)
        await channel.delete(reason=f"Ticket supprimé par {interaction.user}")

    async def reopen_ticket(self, interaction: discord.Interaction):
        if not await self.check_staff(interaction):
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not channel.topic:
            return await interaction.response.send_message("Ticket invalide.", ephemeral=True)
        owner_id = int(channel.topic.split(":", 1)[1])
        owner = interaction.guild.get_member(owner_id)
        if owner:
            await channel.set_permissions(
                owner,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True
            )
        await interaction.response.edit_message(
            content="🔓 Ticket rouvert.", view=TicketOpenView(interaction.guild.id)
        )


# ---------------------------------------------------------------------------
# CONFIG TICKETS
# ---------------------------------------------------------------------------

ticket_config = app_commands.Group(
    name="ticket_config",
    description="Configure le système de tickets"
)

@ticket_config.command(name="apparence", description="Modifier le titre et la description du panneau")
@app_commands.describe(titre="Titre du panneau", description="Description du panneau")
async def apparence(interaction: discord.Interaction, titre: str, description: str):
    if not has_config_permission(interaction):
        return await interaction.response.send_message(
            "Il faut la permission Gérer le serveur.", ephemeral=True
        )
    config = get_config(interaction.guild_id)
    config["title"] = titre[:100]
    config["description"] = description[:1000]
    save_data()
    await interaction.response.send_message("✅ Apparence du panneau mise à jour.", ephemeral=True)

@ticket_config.command(name="categorie", description="Choisir la catégorie où les tickets seront créés")
async def categorie(interaction: discord.Interaction, salon: discord.CategoryChannel):
    if not has_config_permission(interaction):
        return await interaction.response.send_message(
            "Il faut la permission Gérer le serveur.", ephemeral=True
        )
    config = get_config(interaction.guild_id)
    config["category_id"] = salon.id
    save_data()
    await interaction.response.send_message(
        f"✅ Les tickets seront créés dans **{salon.name}**.", ephemeral=True
    )

@ticket_config.command(name="envoyer", description="Envoyer le panneau de tickets dans un salon")
async def envoyer(interaction: discord.Interaction, salon: discord.TextChannel):
    if not has_config_permission(interaction):
        return await interaction.response.send_message(
            "Il faut la permission Gérer le serveur.", ephemeral=True
        )
    config = get_config(interaction.guild_id)
    if not config.get("types"):
        return await interaction.response.send_message(
            "Ajoute au moins un type avec `/ticket_config type_ajouter`.", ephemeral=True
        )
    embed = discord.Embed(
        title=config.get("title", "Support"),
        description=config.get("description", "Choisis un type dans le menu."),
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Support • Sélectionne un type ci-dessous")
    await salon.send(embed=embed, view=TicketPanelView(interaction.guild_id, config))
    await interaction.response.send_message(f"✅ Panneau envoyé dans {salon.mention}.", ephemeral=True)

@ticket_config.command(name="role_staff", description="Définir le rôle qui gère les tickets")
async def role_staff(interaction: discord.Interaction, role: discord.Role):
    if not has_config_permission(interaction):
        return await interaction.response.send_message(
            "Il faut la permission Gérer le serveur.", ephemeral=True
        )
    config = get_config(interaction.guild_id)
    config["staff_role_id"] = role.id
    save_data()
    await interaction.response.send_message(f"✅ Le rôle staff est maintenant {role.mention}.", ephemeral=True)

@ticket_config.command(name="type_ajouter", description="Ajouter un type de ticket au menu")
@app_commands.describe(nom="Nom du type", description="Description du type", emoji="Emoji facultatif, ex. 🎫")
async def type_ajouter(interaction: discord.Interaction, nom: str, description: str, emoji: str = ""):
    if not has_config_permission(interaction):
        return await interaction.response.send_message(
            "Il faut la permission Gérer le serveur.", ephemeral=True
        )
    config = get_config(interaction.guild_id)
    if len(config["types"]) >= 25:
        return await interaction.response.send_message(
            "Le menu Discord accepte au maximum 25 types.", ephemeral=True
        )
    nom = nom.strip()
    if any(t["name"].casefold() == nom.casefold() for t in config["types"]):
        return await interaction.response.send_message("Ce type existe déjà.", ephemeral=True)
    config["types"].append({
        "name": nom[:80],
        "description": description[:100],
        "emoji": emoji[:20]
    })
    save_data()
    await interaction.response.send_message(
        f"✅ Type **{nom}** ajouté. Renvoie le panneau avec `/ticket_config envoyer` pour actualiser le menu.",
        ephemeral=True
    )

@ticket_config.command(name="type_retirer", description="Retirer un type de ticket")
async def type_retirer(interaction: discord.Interaction, nom: str):
    if not has_config_permission(interaction):
        return await interaction.response.send_message(
            "Il faut la permission Gérer le serveur.", ephemeral=True
        )
    config = get_config(interaction.guild_id)
    index = next((i for i, t in enumerate(config["types"]) if t["name"].casefold() == nom.casefold()), None)
    if index is None:
        return await interaction.response.send_message(
            "Type introuvable. Utilise `/ticket_config voir` pour afficher les noms exacts.",
            ephemeral=True
        )
    config["types"].pop(index)
    save_data()
    await interaction.response.send_message(
        f"✅ Type **{nom}** retiré. Renvoie le panneau avec `/ticket_config envoyer`.",
        ephemeral=True
    )

@ticket_config.command(name="voir", description="Voir la configuration actuelle")
async def voir(interaction: discord.Interaction):
    if not has_config_permission(interaction):
        return await interaction.response.send_message(
            "Il faut la permission Gérer le serveur.", ephemeral=True
        )
    config = get_config(interaction.guild_id)
    category = interaction.guild.get_channel(config.get("category_id")) if config.get("category_id") else None
    role = interaction.guild.get_role(config.get("staff_role_id")) if config.get("staff_role_id") else None
    types_text = "\n".join(
        f"{t.get('emoji', '')} **{t['name']}** — {t['description']}"
        for t in config.get("types", [])
    ) or "Aucun type configuré."
    embed = discord.Embed(title="Configuration des tickets", color=discord.Color.blurple())
    embed.add_field(name="Titre", value=config.get("title", "Non défini")[:1024], inline=False)
    embed.add_field(name="Description", value=config.get("description", "Non définie")[:1024], inline=False)
    embed.add_field(name="Catégorie", value=category.mention if category else "Non définie", inline=True)
    embed.add_field(name="Rôle staff", value=role.mention if role else "Non défini", inline=True)
    embed.add_field(name=f"Types ({len(config.get('types', []))}/25)", value=types_text[:1024], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# RÈGLEMENT
# ---------------------------------------------------------------------------

RULES_TEXT = """📜・RÈGLEMENT OFFICIEL

Bienvenue sur notre serveur ! 🔥

Tu trouveras ici des ressources et leaks FiveM, ainsi que des services Discord gratuits : création de serveurs, développement de bots et bien plus encore.

Merci de respecter les règles suivantes.

🤝 1. RESPECT

・Le respect entre tous les membres est obligatoire.
・Les insultes abusives, le harcèlement et les discriminations sont interdits.
・Les provocations et les conflits inutiles ne sont pas tolérés.
・Le staff doit être respecté.

📦 2. RESSOURCES FIVEM

・Les ressources FiveM sont publiées uniquement par le propriétaire du serveur.
・Les membres ne peuvent pas publier leurs propres ressources sans autorisation.
・La redistribution non autorisée des ressources est interdite.
・Respectez les licences et les droits des créateurs.
・Tout fichier malveillant est strictement interdit.

🤖 3. SERVICES DISCORD GRATUITS

・La création de serveurs Discord et de bots est proposée gratuitement.
・Les demandes doivent être effectuées dans le salon prévu à cet effet.
・Décrivez clairement votre projet et vos besoins.
・Les demandes abusives, répétitives ou irréalisables pourront être refusées.
・Le délai de réalisation dépend de la disponibilité du créateur.
・La gratuité du service ne garantit pas une réalisation immédiate.
・Toute modification supplémentaire doit être discutée au préalable.
・Aucune utilisation des bots pour le spam, le raid ou le harcèlement ne sera tolérée.

🎫 4. SUPPORT & COMMANDES

・Ouvrez un ticket pour demander un service ou obtenir de l’aide.
・Une seule demande par ticket, dans la mesure du possible.
・Soyez patient et respectueux pendant le traitement de votre demande.
・Ne contactez pas le créateur en messages privés pour réclamer un service.
・Toute demande doit respecter les règles de Discord.

📢 5. PUBLICITÉ

・La publicité est autorisée uniquement dans les salons prévus à cet effet.
・Le spam et les messages répétitifs sont interdits.
・La publicité non sollicitée en messages privés est interdite.

🔐 6. SÉCURITÉ

・Le partage de mots de passe, de tokens ou de comptes volés est interdit.
・Ne transmettez jamais votre token Discord personnel.
・Les bots doivent respecter les conditions d’utilisation de Discord.
・Toute tentative de piratage, d’arnaque ou de vol de données est interdite.

⚠️ 7. SANCTIONS

En cas de non-respect du règlement, le staff peut appliquer :
・Un avertissement.
・Un mute temporaire.
・Un kick.
・Un bannissement temporaire ou définitif selon la gravité.

✅ 8. ACCEPTATION

En rejoignant et en restant sur ce serveur, tu acceptes ce règlement ainsi que les règles de Discord.

━━━━━━━━━━━━━━━━━━━━

🔥 Merci d’être parmi nous et de profiter de nos ressources et services gratuits !

👑 Le Fondateur
"""

class RulesAcceptView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label="Accepter Le Règlement",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"rules_accept:{guild_id}"
        )
        button.callback = self.accept_rules
        self.add_item(button)

    async def accept_rules(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message(
                "Cette action doit être faite dans un serveur.", ephemeral=True
            )
        config = get_config(interaction.guild.id)
        role_id = config.get("rules_role_id")
        if role_id:
            role = interaction.guild.get_role(role_id)
            member = interaction.guild.get_member(interaction.user.id)
            if role and member:
                try:
                    await member.add_roles(role, reason="Règlement accepté")
                except discord.Forbidden:
                    return await interaction.response.send_message(
                        "Je n'ai pas la permission d'attribuer le rôle. Vérifie que mon rôle est au-dessus du rôle à donner.",
                        ephemeral=True
                    )
                return await interaction.response.send_message(
                    f"✅ Tu as accepté le règlement et reçu le rôle {role.mention}.", ephemeral=True
                )
        await interaction.response.send_message(
            "✅ Merci ! Tu as accepté le règlement.", ephemeral=True
        )

@bot.tree.command(name="rules", description="Publier le règlement dans un salon")
@app_commands.describe(
    salon="Salon dans lequel publier le règlement",
    role="Rôle facultatif à donner quand un membre accepte le règlement"
)
async def rules(
    interaction: discord.Interaction,
    salon: discord.TextChannel,
    role: discord.Role = None
):
    if not interaction.guild or not has_config_permission(interaction):
        return await interaction.response.send_message(
            "Il faut la permission **Gérer le serveur** pour utiliser cette commande.",
            ephemeral=True
        )

    config = get_config(interaction.guild.id)
    config["rules_role_id"] = role.id if role else None
    save_data()

    chunks = []
    remaining = RULES_TEXT
    while remaining:
        if len(remaining) <= 4000:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, 4000)
        if split_at < 1:
            split_at = 4000
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    for index, chunk in enumerate(chunks):
        embed = discord.Embed(
            description=chunk,
            color=discord.Color.blurple()
        )
        if index == 0:
            embed.title = "📜・RÈGLEMENT OFFICIEL"
        await salon.send(
            embed=embed,
            view=RulesAcceptView(interaction.guild.id) if index == len(chunks) - 1 else None
        )

    if role:
        await interaction.response.send_message(
            f"✅ Règlement envoyé dans {salon.mention}. Le bouton donnera aussi le rôle {role.mention}.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"✅ Règlement envoyé dans {salon.mention}. Le bouton confirme l'acceptation.",
            ephemeral=True
        )


# ---------------------------------------------------------------------------
# MODÉRATION
# ---------------------------------------------------------------------------

@bot.tree.command(name="ban", description="Bannir un membre du serveur")
@app_commands.describe(
    membre="Membre à bannir",
    raison="Raison du bannissement"
)
@app_commands.default_permissions(ban_members=True)
async def ban(
    interaction: discord.Interaction,
    membre: discord.Member,
    raison: str = "Aucune raison précisée"
):
    if not interaction.guild or not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message(
            "❌ Tu dois avoir la permission **Bannir des membres**.", ephemeral=True
        )
    if membre.id == interaction.user.id:
        return await interaction.response.send_message(
            "❌ Tu ne peux pas te bannir toi-même.", ephemeral=True
        )
    if membre.id == bot.user.id:
        return await interaction.response.send_message(
            "❌ Je ne peux pas me bannir moi-même.", ephemeral=True
        )
    if interaction.guild.owner_id != interaction.user.id and membre.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "❌ Tu ne peux pas bannir un membre dont le rôle est égal ou supérieur au tien.",
            ephemeral=True
        )
    bot_member = interaction.guild.me
    if bot_member and membre.top_role >= bot_member.top_role:
        return await interaction.response.send_message(
            "❌ Mon rôle doit être placé au-dessus de celui du membre à bannir.",
            ephemeral=True
        )
    try:
        await membre.ban(reason=f"{raison} | Par {interaction.user}", delete_message_days=0)
        await interaction.response.send_message(
            f"🔨 **{membre}** a été banni.\n**Raison :** {raison}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Je n'ai pas la permission de bannir ce membre.", ephemeral=True
        )
    except discord.HTTPException:
        await interaction.response.send_message(
            "❌ Discord n'a pas pu effectuer le bannissement.", ephemeral=True
        )

@bot.tree.command(name="kick", description="Expulser un membre du serveur")
@app_commands.describe(
    membre="Membre à expulser",
    raison="Raison de l'expulsion"
)
@app_commands.default_permissions(kick_members=True)
async def kick(
    interaction: discord.Interaction,
    membre: discord.Member,
    raison: str = "Aucune raison précisée"
):
    if not interaction.guild or not interaction.user.guild_permissions.kick_members:
        return await interaction.response.send_message(
            "❌ Tu dois avoir la permission **Expulser des membres**.", ephemeral=True
        )
    if membre.id == interaction.user.id:
        return await interaction.response.send_message(
            "❌ Tu ne peux pas t'expulser toi-même.", ephemeral=True
        )
    if interaction.guild.owner_id != interaction.user.id and membre.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "❌ Tu ne peux pas expulser un membre dont le rôle est égal ou supérieur au tien.",
            ephemeral=True
        )
    bot_member = interaction.guild.me
    if bot_member and membre.top_role >= bot_member.top_role:
        return await interaction.response.send_message(
            "❌ Mon rôle doit être placé au-dessus de celui du membre à expulser.",
            ephemeral=True
        )
    try:
        await membre.kick(reason=f"{raison} | Par {interaction.user}")
        await interaction.response.send_message(
            f"👢 **{membre}** a été expulsé.\n**Raison :** {raison}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Je n'ai pas la permission d'expulser ce membre.", ephemeral=True
        )
    except discord.HTTPException:
        await interaction.response.send_message(
            "❌ Discord n'a pas pu effectuer l'expulsion.", ephemeral=True
        )

@bot.tree.command(name="mute", description="Rendre un membre muet temporairement")
@app_commands.describe(
    membre="Membre à mute",
    duree="Durée en minutes (maximum 40320, soit 28 jours)",
    raison="Raison du mute"
)
@app_commands.default_permissions(moderate_members=True)
async def mute(
    interaction: discord.Interaction,
    membre: discord.Member,
    duree: app_commands.Range[int, 1, 40320],
    raison: str = "Aucune raison précisée"
):
    if not interaction.guild or not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message(
            "❌ Tu dois avoir la permission **Modérer les membres**.", ephemeral=True
        )
    if membre.id == interaction.user.id:
        return await interaction.response.send_message(
            "❌ Tu ne peux pas te mute toi-même.", ephemeral=True
        )
    if membre.id == bot.user.id:
        return await interaction.response.send_message(
            "❌ Je ne peux pas me mute moi-même.", ephemeral=True
        )
    if membre.guild_permissions.administrator:
        return await interaction.response.send_message(
            "❌ Un membre administrateur ne peut pas être timeout.", ephemeral=True
        )
    if interaction.guild.owner_id != interaction.user.id and membre.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "❌ Tu ne peux pas mute un membre dont le rôle est égal ou supérieur au tien.",
            ephemeral=True
        )
    bot_member = interaction.guild.me
    if bot_member and membre.top_role >= bot_member.top_role:
        return await interaction.response.send_message(
            "❌ Mon rôle doit être placé au-dessus de celui du membre à mute.",
            ephemeral=True
        )
    try:
        await membre.timeout(timedelta(minutes=duree), reason=f"{raison} | Par {interaction.user}")
        if duree < 60:
            duree_txt = f"{duree} minute(s)"
        elif duree % 60 == 0 and duree < 1440:
            duree_txt = f"{duree // 60} heure(s)"
        elif duree % 1440 == 0:
            duree_txt = f"{duree // 1440} jour(s)"
        else:
            duree_txt = f"{duree} minutes"
        await interaction.response.send_message(
            f"🔇 **{membre}** a été mute pendant **{duree_txt}**.\n**Raison :** {raison}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Je n'ai pas la permission de modérer ce membre.", ephemeral=True
        )
    except discord.HTTPException:
        await interaction.response.send_message(
            "❌ Discord n'a pas pu appliquer le mute.", ephemeral=True
        )

@bot.tree.command(name="clear", description="Supprimer un nombre de messages dans le salon")
@app_commands.describe(nombre="Nombre de messages à supprimer (entre 1 et 100)")
@app_commands.default_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, nombre: app_commands.Range[int, 1, 100]):
    if not interaction.guild or not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message(
            "❌ Tu dois avoir la permission **Gérer les messages**.", ephemeral=True
        )
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message(
            "❌ Cette commande doit être utilisée dans un salon textuel.", ephemeral=True
        )

    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = await interaction.channel.purge(
            limit=nombre,
            check=lambda message: not message.pinned
        )
        await interaction.followup.send(
            f"🧹 **{len(deleted)} message(s)** supprimé(s) dans {interaction.channel.mention}.",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Je n'ai pas la permission de gérer les messages dans ce salon.",
            ephemeral=True
        )
    except discord.HTTPException:
        await interaction.followup.send(
            "❌ Discord n'a pas pu supprimer les messages. Les messages de plus de 14 jours ne peuvent pas être supprimés en bloc.",
            ephemeral=True
        )


# ---------------------------------------------------------------------------
# COMMANDES MUSIQUE : /bot_voc  et  /bot_vl
# ---------------------------------------------------------------------------

class VolumePanelView(discord.ui.View):
    """Panneau de contrôle du volume."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id

    def _volume_bar(self, volume: float) -> str:
        filled = int(volume * 10)
        return "🟩" * filled + "⬜" * (10 - filled)

    async def _update_embed(self, interaction: discord.Interaction):
        player = get_player(self.guild_id)
        vol_pct = int(player.volume * 100)
        title = player.current.title if player.current else "Aucune musique"
        embed = discord.Embed(
            title="🎚️ Contrôle du volume",
            description=(
                f"**Volume actuel :** `{vol_pct}%`\n"
                f"{self._volume_bar(player.volume)}\n\n"
                f"**En cours :** {title}"
            ),
            color=discord.Color.green() if player.playing else discord.Color.greyple()
        )
        embed.set_footer(text="Utilise les boutons ci-dessous pour régler le volume")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="-10%", style=discord.ButtonStyle.secondary, emoji="🔉")
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        player.set_volume(player.volume - 0.10)
        await self._update_embed(interaction)

    @discord.ui.button(label="+10%", style=discord.ButtonStyle.secondary, emoji="🔊")
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        player.set_volume(player.volume + 0.10)
        await self._update_embed(interaction)

    @discord.ui.button(label="25%", style=discord.ButtonStyle.primary)
    async def vol_25(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        player.set_volume(0.25)
        await self._update_embed(interaction)

    @discord.ui.button(label="50%", style=discord.ButtonStyle.primary)
    async def vol_50(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        player.set_volume(0.50)
        await self._update_embed(interaction)

    @discord.ui.button(label="100%", style=discord.ButtonStyle.primary)
    async def vol_100(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        player.set_volume(1.0)
        await self._update_embed(interaction)

    @discord.ui.button(label="Mute", style=discord.ButtonStyle.danger, emoji="🔇", row=1)
    async def vol_mute(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        player.set_volume(0.0)
        await self._update_embed(interaction)

    @discord.ui.button(label="Stop musique", style=discord.ButtonStyle.danger, emoji="⏹️", row=1)
    async def stop_music(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)
        await player.stop()
        await interaction.response.edit_message(
            content="⏹️ Musique arrêtée et bot déconnecté du vocal.",
            embed=None,
            view=None
        )


@bot.tree.command(name="bot_voc", description="Le bot rejoint 🔊・Vocal 1 et lance une playlist (rap / hits d'été)")
async def bot_voc(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message(
            "Cette commande doit être utilisée dans un serveur.", ephemeral=True
        )

    if yt_dlp is None:
        return await interaction.response.send_message(
            "❌ **yt-dlp** n'est pas installé.\n"
            "Lance : `pip install yt-dlp` puis redémarre le bot.",
            ephemeral=True
        )

    # Cherche le salon vocal exact
    voice_channel = discord.utils.get(
        interaction.guild.voice_channels,
        name=VOICE_CHANNEL_NAME
    )
    if voice_channel is None:
        # Essai sans emoji / variation de nom
        for ch in interaction.guild.voice_channels:
            if "Vocal 1" in ch.name or "vocal 1" in ch.name.lower():
                voice_channel = ch
                break

    if voice_channel is None:
        return await interaction.response.send_message(
            f"❌ Impossible de trouver le salon vocal **{VOICE_CHANNEL_NAME}**.\n"
            "Vérifie que le nom du salon est exactement `🔊・Vocal 1`.",
            ephemeral=True
        )

    # Permissions
    bot_member = interaction.guild.me
    perms = voice_channel.permissions_for(bot_member)
    if not perms.connect or not perms.speak:
        return await interaction.response.send_message(
            "❌ Je n'ai pas la permission de **rejoindre** ou de **parler** dans ce salon vocal.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True, thinking=True)

    player = get_player(interaction.guild.id)

    try:
        await player.start_playlist(voice_channel)
        title = player.current.title if player.current else "chargement…"
        await interaction.followup.send(
            f"✅ Je suis dans **{voice_channel.name}** et je lance la playlist !\n"
            f"🎵 En cours : **{title}**\n"
            f"🎚️ Volume : `{int(player.volume * 100)}%`\n\n"
            f"Utilise `/bot_vl` pour régler le volume.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(
            f"❌ Erreur en rejoignant le vocal ou en lançant la musique :\n```{e}```",
            ephemeral=True
        )


@bot.tree.command(name="bot_vl", description="Affiche le panneau de réglage du volume de la playlist")
async def bot_vl(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message(
            "Cette commande doit être utilisée dans un serveur.", ephemeral=True
        )

    player = get_player(interaction.guild.id)

    if not player.voice_client or not player.voice_client.is_connected():
        return await interaction.response.send_message(
            "❌ Le bot n'est pas en vocal. Lance d'abord `/bot_voc`.",
            ephemeral=True
        )

    vol_pct = int(player.volume * 100)
    title = player.current.title if player.current else "Aucune musique"
    filled = int(player.volume * 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)

    embed = discord.Embed(
        title="🎚️ Contrôle du volume",
        description=(
            f"**Volume actuel :** `{vol_pct}%`\n"
            f"{bar}\n\n"
            f"**En cours :** {title}"
        ),
        color=discord.Color.green() if player.playing else discord.Color.greyple()
    )
    embed.set_footer(text="Utilise les boutons ci-dessous pour régler le volume")

    view = VolumePanelView(interaction.guild.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# ANTI-SPAM : ban auto (liens Discord + @everyone / @here)
# ---------------------------------------------------------------------------

async def _try_autoban(message: discord.Message, reason: str, tracker: dict, user_id: int):
    """Tente de bannir le membre et nettoie le tracker."""
    try:
        bot_member = message.guild.me
        if (
            bot_member
            and message.author.top_role >= bot_member.top_role
            and message.guild.owner_id != message.author.id
        ):
            # Rôle trop haut → impossible de bannir
            tracker[user_id].clear()
            return

        await message.author.ban(
            reason=reason,
            delete_message_days=1,
        )
        tracker[user_id].clear()

        try:
            await message.channel.send(
                f"🔨 **{message.author}** a été **banni automatiquement**.\n"
                f"**Raison :** {reason}"
            )
        except discord.Forbidden:
            pass

    except discord.Forbidden:
        tracker[user_id].clear()
    except Exception as e:
        print(f"[Anti-spam] Erreur ban : {e}")
        tracker[user_id].clear()


@bot.event
async def on_message(message: discord.Message):
    if not message.guild:
        return

    # ---------- Forward leaks → leak-du-jour (accepte aussi les bots / webhooks) ----------
    try:
        await forward_leak_to_daily(message)
    except Exception as e:
        print(f"[Leak] Erreur forward : {e}")

    # Anti-spam : ignore les bots
    if message.author.bot:
        return

    now = time.time()
    user_id = message.author.id
    content = message.content or ""

    # ---------- Spam de liens d'invitation Discord ----------
    if INVITE_REGEX.search(content):
        timestamps = invite_spam_tracker[user_id]
        timestamps[:] = [t for t in timestamps if now - t < SPAM_WINDOW]
        timestamps.append(now)

        if len(timestamps) >= SPAM_THRESHOLD:
            await _try_autoban(
                message,
                f"Spam de liens Discord ({SPAM_THRESHOLD}+ liens en {int(SPAM_WINDOW)}s)",
                invite_spam_tracker,
                user_id,
            )
            return

    # ---------- Spam de @everyone / @here ----------
    if message.mention_everyone or "@everyone" in content.lower() or "@here" in content.lower():
        timestamps = everyone_spam_tracker[user_id]
        timestamps[:] = [t for t in timestamps if now - t < SPAM_WINDOW]
        timestamps.append(now)

        if len(timestamps) >= SPAM_THRESHOLD:
            await _try_autoban(
                message,
                f"Spam de @everyone / @here ({SPAM_THRESHOLD}+ mentions en {int(SPAM_WINDOW)}s)",
                everyone_spam_tracker,
                user_id,
            )
            return

    # Laisse passer les commandes prefix si tu en utilises
    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# ON READY + LANCEMENT
# ---------------------------------------------------------------------------

bot.tree.add_command(ticket_config)

@bot.event
async def on_ready():
    for guild in bot.guilds:
        config = get_config(guild.id)
        if config.get("types"):
            bot.add_view(TicketPanelView(guild.id, config))
        bot.add_view(TicketOpenView(guild.id))
        bot.add_view(TicketClosedView(guild.id))
        bot.add_view(RulesAcceptView(guild.id))
    if not getattr(bot, "_synced", False):
        try:
            synced = await bot.tree.sync()
            print(f"{len(synced)} commandes slash synchronisées.")
            bot._synced = True
        except Exception as exc:
            print(f"Erreur de synchronisation des commandes : {exc}")
    print(f"Connecté en tant que {bot.user}.")

if not TOKEN:
    raise RuntimeError("Ajoute DISCORD_TOKEN dans les variables d'environnement.")
bot.run(TOKEN)
