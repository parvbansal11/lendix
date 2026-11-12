"""Lendix inside a Nonilion room, over Nonilion's MCP server.

Front Desk is an avatar in the room. It listens for messages, hands each request to the
existing pipeline, and mirrors the office's event log back into Nonilion: the working
agent's status, spoken decisions, escalations walked and messaged to the risk head,
progress updates, replies and issued documents. Agents with their own personal token get
their own avatar; the others are shown through Front Desk's status line.
"""
import asyncio
import logging
import re

from .mcp import NonilionError, NonilionMCP

log = logging.getLogger("lendix.nonilion")

CONVERSATION = "nonilion"
CHARACTERS = {"Front Desk": "elegantolderwoman", "Intake": "scientist", "Risk": "cybernaticarm",
              "Credit": "silverhairman", "Underwriter": "groomedbeard", "Compliance": "sikhgentleman",
              "Disbursement": "techpro", "Collections": "perfectbeardman", "Reporter": "greengown"}
ROLE_PREFIX = re.compile(r"^\s*(?:as\s+)?(borrower|ops|operations|risk[ _-]?head)\s*[:,-]\s*", re.I)
ANIMATED = {"handoff", "tool", "decision", "escalation", "mention", "office_action", "progress", "payout", "payment", "document"}
CHAT = {"reply", "file", "error"}


def short(text, limit=150):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_role(text, sender, owner):
    """Room owner plays the risk head; a prefix like 'borrower:' or 'ops:' picks another demo role."""
    match = ROLE_PREFIX.match(text)
    if match:
        word = match.group(1).lower().replace(" ", "_").replace("-", "_")
        role = {"operations": "ops", "riskhead": "risk_head"}.get(word, word)
        return role, text[match.end():].strip()
    return ("risk_head" if owner and sender == owner else "ops"), text.strip()


