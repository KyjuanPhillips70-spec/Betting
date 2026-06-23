"""
telegram_alerts.py
------------------
Sends +EV bet alerts from the betting bot to a Telegram chat.

Production-hardened: connection pooling, retry-with-backoff (honors Telegram's
retry_after), flood-limit pacing, HTML escaping, dedup, consolidated cards,
and standard logging.

Setup (one-time):
  1. Message @BotFather on Telegram -> /newbot -> copy the token.
     (Reuse an existing bot via /mybots -> select -> "API Token".)
  2. Message your bot once (so it can reply to you).
  3. Run `python telegram_alerts.py --get-chat-id` to discover your chat ID,
     or message @userinfobot to get it instantly.
  4. Put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file.

Usage from the edge layer:
    from telegram_alerts import TelegramAlerter, BetAlert

    alerter = TelegramAlerter()  # reads token/chat_id from env
    alerter.send_batch(bets)               # one message per bet
    alerter.send_consolidated_card(bets)   # single message for the whole slate
"""

from __future__ import annotations

import os
import html
import time
import logging
import argparse
from dataclasses import dataclass
from typing import Optional, Iterable

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
    _HAS_RETRY = True
except ImportError:  # very old urllib3
    _HAS_RETRY = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional; env vars can be set however you like


logger = logging.getLogger("telegram_alerts")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Telegram allows ~30 messages/sec and is stricter per-chat. A small pause
# between sends keeps you comfortably under the per-chat flood cap.
_MIN_SEND_INTERVAL = 0.05  # seconds between consecutive sends
# Telegram hard limit on a single message body.
_MAX_MESSAGE_LEN = 4096


def _esc(text: object) -> str:
    """Escape user/data-supplied text so it can't break HTML parse_mode."""
    return html.escape(str(text), quote=False)


@dataclass
class BetAlert:
    """One recommended bet, ready to be formatted for Telegram."""
    sport: str            # "MLB" or "Soccer"
    event: str            # "Yankees @ Red Sox"
    market: str           # "Aaron Judge 2+ Total Bases" / "Over 8.5"
    book: str             # "FanDuel", "Hard Rock Bet", "PrizePicks"
    line: str             # American odds "+105" or PrizePicks line "o1.5"
    model_prob: float     # your model's probability, e.g. 0.581
    fair_prob: float      # market no-vig probability, e.g. 0.519
    stake_units: float    # fractional-Kelly stake in units

    @property
    def edge(self) -> float:
        return self.model_prob - self.fair_prob

    @property
    def dedup_key(self) -> str:
        """Stable identity for a bet, so the same one isn't sent twice."""
        return f"{self.sport}|{self.event}|{self.market}|{self.book}|{self.line}"


