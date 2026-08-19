# Telegram Asset Bot

A Telegram bot that automatically detects supported URLs and returns:
- **Lummi.ai** → full-resolution image as a downloadable document
- **Hugeicons** → SVG code block + downloadable `.svg` file

## Setup

### Local
```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=your_token python unified_asset_bot.py
```

### Render Deployment
1. Push this repo to GitHub
2. Go to [render.com](https://render.com) → New → Background Worker
3. Connect your GitHub repo
4. Set env var: `TELEGRAM_BOT_TOKEN`
5. Build command: `pip install -r requirements.txt`
6. Start command: `python unified_asset_bot.py`

## UptimeRobot
Render free tier sleeps. To keep the bot alive, add a health endpoint and monitor it via [uptimerobot.com](https://uptimerobot.com) every 5 minutes.

## Supported Links
- `https://www.lummi.ai/photo/...`
- `https://www.lummi.ai/illustration/...`
- `https://www.lummi.ai/3d/...`
- `https://hugeicons.com/icon/...`

## Environment Variables
| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather |