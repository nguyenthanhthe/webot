"""Tests of the decision policy: votes, kills, protections."""

from __future__ import annotations

import unittest

from wvbot.game import GameState, Policy


def state_with(players: list[dict], own_id=1) -> GameState:
    state = GameState()
    state.apply("game-started", {"players": players})
    state.set_own_id(own_id)
    return state


class VoteTargetTest(unittest.TestCase):
    def test_the_most_accused_player_is_voted(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob"},
                {"id": 3, "username": "carl"},
            ]
        )
        state.apply("game:chat-public:msg", {"authorId": 3, "msg": "bob is sus"})
        state.apply("game:chat-public:msg", {"authorId": 1, "msg": "vote bob"})
        self.assertEqual(policy.vote_target(state), 2)

    def test_accusation_needs_a_keyword(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob"},
            ]
        )
        state.apply("game:chat-public:msg", {"authorId": 1, "msg": "hello bob"})
        self.assertIsNone(policy.vote_target(state))

    def test_self_accusation_does_not_count(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob"},
            ]
        )
        state.apply("game:chat-public:msg", {"authorId": 2, "msg": "bob is a wolf"})
        self.assertIsNone(policy.vote_target(state))

    def test_no_signal_votes_to_skip(self) -> None:
        policy = Policy()
        state = state_with([{"id": 1}, {"id": 2}])
        self.assertIsNone(policy.vote_target(state))
        self.assertTrue(policy.skip_vote())

    def test_dead_players_are_not_voted(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob", "isAlive": False},
                {"id": 3, "username": "carl"},
                {"id": 4, "username": "ann"},
            ]
        )
        state.apply(
            "game:chat-public:msg",
            {"authorId": 3, "msg": "vote bob and ann, wolves"},
        )
        # bob is accused but dead, so the living ann gets the vote.
        self.assertEqual(policy.vote_target(state), 4)

    def test_the_bot_never_votes_for_itself(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "carl"},
            ]
        )
        state.apply("game:chat-public:msg", {"authorId": 2, "msg": "me is a wolf"})
        self.assertIsNone(policy.vote_target(state))


class KillTargetTest(unittest.TestCase):
    def test_own_id_is_never_killed(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob"},
            ]
        )
        for _ in range(20):
            self.assertNotEqual(policy.kill_target(state), 1)

    def test_wolf_teammates_are_not_killed(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "pal", "role": "werewolf", "team": "WEREWOLF"},
                {"id": 3, "username": "carl", "role": "seer", "team": "VILLAGE"},
            ]
        )
        for _ in range(20):
            self.assertNotEqual(policy.kill_target(state), 2)

    def test_falls_back_when_everybody_else_is_a_wolf(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "pal", "team": "WEREWOLF"},
            ]
        )
        self.assertEqual(policy.kill_target(state), 2)

    def test_nobody_alive_means_no_kill(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob", "isAlive": False},
            ]
        )
        self.assertIsNone(policy.kill_target(state))

    def test_dead_players_are_not_killed(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob", "isAlive": False},
                {"id": 3, "username": "carl"},
            ]
        )
        self.assertEqual(policy.kill_target(state), 3)


class ProtectTargetTest(unittest.TestCase):
    def test_own_id_is_never_protected(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob"},
            ]
        )
        for _ in range(20):
            self.assertNotEqual(policy.protect_target(state), 1)

    def test_nobody_else_alive_means_no_protection(self) -> None:
        policy = Policy()
        state = state_with([{"id": 1, "username": "me"}])
        self.assertIsNone(policy.protect_target(state))

    def test_protects_known_seer_first(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob", "role": "villager", "level": 10},
                {"id": 3, "username": "carl", "role": "seer", "level": 5},
            ]
        )
        self.assertEqual(policy.protect_target(state), 3)


class ReviveTargetTest(unittest.TestCase):
    def test_medium_revives_dead_villager_not_dead_wolf(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "wolf", "isAlive": False, "role": "werewolf", "team": "WEREWOLF"},
                {"id": 3, "username": "good", "isAlive": False, "role": "villager", "team": "VILLAGE", "level": 50},
            ]
        )
        self.assertEqual(policy.revive_target(state), 3)

    def test_medium_revives_highest_level_villager(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "low", "isAlive": False, "role": "villager", "level": 10},
                {"id": 3, "username": "high", "isAlive": False, "role": "doctor", "level": 80},
            ]
        )
        self.assertEqual(policy.revive_target(state), 3)

    def test_medium_returns_none_if_no_dead(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob", "isAlive": True},
            ]
        )
        self.assertIsNone(policy.revive_target(state))


class RevealTargetTest(unittest.TestCase):
    def test_seer_picks_unrevealed_player(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "known", "role": "doctor"},
                {"id": 3, "username": "unknown", "role": ""},
            ]
        )
        self.assertEqual(policy.reveal_target(state), 3)


class KillerTargetTest(unittest.TestCase):
    def test_safe_mode_suppresses_without_signal(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob"},
                {"id": 3, "username": "carl"},
            ]
        )
        self.assertIsNone(policy.killer_target(state, safe_mode=True))

    def test_safe_mode_kills_confirmed_wolf(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "me"},
                {"id": 2, "username": "bob", "role": "villager"},
                {"id": 3, "username": "bad", "role": "werewolf", "team": "WEREWOLF"},
            ]
        )
        self.assertEqual(policy.killer_target(state, safe_mode=True), 3)

    def test_killer_targets_claimed_wolf_from_chat(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "gunner_bot", "gridIdx": 0},
                {"id": 2, "username": "alice", "gridIdx": 1},
                {"id": 3, "username": "wolf_guy", "gridIdx": 4},  # seat 5
            ],
            own_id=1,
        )
        state.apply("game:chat-public:msg", {"authorId": 3, "msg": "5 w"})
        self.assertEqual(state.claimed_wolf_id, 3)
        self.assertEqual(state.claimed_wolf_seat, 5)
        self.assertEqual(policy.killer_target(state, safe_mode=True), 3)
        self.assertEqual(policy.vote_target(state), 3)

    def test_vill_win_mode_werewolves_do_not_bite_at_night(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "wolf_bot", "role": "werewolf"},
                {"id": 2, "username": "victim", "role": "villager"},
            ],
            own_id=1,
        )
        self.assertIsNone(policy.kill_target(state, vill_win_mode=True))
        self.assertEqual(policy.kill_target(state, vill_win_mode=False), 2)

    def test_bot_does_not_vote_self_when_bot_is_claimed_wolf(self) -> None:
        policy = Policy()
        state = state_with(
            [
                {"id": 1, "username": "wolf_bot", "gridIdx": 2},  # seat 3
                {"id": 2, "username": "villager", "gridIdx": 0},
            ],
            own_id=1,
        )
        state.apply("game:chat-public:msg", {"authorId": 1, "msg": "3 w"})
        self.assertEqual(state.claimed_wolf_id, 1)
        self.assertIsNone(policy.vote_target(state))


if __name__ == "__main__":
    unittest.main()
