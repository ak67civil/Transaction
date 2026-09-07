import os
from datetime import datetime, time
from pymongo import MongoClient

MONGODB_URI = os.environ.get("MONGODB_URI")

_client = None


def get_db():
    global _client
    if _client is None:
        _client = MongoClient(MONGODB_URI)
    return _client["course_bot"]


def init_db():
    db = get_db()
    db.purchases.create_index("user_id")
    db.channel_members.create_index([("user_id", 1), ("channel_id", 1)])
    db.channel_members.create_index("channel_id")
    db.channels.create_index("channel_id", unique=True)


def _date_to_dt(d):
    """Mongo has no date-only type, store as datetime at midnight."""
    return datetime.combine(d, time.min)


def upsert_user(user_id):
    db = get_db()
    db.users.update_one(
        {"_id": user_id},
        {"$setOnInsert": {"first_seen": datetime.utcnow()}},
        upsert=True
    )


def add_purchase(user_id, name, username, course_name, amount, purchase_date, screenshot_file_id):
    upsert_user(user_id)
    db = get_db()
    db.purchases.insert_one({
        "user_id": user_id,
        "name_at_purchase": name,
        "username_at_purchase": username,
        "course_name": course_name,
        "amount": amount,
        "purchase_date": _date_to_dt(purchase_date),
        "screenshot_file_id": screenshot_file_id,
        "added_at": datetime.utcnow(),
    })


def get_purchases(user_id):
    db = get_db()
    docs = list(db.purchases.find({"user_id": user_id}).sort([("purchase_date", 1), ("added_at", 1)]))
    for d in docs:
        if isinstance(d.get("purchase_date"), datetime):
            d["purchase_date"] = d["purchase_date"].date()
    return docs


def register_channel(channel_id, title, invite_link):
    db = get_db()
    update = {"title": title}
    if invite_link:
        update["invite_link"] = invite_link
    db.channels.update_one(
        {"channel_id": channel_id},
        {"$set": update, "$setOnInsert": {"channel_id": channel_id}},
        upsert=True
    )


def get_all_channels():
    db = get_db()
    return list(db.channels.find().sort("title", 1))


def log_join(user_id, channel_id):
    upsert_user(user_id)
    db = get_db()
    db.channel_members.update_many(
        {"user_id": user_id, "channel_id": channel_id, "status": "active"},
        {"$set": {"status": "left", "left_at": datetime.utcnow()}}
    )
    db.channel_members.insert_one({
        "user_id": user_id,
        "channel_id": channel_id,
        "joined_at": datetime.utcnow(),
        "left_at": None,
        "status": "active",
    })


def log_leave(user_id, channel_id):
    db = get_db()
    db.channel_members.update_many(
        {"user_id": user_id, "channel_id": channel_id, "status": "active"},
        {"$set": {"status": "left", "left_at": datetime.utcnow()}}
    )


def get_user_channels(user_id):
    db = get_db()
    memberships = list(db.channel_members.find({"user_id": user_id, "status": "active"}).sort("joined_at", 1))
    result = []
    for m in memberships:
        channel = db.channels.find_one({"channel_id": m["channel_id"]})
        result.append({
            "channel_id": m["channel_id"],
            "joined_at": m["joined_at"],
            "title": channel["title"] if channel else str(m["channel_id"]),
            "invite_link": channel.get("invite_link") if channel else None,
        })
    return result


def get_channel_members(channel_id):
    db = get_db()
    docs = list(db.channel_members.find(
        {"channel_id": channel_id, "status": "active"}
    ).sort("joined_at", 1))
    return [{"user_id": d["user_id"], "joined_at": d["joined_at"]} for d in docs]