class TelegramAlerter:
    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: int = 10,
        max_retries: int = 3,
        min_edge_for_green: float = 0.04,
    ):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_edge_for_green = min_edge_for_green
        if not self.token:
            raise ValueError(
                "No Telegram token. Set TELEGRAM_BOT_TOKEN in your .env "
                "or pass token=..."
            )

        # One pooled session reused for every request (TCP/TLS handshake once).
        self._session = requests.Session()
        if _HAS_RETRY:
            retry = Retry(
                total=self.max_retries,
                backoff_factor=0.5,                 # 0.5s, 1s, 2s ...
                status_forcelist=(500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "POST"]),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=4,
                                  pool_maxsize=4)
            self._session.mount("https://", adapter)

        self._last_send_ts = 0.0
        self._sent_keys: set[str] = set()  # in-run dedup

    # ---- internal pacing ------------------------------------------------
    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_send_ts
        if elapsed < _MIN_SEND_INTERVAL:
            time.sleep(_MIN_SEND_INTERVAL - elapsed)
        self._last_send_ts = time.monotonic()

    # ---- low-level send -------------------------------------------------
    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a raw message. Returns True on success. Never raises on a
        network hiccup so it can't crash your main loop. Honors Telegram's
        429 retry_after, and auto-splits over-length messages."""
        if not self.chat_id:
            raise ValueError(
                "No chat_id. Set TELEGRAM_CHAT_ID in your .env or pass "
                "chat_id=... (run with --get-chat-id to find it)."
            )

        # Split messages that exceed Telegram's 4096-char limit.
        if len(text) > _MAX_MESSAGE_LEN:
            ok = True
            for chunk in _split_text(text, _MAX_MESSAGE_LEN):
                ok = self.send_message(chunk, parse_mode) and ok
            return ok

        url = TELEGRAM_API.format(token=self.token, method="sendMessage")
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                r = self._session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                logger.warning("telegram network error (attempt %d): %s",
                               attempt + 1, e)
                time.sleep(0.5 * (2 ** attempt))
                continue

            if r.status_code == 200 and r.json().get("ok"):
                return True

            # 429: respect Telegram's requested cooldown, then retry.
            if r.status_code == 429:
                try:
                    retry_after = r.json().get(
                        "parameters", {}).get("retry_after", 1)
                except ValueError:
                    retry_after = 1
                logger.warning("telegram 429; sleeping %ss", retry_after)
                time.sleep(float(retry_after) + 0.5)
                continue

            logger.error("telegram send failed: %s %s", r.status_code, r.text)
            return False

        logger.error("telegram send failed after %d attempts", self.max_retries)
        return False

    # ---- formatting helpers --------------------------------------------
    def _format_bet(self, bet: BetAlert) -> str:
        edge_pct = bet.edge * 100
        emoji = "\U0001F7E2" if bet.edge >= self.min_edge_for_green else "\U0001F7E1"
        bar = "━" * 15
        return (
            f"{emoji} <b>+EV BET — {_esc(bet.sport)}</b>\n"
            f"<b>{_esc(bet.event)}</b>\n"
            f"{bar}\n"
            f"\U0001F4CA <b>Market:</b> {_esc(bet.market)}\n"
            f"\U0001F3E6 <b>Book:</b> {_esc(bet.book)}  ({_esc(bet.line)})\n"
            f"\U0001F916 <b>Model:</b> {bet.model_prob*100:.1f}%\n"
            f"⚖️ <b>Fair (no-vig):</b> {bet.fair_prob*100:.1f}%\n"
            f"\U0001F4C8 <b>Edge:</b> +{edge_pct:.1f}%\n"
            f"\U0001F4B0 <b>Stake:</b> {bet.stake_units:.2f} units\n"
            f"{bar}\n"
            f"<i>Review and place manually.</i>"
        )

    # ---- public send methods -------------------------------------------
    def send_bet_alert(self, bet: BetAlert, dedup: bool = True) -> bool:
        if dedup and bet.dedup_key in self._sent_keys:
            logger.info("skipping duplicate bet: %s", bet.dedup_key)
            return True
        ok = self.send_message(self._format_bet(bet))
        if ok and dedup:
            self._sent_keys.add(bet.dedup_key)
        return ok

    def send_batch(self, bets: Iterable[BetAlert], dedup: bool = True) -> int:
        """One message per bet, highest edge first. Returns count sent."""
        bets = list(bets)
        if not bets:
            self.send_message("No +EV bets cleared the threshold today.")
            return 0
        sent = 0
        for bet in sorted(bets, key=lambda b: b.edge, reverse=True):
            if self.send_bet_alert(bet, dedup=dedup):
                sent += 1
        return sent

    def send_consolidated_card(self, bets: Iterable[BetAlert]) -> bool:
        """Single message for the whole slate. Fewer messages = less chance
        of hitting flood limits on big nights. Auto-splits if over 4096 chars."""
        bets = sorted(list(bets), key=lambda b: b.edge, reverse=True)
        if not bets:
            return self.send_message("No +EV bets cleared the threshold today.")
        header = f"\U0001F4CB <b>Today's +EV Card</b>  ({len(bets)} bets)\n\n"
        lines = []
        for b in bets:
            emoji = "\U0001F7E2" if b.edge >= self.min_edge_for_green else "\U0001F7E1"
            lines.append(
                f"{emoji} <b>{_esc(b.sport)}</b> — {_esc(b.market)}\n"
                f"   {_esc(b.event)} | {_esc(b.book)} {_esc(b.line)}\n"
                f"   Edge +{b.edge*100:.1f}% | {b.stake_units:.2f}u\n"
            )
        footer = "\n<i>Review and place manually.</i>"
        return self.send_message(header + "\n".join(lines) + footer)

    def reset_dedup(self) -> None:
        """Clear the in-run dedup set (call at the start of each scheduled run)."""
        self._sent_keys.clear()

    # ---- helper to discover your chat id -------------------------------
    def get_chat_id(self) -> None:
        url = TELEGRAM_API.format(token=self.token, method="getUpdates")
        try:
            r = self._session.get(url, timeout=self.timeout)
            data = r.json()
        except requests.RequestException as e:
            print(f"Network error: {e}")
            return
        results = data.get("result", [])
        if not results:
            print(
                "No updates found. Send a message to your bot in Telegram "
                "first, then run this again."
            )
            return
        seen = set()
        for upd in results:
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat", {})
            cid = chat.get("id")
            if cid and cid not in seen:
                seen.add(cid)
                name = chat.get("username") or chat.get("first_name") or "?"
                print(f"chat_id={cid}  ({name})")
        print("\nPut the chat_id above into TELEGRAM_CHAT_ID in your .env")


def _split_text(text: str, limit: int) -> list:
    """Split a long message on line boundaries, staying under `limit`."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Telegram alert utility")
    parser.add_argument("--get-chat-id", action="store_true",
                        help="Discover your chat ID (message your bot first).")
    parser.add_argument("--test", action="store_true",
                        help="Send a sample bet alert to verify the setup.")
    parser.add_argument("--test-card", action="store_true",
                        help="Send a sample consolidated multi-bet card.")
    args = parser.parse_args()

    alerter = TelegramAlerter()

    sample = [
        BetAlert("MLB", "Yankees @ Red Sox", "Aaron Judge 2+ Total Bases",
                 "FanDuel", "+105", 0.581, 0.519, 1.4),
        BetAlert("Soccer", "Arsenal vs Chelsea", "Over 2.5 Goals",
                 "Hard Rock Bet", "-120", 0.560, 0.531, 0.8),
    ]

    if args.get_chat_id:
        alerter.get_chat_id()
    elif args.test:
        ok = alerter.send_bet_alert(sample[0])
        print("Sent!" if ok else "Failed - check token/chat_id.")
    elif args.test_card:
        ok = alerter.send_consolidated_card(sample)
        print("Sent!" if ok else "Failed - check token/chat_id.")
    else:
        parser.print_help()
