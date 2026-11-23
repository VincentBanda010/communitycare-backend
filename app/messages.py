import hmac
import json
from uuid import UUID
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, field_validator


class MessageForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 2000 or any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("Write a message of 1 to 2000 characters")
        return value


class TeamMessages:
    def __init__(self, storage):
        self.storage = storage

    def decode(self, row):
        context = f"message/{row['org_unit']}/{row['message_id']}/{row['sender_id']}"
        payload = json.loads(self.storage.crypto.decrypt(context, row["payload"]))
        return {"id": str(row["message_id"]), "senderId": row["sender_id"],
                "senderName": payload["name"], "text": payload["text"],
                "sentAt": row["sent_at"].isoformat()}

    def list(self, org):
        with self.storage.pool.connection() as connection:
            rows = connection.execute("SELECT * FROM team_message WHERE org_unit=%s ORDER BY sent_at DESC, message_id DESC LIMIT 100", (org,)).fetchall()
        return {"orgUnit": org, "messages": [self.decode(row) for row in reversed(rows)]}

    def send(self, org, actor, identity: UUID, form):
        sender = actor["sub"]
        name = actor.get("name") or actor.get("preferred_username") or "Team member"
        name = str(name)[:160]
        context = f"message/{org}/{identity}/{sender}"
        digest = self.storage.crypto.mac(context + "|" + form.text)
        payload = json.dumps({"name": name, "text": form.text}, ensure_ascii=False).encode()
        with self.storage.pool.connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"messages/{org}/{sender}",))
            previous = connection.execute("SELECT * FROM team_message WHERE org_unit=%s AND message_id=%s", (org, identity)).fetchone()
            if previous:
                if previous["sender_id"] != sender or not hmac.compare_digest(previous["payload_digest"], digest):
                    raise HTTPException(409, "Message reference already used")
                return self.decode(previous)
            count = connection.execute("SELECT count(*) AS n FROM team_message WHERE org_unit=%s AND sender_id=%s AND sent_at > now()-interval '1 minute'", (org, sender)).fetchone()["n"]
            if count >= 30:
                raise HTTPException(429, "Please wait before sending more messages")
            row = connection.execute("""INSERT INTO team_message(org_unit,message_id,sender_id,payload,payload_digest)
                VALUES(%s,%s,%s,%s,%s) RETURNING *""",
                (org, identity, sender, self.storage.crypto.encrypt(context,payload),digest)).fetchone()
            return self.decode(row)
