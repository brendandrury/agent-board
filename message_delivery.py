"""Mailbox enrollment and delivery policy, independent of process presence.

Explicit subscribe/resume actions can enroll an agent. Messages, heartbeat,
reconnect and history reads cannot. This module never launches a model.
"""
import datetime

MAX_SUBSCRIPTIONS = 200
MAX_PENDING = 100
MAX_PENDING_RECEIPTS = 500
PENDING_DAYS = 7
LIFECYCLES = ("active", "paused", "finished")
SUBSCRIPTIONS = ("following", "muted", "left")


def lifecycle(agent):
    return agent.get("lifecycle", "active")


def subscription(agent, topic):
    return agent.get("subscriptions", {}).get(topic, {"state": "following", "after": 0, "read_through": 0})


def revise(agent):
    agent["settings_revision"] = agent.get("settings_revision", 0) + 1
    # A future wake dispatcher must re-check both enrollment and this epoch.
    # It is a cancellation generation, not evidence that wake-up is enabled.
    agent["delivery_epoch"] = agent.get("delivery_epoch", 0) + 1


def now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="microseconds")


def expired(record):
    return record["expires_at"] <= now()


def settle(state, key, status):
    record = state.setdefault("pending", {}).pop(key)
    receipts = state.setdefault("pending_receipts", {})
    receipts[key] = {"status": status, "recipient": record["message"]["to"]}
    for old in sorted(receipts, key=int)[:-MAX_PENDING_RECEIPTS]:
        del receipts[old]


def prune_pending(state):
    for key, record in list(state.get("pending", {}).items()):
        if expired(record):
            settle(state, key, "expired")


def queue(state, message):
    prune_pending(state)
    pending = state.setdefault("pending", {})
    if len(pending) >= MAX_PENDING:
        raise ValueError("Pending-recipient queue is full; cancel old requests or wait for expiry")
    pending[str(message["id"])] = {"message": message, "state": "waiting", "expires_at": (
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=PENDING_DAYS)).isoformat(timespec="microseconds")}


def accept_pending(state, agent):
    """Acceptance never changes lifecycle or subscription state."""
    prune_pending(state)
    accepted = []
    for key, record in state.get("pending", {}).items():
        if record["message"]["to"] == agent["id"] and record["state"] == "waiting":
            record["state"] = "accepted"
            accepted.append(int(key))
    agent["settings_revision"] = agent.get("settings_revision", 0) + 1
    return accepted


def acknowledge_pending(state, agent, through):
    prune_pending(state)
    for key, record in list(state.get("pending", {}).items()):
        if record["message"]["to"] == agent["id"] and record["state"] == "accepted" and int(key) <= through:
            settle(state, key, "acknowledged")


def cancel_pending(state, agent, message_id):
    key = str(message_id)
    record = state.get("pending", {}).get(key)
    if not record:
        raise LookupError("Pending message not found")
    if agent["id"] not in (record["message"]["from"], record["message"]["to"]):
        raise PermissionError("Only the sender or recipient can cancel pending delivery")
    settle(state, key, "cancelled")


def enroll(agent, value, cursor):
    if value not in LIFECYCLES:
        raise ValueError("lifecycle must be active, paused or finished")
    previous = lifecycle(agent)
    if value == previous:
        return
    agent["lifecycle"] = value
    if value == "finished" or previous == "finished":
        # Re-enrollment starts fresh; old work never silently becomes runnable.
        agent["delivery_after"] = cursor
    revise(agent)


def follow(agent, topic, value, cursor):
    if value not in SUBSCRIPTIONS:
        raise ValueError("subscription must be following, muted or left")
    entries = agent.setdefault("subscriptions", {})
    if topic not in entries and len(entries) >= MAX_SUBSCRIPTIONS:
        raise ValueError("Subscription limit reached")
    previous = subscription(agent, topic)
    after = cursor if value == "left" or previous["state"] == "left" else previous["after"]
    entries[topic] = previous | {"state": value, "after": after}
    revise(agent)


def read(agent, topic, through):
    entries = agent.setdefault("subscriptions", {})
    if topic not in entries and len(entries) >= MAX_SUBSCRIPTIONS:
        raise ValueError("Subscription limit reached")
    previous = subscription(agent, topic)
    entries[topic] = previous | {"read_through": max(through, previous.get("read_through", 0))}
    # Reading must not subscribe, unmute, re-enroll, or acknowledge delivery.
    agent["settings_revision"] = agent.get("settings_revision", 0) + 1


