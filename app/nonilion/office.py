"""Lendix inside a Nonilion room, over Nonilion's MCP server.

Front Desk is an avatar in the room. It listens for messages, hands each request to the
existing pipeline, and mirrors the office's event log back into Nonilion: the working
agent's status, spoken decisions, escalations walked and messaged to the risk head,
progress updates, replies and issued documents. Agents with their own personal token get
their own avatar; the others are shown through Front Desk's status line.
"""
import asyncio
import logging
import random
import re

from .mcp import NonilionError, NonilionMCP

log = logging.getLogger("lendix.nonilion")

CONVERSATION = "nonilion"
CHARACTERS = {"Front Desk": "elegantolderwoman", "Intake": "scientist", "Risk": "cybernaticarm",
              "Credit": "silverhairman", "Underwriter": "groomedbeard", "Compliance": "sikhgentleman",
              "Disbursement": "techpro", "Collections": "perfectbeardman", "Reporter": "greengown"}
# Desk spots around the room's centre, checked as open floor on the Lendix room map.
DESKS = {"Front Desk": (20, -36), "Intake": (-380, -260), "Risk": (-422, 163), "Credit": (0, 330),
         "Underwriter": (420, 180), "Compliance": (380, -260), "Disbursement": (-700, -20),
         "Collections": (673, -17), "Reporter": (0, -380)}
# What each agent is responsible for, shown as its status between loans.
DUTIES = {"Front Desk": "taking loan requests", "Intake": "checking application files",
          "Risk": "watching for fraud flags", "Credit": "working out affordability",
          "Underwriter": "applying credit policy v1", "Compliance": "checking every document",
          "Disbursement": "verifying payout accounts", "Collections": "tracking repayments",
          "Reporter": "filing today's documents"}
HANDOFF = re.compile(r"^(?P<source>[A-Za-z ]+?) → (?P<target>[A-Za-z ]+?):")
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
        self.queues = {}
        self.last_sender = None
        self.last_activity = 0.0

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

    def act(self, agent, steps):
        """Queue moves for one avatar; they run in order so a walk finishes before the next step."""
        if agent not in self.avatars:
            return
        queue = self.queues.get(agent)
        if queue is None:
            queue = self.queues[agent] = asyncio.Queue()
            task = asyncio.create_task(self.perform(agent, queue))
            self.tasks.append(task)
        queue.put_nowait(list(steps))

    async def perform(self, agent, queue):
        while True:
            steps = await queue.get()
            try:
                for tool, args in steps:
                    if tool == "pause":
                        await self.sleep(args)
                        continue
                    _, data = await self.say(agent, tool, args)
                    if tool == "walk_to":
                        # Let the walk play out before the next step; Nonilion reports its length.
                        await self.sleep(min(float((data or {}).get("seconds") or 1), 12) + 0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("nonilion %s move failed: %s", agent, type(exc).__name__)
            finally:
                queue.task_done()

    def desk(self, agent):
        x, y = DESKS.get(agent, DESKS["Front Desk"])
        return {"x": x, "y": y}

    def busy(self):
        return asyncio.get_running_loop().time() - self.last_activity < 30

    async def office_life(self):
        """Between loans, idle agents look busy with their own work and visit each other."""
        while True:
            await self.sleep(random.uniform(25, 50))
            try:
                if not self.connected or self.busy():
                    continue
                idle = [a for a in self.avatars if self.queues.get(a) is None or self.queues[a].empty()]
                if not idle:
                    continue
                agent = random.choice(idle)
                others = [a for a in self.avatars if a != agent]
                routine = random.random()
                if routine < 0.4 and others:
                    friend = random.choice(others)
                    self.act(agent, [("walk_to", self.desk(friend)), ("emote", {"emote": "wave", "seconds": 3}),
                                     ("pause", 4), ("walk_to", self.desk(agent))])
                elif routine < 0.7:
                    self.act(agent, [("emote", {"emote": "coffee", "seconds": 5})])
                else:
                    self.act(agent, [("emote", {"emote": "think", "seconds": 4})])
                self.status_soon(agent, f"{agent} · {DUTIES.get(agent, 'at work')}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("nonilion office life failed: %s", type(exc).__name__)

    def status_soon(self, agent, text):
        asyncio.ensure_future(self.status(agent, text))

    async def enter(self):
        _, me = await self.front.call("whoami")
        self.owner = (me or {}).get("owner")
        self.risk_head = self.risk_head or self.owner
        for agent, client in list(self.avatars.items()):
            args = {"name": f"Lendix · {agent}", "character": CHARACTERS[agent]}
            if self.room:
                args["room"] = self.room
            try:
                await client.call("enter_room", args)
            except NonilionError as exc:
                if agent == "Front Desk":
                    raise
                # A bad agent token only costs that agent its avatar; Front Desk shows its work instead.
                log.warning("nonilion avatar for %s unavailable: %s", agent, str(exc)[:80])
                del self.avatars[agent]
                await client.close()
                continue
            await self.say(agent, "set_status", {"status": f"{agent} · {DUTIES.get(agent, 'ready')}", "minutes": 240})
            self.act(agent, [("walk_to", self.desk(agent)), ("emote", {"emote": "coffee", "seconds": 3})])
        self.connected = True

    async def start(self):
        self.cursor = max((e["id"] for e in self.store.events(0, limit=1_000_000)), default=0)
        self.tasks = [asyncio.create_task(self.listen_loop()), asyncio.create_task(self.mirror_loop()),
                      asyncio.create_task(self.office_life())]

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
        self.last_sender = event.get("from") or self.last_sender
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
        if any(e["kind"] in ANIMATED for e in batch):
            self.last_activity = asyncio.get_running_loop().time()
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
            match = HANDOFF.match(text)
            source = match.group("source").strip() if match else None
            # The sender carries the file over; agents without an avatar are covered by Front Desk.
            courier = source if source in self.avatars else "Front Desk"
            if courier != agent:
                self.act(courier, [("walk_to", self.desk(agent)),
                                   ("say_out_loud", {"text": short(f"{loan} for you, {agent}.", 80)}),
                                   ("emote", {"emote": "thumbs_up", "seconds": 2}),
                                   ("walk_to", self.desk(courier))])
            if agent in self.avatars:
                self.act(agent, [("emote", {"emote": "nod", "seconds": 2})])
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
        name = await self.spot_risk_head(agent)
        if name is None:
            # Out of sight: walk to the middle of the office and look again.
            _, walked = await self.say(agent, "walk_to", self.desk("Front Desk"))
            await self.sleep(min(float((walked or {}).get("seconds") or 1), 12) + 0.5)
            name = await self.spot_risk_head(agent)
        return name

    async def spot_risk_head(self, agent):
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
        text = short(event["text"], 3500)
        sent, _ = await self.say("Front Desk", "reply", {"text": text, "speak": False})
        if sent is None:
            # The chat reply can be rejected (for example once that conversation has moved on);
            # the answer still reaches the person in room chat.
            tag = [f"@{self.last_sender}"] if self.last_sender else []
            await self.say("Front Desk", "post_room_chat", {"text": text, "mention": tag})
