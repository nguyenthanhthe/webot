"""Tests of the game state: payload parsing, the XP tables, the events."""

from __future__ import annotations

import unittest

from wvbot.game import (
    LEVEL_XP,
    LEVEL_XP_TOTAL,
    GameState,
    Player,
    parse_payload,
    xp_to_next_level,
    xp_total_for_level,
)


class ParsePayloadTest(unittest.TestCase):
    def test_plain_object_passes_through(self) -> None:
        self.assertEqual(parse_payload({"a": 1}), {"a": 1})

    def test_json_string_is_unwrapped(self) -> None:
        self.assertEqual(parse_payload('{"a": 1}'), {"a": 1})

    def test_double_encoded_string_is_unwrapped_twice(self) -> None:
        raw = '"{\\"targetPlayerId\\": 4}"'
        self.assertEqual(parse_payload(raw), {"targetPlayerId": 4})

    def test_non_json_string_stays_a_string(self) -> None:
        self.assertEqual(parse_payload("hello"), "hello")

    def test_none_stays_none(self) -> None:
        self.assertIsNone(parse_payload(None))


class XpTablesTest(unittest.TestCase):
    def test_first_level_needs_the_first_entry(self) -> None:
        self.assertEqual(xp_to_next_level(1), LEVEL_XP[0])

    def test_second_level_needs_the_second_entry(self) -> None:
        self.assertEqual(xp_to_next_level(2), LEVEL_XP[1])

    def test_level_below_one_is_clamped(self) -> None:
        self.assertEqual(xp_to_next_level(0), LEVEL_XP[0])
        self.assertEqual(xp_to_next_level(-5), LEVEL_XP[0])

    def test_level_past_the_table_returns_the_last_entry(self) -> None:
        self.assertEqual(xp_to_next_level(len(LEVEL_XP) + 10), LEVEL_XP[-1])

    def test_total_is_the_prefix_sum(self) -> None:
        self.assertEqual(xp_total_for_level(1), 0)
        self.assertEqual(xp_total_for_level(2), LEVEL_XP[0])
        self.assertEqual(xp_total_for_level(3), LEVEL_XP[0] + LEVEL_XP[1])
        self.assertEqual(LEVEL_XP_TOTAL[1], LEVEL_XP[0])


class PlayerTest(unittest.TestCase):
    def test_full_object(self) -> None:
        player = Player.from_object(
            {
                "id": 1,
                "username": "ann",
                "gridIdx": 3,
                "isAlive": False,
                "role": {"id": "doctor", "team": "VILLAGE"},
                "level": "9",
            }
        )
        self.assertIsNotNone(player)
        self.assertEqual(player.id, 1)
        self.assertEqual(player.username, "ann")
        self.assertEqual(player.grid_idx, 3)
        self.assertFalse(player.is_alive)
        self.assertEqual(player.role, "doctor")
        self.assertEqual(player.team, "VILLAGE")
        self.assertEqual(player.level, 9)

    def test_grid_index_zero_stays_zero(self) -> None:
        # The first grid slot is index 0; a falsy-zero must not become -1.
        player = Player.from_object({"id": 2, "gridIdx": 0})
        self.assertEqual(player.grid_idx, 0)

    def test_role_as_plain_string(self) -> None:
        player = Player.from_object({"id": 3, "role": "werewolf", "team": "WEREWOLF"})
        self.assertEqual(player.role, "werewolf")
        self.assertEqual(player.team, "WEREWOLF")

    def test_missing_id_is_rejected(self) -> None:
        self.assertIsNone(Player.from_object({"username": "nobody"}))
        self.assertIsNone(Player.from_object("not a dict"))

    def test_bad_level_becomes_zero(self) -> None:
        player = Player.from_object({"id": 4, "level": "many"})
        self.assertEqual(player.level, 0)


def make_state() -> GameState:
    state = GameState()
    state.set_own_id(1)
    return state


