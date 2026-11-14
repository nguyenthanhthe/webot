import unittest
from unittest.mock import MagicMock

from wvbot.game import GameState, Player
from wvbot.reflex import ReflexEngine


class ReflexEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ReflexEngine(cooldown_sec=10.0)
        self.state = GameState()
        p1 = Player(id="p1", username="Alice", is_alive=True, grid_idx=0)
        p2 = Player(id="p2", username="botzxc", is_alive=True, grid_idx=13)
        self.state.players = [p1, p2]
        self.state.set_own_id("p2")

    def test_ignore_own_messages(self) -> None:
        reply = self.engine.decide_chat_response(
            msg="hello botzxc",
            author_id="p2",
            state=self.state,
            own_username="botzxc",
        )
        self.assertIsNone(reply)

    def test_cooldown_enforced(self) -> None:
        self.engine.note_reply("v", now=100.0)
        reply = self.engine.decide_chat_response(
            msg="botzxc u there?",
            author_id="p1",
            state=self.state,
            own_username="botzxc",
            now=105.0,  # Only 5s later, cooldown is 10s
        )
        self.assertIsNone(reply)

        # After cooldown
        reply_after = self.engine.decide_chat_response(
            msg="botzxc u there?",
            author_id="p1",
            state=self.state,
            own_username="botzxc",
            now=111.0,
        )
        self.assertIsNotNone(reply_after)

    def test_direct_mention_by_username(self) -> None:
        reply = self.engine.decide_chat_response(
            msg="hey botzxc what are you doing",
            author_id="p1",
            state=self.state,
            own_username="botzxc",
            now=10.0,
        )
        self.assertIsNotNone(reply)

    def test_direct_mention_by_seat_number(self) -> None:
        # grid_idx is 13, so seat number is 14
        reply = self.engine.decide_chat_response(
            msg="14 afk?",
            author_id="p1",
            state=self.state,
            own_username="botzxc",
            now=10.0,
        )
        self.assertIsNotNone(reply)

    def test_role_inquiry_when_matching_role(self) -> None:
        self.state.role_id = "medium"
        reply = self.engine.decide_chat_response(
            msg="who is med?",
            author_id="p1",
            state=self.state,
            now=10.0,
        )
        self.assertIn(reply, ["me", "im med", "med here"])

    def test_role_inquiry_when_not_matching_role(self) -> None:
        self.state.role_id = "villager"
        reply = self.engine.decide_chat_response(
            msg="who is med?",
            author_id="p1",
            state=self.state,
            now=10.0,
        )
        self.assertIsNone(reply)

    def test_direct_mention_role_claim_aura_seer(self) -> None:
        self.state.role_id = "aura-seer"
        reply = self.engine.decide_chat_response(
            msg="14 role?",
            author_id="p1",
            state=self.state,
            own_username="botzxc",
            now=10.0,
        )
        self.assertEqual(reply, "im as")

    def test_direct_mention_role_claim_werewolf(self) -> None:
        self.state.role_id = "werewolf"
        reply = self.engine.decide_chat_response(
            msg="14 claim",
            author_id="p1",
            state=self.state,
            own_username="botzxc",
            now=10.0,
        )
        self.assertIn(reply, ["w", "im ww", "ww"])

    def test_direct_mention_role_claim_repeated(self) -> None:
        self.state.role_id = "aura-seer"
        self.engine.decide_chat_response(
            msg="14 role?",
            author_id="p1",
            state=self.state,
            own_username="botzxc",
            now=10.0,
        )
        reply = self.engine.decide_chat_response(
            msg="14 role please",
            author_id="p1",
            state=self.state,
            own_username="botzxc",
            now=25.0,  # Within 45s window
        )
        self.assertIn(reply, ["already said", "told u", "said it"])

    def test_typing_delay_calculation(self) -> None:
        delay_short = self.engine.calculate_typing_delay("v")
        delay_long = self.engine.calculate_typing_delay("this is a longer sentence for test")
        self.assertGreater(delay_long, delay_short)
        self.assertGreaterEqual(delay_short, 1.2)

    def test_game_over_response(self) -> None:
        responses = [self.engine.decide_game_over_response() for _ in range(100)]
        has_gg = any(r in ["gg", "ggs", "wp", "gj all", "gg wp"] for r in responses if r)
        self.assertTrue(has_gg)

    def test_removed_hardcoded_usernames_do_not_trigger_mention(self) -> None:
        # Other accounts must not react to "botzxc" / "zxc" in chat.
        for own_name in ("alice", ""):
            reply = self.engine.decide_chat_response(
                msg="hey botzxc and zxc",
                author_id="p1",
                state=self.state,
                own_username=own_name,
                now=10.0,
            )
            self.assertIsNone(reply)

    def test_negative_grid_idx_does_not_trigger_seat_zero_mention(self) -> None:
        self.state.players = [
            Player(id="p1", username="Alice", is_alive=True, grid_idx=-1),
            Player(id="p2", username="other", is_alive=True, grid_idx=-1),
        ]
        reply = self.engine.decide_chat_response(
            msg="0 afk?",
            author_id="p1",
            state=self.state,
            own_username="other",
            now=10.0,
        )
        self.assertIsNone(reply)

    def test_reset_game_clears_claim_memory_across_matches(self) -> None:
        self.state.role_id = "aura-seer"
        first = self.engine.decide_chat_response(
            msg="14 role?",
            author_id="p1",
            state=self.state,
            own_username="botzxc",
            now=10.0,
        )
        self.assertEqual(first, "im as")

        self.engine.reset_game()
        second = self.engine.decide_chat_response(
            msg="14 role?",
            author_id="p1",
            state=self.state,
            own_username="botzxc",
            now=35.0,  # Within 45s of first game's claim, past 20s cooldown
        )
        self.assertEqual(second, "im as")

    def test_note_reply_updates_last_claim_time_for_w_and_ww(self) -> None:
        self.engine.note_reply("w", now=123.0)
        self.assertEqual(self.engine.last_claim_time, 123.0)
        self.engine.note_reply("ww", now=456.0)
        self.assertEqual(self.engine.last_claim_time, 456.0)


if __name__ == "__main__":
    unittest.main()
