"""Deterministic Reflex Engine: instant rule-based chat and game reflexes.

Zero AI / LLM dependencies:
* 0.1ms latency (instant evaluation)
* Zero external pip packages (pure Python standard library: re, random, time)
* Natural human typing jitter and strict cooldown to prevent spam
* Context-aware role claims and bot-check evasion
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

log = logging.getLogger("wvbot.reflex")

# Slang and natural casual chat pools
BOT_CHECK_REPLIES = [
    "im here",
    "here",
    "v",
    "lag sry",
    "skip",
    "reading",
    "yes?",
    "what",
]

GAME_OVER_REPLIES = [
    "gg",
    "ggs",
    "wp",
    "gj all",
    "gg wp",
]

SKIP_ECHO_REPLIES = [
    "skip",
    "s",
    "skip please",
]

ROLE_SHORT_NAMES: dict[str, str] = {
    "aura-seer": "as",
    "seer": "seer",
    "spirit-seer": "ss",
    "medium": "med",
    "doctor": "doc",
    "bodyguard": "bg",
    "detective": "det",
    "sheriff": "sheriff",
    "marksman": "mm",
    "gunner": "gunner",
    "jailer": "jailer",
    "beast-hunter": "bh",
    "priest": "priest",
    "red-lady": "rl",
    "forger": "forger",
    "witch": "witch",
    "corruptor": "corruptor",
    "cupid": "cupid",
    "fool": "fool",
    "headhunter": "hh",
    "villager": "vill",
    "mayor": "mayor",
    "clerk": "clerk",
    "flower-child": "fc",
    "pacifist": "paci",
    "grumpy-grandma": "grandma",
    "avenger": "avenger",
    "loudmouth": "lm",
    "trapper": "trapper",
    "trappers": "trapper",
}


class ReflexEngine:
    """Evaluates incoming public chat messages and generates human-like reflexes."""

    def __init__(self, cooldown_sec: float = 20.0):
        self.cooldown_sec = cooldown_sec
        self.last_reply_time = 0.0
        self.last_reply_text = ""
        self.last_claimed_role = ""
        self.last_claim_time = 0.0

    def reset_game(self) -> None:
        """Clear per-game claim memory between matches."""
        self.last_claimed_role = ""
        self.last_claim_time = 0.0

    def can_reply(self, now: float | None = None) -> bool:
        """True if sufficient time has elapsed since the last automated reply."""
        if now is None:
            now = time.time()
        return (now - self.last_reply_time) >= self.cooldown_sec

    def note_reply(self, text: str, now: float | None = None) -> None:
        """Record timestamp of emitted reply."""
        if now is None:
            now = time.time()
        self.last_reply_time = now
        self.last_reply_text = text
        if (
            text.startswith("im ")
            or text in ("me", "w", "ww", "doc", "med here", "seer here")
        ):
            self.last_claim_time = now

    def calculate_typing_delay(self, text: str) -> float:
        """Calculates natural human typing delay in seconds."""
        # Baseline human reaction ~1.2s + ~70ms per character + random hesitation
        char_time = len(text) * 0.07
        hesitation = random.SystemRandom().uniform(0.3, 1.2)
        return round(1.2 + char_time + hesitation, 2)

    def decide_chat_response(
        self,
        msg: str,
        author_id: Any,
        state: Any,
        own_username: str = "",
        now: float | None = None,
    ) -> str | None:
        """Deterministic evaluation of a chat message.

        Returns string to reply, or None if no response is warranted.
        """
        if not msg or not isinstance(msg, str):
            return None

        own_id = getattr(state, "own_id", lambda: None)() if state is not None else None
        # Do not reply to self
        if own_id is not None and str(author_id) == str(own_id):
            return None

        if not self.can_reply(now):
            return None

        clean = msg.strip().lower()

        # 1. Direct Mention / Username match
        bot_names: list[str] = []
        if own_username and own_username.strip():
            bot_names.append(own_username.strip().lower())

        mentioned = False
        for name in bot_names:
            if re.search(r"\b" + re.escape(name) + r"\b", clean):
                mentioned = True
                break

        # Check seat number mention if available
        if state is not None and hasattr(state, "players") and own_id is not None:
            for p in getattr(state, "players", []):
                if own_id == p.id:
                    grid = getattr(p, "grid_idx", -1)
                    if isinstance(grid, int) and not isinstance(grid, bool) and grid >= 0:
                        seat_str = str(grid + 1)
                        # e.g., "14 afk?", "14 role?", "14 v?"
                        if re.search(r"\b" + seat_str + r"\b", clean):
                            mentioned = True
                    break

        if now is None:
            now = time.time()

        if mentioned:
            # If asked directly about role
            role_id = (getattr(state, "role_id", "") or "").lower().strip()
            is_role_query = any(k in clean for k in ("role", "claim", "what are you", "who r u", "who are you"))
            if is_role_query:
                if self.last_claimed_role == role_id and (now - self.last_claim_time) < 45.0:
                    return random.SystemRandom().choice(["already said", "told u", "said it"])
                self.last_claimed_role = role_id
                self.last_claim_time = now
                if "wolf" in role_id or "werewolf" in role_id:
                    return random.SystemRandom().choice(["w", "im ww", "ww"])
                short_role = ROLE_SHORT_NAMES.get(role_id, role_id.replace("-", " ") if role_id else "vill")
                return f"im {short_role}"

            # Generic response to being called out / pinged (never use filler if asked for role)
            choices = [c for c in BOT_CHECK_REPLIES if c != self.last_reply_text]
            return random.SystemRandom().choice(choices or BOT_CHECK_REPLIES)

        # 2. General bot / AFK inquiry
        if re.search(r"\b(are you (a )?bot|u bot|bot\?|who is bot|anyone afk)\b", clean):
            choices = ["no", "im here", "not bot", "v", "playing"]
            return random.SystemRandom().choice(choices)

        # 3. Role inquiry ("who med", "who seer", "who doc")
        role_id = (getattr(state, "role_id", "") or "").lower()
        if re.search(r"\bwho (is )?(med|medium)\b", clean):
            if role_id == "medium":
                return random.SystemRandom().choice(["me", "im med", "med here"])
            return None

        if re.search(r"\bwho (is )?(seer)\b", clean):
            if role_id == "seer":
                return random.SystemRandom().choice(["me", "im seer", "seer here"])
            return None

        if re.search(r"\bwho (is )?(doc|doctor)\b", clean):
            if role_id == "doctor":
                return random.SystemRandom().choice(["me", "im doc", "doc"])
            return None

        # 4. Skip consensus echo during voting phase
        phase = getattr(state, "phase", "")
        if phase in ("day-voting", "voting"):
            if re.match(r"^(s|skip|vote skip)$", clean):
                # 25% chance to echo to look like a participating voter
                if random.SystemRandom().random() < 0.25:
                    return random.SystemRandom().choice(SKIP_ECHO_REPLIES)

        return None

    def decide_game_over_response(self) -> str | None:
        """35% chance to send gg/wp at game completion."""
        if random.SystemRandom().random() < 0.35:
            return random.SystemRandom().choice(GAME_OVER_REPLIES)
        return None
