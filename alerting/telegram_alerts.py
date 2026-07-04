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
    alerter.send_game_cards(bets)          # one message per game/event
    alerter.send_consolidated_card(bets)   # single message for the whole slate
"""

from __future__ import annotations

import os
import html
import time
import logging
import argparse
from dataclasses import dataclass, field
from typing import Optional, Iterable

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
    _HAS_RETRY = True
except ImportError:
    _HAS_RETRY = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


logger = logging.getLogger("telegram_alerts")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

_MIN_SEND_INTERVAL = 0.05
_MAX_MESSAGE_LEN   = 4096


def _esc(text: object) -> str:
    return html.escape(str(text), quote=False)


@dataclass
class BetAlert:
    """One recommended bet, ready to be formatted for Telegram."""
    sport:          str    # "MLB" or "Soccer"
    event:          str    # "Yankees @ Red Sox"
    market:         str    # "Over 8.5" / "Colombia ML"
    book:           str    # "FanDuel"
    line:           str    # "+105"
    model_prob:     float  # blended model probability
    fair_prob:      float  # market no-vig probability
    stake_units:    float  # fractional-Kelly stake in units
    projected_score: str = field(default="")  # "Proj: USA 1.18 – Morocco 1.05 goals"

    @property
    def edge(self) -> float:
        return self.model_prob - self.fair_prob

    @property
    def dedup_key(self) -> str:
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

        self._session = requests.Session()
        if _HAS_RETRY:
            retry = Retry(
                total=self.max_retries,
                backoff_factor=0.5,
                status_forcelist=(500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "POST"]),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=4,
                                  pool_maxsize=4)
            self._session.mount("https://", adapter)

        self._last_send_ts = 0.0
        self._sent_keys: set[str] = set()

    # ---- internal pacing ------------------------------------------------
    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_send_ts
        if elapsed < _MIN_SEND_INTERVAL:
            time.sleep(_MIN_SEND_INTERVAL - elapsed)
        self._last_send_ts = time.monotonic()

    # ---- low-level send -------------------------------------------------
    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self.chat_id:
            raise ValueError(
                "No chat_id. Set TELEGRAM_CHAT_ID in your .env or pass "
                "chat_id=... (run with --get-chat-id to find it)."
            )
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
                logger.warning("telegram network error (attempt %d): %s", attempt + 1, e)
                time.sleep(0.5 * (2 ** attempt))
                continue

            if r.status_code == 200 and r.json().get("ok"):
                return True

            if r.status_code == 429:
                try:
                    retry_after = r.json().get("parameters", {}).get("retry_after", 1)
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
        bar = "━" * 20
        lines = [
            f"{emoji} <b>+EV BET — {_esc(bet.sport)}</b>",
            f"<b>{_esc(bet.event)}</b>",
            bar,
        ]
        if bet.projected_score:
            lines.append(f"\U0001F4CA <i>{_esc(bet.projected_score)}</i>")
        lines += [
            f"\U0001F4CA <b>Market:</b> {_esc(bet.market)}",
            f"\U0001F3E6 <b>Book:</b> {_esc(bet.book)}  ({_esc(bet.line)})",
            f"\U0001F916 <b>Model:</b> {bet.model_prob*100:.1f}%",
            f"⚖️ <b>Fair (no-vig):</b> {bet.fair_prob*100:.1f}%",
            f"\U0001F4C8 <b>Edge:</b> +{edge_pct:.1f}%",
            f"\U0001F4B0 <b>Stake:</b> {bet.stake_units:.2f} units",
            bar,
            "<i>Review and place manually.</i>",
        ]
        return "\n".join(lines)

    def _format_game_card(self, bets: list[BetAlert]) -> str:
        """One card for a single event: header, projected score, then all picks."""
        b0 = bets[0]
        sport_emoji = "⚾" if b0.sport == "MLB" else "⚽"
        bar = "━" * 22
        lines = [
            f"{sport_emoji} <b>{_esc(b0.event)}</b>",
            bar,
        ]
        if b0.projected_score:
            lines.append(f"\U0001F4CA <i>{_esc(b0.projected_score)}</i>")
            lines.append("")
        for bet in bets:
            emoji = "\U0001F7E2" if bet.edge >= self.min_edge_for_green else "\U0001F7E1"
            lines.append(
                f"{emoji} <b>{_esc(bet.market)}</b>\n"
                f"   \U0001F3E6 {_esc(bet.book)}  <b>{_esc(bet.line)}</b>\n"
                f"   Model <b>{bet.model_prob*100:.1f}%</b> | "
                f"Fair {bet.fair_prob*100:.1f}% | "
                f"Edge <b>+{bet.edge*100:.1f}%</b> | {bet.stake_units:.2f}u"
            )
        lines.append("")
        lines.append("<i>Review and place manually.</i>")
        return "\n".join(lines)

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
        """One message per bet, highest edge first."""
        bets = list(bets)
        if not bets:
            self.send_message("No +EV bets cleared the threshold today.")
            return 0
        sent = 0
        for bet in sorted(bets, key=lambda b: b.edge, reverse=True):
            if self.send_bet_alert(bet, dedup=dedup):
                sent += 1
        return sent

    def send_game_cards(self, bets: Iterable[BetAlert]) -> int:
        """
        Send one Telegram card per game/event (user-requested format).
        Within each card, picks are sorted best-edge first.
        Returns the number of cards sent.
        """
        bets = list(bets)
        if not bets:
            self.send_message("No +EV bets cleared the threshold today.")
            return 0
        by_event: dict[str, list[BetAlert]] = {}
        for bet in bets:
            by_event.setdefault(bet.event, []).append(bet)
        sent = 0
        for event_bets in by_event.values():
            event_bets.sort(key=lambda b: b.edge, reverse=True)
            if self.send_message(self._format_game_card(event_bets)):
                sent += 1
        logger.info("Sent %d game card(s) to Telegram.", sent)
        return sent

    def send_consolidated_card(self, bets: Iterable[BetAlert]) -> bool:
        """Single message for the whole slate (legacy; prefer send_game_cards)."""
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
        self._sent_keys.clear()

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
    parser.add_argument("--get-chat-id", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--test-card", action="store_true")
    args = parser.parse_args()

    alerter = TelegramAlerter()

    sample = [
        BetAlert("MLB", "Yankees @ Red Sox", "Aaron Judge 2+ Total Bases",
                 "FanDuel", "+105", 0.581, 0.519, 1.4,
                 "Proj: Yankees 5.1 @ Red Sox 4.3 (9.4 total)"),
        BetAlert("Soccer", "Argentina vs France", "Over 2.5 Goals",
                 "Hard Rock Bet", "-120", 0.560, 0.531, 0.8,
                 "Proj: Argentina 1.52 – France 1.41 goals"),
    ]

    if args.get_chat_id:
        alerter.get_chat_id()
    elif args.test:
        ok = alerter.send_bet_alert(sample[0])
        print("Sent!" if ok else "Failed - check token/chat_id.")
    elif args.test_card:
        ok = alerter.send_game_cards(sample) > 0
        print("Sent!" if ok else "Failed - check token/chat_id.")
    else:
        parser.print_help()
