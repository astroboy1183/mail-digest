#!/usr/bin/env python3
"""Offline unit tests for mail_digest — no network, no Gmail, no API keys.

Covers: the anchored digest window, thread grouping, the deterministic VIP
block, hallucinated-link stripping, the noise-memory tail, pruning, and
unsubscribe suggestions.
"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import mail_digest as md

IST = ZoneInfo("Asia/Kolkata")


def make_fixed_datetime(fixed):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed

    return Fixed


def email(subject="s", vip=False, thread="t1", link="https://mail.google.com/mail/u/0/#all/x"):
    return {"from": "a@b.c", "subject": subject, "snippet": "…",
            "link": link, "vip": vip, "threadId": thread}


class DigestWindowTest(unittest.TestCase):

    def _window(self, hour):
        saved = md.datetime
        md.datetime = make_fixed_datetime(datetime(2026, 7, 9, hour, 30, tzinfo=IST))
        try:
            return md.digest_window()
        finally:
            md.datetime = saved

    def test_after_six_anchors_to_this_morning(self):
        start, end = self._window(7)
        self.assertEqual((end.day, end.hour), (9, 6))
        self.assertEqual((end - start).days, 1)

    def test_before_six_anchors_to_yesterday(self):
        start, end = self._window(5)
        self.assertEqual((end.day, end.hour), (8, 6))


class ThreadGroupingTest(unittest.TestCase):

    def test_same_thread_collapses_keeping_newest(self):
        out = md.group_by_thread([email(subject="newest"), email(subject="older")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["subject"], "newest")
        self.assertEqual(out[0]["thread_count"], 2)

    def test_vip_anywhere_in_thread_marks_the_thread(self):
        out = md.group_by_thread([email(vip=False), email(vip=True)])
        self.assertTrue(out[0]["vip"])

    def test_missing_thread_id_never_merges(self):
        out = md.group_by_thread([
            email(thread=None, link="https://mail.google.com/mail/u/0/#all/1"),
            email(thread=None, link="https://mail.google.com/mail/u/0/#all/2"),
        ])
        self.assertEqual(len(out), 2)


class VipBlockAndLinksTest(unittest.TestCase):

    def test_vip_block_empty_without_vips(self):
        self.assertEqual(md.vip_block([email()]), "")

    def test_vip_block_lists_vips(self):
        block = md.vip_block([email(vip=True, subject="URGENT")])
        self.assertIn("🔔 VIP", block)
        self.assertIn("URGENT", block)

    def test_invented_links_are_stripped(self):
        real = "https://mail.google.com/mail/u/0/#all/abc"
        fake = "https://mail.google.com/mail/u/0/#all/FAKE"
        text = f"see {real} and {fake}."
        out = md.validate_links(text, [real])
        self.assertIn(real, out)
        self.assertNotIn(fake, out)
        self.assertIn("[invalid link removed].", out)


class NoiseMemoryTest(unittest.TestCase):

    def test_split_state_extracts_senders(self):
        reply = f'digest text\n{md.STATE_MARKER}\n{{"noise_senders": ["a@b.c"]}}'
        text, senders = md.split_state(reply)
        self.assertEqual((text, senders), ("digest text", ["a@b.c"]))

    def test_garbage_tail_costs_memory_not_digest(self):
        text, senders = md.split_state(f"digest\n{md.STATE_MARKER}\nnot json")
        self.assertEqual((text, senders), ("digest", []))

    def test_load_noise_prunes_old_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = md.STATE_FILE
            md.STATE_FILE = Path(tmp) / "noise.json"
            try:
                today = datetime.now(IST).strftime("%Y-%m-%d")
                md.STATE_FILE.write_text(json.dumps(
                    {"new@x.c": [today], "old@x.c": ["2020-01-01"]}
                ))
                noise = md.load_noise()
            finally:
                md.STATE_FILE = saved
        self.assertIn("new@x.c", noise)
        self.assertNotIn("old@x.c", noise)

    def test_unsubscribe_block_needs_threshold(self):
        quiet = {"a@x.c": ["d1", "d2"]}
        noisy = {"spam@x.c": [f"d{i}" for i in range(6)]}
        self.assertEqual(md.unsubscribe_block(quiet), "")
        block = md.unsubscribe_block(noisy)
        self.assertIn("spam@x.c — 6 days", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