def policy(message, agent):
    """Eligibility for an explicit inbox read, not an acknowledgment decision."""
    if message["from"] == agent["id"]:
        return "self"
    if message["to"] not in (agent["id"], "*"):
        return "other_recipient"
    if message["to"] == "*" and message["id"] <= agent["joined_after"]:
        return "before_join"
    state = lifecycle(agent)
    if state == "finished":
        return "finished"
    setting = subscription(agent, message.get("topic", ""))
    if setting["state"] == "left":
        return "unsubscribed"
    if message["id"] <= max(agent.get("delivery_after", 0), setting["after"]):
        return "before_resume"
    if state == "paused":
        return "paused"
    return "muted" if setting["state"] == "muted" else "ready"


def incoming(state, agent, after, notifications=False, include_pending=True):
    allowed = ("ready",) if notifications else ("ready", "muted")
    messages = [m for m in state["messages"] if not m.get("pending_recipient") and m["id"] > after and policy(m, agent) in allowed]
    for record in state.get("pending", {}).values():
        m = record["message"]
        if record["state"] == "accepted" and not expired(record) and (include_pending or m["id"] > after) and policy(m, agent) in allowed:
            messages.append(m)
    return sorted(messages, key=lambda m: m["id"])


def receipt(state, message):
    if message["to"] == "human":
        return "Question posted for the human"
    if message["to"] == "*":
        recipients = [a for a in state["agents"].values() if a["id"] != message["from"] and a["joined_after"] < message["id"]]
        acknowledged = sum(a["acknowledged_through"] >= message["id"] for a in recipients)
        eligible = sum(a["acknowledged_through"] < message["id"] and policy(message, a) in ("ready", "muted") for a in recipients)
        return f"{acknowledged} peers acknowledged receipt; {eligible} currently eligible"
    agent = state["agents"].get(message["to"])
    if message.get("pending_recipient"):
        key = str(message["id"])
        record = state.get("pending", {}).get(key)
        if not record:
            return "Pending delivery: " + state.get("pending_receipts", {}).get(key, {}).get("status", "no longer queued; see history")
        if expired(record):
            return "Pending delivery expired"
        if not agent:
            return "Queued for an unregistered recipient; not delivered"
        if record["state"] == "waiting":
            return "Recipient joined; awaiting explicit acceptance"
    if not agent:
        return "Recipient is not registered"
    if not message.get("pending_recipient") and agent["acknowledged_through"] >= message["id"]:
        return "Recipient acknowledged receipt"
    return {"finished": "Finished mailbox; not delivered", "unsubscribed": "Unsubscribed; not delivered",
            "before_resume": "Before re-enrollment; history only", "paused": "Paused; delivery held",
            "muted": "Queued; notifications muted", "ready": "Queued; no acknowledgment yet"}.get(policy(message, agent), "History only")


def summary(state, agent, topics):
    """Unread topic activity is separate from inbox receipt/acknowledgment."""
    rows = []
    for row in topics:
        setting = subscription(agent, row["topic"])
        read_after = max(agent["joined_after"], agent.get("delivery_after", 0),
                         setting["after"], setting.get("read_through", 0))
        rows.append(row | {"subscription": setting["state"], "read_through": setting.get("read_through", 0),
                           "unread": row["last_id"] > read_after,
                           "predates_join": row.get("first_id", row["last_id"]) <= agent["joined_after"]})
    cursor = agent["acknowledged_through"]
    ready = incoming(state, agent, cursor, notifications=True)
    pending = [{"id": int(key), "from": r["message"]["from"], "to": r["message"]["to"],
                "topic": r["message"]["topic"], "text": r["message"]["text"],
                "state": "expired" if expired(r) else r["state"], "expires_at": r["expires_at"]}
               for key, r in state.get("pending", {}).items() if agent["id"] in (r["message"]["from"], r["message"]["to"])]
    return {"agent": agent["id"], "lifecycle": lifecycle(agent), "topics": rows,
            "settings_revision": agent.get("settings_revision", 0), "delivery_epoch": agent.get("delivery_epoch", 0),
            "inbox_in_live_feed": len(incoming(state, agent, cursor)), "notifications_in_live_feed": len(ready),
            "notifications": ready, "pending": pending, "wake_enabled": False,
            "notice": "Notifications are passive; no external session is launched or resumed. Muted topics remain readable. "
                      "Unsubscribed topics and finished agents cannot be revived by incoming messages. "
                      "History reading, unread activity and delivery acknowledgment are separate."}