class OfficeBridge:
    def __init__(self, store, settings, run_message, *, token, agent_tokens=None, room=None,
                 risk_head=None, client_factory=NonilionMCP, sleep=asyncio.sleep):
        self.store, self.settings, self.run_message = store, settings, run_message
        self.room, self.sleep = room or None, sleep
        self.front = client_factory(token)
        self.avatars = {"Front Desk": self.front}
        for agent, value in (agent_tokens or {}).items():
            if agent in CHARACTERS and agent != "Front Desk" and value:
                self.avatars[agent] = client_factory(value)
        self.owner = None
        self.risk_head = risk_head or None
        self.connected = False
        self.cursor = 0
        self.last_status = {}
        self.tasks = []
        self.gestures = set()
        self.status_workers = {}
        self.gesture_slots = asyncio.Semaphore(4)

    async def say(self, agent, tool, args):
        """Best effort: a failed office gesture must never break a loan decision."""
        client = self.avatars.get(agent, self.front)
        try:
            return await client.call(tool, args)
        except NonilionError as exc:
            log.warning("nonilion %s failed: %s", tool, exc)
            return None, None

    def gesture(self, agent, tool, args):
        """Status, emote and speech run alongside the log; each Nonilion call takes seconds."""
        async def run():
            async with self.gesture_slots:
                await self.say(agent, tool, args)
        task = asyncio.create_task(run())
        self.gestures.add(task)
        task.add_done_callback(self.gestures.discard)

    async def enter(self):
        _, me = await self.front.call("whoami")
        self.owner = (me or {}).get("owner")
        self.risk_head = self.risk_head or self.owner
        for agent, client in self.avatars.items():
            args = {"name": f"Lendix · {agent}", "character": CHARACTERS[agent]}
            if self.room:
                args["room"] = self.room
            await client.call("enter_room", args)
            await self.say(agent, "set_status", {"status": f"{agent} · ready", "minutes": 240})
        self.connected = True

    async def start(self):
        self.cursor = max((e["id"] for e in self.store.events(0, limit=1_000_000)), default=0)
        self.tasks = [asyncio.create_task(self.listen_loop()), asyncio.create_task(self.mirror_loop())]

    async def stop(self):
        for task in [*self.tasks, *self.gestures]:
            task.cancel()
        await asyncio.gather(*self.tasks, *self.gestures, return_exceptions=True)
        # No leave_room: on a redeploy the new instance enters before this one stops, and the
        # avatar belongs to the token, so leaving here would pull the new instance out too.
        for client in self.avatars.values():
            await client.close()

    # Inbound: people talking to Front Desk.
    async def listen_loop(self):
        backoff = 5
        while True:
            try:
                if not self.connected:
                    await self.enter()
                _, data = await self.front.call("listen", {"wait_seconds": 20, "include_room_chat": False})
                backoff = 5
                for event in (data or {}).get("events", []):
                    if event.get("type") == "message" and event.get("text", "").strip():
                        await self.handle(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("nonilion listen failed: %s", type(exc).__name__ if not isinstance(exc, NonilionError) else exc)
                if isinstance(exc, NonilionError) and ("enter_room" in str(exc) or "avatar" in str(exc).lower()):
                    self.connected = False
                await self.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def handle(self, event):
        role, text = parse_role(event["text"], event.get("from"), self.owner)
        if not text:
            return
        self.gesture("Front Desk", "emote", {"emote": "nod", "seconds": 3})
        self.gesture("Front Desk", "set_status", {"status": short(f"Working on: {text}", 60), "minutes": 30})
        await self.run_message(text, role, CONVERSATION)

    # Outbound: the office's event log, shown in the room.
    async def mirror_loop(self):
        while True:
            try:
                batch = self.store.events(self.cursor, limit=200)
                if batch:
                    self.cursor = batch[-1]["id"]
                    await self.mirror(batch)
                else:
                    await self.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("nonilion mirror failed: %s", type(exc).__name__)
                await self.sleep(3)

    async def mirror(self, batch):
        # Many tool events arrive at once; only the latest per agent is worth a status change.
        last_tool = {e["agent"]: e["id"] for e in batch if e["kind"] in {"tool", "status"}}
        for event in batch:
            kind, mine = event["kind"], event.get("conversation_id") == CONVERSATION
            if kind in {"tool", "status"} and last_tool.get(event["agent"]) != event["id"]:
                continue
            if kind in ANIMATED or kind == "status":
                await self.animate(event)
            if mine and kind in CHAT:
                await self.chat(event)

    async def status(self, agent, text):
        text = short(text, 60)
        target = agent if agent in self.avatars else "Front Desk"
        if self.last_status.get(target) == text:
            return
        self.last_status[target] = text
        if target in self.status_workers:
            return  # the running worker picks up the newest text when its call returns

        async def worker():
            sent = None
            try:
                while self.last_status.get(target) != sent:
                    sent = self.last_status[target]
                    await self.say(target, "set_status", {"status": sent, "minutes": 30})
            finally:
                self.status_workers.pop(target, None)

        task = asyncio.create_task(worker())
        self.status_workers[target] = task
        self.gestures.add(task)
        task.add_done_callback(self.gestures.discard)

    async def animate(self, event):
        kind, agent, text, loan = event["kind"], event["agent"], event["text"], event.get("loan_id") or ""
        label = f"{agent} · {loan}".strip(" ·")
        if kind == "tool":
            await self.status(agent, f"{label} · {text.replace('_', ' ')}")
        elif kind == "status":
            if text.startswith("Job "):
                await self.status("Front Desk", "Front Desk · ready")
        elif kind == "handoff":
            await self.status(agent, f"{label} · picked up the file")
            if agent in self.avatars:
                self.gesture(agent, "emote", {"emote": "nod", "seconds": 2})
        elif kind == "decision":
            speaker = agent if agent in self.avatars else "Front Desk"
            self.gesture(speaker, "say_out_loud", {"text": short(f"{loan}: {text}", 160)})
            self.gesture(speaker, "emote", {"emote": "thumbs_up" if "approve" in text.lower() else "think", "seconds": 3})
        elif kind == "escalation":
            await self.walk_to_risk_head(f"{loan} needs your decision. {text}")
        elif kind == "mention":
            await self.message_risk_head(re.sub(r"^@\w+\s*", "", text))
        elif kind == "office_action":
            await self.walk_to_risk_head(None)
        elif kind == "progress":
            self.gesture("Reporter", "broadcast_update", {"status": "IN_PROGRESS", "message": short(text, 400)})
        elif kind in {"payout", "payment", "document"}:
            await self.status(agent, f"{label} · {text}")

    async def find_risk_head(self, agent):
        """The risk head's name as the room shows it now (display names change; handles don't resolve)."""
        _, seen = await self.say(agent, "look_around", {})
        people = [p.get("name") for p in (seen or {}).get("people", []) if p.get("name")]
        wanted = (self.risk_head or "").casefold()
        for name in people:
            if wanted and (wanted == name.casefold() or wanted in name.casefold()):
                return name
        for name in people:
            if "risk" in name.casefold() or (self.owner and self.owner.casefold() in name.casefold()):
                return name
        return people[0] if len(people) == 1 else None

    async def walk_to_risk_head(self, note):
        walker = "Reporter" if "Reporter" in self.avatars else "Front Desk"
        person = await self.find_risk_head(walker)
        if person:
            await self.say(walker, "walk_to", {"person": person})
            self.gesture(walker, "emote", {"emote": "raise_hand", "seconds": 4})
        if note:
            await self.message_risk_head(note, person)

    async def message_risk_head(self, text, person=None):
        person = person or await self.find_risk_head("Reporter")
        sent = None
        if person:
            sent, _ = await self.say("Reporter", "send_direct_message", {"to": person, "text": short(text, 1500)})
        if sent is None:
            # Nobody to DM right now: tag the room host in chat so the case is not lost.
            tag = [f"@{self.owner}"] if self.owner else []
            await self.say("Reporter", "post_room_chat", {"text": short(f"Risk head: {text}", 1500), "mention": tag})

    async def chat(self, event):
        kind = event["kind"]
        if kind == "file":
            url = (event.get("data") or {}).get("url", "")
            if url.startswith("https://"):
                await self.say("Reporter", "post_room_chat", {"text": f"{event['text']} ({event.get('loan_id') or 'Lendix'})", "media_urls": [url]})
            return
        await self.say("Front Desk", "reply", {"text": short(event["text"], 3500), "speak": False})
