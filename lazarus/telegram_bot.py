"""Lazarus-CI Telegram Bot Integration.

Sends live incident alerts, diagnosis summaries, and provides inline
interactive Approve/Reject buttons for Human-In-The-Loop (HITL) gate.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from dotenv import load_dotenv
import httpx

load_dotenv()
logger = logging.getLogger(__name__)

_poll_thread: Optional[threading.Thread] = None


def get_token() -> str:
    return os.getenv("TELEGRAM_TOKEN", "").strip()


def get_chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def enabled() -> bool:
    return bool(get_token() and get_chat_id())


def _api(endpoint: str) -> str:
    return f"https://api.telegram.org/bot{get_token()}/{endpoint}"


def send(text: str, buttons: Optional[List[Dict[str, str]]] = None) -> bool:
    """Send message to the configured Telegram chat with optional inline keyboard buttons."""
    if not enabled():
        logger.debug("Telegram bot is not enabled (missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID)")
        return False

    body: Dict[str, Any] = {
        "chat_id": get_chat_id(),
        "text": text,
        "disable_web_page_preview": True,
    }
    if buttons:
        body["reply_markup"] = {"inline_keyboard": [buttons]}

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(_api("sendMessage"), json=body)
            return resp.status_code == 200
    except Exception as e:
        logger.warning("Failed to send Telegram message: %s", e)
        return False


def approval_buttons(inc_id: int) -> List[Dict[str, str]]:
    """Generate inline callback buttons for incident approval."""
    return [
        {"text": "✅ Approve & merge", "callback_data": f"approve:{inc_id}"},
        {"text": "⛔ Reject", "callback_data": f"reject:{inc_id}"},
    ]


def _poll(on_decision: Callable[[int, bool], Any]) -> None:
    offset = 0
    token = get_token()
    chat_id = get_chat_id()

    while True:
        if not get_token() or not get_chat_id():
            time.sleep(5)
            continue

        try:
            with httpx.Client(timeout=35.0) as client:
                r = client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"timeout": 20, "offset": offset},
                ).json()

                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    cq = u.get("callback_query")
                    if not cq:
                        continue

                    from_chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                    if from_chat != str(chat_id):
                        continue

                    data = cq.get("data", "")
                    if ":" in data:
                        action, inc_str = data.split(":", 1)
                        try:
                            inc_num = int(inc_str)
                            is_approved = action == "approve"
                            on_decision(inc_num, is_approved)
                            client.post(
                                f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                                json={
                                    "callback_query_id": cq["id"],
                                    "text": f"Decision recorded: {'Approved' if is_approved else 'Rejected'}",
                                },
                            )
                        except Exception as ex:
                            logger.error("Error processing Telegram callback: %s", ex)
        except Exception as e:
            time.sleep(3)


def start(on_decision: Callable[[int, bool], Any]) -> None:
    """Start background listener for Telegram inline callback button clicks."""
    global _poll_thread
    if enabled() and (_poll_thread is None or not _poll_thread.is_alive()):
        _poll_thread = threading.Thread(target=_poll, args=(on_decision,), daemon=True)
        _poll_thread.start()
        logger.info("Telegram approval listener started.")
