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

    def test_typing_delay_calculation(self) -> None:
        delay_short = self.engine.calculate_typing_delay("v")
        delay_long = self.engine.calculate_typing_delay("this is a longer sentence for test")
        self.assertGreater(delay_long, delay_short)
        self.assertGreaterEqual(delay_short, 1.2)

    def test_game_over_response(self) -> None:
        responses = [self.engine.decide_game_over_response() for _ in range(100)]
        has_gg = any(r in ["gg", "ggs", "wp", "gj all", "gg wp"] for r in responses if r)
        self.assertTrue(has_gg)


if __name__ == "__main__":
    unittest.main()
