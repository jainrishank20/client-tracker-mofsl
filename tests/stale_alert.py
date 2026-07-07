"""
Stale-data Telegram alert.
Called by stale_alert.yml when no successful daily run is detected today.
"""
import json, urllib.request, urllib.parse, os, sys
from datetime import datetime, timezone

cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bot_config.json')
cfg      = json.load(open(cfg_path))
token    = cfg['telegram_token']
chat_ids = [c.strip() for c in str(cfg['allowed_chat_id']).split(',')]

now_utc = datetime.now(timezone.utc).strftime('%H:%M UTC')
msg = (
    "⚠️ *Stale Data Alert*\n"
    f"`No successful daily run today \\({now_utc}\\)`\n\n"
    "Pipeline may have missed the 8:30 PM schedule or failed\\.\n"
    "Please check GitHub Actions and trigger manually if needed\\."
)

for chat_id in chat_ids:
    data = urllib.parse.urlencode({
        'chat_id':    chat_id,
        'text':       msg,
        'parse_mode': 'MarkdownV2',
    }).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data
            ),
            timeout=15
        )
        print(f"Alert sent to {chat_id}")
    except Exception as e:
        print(f"Failed to send to {chat_id}: {e}", file=sys.stderr)
        sys.exit(1)
