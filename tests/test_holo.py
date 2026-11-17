"""Tests for HoloDriver: Lump's routing on HoloPi, with a small index and a fake HoloPi.

    python3 -m unittest discover -s tests -v
"""
import importlib.util, os, random, unittest

_spec = importlib.util.spec_from_file_location(
    "holo_driver", os.path.join(os.path.dirname(__file__), "..", "tools", "holo_driver.py"))
holo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(holo)


def entry(name, kind, home, to=None, ticks=48):
    return {"file": name.replace(">", "-to-") + "_qs8x6a0.75.mp4", "kind": kind, "from": home, "to": to or home,
            "ticks": ticks}


INDEX = {
    **{m: entry(m, 0, m) for m in ("idle", "working", "alert", "waiting", "longhaul", "night")},
    **{f"{a}>{b}": entry(f"{a}>{b}", 1, a, b, 24) for a, b in [
        ("idle", "working"), ("working", "idle"), ("idle", "alert"), ("alert", "idle"), ("working", "alert"),
        ("alert", "waiting"), ("waiting", "alert"), ("working", "longhaul"), ("longhaul", "working"), ("idle", "night")]},
    "oops": entry("oops", 2, "working", ticks=72),
    "coffee": entry("coffee", 2, "idle", ticks=136),
    "tap_plane_idle": entry("tap_plane_idle", 2, "idle"),
    "tap_plane_working": entry("tap_plane_working", 2, "working"),
    "tap_shock_idle": entry("tap_shock_idle", 2, "idle"),
    "tap_startle_night": entry("tap_startle_night", 2, "night"),
}


def name(body):
    """A request, as the names of what plays: [once, then] or [loop]."""
    return [body["media_name"].replace("_qs8x6a0.75.mp4", "")] + (
        [body["then"]["media_name"].replace("_qs8x6a0.75.mp4", "")] if "then" in body else [])


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.driver = holo.HoloDriver(INDEX, self.sent.append, rng=random.Random(1))
        self.driver.set_mood("idle", 0)

    def played(self):
        played, self.sent[:] = [name(b) for b in self.sent], []
        return played

    def test_the_first_mood_shows_its_loop_at_once(self):
        self.assertEqual(self.played(), [["idle"]])

    def test_a_new_mood_plays_its_transition_once_then_its_loop(self):
        self.played()
        self.driver.set_mood("working", 10)
        self.assertEqual(self.played(), [["idle-to-working", "working"]])

    def test_what_plays_finishes_before_the_next_step(self):
        self.played()
        self.driver.set_mood("working", 10)        # idle>working: 24 ticks, 1 second
        self.driver.set_mood("idle", 10.5)
        self.assertEqual(self.played(), [["idle-to-working", "working"]])
        self.driver.tick(11.1)
        self.assertEqual(self.played(), [["working-to-idle", "idle"]])

    def test_an_event_plays_in_its_home_mood_then_goes_back(self):
        self.played()
        self.assertEqual(self.driver.event("coffee", 5), "coffee")
        self.assertEqual(self.played(), [["coffee", "idle"]])

    def test_an_event_away_from_home_goes_there_first(self):
        self.played()
        self.driver.event("oops", 5)                # home: working
        self.assertEqual(self.played(), [["idle-to-working", "working"]])
        self.driver.tick(6.1)
        self.assertEqual(self.played(), [["oops", "working"]])
        self.driver.tick(9.2)                       # oops: 3 s; then back to the bridge's mood
        self.assertEqual(self.played(), [["working-to-idle", "idle"]])

    def test_needs_you_escalates_to_waiting_after_45_seconds(self):
        self.played()
        self.driver.set_mood("alert", 100)
        self.assertEqual(self.played(), [["idle-to-alert", "alert"]])
        self.driver.tick(144)
        self.assertEqual(self.played(), [])
        self.driver.tick(145)
        self.assertEqual(self.played(), [["alert-to-waiting", "waiting"]])

    def test_working_slides_into_longhaul_after_ten_minutes(self):
        self.driver.set_mood("working", 0)
        self.played()
        self.driver.tick(1.1)
        self.driver.tick(600)
        self.assertEqual(self.played(), [["working-to-longhaul", "longhaul"]])

    def test_a_tap_picks_a_gag_of_his_mood(self):
        self.played()
        sequence = self.driver.event("tap", 5)
        self.assertIn(sequence, ("tap_plane_idle", "tap_shock_idle"))
        self.assertEqual(self.played(), [[sequence, "idle"]])

    def test_a_tap_at_night_startles_him(self):
        self.driver.set_mood("night", 0)
        self.driver.tick(1.1)
        self.assertEqual(self.driver.event("tap", 5), "tap_startle_night")

    def test_a_named_gag_takes_his_moods_version(self):
        self.driver.set_mood("working", 0)
        self.driver.tick(1.1)
        self.assertEqual(self.driver.event("tap_plane", 5), "tap_plane_working")

    def test_an_unknown_event_plays_nothing(self):
        self.played()
        self.assertIsNone(self.driver.event("juggle", 5))
        self.assertEqual(self.played(), [])

    def test_with_no_drawn_way_it_goes_straight_to_the_loop(self):
        self.driver.set_mood("night", 0)
        self.driver.tick(1.1)
        self.played()
        self.driver.set_mood("working", 5)          # no night>working
        self.assertEqual(self.played(), [["working"]])


if __name__ == "__main__":
    unittest.main()
