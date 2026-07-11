#!/usr/bin/env python3
"""Offline unit tests for mail_digest — no network, no Gmail, no API keys.

Covers: the anchored digest window, thread grouping, the deterministic VIP
block, hallucinated-link stripping, the noise-memory tail, pruning, and
unsubscribe suggestions.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
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


def email(subject="s", vip=False, thread="t1",
          link="https://mail.google.com/mail/u/0/#all/x",
          unread=False, attach=False, categories=(), ts=0):
    return {"id": link.rsplit("/", 1)[-1], "from": "a@b.c",
            "sender_email": "a@b.c", "subject": subject,
            "snippet": "…", "link": link, "vip": vip, "threadId": thread,
            "unread": unread, "attach": attach, "files": [],
            "categories": list(categories), "ts": ts}


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

    def test_unread_and_attach_propagate_across_thread(self):
        out = md.group_by_thread([
            email(unread=False, attach=False),
            email(unread=True, attach=True),
        ])
        self.assertTrue(out[0]["unread"])
        self.assertTrue(out[0]["attach"])


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

    def test_split_state_extracts_senders_and_actions(self):
        reply = (f'digest text\n{md.STATE_MARKER}\n'
                 '{"noise_senders": ["a@b.c"], "actions": '
                 '[{"what": "reply to HR", "link": "L", "deadline": null}]}')
        text, senders, actions = md.split_state(reply)
        self.assertEqual(text, "digest text")
        self.assertEqual(senders, ["a@b.c"])
        self.assertEqual(actions[0]["what"], "reply to HR")

    def test_garbage_tail_costs_memory_not_digest(self):
        text, senders, actions = md.split_state(f"digest\n{md.STATE_MARKER}\nnot json")
        self.assertEqual((text, senders, actions), ("digest", [], []))

    def test_load_noise_prunes_old_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = md.NOISE_FILE
            md.NOISE_FILE = Path(tmp) / "noise.json"
            try:
                today = datetime.now(IST).strftime("%Y-%m-%d")
                md.NOISE_FILE.write_text(json.dumps(
                    {"new@x.c": [today], "old@x.c": ["2020-01-01"]}
                ))
                noise = md.load_noise()
            finally:
                md.NOISE_FILE = saved
        self.assertIn("new@x.c", noise)
        self.assertNotIn("old@x.c", noise)

    def test_unsubscribe_block_needs_threshold(self):
        quiet = {"a@x.c": ["d1", "d2"]}
        noisy = {"spam@x.c": [f"d{i}" for i in range(6)]}
        self.assertEqual(md.unsubscribe_block(quiet), "")
        block = md.unsubscribe_block(noisy)
        self.assertIn("spam@x.c — 6 days", block)


class NoiseLineTest(unittest.TestCase):

    def test_counts_by_gmail_category(self):
        emails = [
            email(categories=("promos",)), email(categories=("promos",)),
            email(categories=("updates",)), email(),
        ]
        line = md.noise_line(emails)
        self.assertEqual(line, "🗑 3 noise — 2 promos · 1 updates")

    def test_silent_without_categorized_mail(self):
        self.assertEqual(md.noise_line([email(), email()]), "")


class StillUnreadTest(unittest.TestCase):

    def test_block_lists_oldest_with_links(self):
        items = [email(subject="LIC renewal", ts=1_752_000_000_000)]
        block = md.still_unread_block(items, extra=2)
        self.assertIn("⏳ STILL UNREAD", block)
        self.assertIn("LIC renewal", block)
        self.assertIn("https://mail.google.com", block)
        self.assertIn("…and 2 more", block)

    def test_empty_means_no_block(self):
        self.assertEqual(md.still_unread_block([], 0), "")


class StatsTest(unittest.TestCase):

    def test_record_and_weekly_rollup(self):
        stats = {}
        day1 = [email(categories=("promos",)), email(), email()]
        stats = md.record_stats(stats, "2026-07-09", day1, stale_count=2)
        stats = md.record_stats(stats, "2026-07-10", [email()], stale_count=1)
        self.assertEqual(stats["2026-07-09"]["total"], 3)
        self.assertEqual(stats["2026-07-09"]["noise"], 1)
        week = md.weekly_block(stats)
        self.assertIn("4 emails", week)
        self.assertIn("25% noise", week)
        self.assertIn("still-unread pile: 1", week)
        self.assertIn("a@b.c (4)", week)

    def test_weekly_block_empty_without_data(self):
        self.assertEqual(md.weekly_block({}), "")


class BareEmailTest(unittest.TestCase):

    def test_extracts_and_lowers(self):
        self.assertEqual(md.bare_email("Boss Man <Boss@Co.IN>"), "boss@co.in")
        self.assertEqual(md.bare_email("no-address-here"), "no-address-here")


class LedgerTest(unittest.TestCase):

    def test_ahead_block_orders_and_caps(self):
        base = datetime.now(IST)
        deadlines = [
            {"date": (base + timedelta(days=5)).strftime("%Y-%m-%d"),
             "what": "Form-16 upload", "link": "L2"},
            {"date": (base + timedelta(days=1)).strftime("%Y-%m-%d"),
             "what": "LIC premium", "link": "L1"},
            {"date": (base + timedelta(days=40)).strftime("%Y-%m-%d"),
             "what": "beyond horizon", "link": "L3"},
        ]
        block = md.ahead_block(deadlines)
        self.assertTrue(block.startswith("📅 Ahead: "))
        self.assertLess(block.index("LIC"), block.index("Form-16"))
        self.assertNotIn("beyond horizon", block)

    def test_carried_block_ages_and_skips_todays(self):
        day_ms = 86_400_000
        now_ms = int(datetime.now(IST).timestamp() * 1000)
        actions = [
            {"link": "OLD", "what": "reply to HR", "first_ms": now_ms - 3 * day_ms},
            {"link": "NEW", "what": "fresh today", "first_ms": now_ms},
        ]
        block = md.carried_block(actions, today_links={"NEW"})
        self.assertIn("🔁 CARRIED", block)
        self.assertIn("3d — reply to HR", block)
        self.assertNotIn("fresh today", block)
        self.assertEqual(md.carried_block([], set()), "")

    def test_merge_actions_requires_real_email_and_dedupes(self):
        e = email(link="https://mail.google.com/mail/u/0/#all/real")
        by_link = {e["link"]: e}
        merged = md.merge_actions(
            [], [
                {"what": "reply", "link": e["link"]},
                {"what": "ghost", "link": "https://mail.google.com/fake"},
                {"what": "dupe", "link": e["link"]},
            ], by_link,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sender"], "a@b.c")
        self.assertEqual(merged[0]["thread"], "t1")

    def test_merge_deadlines_validates_dates(self):
        merged = md.merge_deadlines([], [
            {"what": "bill", "link": "L", "deadline": "2026-08-01"},
            {"what": "vague", "link": "L2", "deadline": "next week"},
            {"what": "none", "link": "L3", "deadline": None},
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["date"], "2026-08-01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
