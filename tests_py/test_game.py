from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest

import support  # noqa: F401

from tarkov_cis.game import GameProbe, Phase, classify, session_id


def local(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").astimezone().timestamp()


class ClassifyTests(unittest.TestCase):
    def test_markers(self):
        cases = {
            "2026-09-24 21:56:03.677|1.1.5.1|Info|output|application|ShowCharacterSelectionScreen": Phase.CHARACTER_SELECT,
            "2026-09-24 21:58:32.122|Info|application|TRACE-NetworkGameMatching G": Phase.MATCHMAKING,
            '2026-09-24 22:01:00.000|Info|push-notifications|Got notification | UserConfirmed {"ip":"203.0.113.9"}': Phase.RAID,
            "2026-09-24 22:00:19.109|Info|application|GameStarted:299.9 real:321.7": Phase.RAID,
            "2026-09-24 22:10:00.000|Info|push-notifications|Got notification | UserMatchOver": Phase.POST_RAID,
            "2026-09-25 00:23:52.000|Info|Successful login": Phase.LOGIN,
        }
        for line, phase in cases.items():
            with self.subTest(line=line):
                self.assertEqual(classify(line)[0], phase)

    def test_untimestamped_stack_frames_never_count(self):
        self.assertIsNone(classify("EFT.MainMenuShowOperation:Execute()"))
        self.assertIsNone(classify("  at EFT.NetworkGameCreate.GameStarted:Invoke()"))

    def test_session_id_normalizes_unpadded_hour(self):
        path = Path("C:/Logs/log_2026.09.25_0-23-51_1.0/application.log")
        self.assertEqual(session_id(path), "2026.09.25_00-23-51")


class GameProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.running: bool | None = True
        self.now = local("2026-09-24 22:30:00")

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self) -> GameProbe:
        return GameProbe(lambda: [self.root], max_age_days=100000, process_running=lambda: self.running, clock=lambda: self.now)

    def log(self, text: str, session: str = "log_2026.09.24_21-50-00_1.0", name: str = "application.log") -> Path:
        directory = self.root / session
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_match_markers_protect_until_menu(self):
        self.log(
            "2026-09-24 21:58:32.122|Info|application|TRACE-NetworkGameMatching G\n"
            "2026-09-24 22:00:19.109|Info|application|GameStarted:299.9\n"
        )
        snapshot = self.probe()()
        self.assertEqual(snapshot.phase, Phase.RAID)
        self.assertTrue(snapshot.in_match)

    def test_post_raid_settlement_is_still_in_match(self):
        self.log(
            "2026-09-24 22:00:00.000|Info|application|GameStarted:1\n"
            "2026-09-24 22:01:00.000|Info|push-notifications|Got notification | UserMatchOver\n"
        )
        self.assertEqual(self.probe()().phase, Phase.POST_RAID)
        self.assertTrue(self.probe()().in_match)

    def test_character_select_is_not_protected(self):
        self.log("2026-09-24 21:56:03.677|Info|output|ShowCharacterSelectionScreen\n")
        self.assertFalse(self.probe()().in_match)

    def test_stale_marker_is_not_protected_after_game_exits(self):
        self.log("2026-09-24 21:58:32.122|Info|application|TRACE-NetworkGameMatching\n")
        self.running = False
        self.assertFalse(self.probe()().in_match)

    def test_marker_older_than_three_hours_does_not_freeze_a_new_process(self):
        self.log("2026-09-24 18:00:00.000|Info|application|TRACE-NetworkGameMatching\n")
        snapshot = self.probe()()
        self.assertEqual(snapshot.phase, Phase.UNKNOWN)
        self.assertFalse(snapshot.in_match)

    def test_stack_frame_keeps_its_header_time(self):
        self.log(
            "2026-09-24 21:58:00.000|Info|output|FrameTicks\n"
            "EFT.MainMenuShowOperation:Execute()\n"
            "2026-09-24 22:02:00.000|Info|output|unrelated late log entry\n",
            name="output.log",
        )
        self.log("2026-09-24 22:00:00.000|Debug|application|GameStarted:1\n")
        self.assertEqual(self.probe()().phase, Phase.RAID)

    def test_tail_starting_inside_a_stack_cannot_unlock_a_raid(self):
        self.log("EFT.MainMenuShowOperation:Execute()\n2026-09-24 22:02:00.000|Info|noise\n", name="output.log")
        self.log("2026-09-24 22:00:00.000|Debug|GameStarted:1\n")
        self.assertEqual(self.probe()().phase, Phase.RAID)

    def test_latest_launch_ignores_unfinished_raid_from_previous_launch(self):
        self.log("2026-09-24 22:00:00.000|Debug|GameStarted:1\n", session="log_2026.09.24_21-00-00_1.0")
        self.log("2026-09-24 22:05:00.000|Info|startup\n", session="log_2026.09.24_22-05-00_1.0")
        snapshot = self.probe()()
        self.assertEqual(snapshot.phase, Phase.UNKNOWN)
        self.assertEqual(snapshot.session_id, "2026.09.24_22-05-00")

    def test_unpadded_midnight_hour_is_the_newest_launch(self):
        self.now = local("2026-09-25 00:30:00")
        self.log("2026-09-24 23:59:58.000|Debug|GameStarted:1\n", session="log_2026.09.24_23-59-57_1.0")
        self.log("2026-09-25 00:23:52.000|Info|Successful login\n", session="log_2026.09.25_0-23-51_1.0")
        self.assertEqual(self.probe()().phase, Phase.LOGIN)

    def test_known_match_stays_locked_when_the_log_tail_rolls(self):
        path = self.log("2026-09-24 22:00:00.000|Debug|GameStarted:1\n")
        probe = self.probe()
        self.assertTrue(probe().in_match)
        path.write_text("2026-09-24 22:05:00.000|Info|noise without markers\n", encoding="utf-8")
        snapshot = probe()
        self.assertEqual(snapshot.phase, Phase.RAID)
        self.assertTrue(snapshot.in_match)

    def test_game_exit_releases_the_latch(self):
        path = self.log("2026-09-24 22:00:00.000|Debug|GameStarted:1\n")
        probe = self.probe()
        self.assertTrue(probe().in_match)
        path.write_text("2026-09-24 22:05:00.000|Info|noise\n", encoding="utf-8")
        self.running = False
        self.assertFalse(probe().in_match)

    def test_unreadable_process_list_keeps_protection(self):
        self.log("2026-09-24 22:00:00.000|Debug|GameStarted:1\n")
        self.running = None
        self.assertTrue(self.probe()().in_match)

    def test_new_bytes_are_seen_between_checks(self):
        path = self.log("2026-09-24 22:00:00.000|Info|Successful login\n")
        probe = self.probe()
        self.assertEqual(probe().phase, Phase.LOGIN)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("2026-09-24 22:01:00.000|Debug|TRACE-NetworkGameMatching G\n")
        self.assertEqual(probe().phase, Phase.MATCHMAKING)


if __name__ == "__main__":
    unittest.main()
