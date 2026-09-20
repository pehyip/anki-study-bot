import os
import re
import json
import sqlite3
import zipfile
import tempfile
import shutil
import random
import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord.ui import Button, Modal, TextInput, View
from PIL import Image, ImageDraw, ImageFont

try:
    import zstandard as zstd
except ImportError:
    zstd = None

import config

MEDIA_FOLDER = "extracted_media"

# ==========================================
# DISCORD BOT SETUP
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

card_library = {}
user_progress = {}
MEDIA_UPLOAD_LOCK = asyncio.Lock()
DECK_REFRESH_LOCK = asyncio.Lock()
study_requests = defaultdict(deque)

def database_connection():
    connection = sqlite3.connect(config.DATABASE_FILE)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection

def initialize_database():
    os.makedirs(os.path.dirname(config.DATABASE_FILE), exist_ok=True)
    with database_connection() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS cards (
                card_id TEXT PRIMARY KEY,
                source_deck TEXT NOT NULL,
                front TEXT NOT NULL,
                back TEXT NOT NULL,
                gender TEXT NOT NULL,
                images TEXT NOT NULL,
                audios TEXT NOT NULL,
                stress TEXT NOT NULL,
                plural TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_cards (
                user_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                interval INTEGER NOT NULL,
                repetitions INTEGER NOT NULL,
                ease_factor REAL NOT NULL,
                due_day INTEGER NOT NULL,
                status TEXT NOT NULL,
                last_review TEXT,
                PRIMARY KEY (user_id, card_id),
                FOREIGN KEY (card_id) REFERENCES cards(card_id)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                review_date TEXT NOT NULL,
                rating INTEGER NOT NULL,
                FOREIGN KEY (card_id) REFERENCES cards(card_id)
            );
        """)

def new_user_state():
    return {"cards": {}, "reviews": []}

def get_user_state(user_id):
    user_key = str(user_id)
    if user_key not in user_progress:
        user_progress[user_key] = new_user_state()
    return user_progress[user_key]

def get_card_state(user_id, card_id):
    state = get_user_state(user_id)
    if card_id not in state["cards"]:
        state["cards"][card_id] = {
            "interval": 0,
            "repetitions": 0,
            "ease_factor": 2.5,
            "due_day": 0,
            "status": "new",
            "last_review": None,
        }
    return state["cards"][card_id]

def is_card_due(card):
    if card.get("status") != "review":
        return False
    if not card.get("last_review"):
        return card.get("due_day", 0) <= 0
    try:
        last_review = datetime.fromisoformat(card["last_review"]).date()
    except ValueError:
        return card.get("due_day", 0) <= 0
    days_since_review = (datetime.now(timezone.utc).date() - last_review).days
    return days_since_review >= max(1, card.get("due_day", 0))

def study_rate_limit(user_id, word_count):
    now = time.monotonic()
    requests = study_requests[str(user_id)]
    while requests and now - requests[0][0] >= 3600:
        requests.popleft()

    sessions_used = len(requests)
    words_used = sum(request[1] for request in requests)
    if sessions_used >= config.MAX_STUDY_SESSIONS_PER_HOUR:
        return False, "You have reached the hourly study-session limit. Try again later."
    if words_used + word_count > config.MAX_STUDY_WORDS_PER_HOUR:
        return False, "You have reached the hourly word limit. Try again later."

    requests.append((now, word_count))
    return True, ""

def load_user_progress():
    global user_progress
    initialize_database()

    with database_connection() as connection:
        card_rows = connection.execute(
            "SELECT card_id, source_deck, front, back, gender, images, audios, stress, plural FROM cards"
        ).fetchall()
        user_rows = connection.execute(
            "SELECT user_id, card_id, interval, repetitions, ease_factor, due_day, status, last_review FROM user_cards"
        ).fetchall()
        review_rows = connection.execute(
            "SELECT user_id, card_id, review_date, rating FROM reviews ORDER BY review_id"
        ).fetchall()

    card_library.clear()
    for row in card_rows:
        source_deck = row[1] or row[0].rsplit("_", 1)[0]
        card_library[row[0]] = {
            "source_deck": source_deck, "front": row[2], "back": row[3], "gender": row[4],
            "images": json.loads(row[5]), "audios": json.loads(row[6]), "stress": row[7], "plural": row[8],
        }

    user_progress = {}
    for row in user_rows:
        state = get_user_state(row[0])
        state["cards"][row[1]] = {
            "interval": row[2], "repetitions": row[3], "ease_factor": row[4],
            "due_day": row[5], "status": row[6], "last_review": row[7],
        }
    for row in review_rows:
        get_user_state(row[0])["reviews"].append({
            "card_id": row[1], "date": row[2], "rating": row[3],
        })

def save_user_progress():
    initialize_database()
    with database_connection() as connection:
        for card_id, card in card_library.items():
            connection.execute(
                """
                INSERT INTO cards (card_id, source_deck, front, back, gender, images, audios, stress, plural)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_id) DO UPDATE SET
                    source_deck = excluded.source_deck,
                    front = excluded.front,
                    back = excluded.back,
                    gender = excluded.gender,
                    images = excluded.images,
                    audios = excluded.audios,
                    stress = excluded.stress,
                    plural = excluded.plural
                """,
                (card_id, card.get("source_deck", ""), card["front"], card["back"], card["gender"],
                 json.dumps(card.get("images", [])), json.dumps(card.get("audios", [])),
                 card.get("stress", ""), card.get("plural", "")),
            )
        for user_id, state in user_progress.items():
            for card_id, card in state.get("cards", {}).items():
                connection.execute(
                    "INSERT OR REPLACE INTO user_cards VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, card_id, card["interval"], card["repetitions"], card["ease_factor"],
                     card["due_day"], card["status"], card.get("last_review")),
                )
            connection.execute("DELETE FROM reviews WHERE user_id = ?", (user_id,))
            connection.executemany(
                "INSERT INTO reviews (user_id, card_id, review_date, rating) VALUES (?, ?, ?, ?)",
                [(user_id, review["card_id"], review["date"], review["rating"])
                 for review in state.get("reviews", [])],
            )

def save_card_progress(user_id, card_id, card, review):
    with database_connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO user_cards VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(user_id), card_id, card["interval"], card["repetitions"], card["ease_factor"],
             card["due_day"], card["status"], card.get("last_review")),
        )
        connection.execute(
            "INSERT INTO reviews (user_id, card_id, review_date, rating) VALUES (?, ?, ?, ?)",
            (str(user_id), review["card_id"], review["date"], review["rating"]),
        )

# ==========================================
# ANKI & MEDIA EXTRACTORS
# ==========================================
def clean_html_and_extract_media(raw_string: str):
    # Extract images and sound references before stripping tags
    images = re.findall(r'(?:src=["\']|filename=["\'])([^"\']+\.(?:png|jpg|jpeg|gif|webp))', raw_string, re.IGNORECASE)
    audios = re.findall(r'\[sound:([^\]]+)\]', raw_string, re.IGNORECASE)
    audios += re.findall(r'(?:src=["\']|filename=["\'])([^"\']+\.(?:mp3|wav|ogg|m4a))', raw_string, re.IGNORECASE)
    
    # Clean out media tags, styles, scripts, and HTML tags entirely
    text = re.sub(r'\[sound:[^\]]+\]', '', raw_string)
    text = re.sub(r'<style.*?>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script.*?>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text, list(set(images)), list(set(audios))

def extract_gender(text: str) -> str:
    text_clean = text.strip().lower()
    if not text_clean or text_clean == "vocabulary item":
        return "N/A"
    
    words = [w for w in re.findall(r'\b[а-яёА-ЯЁa-zA-Z]+\b', text_clean)]
    target_word = words[0] if words else text_clean
    
    if target_word.endswith(('а', 'я')):
        return "Feminine (Женский род)"
    elif target_word.endswith(('о', 'е', 'ё')):
        return "Neuter (Средний род)"
    elif target_word.endswith('ь'):
        return "Soft Sign (Feminine/Masculine)"
    elif re.search(r'[бвгджзклмнпрстфхцчшщ]$', target_word):
        return "Masculine (Мужской род)"
    
    return "Masculine (Мужской род)"

def extract_cards_from_db(db_path, file_name):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [t[0] for t in cursor.fetchall()]

    cards = []

    if "cards" in tables and "notes" in tables:
        # Fetch actual cards mapped to their parent notes
        cursor.execute("SELECT cards.id, notes.flds FROM cards JOIN notes ON cards.nid = notes.id")
        rows = cursor.fetchall()

        for card_id, flds in rows:
            parts = flds.split("\x1f") if isinstance(flds, str) else [str(flds)]
            if not parts:
                continue

            all_images = []
            all_audios = []
            valid_texts = []

            for part in parts:
                if "Please update to the latest Anki version" in part:
                    continue
                txt, imgs, auds = clean_html_and_extract_media(part)
                if txt:
                    valid_texts.append(txt)
                all_images.extend(imgs)
                all_audios.extend(auds)

            images = list(set(all_images))
            audios = list(set(all_audios))

            front_txt = valid_texts[0] if valid_texts else "Vocabulary Item"
            back_txt = valid_texts[1] if len(valid_texts) > 1 else ("N/A" if len(valid_texts) == 1 else "See flashcard media")

            cards.append({
                "id": f"{file_name}_{card_id}",
                "source_deck": file_name,
                "front": front_txt,
                "back": back_txt,
                "images": images,
                "audios": audios,
                "stress": valid_texts[2] if len(valid_texts) > 2 else "",
                "plural": valid_texts[3] if len(valid_texts) > 3 else "",
            })

    conn.close()
    return cards

def extract_card_media(card, target_directory):
    source_deck = card.get("source_deck")
    if not source_deck:
        return

    deck_path = os.path.join(config.DECKS_DIRECTORY, source_deck)
    if not os.path.exists(deck_path):
        return

    requested_names = set(card.get("images", []) + card.get("audios", []))
    if not requested_names:
        return

    os.makedirs(target_directory, exist_ok=True)
    with zipfile.ZipFile(deck_path, "r") as archive:
        try:
            media_map = json.loads(archive.read("media").decode("utf-8"))
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
            return

        for numeric_name, real_name in media_map.items():
            if real_name not in requested_names and os.path.basename(real_name) not in requested_names:
                continue
            target_name = os.path.basename(real_name)
            target_path = os.path.join(target_directory, target_name)
            with archive.open(numeric_name, "r") as source, open(target_path, "wb") as target:
                shutil.copyfileobj(source, target)

def audit_and_import_anki_decks(directory: str):
    if os.path.isdir(MEDIA_FOLDER):
        shutil.rmtree(MEDIA_FOLDER)
    if not os.path.exists(directory):
        os.makedirs(directory)
        return

    apkg_files = [f for f in os.listdir(directory) if f.endswith('.apkg')]
    if not apkg_files:
        print(f"[!] No .apkg files found in '{directory}'.")
        return

    print(f"[*] Auditing and importing {len(apkg_files)} Anki deck(s)...")

    total_new = 0
    for file_name in apkg_files:
        file_path = os.path.join(directory, file_name)
        with tempfile.TemporaryDirectory() as extract_dir:
            try:
                with zipfile.ZipFile(file_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)

                cards = []
                zstd_db_path = os.path.join(extract_dir, "collection.anki21b")
                std_db_path = os.path.join(extract_dir, "collection.anki2")
                v21_db_path = os.path.join(extract_dir, "collection.anki21")

                if os.path.exists(zstd_db_path) and zstd is not None:
                    decompressed_path = os.path.join(extract_dir, "decompressed.anki2")
                    dctx = zstd.ZstdDecompressor()
                    with open(zstd_db_path, 'rb') as ifh, open(decompressed_path, 'wb') as ofh:
                        dctx.copy_stream(ifh, ofh)
                    cards = extract_cards_from_db(decompressed_path, file_name)

                if not cards and os.path.exists(v21_db_path):
                    cards = extract_cards_from_db(v21_db_path, file_name)

                if not cards and os.path.exists(std_db_path):
                    cards = extract_cards_from_db(std_db_path, file_name)

                imported_count = 0
                for c in cards:
                    card_id = c["id"]
                    is_new_card = card_id not in card_library
                    gender = extract_gender(c["front"])
                    card_library[card_id] = {
                        "source_deck": c["source_deck"],
                        "front": c["front"],
                        "back": c["back"],
                        "gender": gender,
                        "images": c["images"],
                        "audios": c["audios"],
                        "stress": c["stress"],
                        "plural": c["plural"],
                    }
                    imported_count += 1
                    if is_new_card:
                        total_new += 1

                print(f"[✓] Successfully imported {imported_count} cards from {file_name}.")
            except Exception as e:
                print(f"[X] Failed to parse {file_name}: {e}")

    save_user_progress()
    return total_new

# ==========================================
# SPACED REPETITION ALGORITHM (SM-2)
# ==========================================
def calculate_sm2(rating: int, interval: int, reps: int, ease: float):
    if rating == 0:
        reps = 0
        interval = 1
        ease = max(1.3, ease - 0.2)
    elif rating == 1:
        interval = max(2, round(interval * 1.2)) if reps else 2
        reps = max(1, reps)
        ease = max(1.3, ease - 0.15)
    else:
        if reps == 0:
            interval = 4 if rating == 2 else 7
        elif reps == 1:
            interval = 6
        else:
            interval = round(interval * ease)
        reps += 1

        if rating == 3:
            interval = max(interval + 1, round(interval * 1.3))
            ease += 0.1

        ease = max(1.3, ease)

    return interval, reps, ease

# ==========================================
# SEQUENTIAL QUIZ SESSION & UI
# ==========================================
def flashcard_font(size):
    for font_path in ("C:\\Windows\\Fonts\\arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(font_path):
            return ImageFont.truetype(font_path, size)
    return ImageFont.load_default()


def create_reveal_gif(card, target_directory):
    width, height = 1200, 700
    title_font = flashcard_font(42)
    word_font = flashcard_font(92)
    meaning_font = flashcard_font(64)
    frames = []

    def make_slide(label, value, x_offset=0):
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, width, 12), fill=(30, 80, 150))
        draw.text((width // 2 + x_offset, 170), label, font=title_font, fill="black", anchor="mm")
        draw.text((width // 2 + x_offset, 360), value, font=word_font if label == "Russian" else meaning_font,
                  fill="black", anchor="mm", align="center")
        return image

    russian = str(card["front"])
    meaning = str(card["back"])
    slides = [make_slide("Russian", russian), make_slide("Meaning", meaning)]
    for current, following in zip(slides, slides[1:]):
        frames.append(current)
        for step in range(1, 7):
            offset = width * step // 6
            transition = Image.new("RGB", (width, height), "white")
            transition.paste(current, (-offset, 0))
            transition.paste(following, (width - offset, 0))
            frames.append(transition)
    frames.append(slides[-1])

    gif_path = os.path.join(target_directory, "reveal.gif")
    frames[0].save(
        gif_path,
        save_all=True,
        append_images=frames[1:],
        duration=[1400] + [90] * 6 + [2200],
        loop=0,
    )
    return gif_path


class QuizSession:
    def __init__(self, channel, user_id, queue, mode):
        self.channel = channel
        self.user_id = str(user_id)
        self.queue = queue
        self.mode = mode
        self.current_index = 0
        self.media_directory = None

    def clear_media(self):
        if self.media_directory is not None:
            self.media_directory.cleanup()
            self.media_directory = None

    async def send_card(self, interaction: discord.Interaction = None):
        if self.current_index >= len(self.queue):
            msg = "🎉 **Quiz session complete! Excellent work."
            self.clear_media()
            if interaction:
                await interaction.followup.send(msg)
            else:
                await self.channel.send(msg)
            return

        await MEDIA_UPLOAD_LOCK.acquire()
        try:
            card_id = self.queue[self.current_index]
            card = card_library[card_id]
            self.clear_media()
            self.media_directory = tempfile.TemporaryDirectory(prefix="quiz_media_")
            extract_card_media(card, self.media_directory.name)
            if self.mode == "review":
                direction = "image_to_ru"
                prompt = "What is this in Russian?"
                prompt_label = "Identify the image"
            else:
                direction = random.choice(("ru_to_en", "en_to_ru"))
                prompt = card["front"] if direction == "ru_to_en" else card["back"]
                prompt_label = "Russian Word" if direction == "ru_to_en" else "English Meaning"

            files = []
            embed = discord.Embed(title="🇷🇺 Russian Vocabulary Flashcard", color=discord.Color.blue())
            if self.mode == "review":
                embed.description = "### What is this in Russian?\n\n**Type the Russian word after revealing the answer.**"
            else:
                embed.description = f"### {prompt_label}\n\n## {prompt}"
            if self.mode != "review":
                embed.add_field(name="Grammar Gender", value=card["gender"], inline=True)
            embed.set_footer(text=f"Card {self.current_index + 1} of {len(self.queue)}")

            if self.mode == "new":
                reveal_path = create_reveal_gif(card, self.media_directory.name)
                embed.description = "### Learn this word\n\nWatch the word slide into its meaning, then write the Russian word."
                files.append(discord.File(reveal_path, filename="reveal.gif"))
                embed.set_image(url="attachment://reveal.gif")
                view = RecallPromptView(card_id, embed, self)
            else:
                for image_name in card.get("images", []):
                    image_path = os.path.join(self.media_directory.name, os.path.basename(image_name))
                    if os.path.exists(image_path):
                        files.append(discord.File(image_path, filename=image_name))
                        embed.set_image(url=f"attachment://{os.path.basename(image_name)}")
                        break
                view = QuizView(card_id, embed, self, direction)

            for audio_name in card.get("audios", []):
                audio_path = os.path.join(self.media_directory.name, os.path.basename(audio_name))
                if os.path.exists(audio_path):
                    audio_ext = os.path.splitext(audio_name)[1].lower()
                    files.append(discord.File(audio_path, filename=f"pronunciation{audio_ext}"))
                    break

            if interaction:
                await interaction.followup.send(embed=embed, files=files, view=view)
            else:
                await self.channel.send(embed=embed, files=files, view=view)
        finally:
            MEDIA_UPLOAD_LOCK.release()

class QuizView(View):
    def __init__(self, card_id: str, embed: discord.Embed, session: QuizSession, direction: str):
        super().__init__(timeout=120)
        self.card_id = card_id
        self.embed = embed
        self.session = session
        self.direction = direction

    def owns_interaction(self, interaction):
        return str(interaction.user.id) == self.session.user_id

    async def on_timeout(self):
        self.session.clear_media()

    @discord.ui.button(label="Type Answer", style=discord.ButtonStyle.secondary)
    async def type_answer(self, interaction: discord.Interaction, button: Button):
        if not self.owns_interaction(interaction):
            await interaction.response.send_message("This quiz card belongs to another learner.", ephemeral=True)
            return
        await interaction.response.send_modal(AnswerModal(self.card_id, self.session, self.direction))

    @discord.ui.button(label="Show Answer", style=discord.ButtonStyle.primary)
    async def show_answer(self, interaction: discord.Interaction, button: Button):
        if not self.owns_interaction(interaction):
            await interaction.response.send_message("This quiz card belongs to another learner.", ephemeral=True)
            return
        card = card_library[self.card_id]
        reveal_path = create_reveal_gif(card, self.session.media_directory.name)
        self.embed.description = "### Reveal\n\nWatch the word and meaning, then write the Russian word from memory."
        reveal_view = RecallPromptView(self.card_id, self.embed, self.session)
        await interaction.response.edit_message(
            embed=self.embed,
            attachments=[discord.File(reveal_path, filename="reveal.gif")],
            view=reveal_view,
        )


    async def reveal_answer(self, interaction: discord.Interaction, card):
        answer = card["front"] if self.direction == "image_to_ru" else (card["back"] if self.direction == "ru_to_en" else card["front"])
        self.embed.add_field(name="Answer", value=f"## {answer}", inline=False)
        if self.direction == "image_to_ru":
            self.embed.add_field(name="Translation / Meaning", value=card["back"], inline=False)
        else:
            if card.get("stress"):
                self.embed.add_field(name="Stress / Pronunciation", value=card["stress"], inline=True)
            if card.get("plural"):
                self.embed.add_field(name="Plural", value=card["plural"], inline=True)

        rating_view = RatingView(self.card_id, self.embed, self.session)
        await interaction.response.edit_message(embed=self.embed, view=rating_view)


class RecallPromptView(View):
    def __init__(self, card_id: str, embed: discord.Embed, session: QuizSession):
        super().__init__(timeout=120)
        self.card_id = card_id
        self.embed = embed
        self.session = session

    @discord.ui.button(label="Write Russian Answer", style=discord.ButtonStyle.primary)
    async def write_answer(self, interaction: discord.Interaction, button: Button):
        if str(interaction.user.id) != self.session.user_id:
            await interaction.response.send_message("This quiz card belongs to another learner.", ephemeral=True)
            return
        await interaction.response.send_modal(
            RecallModal(self.card_id, self.embed, self.session)
        )


class RecallModal(Modal):
    answer = TextInput(
        label="Write the Russian word",
        placeholder="Type what the image shows in Russian",
        required=True,
        max_length=200,
    )

    def __init__(self, card_id, embed, session):
        super().__init__(title="Recall the Russian word")
        self.card_id = card_id
        self.embed = embed
        self.session = session

    async def on_submit(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.session.user_id:
            await interaction.response.send_message("This quiz card belongs to another learner.", ephemeral=True)
            return

        card = card_library[self.card_id]
        normalize = lambda value: re.sub(r"[^\w\sёЁ-]", "", value.casefold())
        expected = re.sub(r"\s+", " ", normalize(card["front"]).strip())
        actual = re.sub(r"\s+", " ", normalize(str(self.answer)).strip())
        answer_label = "Your answer (correct)" if actual == expected else "Your answer"
        self.embed.add_field(name=answer_label, value=f"## {self.answer}", inline=False)
        self.embed.add_field(name="Answer", value=f"## {card['front']}", inline=False)
        self.embed.add_field(name="Translation / Meaning", value=card["back"], inline=False)
        rating_view = RatingView(self.card_id, self.embed, self.session)
        await interaction.response.edit_message(embed=self.embed, view=rating_view)

class AnswerModal(Modal):
    answer = TextInput(label="Your answer", placeholder="Type the translation", required=True, max_length=200)

    def __init__(self, card_id, session, direction):
        title = "Type the Russian word" if direction == "image_to_ru" else ("Type the English translation" if direction == "ru_to_en" else "Type the Russian translation")
        super().__init__(title=title)
        self.card_id = card_id
        self.session = session
        self.direction = direction

    async def on_submit(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.session.user_id:
            await interaction.response.send_message("This quiz card belongs to another learner.", ephemeral=True)
            return
        card = card_library[self.card_id]
        expected = card["back"] if self.direction == "ru_to_en" else card["front"]
        normalize = lambda value: re.sub(r"[^\w\sёЁ-]", "", value.casefold())
        expected_normalized = re.sub(r"\s+", " ", normalize(expected).strip())
        actual_normalized = re.sub(r"\s+", " ", normalize(str(self.answer)).strip())

        if actual_normalized != expected_normalized:
            await interaction.response.send_message(
                f"Not quite. The answer is **{expected}**", ephemeral=True
            )
            return

        learner_card = get_card_state(self.session.user_id, self.card_id)
        interval, repetitions, ease = calculate_sm2(
            3, learner_card["interval"], learner_card["repetitions"], learner_card["ease_factor"]
        )
        learner_card.update({
            "interval": interval,
            "repetitions": repetitions,
            "ease_factor": ease,
            "due_day": interval,
            "status": "review",
            "last_review": datetime.now(timezone.utc).date().isoformat(),
        })
        review = {
            "date": learner_card["last_review"],
            "rating": 3,
            "card_id": self.card_id,
        }
        get_user_state(self.session.user_id)["reviews"].append(review)
        save_card_progress(self.session.user_id, self.card_id, learner_card, review)
        await interaction.response.send_message(
            f"Correct! Marked **Easy**. Next review in {interval} day(s).", ephemeral=True
        )
        self.session.current_index += 1
        await self.session.send_card(interaction)

class RatingView(View):
    def __init__(self, card_id: str, embed: discord.Embed, session: QuizSession):
        super().__init__(timeout=120)
        self.card_id = card_id
        self.embed = embed
        self.session = session

    def owns_interaction(self, interaction):
        return str(interaction.user.id) == self.session.user_id

    async def on_timeout(self):
        self.session.clear_media()

    async def process_rating(self, interaction: discord.Interaction, rating: int, label: str):
        if not self.owns_interaction(interaction):
            await interaction.response.send_message("This quiz card belongs to another learner.", ephemeral=True)
            return
        card = get_card_state(self.session.user_id, self.card_id)
        interval, reps, ease = calculate_sm2(
            rating, card["interval"], card["repetitions"], card["ease_factor"]
        )

        await interaction.response.defer(ephemeral=True)

        card["interval"] = interval
        card["repetitions"] = reps
        card["ease_factor"] = ease
        card["due_day"] = interval
        card["status"] = "review"
        card["last_review"] = datetime.now(timezone.utc).date().isoformat()
        review = {
            "date": card["last_review"],
            "rating": rating,
            "card_id": self.card_id,
        }
        get_user_state(self.session.user_id)["reviews"].append(review)
        save_card_progress(self.session.user_id, self.card_id, card, review)

        await interaction.followup.send(
            f"Logged **{label}**! Next review in {interval} day(s).", ephemeral=True
        )
        self.stop()

        self.session.current_index += 1
        await self.session.send_card(interaction)

    @discord.ui.button(label="Again (0)", style=discord.ButtonStyle.danger)
    async def again(self, interaction: discord.Interaction, button: Button):
        await self.process_rating(interaction, 0, "Again")

    @discord.ui.button(label="Hard (1)", style=discord.ButtonStyle.secondary)
    async def hard(self, interaction: discord.Interaction, button: Button):
        await self.process_rating(interaction, 1, "Hard")

    @discord.ui.button(label="Good (2)", style=discord.ButtonStyle.success)
    async def good(self, interaction: discord.Interaction, button: Button):
        await self.process_rating(interaction, 2, "Good")

    @discord.ui.button(label="Easy (3)", style=discord.ButtonStyle.primary)
    async def easy(self, interaction: discord.Interaction, button: Button):
        await self.process_rating(interaction, 3, "Easy")

class StudyCountModal(Modal):
    count = TextInput(label="How many words?", placeholder="Enter a number", required=True, max_length=3)

    def __init__(self, user_id, mode):
        super().__init__(title="Choose your session size")
        self.user_id = str(user_id)
        self.mode = mode

    async def on_submit(self, interaction: discord.Interaction):
        try:
            count = int(str(self.count).strip())
        except ValueError:
            await interaction.response.send_message("Please enter a whole number.", ephemeral=True)
            return
        if count < 1:
            await interaction.response.send_message("Choose at least 1 word.", ephemeral=True)
            return

        if self.mode == "new":
            candidates = [card_id for card_id in card_library if get_card_state(self.user_id, card_id)["status"] == "new"]
        else:
            candidates = [card_id for card_id in card_library if card_library[card_id].get("images") and is_card_due(get_card_state(self.user_id, card_id))]

        if not candidates:
            message = "You have no image-based old words due right now." if self.mode == "review" else "You have no new words left."
            await interaction.response.send_message(message, ephemeral=True)
            return

        queue = random.sample(candidates, min(count, len(candidates)))
        allowed, message = study_rate_limit(self.user_id, len(queue))
        if not allowed:
            await interaction.response.send_message(message, ephemeral=True)
            return
        await interaction.response.send_message(
            f"Starting {len(queue)} {'new' if self.mode == 'new' else 'old'} word(s).", ephemeral=True
        )
        await QuizSession(interaction.channel, self.user_id, queue, self.mode).send_card()

class StudySetupView(View):
    def __init__(self, user_id):
        super().__init__(timeout=120)
        self.user_id = str(user_id)
        self.message = None

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.NotFound:
                pass

    @discord.ui.button(label="Learn New Words", style=discord.ButtonStyle.success)
    async def new_words(self, interaction: discord.Interaction, button: Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This study setup belongs to another learner.", ephemeral=True)
            return
        await interaction.response.send_modal(StudyCountModal(self.user_id, "new"))

    @discord.ui.button(label="Review Old Words", style=discord.ButtonStyle.primary)
    async def old_words(self, interaction: discord.Interaction, button: Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This study setup belongs to another learner.", ephemeral=True)
            return
        due = [card_id for card_id in card_library if card_library[card_id].get("images") and is_card_due(get_card_state(self.user_id, card_id))]
        if not due:
            await interaction.response.send_message("You have no old words due, so choose Learn New Words.", ephemeral=True)
            return
        await interaction.response.send_modal(StudyCountModal(self.user_id, "review"))

# ==========================================
# BOT EVENTS & COMMANDS
# ==========================================
@tasks.loop(hours=24)
async def daily_quiz_reminder():
    channel = bot.get_channel(config.TARGET_CHANNEL_ID)
    if not channel:
        return

    await channel.send(
        f"<@&{config.PING_ROLE_ID}> **Daily Russian Vocab Reminder!**\n"
        "Use `!study` to choose new words or due reviews and set your own amount."
    )

@daily_quiz_reminder.before_loop
async def before_daily_quiz():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    load_user_progress()
    if not card_library:
        audit_and_import_anki_decks(config.DECKS_DIRECTORY)

    await bot.change_presence(
        status=discord.Status.idle,
        activity=discord.Game(name="!study to learn Russian 🇷🇺"),
    )

    if not daily_quiz_reminder.is_running():
        daily_quiz_reminder.start()
    print(f"[✓] Logged in as {bot.user.name} (ID: {bot.user.id})")

@bot.command(name="study")
async def trigger_study(ctx):
    view = StudySetupView(ctx.author.id)
    view.message = await ctx.send(
        f"{ctx.author.mention}, choose what you want to study and then enter the number of words.",
        view=view,
    )

@bot.command(name="refresh")
async def refresh_decks(ctx):
    if not any(role.id == config.MOD_ROLE_ID for role in getattr(ctx.author, "roles", [])):
        await ctx.send("You need the moderator role to refresh the decks.", delete_after=10)
        return

    async with DECK_REFRESH_LOCK:
        await ctx.send("Refreshing Anki decks. Existing learner progress will be preserved.", delete_after=10)
        imported_count = await asyncio.to_thread(audit_and_import_anki_decks, config.DECKS_DIRECTORY)

    await ctx.send(f"Deck refresh complete. Added {imported_count} new card(s); existing progress was preserved.", delete_after=15)

@bot.command(name="stats")
async def show_stats(ctx):
    state = get_user_state(ctx.author.id)
    cards = [get_card_state(ctx.author.id, card_id) for card_id in card_library]
    reviews = state.get("reviews", [])
    today = datetime.now(timezone.utc).date()
    week_reviews = [review for review in reviews if (today - datetime.fromisoformat(review["date"]).date()).days < 7]
    learned = sum(card["status"] == "review" for card in cards)
    due = sum(is_card_due(card) for card in cards)
    correct = sum(review["rating"] >= 2 for review in week_reviews)
    accuracy = round(correct / len(week_reviews) * 100) if week_reviews else 0

    embed = discord.Embed(title=f"{ctx.author.display_name}'s Russian Progress", color=discord.Color.green())
    embed.add_field(name="New words learned", value=str(learned), inline=True)
    embed.add_field(name="Reviews due", value=str(due), inline=True)
    embed.add_field(name="Reviews this week", value=str(len(week_reviews)), inline=True)
    embed.add_field(name="Weekly accuracy", value=f"{accuracy}%", inline=True)
    await ctx.send(embed=embed)

if __name__ == "__main__":
    bot.run(config.DISCORD_BOT_TOKEN)