class GameStateEventTest(unittest.TestCase):
    def test_game_started_sets_role_and_players(self) -> None:
        state = GameState()
        state.apply(
            "game-started",
            {
                "role": {"id": "serial-killer", "name": "Serial Killer", "team": "EVIL"},
                "players": [
                    {"id": 1, "username": "me"},
                    {"id": 2, "username": "bob"},
                ],
            },
        )
        self.assertEqual(state.phase, "started")
        self.assertEqual(state.role_id, "serial-killer")
        self.assertEqual(state.role_team, "EVIL")
        self.assertEqual(len(state.players), 2)

    def test_game_started_accepts_json_string(self) -> None:
        state = GameState()
        state.apply("game-started", '{"role": {"id": "jailer"}, "players": []}')
        self.assertEqual(state.role_id, "jailer")

    def test_wolf_team_detection(self) -> None:
        state = GameState()
        state.apply("game-started", {"role": {"id": "werewolf", "team": "WEREWOLF"}})
        self.assertTrue(state.is_wolf)

    def test_wolf_role_without_team(self) -> None:
        state = GameState()
        state.apply("game-started", {"role": {"id": "alpha-werewolf"}})
        self.assertTrue(state.is_wolf)

    def test_village_role_is_not_wolf(self) -> None:
        state = GameState()
        state.apply("game-started", {"role": {"id": "seer", "team": "VILLAGE"}})
        self.assertFalse(state.is_wolf)

    def test_killed_players_from_a_list(self) -> None:
        state = make_state()
        state.apply("game-started", {"players": [{"id": 1}, {"id": 2}, {"id": 3}]})
        state.apply("game-players-killed", [2, 3])
        state.apply("game-players-killed", [2])
        self.assertEqual(state.dead_ids, [2, 3])
        self.assertEqual(len(state.alive), 1)

    def test_killed_players_from_a_dict(self) -> None:
        state = make_state()
        state.apply("game-started", {"players": [{"id": 1}, {"id": 5}]})
        state.apply("game-players-killed", {"playerIds": [5]})
        self.assertEqual(state.dead_ids, [5])

    def test_day_vote_records_the_author(self) -> None:
        state = make_state()
        state.apply("game-day-vote-set", {"authorId": 7, "targetPlayerId": 9})
        self.assertEqual(state.day_votes, {7: 9})

    def test_day_vote_without_author_uses_own_id(self) -> None:
        state = make_state()
        state.apply("game-day-vote-set", '{"targetPlayerId": 9}')
        self.assertEqual(state.day_votes, {1: 9})

    def test_werewolf_vote_switches_the_phase(self) -> None:
        state = make_state()
        state.apply("game-werewolves-vote-set", {"targetPlayerId": 4})
        self.assertEqual(state.phase, "night-voting")
        self.assertEqual(state.night_votes, {1: 4})

    def test_award_is_kept(self) -> None:
        state = make_state()
        state.apply(
            "game-over-awards-available",
            {"playerAward": {"awardedTotalXp": 15, "awardedLevels": 0}},
        )
        self.assertEqual(state.phase, "game-over")
        self.assertEqual(state.last_award["awardedTotalXp"], 15)

    def test_chat_is_capped(self) -> None:
        state = make_state()
        for index in range(230):
            state.apply("game:chat-public:msg", {"msg": f"hi {index}", "authorId": 2})
        self.assertEqual(len(state.messages), 200)
        self.assertEqual(state.messages[-1]["msg"], "hi 229")

    def test_equipped_items_fill_the_levels(self) -> None:
        state = make_state()
        state.apply("game-started", {"players": [{"id": 1}, {"id": 2}]})
        state.apply(
            "players-and-equipped-items",
            {"players": [{"id": 2, "level": 21, "username": "bob"}]},
        )
        self.assertEqual(state.by_id(2).level, 21)
        self.assertEqual(state.by_id(2).username, "bob")

    def test_equipped_items_appends_an_unknown_player(self) -> None:
        state = make_state()
        state.apply("player-joined-and-equipped-items", {"player": {"id": 9, "level": 3}})
        self.assertEqual(state.by_id(9).level, 3)

    def test_reset_clears_everything(self) -> None:
        state = make_state()
        state.apply("game-started", {"role": {"id": "witch"}, "players": [{"id": 2}]})
        state.apply("game-players-killed", [2])
        state.reset()
        self.assertEqual(state.players, [])
        self.assertEqual(state.dead_ids, [])
        self.assertEqual(state.role_id, "")
        self.assertEqual(state.phase, "lobby")
        self.assertIsNone(state.own_id())

    def test_own_id_roundtrip(self) -> None:
        state = GameState()
        self.assertIsNone(state.own_id())
        state.set_own_id(42)
        self.assertEqual(state.own_id(), 42)

    def test_wolf_claim_ww_say_m(self) -> None:
        state = make_state()
        state.apply(
            "game-started",
            {
                "role": {"id": "villager"},
                "players": [
                    {"id": "p1", "gridIdx": 0, "username": "player1"},
                    {"id": "p2", "gridIdx": 1, "username": "player2"},
                    {"id": "p3", "gridIdx": 2, "username": "player3"},
                ],
            },
        )
        state.set_own_id("p1")
        # Player 2 claims wolf with simple "m" in WW Say M room
        state.apply("game:chat-public:msg", {"authorId": "p2", "msg": "m"})
        self.assertEqual(state.claimed_wolf_id, "p2")
        self.assertEqual(state.claimed_wolf_seat, 2)

        # Seat-based claim "3 m" updates claimed wolf to seat 3 (not own_id)
        state.apply("game:chat-public:msg", {"authorId": "p2", "msg": "3 m"})
        self.assertEqual(state.claimed_wolf_id, "p3")
        self.assertEqual(state.claimed_wolf_seat, 3)

        # "1 m" by another player must not overwrite claimed_wolf_id with non-wolf own_id
        state.apply("game:chat-public:msg", {"authorId": "p2", "msg": "1 m"})
        self.assertEqual(state.claimed_wolf_id, "p3")
        self.assertEqual(state.claimed_wolf_seat, 3)

    def test_wolf_claim_ww_and_seat_variants(self) -> None:
        state = make_state()
        state.apply(
            "game-started",
            {
                "role": {"id": "villager"},
                "players": [
                    {"id": "p1", "gridIdx": 0, "username": "player1"},
                    {"id": "p2", "gridIdx": 1, "username": "player2"},
                ],
            },
        )
        state.set_own_id("p1")
        # "2 ww" claim
        state.apply("game:chat-public:msg", {"authorId": "p2", "msg": "2 ww"})
        self.assertEqual(state.claimed_wolf_id, "p2")

        # "ww 2" claim
        state.apply("game:chat-public:msg", {"authorId": "p1", "msg": "ww 2"})
        self.assertEqual(state.claimed_wolf_id, "p2")

        # Author says "im ww"
        state.apply("game:chat-public:msg", {"authorId": "p2", "msg": "im ww"})
        self.assertEqual(state.claimed_wolf_id, "p2")

        # "2 is ww" claim (observed in live games)
        state.apply("game:chat-public:msg", {"authorId": "p1", "msg": "2 is ww"})
        self.assertEqual(state.claimed_wolf_id, "p2")

    def test_bandwagon_voting(self) -> None:
        from wvbot.game import Policy
        state = make_state()
        state.apply(
            "game-started",
            {
                "role": {"id": "villager"},
                "players": [
                    {"id": "bot", "gridIdx": 0, "username": "bot"},
                    {"id": "p2", "gridIdx": 1, "username": "player2"},
                    {"id": "p3", "gridIdx": 2, "username": "player3"},
                ],
            },
        )
        state.set_own_id("bot")
        policy = Policy()

        # Nobody voted yet -> None
        self.assertIsNone(policy.vote_target(state))

        # p2 votes for p3 -> bandwagon policy returns p3!
        state.day_votes["p2"] = "p3"
        self.assertEqual(policy.vote_target(state), "p3")

        # p3 votes for bot -> policy should NOT vote for bot itself
        state.day_votes = {"p3": "bot"}
        self.assertIsNone(policy.vote_target(state))

        # Majority vote selection
        state.day_votes = {"p1": "p2", "p4": "p3", "p5": "p3"}
        self.assertEqual(policy.vote_target(state), "p3")

    def test_wolf_claim_seer_red_and_is_ww(self) -> None:
        state = make_state()
        state.apply(
            "game-started",
            {
                "role": {"id": "villager"},
                "players": [
                    {"id": "bot", "gridIdx": 0, "username": "bot"},
                    {"id": "p2", "gridIdx": 1, "username": "player2"},
                    {"id": "p4", "gridIdx": 3, "username": "player4"},
                ],
            },
        )
        state.set_own_id("bot")

        # "So 4 is ww"
        state.apply("game:chat-public:msg", {"authorId": "p2", "msg": "So 4 is ww"})
        self.assertEqual(state.claimed_wolf_id, "p4")

        # "4 red"
        state.apply("game:chat-public:msg", {"authorId": "p2", "msg": "4 red"})
        self.assertEqual(state.claimed_wolf_id, "p4")

        # "red 2"
        state.apply("game:chat-public:msg", {"authorId": "p4", "msg": "red 2"})
        self.assertEqual(state.claimed_wolf_id, "p2")

    def test_accusation_counts_seat_numbers(self) -> None:
        from wvbot.game import Policy
        state = make_state()
        state.apply(
            "game-started",
            {
                "role": {"id": "villager"},
                "players": [
                    {"id": "bot", "gridIdx": 0, "username": "bot"},
                    {"id": "p2", "gridIdx": 1, "username": "player2"},
                    {"id": "p3", "gridIdx": 2, "username": "player3"},
                ],
            },
        )
        state.set_own_id("bot")
        policy = Policy()

        # Chat message mentioning seat: "Vote 2" and "go 2"
        state.messages.append({"authorId": "p3", "msg": "Vote 2"})
        state.messages.append({"authorId": "p3", "msg": "2 sus"})
        counts = policy._accusation_counts(state)
        self.assertEqual(counts.get("p2"), 2)

    def test_started_preserves_lobby_levels_and_clears_per_game_state(self) -> None:
        state = make_state()
        state.apply(
            "players-and-equipped-items",
            {"players": [{"id": 1, "level": 42}, {"id": 2, "level": 19}]},
        )
        state.apply(
            "game-started",
            {
                "role": {"id": "werewolf", "name": "Werewolf", "team": "WEREWOLF"},
                "players": [
                    {"id": 1, "username": "me"},
                    {"id": 2, "username": "bob"},
                ],
            },
        )
        self.assertEqual(state.by_id(1).level, 42)
        self.assertEqual(state.by_id(2).level, 19)
        state.apply("game:chat-public:msg", {"authorId": 2, "msg": "hi"})
        self.assertEqual(len(state.messages), 1)

        # Second game in same room without a role object: previous role/chat must not leak.
        state.apply(
            "game-started",
            {
                "players": [
                    {"id": 1, "username": "me"},
                    {"id": 2, "username": "bob"},
                ],
            },
        )
        self.assertEqual(state.messages, [])
        self.assertEqual(state.role_id, "")
        self.assertEqual(state.role_name, "")
        self.assertEqual(state.role_team, "")
        self.assertEqual(state.by_id(1).level, 42)

    def test_player_by_seat_respects_sparse_grid_and_negative_fallback(self) -> None:
        state = make_state()
        # Mixed grid indices: seat 2 is empty and must NOT fall back to players[1].
        state.apply(
            "game-started",
            {
                "players": [
                    {"id": 1, "gridIdx": 0},
                    {"id": 2, "gridIdx": 3},
                ]
            },
        )
        self.assertEqual(state.player_by_seat(1).id, 1)
        self.assertIsNone(state.player_by_seat(2))
        self.assertEqual(state.player_by_seat(4).id, 2)

        # All grid_idx < 0 falls back cleanly to 1-based list order.
        state.apply("game-started", {"players": [{"id": 10}, {"id": 20}]})
        self.assertEqual(state.player_by_seat(1).id, 10)
        self.assertEqual(state.player_by_seat(2).id, 20)

    def test_votes_clear_on_phase_start(self) -> None:
        state = make_state()
        state.day_votes = {1: 2}
        state.night_votes = {1: 3}
        state.apply("game-day-voting-started", {})
        self.assertEqual(state.phase, "day-voting")
        self.assertEqual(state.day_votes, {})
        self.assertEqual(state.night_votes, {1: 3})

        state.apply("game-night-started", {})
        self.assertEqual(state.phase, "night")
        self.assertEqual(state.night_votes, {})

    def test_on_killed_dict_items_and_clears_claimed_wolf(self) -> None:
        state = make_state()
        state.apply(
            "game-started",
            {
                "players": [
                    {"id": 1, "gridIdx": 0, "username": "me"},
                    {"id": 2, "gridIdx": 1, "username": "wolf"},
                ]
            },
        )
        state.set_own_id(1)
        state.apply("game:chat-public:msg", {"authorId": 2, "msg": "2 w"})
        self.assertEqual(state.claimed_wolf_id, 2)

        state.apply("game-players-killed", {"players": [{"id": 2}]})
        self.assertIn(2, state.dead_ids)
        self.assertFalse(state.by_id(2).is_alive)
        self.assertIsNone(state.claimed_wolf_id)
        self.assertIsNone(state.claimed_wolf_seat)

    def test_wolf_claim_ignores_questions_negations_and_non_wolf_own_id(self) -> None:
        state = make_state()
        state.apply(
            "game-started",
            {
                "role": {"id": "villager"},
                "players": [
                    {"id": 1, "gridIdx": 0, "username": "me"},
                    {"id": 2, "gridIdx": 1, "username": "bob"},
                    {"id": 3, "gridIdx": 2, "username": "carl"},
                ],
            },
        )
        state.set_own_id(1)
        state.apply("game:chat-public:msg", {"authorId": 2, "msg": "3 w?"})
        self.assertIsNone(state.claimed_wolf_id)

        state.apply("game:chat-public:msg", {"authorId": 2, "msg": "3 not w"})
        self.assertIsNone(state.claimed_wolf_id)

        state.apply("game:chat-public:msg", {"authorId": 2, "msg": "1 w"})
        self.assertIsNone(state.claimed_wolf_id)

        # When bot itself is wolf, saying its own seat claim does register.
        state.role_id = "werewolf"
        state.apply("game:chat-public:msg", {"authorId": 1, "msg": "1 w"})
        self.assertEqual(state.claimed_wolf_id, 1)


if __name__ == "__main__":
    unittest.main()
