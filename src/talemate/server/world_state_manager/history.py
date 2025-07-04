import pydantic
import asyncio
import structlog

from talemate.instance import get_agent
from talemate.history import (
    history_with_relative_time,
    rebuild_history,
    HistoryEntry,
    update_history_entry,
    regenerate_history_entry,
    collect_source_entries,
    add_history_entry,
    delete_history_entry,
)
from talemate.server.world_state_manager import world_state_templates
from talemate.util.time import amount_unit_to_iso8601_duration

log = structlog.get_logger("talemate.server.world_state_manager.history")


class RegenerateHistoryPayload(pydantic.BaseModel):
    generation_options: world_state_templates.GenerationOptions | None = None


class HistoryEntryPayload(pydantic.BaseModel):
    entry: HistoryEntry


class AddHistoryEntryPayload(pydantic.BaseModel):
    text: str
    amount: int
    unit: str


class HistoryMixin:
    """
    Handles history-related operations for the world state manager.
    """

    async def handle_request_scene_history(self, data):
        """
        Request the entire history for the scene.
        """

        history = history_with_relative_time(
            self.scene.archived_history, self.scene.ts, layer=0
        )

        layered_history = []

        summarizer = get_agent("summarizer")

        if summarizer.layered_history_enabled:
            for index, layer in enumerate(self.scene.layered_history):
                layered_history.append(
                    history_with_relative_time(layer, self.scene.ts, layer=index + 1)
                )

        self.websocket_handler.queue_put(
            {
                "type": "world_state_manager",
                "action": "scene_history",
                "data": {
                    "history": history,
                    "layered_history": layered_history,
                },
            }
        )

    async def handle_regenerate_history(self, data):
        """
        Regenerate the history for the scene.
        """

        payload = RegenerateHistoryPayload(**data)

        async def callback():
            self.scene.emit_status()
            await self.handle_request_scene_history(data)

        task = asyncio.create_task(
            rebuild_history(
                self.scene,
                callback=callback,
                generation_options=payload.generation_options,
            )
        )

        async def done():
            self.websocket_handler.queue_put(
                {
                    "type": "world_state_manager",
                    "action": "history_regenerated",
                    "data": payload.model_dump(),
                }
            )

            await self.signal_operation_done()
            await self.handle_request_scene_history(data)

        # when task is done,  queue a message to the client
        task.add_done_callback(lambda _: asyncio.create_task(done()))

    async def handle_update_history_entry(self, data):
        payload = HistoryEntryPayload(**data)

        entry = await update_history_entry(self.scene, payload.entry)

        self.websocket_handler.queue_put(
            {
                "type": "world_state_manager",
                "action": "history_entry_updated",
                "data": entry.model_dump(),
            }
        )

        await self.signal_operation_done()

    async def handle_regenerate_history_entry(self, data):
        """
        Regenerate a single history entry.
        """

        payload = HistoryEntryPayload(**data)

        log.debug("regenerate_history_entry", payload=payload)

        try:
            entry = await regenerate_history_entry(self.scene, payload.entry)
        except Exception as e:
            log.error("regenerate_history_entry", error=e)
            await self.signal_operation_failed(str(e))
            return

        log.debug("regenerate_history_entry (done)", entry=entry)

        self.websocket_handler.queue_put(
            {
                "type": "world_state_manager",
                "action": "history_entry_regenerated",
                "data": entry.model_dump(),
            }
        )

        await self.signal_operation_done()

    async def handle_inspect_history_entry(self, data):
        """
        Inspect a single history entry.
        """

        payload = HistoryEntryPayload(**data)

        entries = collect_source_entries(self.scene, payload.entry)

        self.websocket_handler.queue_put(
            {
                "type": "world_state_manager",
                "action": "history_entry_source_entries",
                "data": {
                    "entries": [entry.model_dump() for entry in entries],
                    "entry": payload.entry.model_dump(),
                },
            }
        )

    async def handle_add_history_entry(self, data):
        """
        Add a new manual history entry to the base (archived) layer.
        """

        payload = AddHistoryEntryPayload(**data)

        try:
            iso_offset = amount_unit_to_iso8601_duration(
                int(payload.amount), payload.unit
            )
        except ValueError as e:
            await self.signal_operation_failed(str(e))
            return

        try:
            await add_history_entry(self.scene, payload.text, iso_offset)
        except Exception as e:
            log.error("add_history_entry", error=e)
            await self.signal_operation_failed(str(e))
            return

        # Send updated history to the client via existing handler
        await self.handle_request_scene_history({})

        await self.signal_operation_done()

    async def handle_delete_history_entry(self, data):
        """
        Delete a manual base-layer history entry (no start/end indices).
        """
        payload = HistoryEntryPayload(**data)

        try:
            await delete_history_entry(self.scene, payload.entry)
        except Exception as e:
            log.error("delete_history_entry", error=e)
            await self.signal_operation_failed(str(e))
            return

        # Send updated history to client
        await self.handle_request_scene_history({})

        await self.signal_operation_done()
