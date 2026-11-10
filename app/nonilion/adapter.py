"""Transport boundary; no external endpoints or authentication are assumed."""
from abc import ABC, abstractmethod
import os


class Adapter(ABC):
    @abstractmethod
    async def on_message(self, event): ...
    @abstractmethod
    async def send_reply(self, conversation_id, markdown): ...
    @abstractmethod
    async def send_file(self, conversation_id, url, filename): ...
    @abstractmethod
    async def post_update(self, text): ...
    @abstractmethod
    async def mention(self, user, text): ...
    @abstractmethod
    async def office_action(self, name, **kwargs): ...


class LocalAdapter(Adapter):
    def __init__(self, store):
        self.store = store
        self.orchestrator = None

    @property
    def conversation_id(self):
        return self.orchestrator.conversation if self.orchestrator else "office"

    async def on_message(self, event):
        if not self.orchestrator:
            raise RuntimeError("The local office is not initialized.")
        return await self.orchestrator.run(event["message"], event["role"], event["conversation_id"])

    async def send_reply(self, conversation_id, markdown):
        self.store.emit("reply", "Saakh", markdown, conversation_id=conversation_id)

    async def send_file(self, conversation_id, url, filename):
        self.store.emit("file", "Reporter", filename, conversation_id=conversation_id,
                        data={"url": url, "filename": filename})

    async def post_update(self, text):
        self.store.emit("progress", "Reporter", text, conversation_id=self.conversation_id)

    async def mention(self, user, text):
        self.store.emit("mention", "Reporter", f"@{user} {text}", conversation_id=self.conversation_id)

    async def office_action(self, name, **kwargs):
        self.store.emit("office_action", "Reporter", f"Local office action: {name}",
                        conversation_id=self.conversation_id, data=kwargs)


class NonilionAdapter(Adapter):
    # TODO from the Nonilion Bot API guide: webhook payload; verification of its
    # origin; reply, file, mention and office-action endpoints; pairing flow.
    # Only use NONILION_PAIRING_CODE and NONILION_BOT_TOKEN when implementing it.
    async def on_message(self, event):
        raise NotImplementedError("fill from the Nonilion Bot API guide")

    async def send_reply(self, conversation_id, markdown):
        raise NotImplementedError("fill from the Nonilion Bot API guide")

    async def send_file(self, conversation_id, url, filename):
        raise NotImplementedError("fill from the Nonilion Bot API guide")

    async def post_update(self, text):
        raise NotImplementedError("fill from the Nonilion Bot API guide")

    async def mention(self, user, text):
        raise NotImplementedError("fill from the Nonilion Bot API guide")

    async def office_action(self, name, **kwargs):
        raise NotImplementedError("fill from the Nonilion Bot API guide")


def verify_nonilion_request(request) -> bool:
    # TODO: implement and test request verification from the official guide.
    # This explicit developer bypass does not make the adapter functional.
    return os.getenv("NONILION_DEV_ALLOW_UNVERIFIED", "0") == "1"
