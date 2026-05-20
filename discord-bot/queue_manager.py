"""
queue_manager.py
----------------
Manages an async FIFO queue of Discord commands.

Design goals
------------
- Commands are sent one-by-one with a configurable delay between them.
- Only one autores session can be active per channel at a time
  (tracked via a set of active channel IDs).
- Failures on individual commands are logged but do NOT stop the queue.
- The queue worker runs as a background asyncio task so the bot remains
  fully responsive while commands are being dispatched.
"""

import asyncio
import logging

logger = logging.getLogger("queue_manager")

# Delay between consecutive commands (seconds).
COMMAND_DELAY = 2.0


class ChannelQueue:
    """
    Encapsulates the command queue and worker task for a single channel.

    Lifecycle
    ---------
    1. Instantiate with the target discord.TextChannel.
    2. Call enqueue(commands) to add a batch of commands.
    3. The internal worker sends them one-by-one and then stops.
    4. is_running() returns True while the worker is active.
    """

    def __init__(self, channel):
        self.channel = channel
        # asyncio.Queue holds the individual command strings.
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def is_running(self) -> bool:
        """Return True if the worker task is still processing commands."""
        return self._task is not None and not self._task.done()

    def enqueue(self, commands: list[str]) -> None:
        """Add a list of command strings to the queue and start the worker."""
        for cmd in commands:
            self._queue.put_nowait(cmd)

        # Start the worker task only if it isn't already running.
        if not self.is_running():
            self._task = asyncio.create_task(self._worker())

    async def _worker(self) -> None:
        """
        Background task: drain the queue by sending each command to Discord.
        A fixed delay is applied between sends to avoid rate-limiting and to
        give the reserve bot time to process each command.
        """
        logger.info(
            "Queue worker started for channel #%s (%d commands)",
            self.channel.name,
            self._queue.qsize(),
        )

        while not self._queue.empty():
            cmd = await self._queue.get()
            try:
                await self.channel.send(cmd)
                logger.info("Sent: %s", cmd)
            except Exception as exc:
                # Log the error but continue processing remaining commands.
                logger.warning("Failed to send '%s': %s", cmd, exc)
            finally:
                self._queue.task_done()

            # Wait between commands even after a failure so we don't flood.
            if not self._queue.empty():
                await asyncio.sleep(COMMAND_DELAY)

        logger.info("Queue worker finished for channel #%s", self.channel.name)


class QueueManager:
    """
    Manages per-channel ChannelQueue instances.

    Usage
    -----
    manager = QueueManager()
    manager.start_session(channel, commands)   # raises if channel is busy
    manager.is_busy(channel)                   # True while running
    """

    def __init__(self):
        # Maps channel_id -> ChannelQueue
        self._queues: dict[int, ChannelQueue] = {}

    def is_busy(self, channel) -> bool:
        """Return True if a session is currently active in this channel."""
        cq = self._queues.get(channel.id)
        return cq is not None and cq.is_running()

    def start_session(self, channel, commands: list[str]) -> None:
        """
        Start processing commands in the given channel.

        Raises
        ------
        RuntimeError
            If a session is already active in this channel (anti-spam guard).
        """
        if self.is_busy(channel):
            raise RuntimeError(
                f"An autores session is already running in #{channel.name}. "
                "Please wait for it to finish."
            )

        cq = ChannelQueue(channel)
        self._queues[channel.id] = cq
        cq.enqueue(commands)
        logger.info(
            "Session started in #%s with %d commands",
            channel.name,
            len(commands),
        )
