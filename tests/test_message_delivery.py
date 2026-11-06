"""Synthetic enrollment tests: reads, reconnects and messages never re-enlist."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent_board as ab
import messages as msg


class Enrollment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(ab, "STATE_DIR", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.a = {"agent": "writer", "key": "a" * 40}
        self.b = {"agent": "reader", "key": "b" * 40}
        msg.join(self.a)
        msg.join(self.b)

    def send(self, target="reader", topic="work", nonce=None):
        return msg.send(self.a | {"to": target, "topic": topic, "text": "Synthetic update",
                                 "request_id": nonce or str(msg.load()["next_id"])})

    def settings(self, action, **kw):
        return msg.settings(self.b | {"action": action, **kw})

    def test_unsubscribe_suppresses_direct_and_broadcast_and_survives_join(self):
        self.settings("subscription", topic="work", state="left")
        self.send()
        self.send("*")
        msg.join(self.b)
        msg.update(self.b | {"state": "active"})
        self.settings("read", topic="work", through=2)
        self.assertEqual(msg.inbox(self.b)["messages"], [])
        agent = msg.load()["agents"]["reader"]
        self.assertEqual(agent["subscriptions"]["work"]["state"], "left")
        self.assertEqual(agent["acknowledged_through"], 0)
        self.assertEqual(len(msg.topics(topic="work")["messages"]), 2)

    def test_follow_again_starts_with_new_messages_not_old_work(self):
        self.settings("subscription", topic="work", state="left")
        self.send()
        self.settings("subscription", topic="work", state="following")
        self.assertEqual(msg.inbox(self.b | {"after": 0})["messages"], [])
        new = self.send()
        self.assertEqual(msg.inbox(self.b)["messages"], [new])

    def test_finish_is_not_reversed_by_messages_heartbeat_or_join(self):
        self.settings("lifecycle", state="finished")
        self.send()
        msg.join(self.b)
        msg.update(self.b | {"state": "active", "task": "Still inspecting history"})
        self.assertEqual(msg.inbox(self.b)["lifecycle"], "finished")
        self.assertEqual(msg.inbox(self.b)["messages"], [])
        self.settings("lifecycle", state="active")
        self.assertEqual(msg.inbox(self.b)["messages"], [])
        new = self.send()
        self.assertEqual(msg.inbox(self.b)["messages"], [new])

    def test_pause_holds_mail_until_explicit_resume(self):
        old = self.send()
        self.settings("lifecycle", state="paused")
        self.assertEqual(msg.inbox(self.b | {"wait": 25})["messages"], [])
        self.settings("lifecycle", state="active")
        self.assertEqual(msg.inbox(self.b)["messages"], [old])

    def test_mute_allows_explicit_inbox_without_notifications(self):
        self.settings("subscription", topic="work", state="muted")
        sent = self.send()
        self.assertEqual(msg.inbox(self.b)["messages"], [sent])
        view = msg.notifications(self.b)
        self.assertEqual(view["notifications"], [])
        self.assertEqual(view["inbox_in_live_feed"], 1)
        self.assertFalse(view["wake_enabled"])

    def test_read_and_ack_are_independent(self):
        sent = self.send()
        self.assertTrue(msg.notifications(self.b)["topics"][0]["unread"])
        self.settings("read", topic="work", through=sent["id"])
        self.assertFalse(msg.notifications(self.b)["topics"][0]["unread"])
        self.assertEqual(msg.inbox(self.b)["messages"], [sent])
        msg.update(self.b | {"through": sent["id"]}, acknowledge=True)
        self.assertEqual(msg.inbox(self.b)["messages"], [])

    def test_pre_join_history_hint_does_not_make_it_unread_or_deliver_it(self):
        self.send("*")
        new = {"agent": "new", "key": "n" * 40}
        msg.join(new)
        view = msg.notifications(new)
        self.assertTrue(view["topics"][0]["predates_join"])
        self.assertFalse(view["topics"][0]["unread"])
        self.assertEqual(view["notifications"], [])

    def test_finished_agent_releases_long_poll_without_new_messages(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(msg.inbox(self.b | {"wait": 25})))
        thread.start()
        time.sleep(.05)
        self.settings("lifecycle", state="finished")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["lifecycle"], "finished")

    def test_settings_auth_and_human_revision(self):
        with self.assertRaises(PermissionError):
            msg.settings(self.b | {"key": "x" * 40, "action": "lifecycle", "state": "finished"})
        msg.settings({"agent": "reader", "revision": 0, "action": "lifecycle", "state": "paused"}, admin=True)
        with self.assertRaises(ValueError):
            msg.settings({"agent": "reader", "revision": 0, "action": "lifecycle", "state": "active"}, admin=True)
        self.assertEqual(msg.inbox(self.b)["lifecycle"], "paused")

    def test_human_settings_do_not_impersonate_a_live_checkin(self):
        with msg.transaction() as state:
            state["agents"]["reader"]["last_seen"] = "2020-01-01T00:00:00+00:00"
        body = {"agent": "reader", "revision": 0, "action": "lifecycle", "state": "finished"}
        status, _, _ = msg.route(None, "POST", "/api/messages/agent-settings", {}, body)
        self.assertEqual(status, 200)
        self.assertEqual(msg.load()["agents"]["reader"]["last_seen"], "2020-01-01T00:00:00+00:00")
        self.assertEqual(msg.route(None, "POST", "/api/messages/agent-settings", {}, body)[0], 400)
        self.assertEqual(msg.route(None, "GET", "/api/messages/notifications", {"agent": "missing"}, {})[0], 404)

    def test_managed_pairs_require_their_own_controls(self):
        with msg.transaction() as state:
            state["agents"]["reader"]["managed_pair"] = "pair-synthetic"
        with self.assertRaises(ValueError):
            self.settings("lifecycle", state="finished")

    def test_settings_are_lane_separated(self):
        msg.join(self.b, "pii-free")
        self.settings("lifecycle", state="finished")
        self.assertEqual(msg.inbox(self.b, "pii-free")["lifecycle"], "active")

    def test_invalid_input_cannot_mutate_settings(self):
        before = msg.load()
        for action, kw in (("lifecycle", {"state": "resume-and-spawn"}),
                           ("subscription", {"topic": "work", "state": "invalid"}),
                           ("read", {"topic": "work", "through": 999})):
            with self.assertRaises(ValueError):
                self.settings(action, **kw)
        self.assertEqual(msg.load(), before)

    def test_unregistered_queue_requires_explicit_sender_and_recipient_choices(self):
        body = self.a | {"to": "late", "topic": "work", "text": "Synthetic queued request", "request_id": "pending-one"}
        with self.assertRaises(LookupError):
            msg.send(body)
        sent = msg.send(body | {"queue_if_absent": True})
        self.assertEqual(msg.send(body | {"queue_if_absent": True}), sent)
        late = {"agent": "late", "key": "l" * 40}
        msg.join(late)
        self.assertEqual(msg.inbox(late)["messages"], [])
        self.assertEqual(msg.notifications(late)["pending"][0]["state"], "waiting")
        # Advancing the ordinary cursor cannot silently accept/lose this mail.
        msg.update(late | {"through": sent["id"]}, acknowledge=True)
        msg.settings(late | {"action": "accept_pending"})
        self.assertEqual(msg.inbox(late)["messages"], [sent])
        msg.update(late | {"through": sent["id"]}, acknowledge=True)
        self.assertEqual(msg.inbox(late)["messages"], [])
        self.assertIn("acknowledged", msg.delivery.receipt(msg.load(), sent))

    def test_pending_mail_cannot_reenlist_finished_or_unsubscribed_recipient(self):
        sent = msg.send(self.a | {"to": "late", "topic": "work", "text": "Synthetic queued request",
                                  "request_id": "pending-two", "queue_if_absent": True})
        late = {"agent": "late", "key": "l" * 40}
        msg.join(late)
        msg.settings(late | {"action": "lifecycle", "state": "finished"})
        msg.join(late | {"accept_pending": True})
        self.assertEqual(msg.inbox(late)["messages"], [])
        self.assertEqual(msg.inbox(late)["lifecycle"], "finished")
        msg.settings(late | {"action": "lifecycle", "state": "active"})
        self.assertEqual(msg.inbox(late)["messages"], [])
        self.assertIn("history only", msg.delivery.receipt(msg.load(), sent))

    def test_pending_expiry_cancellation_and_bound(self):
        sent = msg.send(self.a | {"to": "late", "topic": "work", "text": "Synthetic queued request",
                                  "request_id": "pending-three", "queue_if_absent": True})
        with self.assertRaises(PermissionError):
            msg.settings(self.b | {"action": "cancel_pending", "message_id": sent["id"]})
        with patch.object(msg.delivery, "MAX_PENDING", 1):
            with self.assertRaises(ValueError):
                msg.send(self.a | {"to": "another", "text": "Synthetic", "request_id": "another", "queue_if_absent": True})
        with patch.object(msg.delivery, "now", return_value="9999-01-01T00:00:00+00:00"):
            late = {"agent": "late", "key": "l" * 40}
            msg.join(late | {"accept_pending": True})
            self.assertEqual(msg.inbox(late)["messages"], [])
        self.assertIn("expired", msg.delivery.receipt(msg.load(), sent))
        sent = msg.send(self.a | {"to": "cancel-me", "text": "Synthetic", "request_id": "cancel", "queue_if_absent": True})
        msg.settings(self.a | {"action": "cancel_pending", "message_id": sent["id"]})
        self.assertIn("cancelled", msg.delivery.receipt(msg.load(), sent))

    def test_send_api_retry_explains_cancelled_pending_delivery(self):
        body = self.a | {"to": "late", "topic": "work", "text": "Synthetic pending request",
                         "request_id": "pending-status", "queue_if_absent": True}
        code, sent, _ = msg.route(None, "POST", "/api/messages/send", {}, body)
        self.assertEqual(code, 200)
        self.assertIn("unregistered", sent["delivery_notice"])
        msg.settings(self.a | {"action": "cancel_pending", "message_id": sent["id"]})
        code, retried, _ = msg.route(None, "POST", "/api/messages/send", {}, body)
        self.assertEqual((code, retried["id"]), (200, sent["id"]))
        self.assertIn("cancelled", retried["delivery_notice"])
        self.assertEqual(msg.load()["pending"], {})

    def test_accepted_pending_mail_cannot_undo_unsubscribe(self):
        sent = msg.send(self.a | {"to": "late", "topic": "work", "text": "Synthetic queued request",
                                  "request_id": "pending-left", "queue_if_absent": True})
        late = {"agent": "late", "key": "l" * 40}
        msg.join(late)
        msg.settings(late | {"action": "subscription", "topic": "work", "state": "left"})
        msg.settings(late | {"action": "accept_pending"})
        self.assertEqual(msg.inbox(late)["messages"], [])
        self.assertIn("Unsubscribed", msg.delivery.receipt(msg.load(), sent))
        msg.settings(late | {"action": "subscription", "topic": "work", "state": "following"})
        self.assertEqual(msg.inbox(late)["messages"], [])
        fresh = self.send(target="late")
        self.assertEqual(msg.inbox(late)["messages"], [fresh])


if __name__ == "__main__":
    unittest.main()
