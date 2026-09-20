# Russian Vocabulary Study Bot (Anki decks)

A Discord bot written in Python with discord.py that helps members of a language-learning server study Russian vocabulary from Anki decks. It schedules reviews with a spaced-repetition algorithm and saves each learner's progress in a SQLite database.

## Features

- **Deck import:** reads Anki `.apkg` files from the `anki_decks/` folder (including compressed decks when `zstandard` is installed) and builds a card library, with each card's media files.
- **`!study`:** choose what to study (new words or due reviews) and how many words. Answer each card through buttons and pop-up forms, then rate how well you knew it. The answer is revealed with an image generated with Pillow.
- **Spaced repetition:** an SM-2-style algorithm updates each card's interval, repetition count and ease factor after every rating, so cards come back when they are due.
- **`!stats`:** shows your new words learned, reviews due, reviews this week and weekly accuracy.
- **`!refresh`** (moderator role only): re-imports the decks, adding new cards while keeping every learner's progress.
- **Limits:** each user can start at most 5 study sessions and study 100 words per hour (set in `config.py`).
- **Daily reminder:** once every 24 hours the bot posts a reminder in a chosen channel and pings a role.

## Tech stack

Python 3, discord.py (commands, buttons, modals, background tasks), SQLite, Pillow, zstandard, python-dotenv.

## Setup

1. Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications), copy its token, and turn on the **Message Content Intent** (the bot uses `!` commands).
2. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a file named `.env` in the project folder (see `.env.example`):
   ```
   DISCORD_BOT_TOKEN=your_discord_bot_token_here
   TARGET_CHANNEL_ID=your_channel_id_here
   PING_ROLE_ID=your_role_id_here
   ```
4. Put your own Anki deck files (`.apkg`) in an `anki_decks/` folder next to `bot.py`. Decks are not included in this repository.
5. Run the bot:
   ```bash
   python bot.py
   ```
   The progress database (`data/user_progress.sqlite3`) is created automatically on the first run.

## Notes

- The moderator role ID in `config.py` is set for my own server. Change it for yours.
- Deck files are not published because they may be copyrighted and are large.
- Never commit your `.env` file or the database.

## Project structure

```
anki-study-bot/
  bot.py             commands, spaced-repetition logic, quiz interface, deck import
  config.py          settings and limits
  requirements.txt
  .env.example
  anki_decks/        your own .apkg files (not in the repository)
  data/              progress database (created when the bot runs)
```

Author: Muhammad Farhan
