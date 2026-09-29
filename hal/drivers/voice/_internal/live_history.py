"""Join live input/output by provider turn ID and notify OS without blocking audio."""

from collections import OrderedDict
import logging
import queue
import threading

from hal import config
from hal.telemetry import voice_metrics

logger = logging.getLogger("hal.voice")


class LiveHistory:
    def __init__(self, sender, harness_voice=None, clean_reply=lambda text: text):
        self._sender = sender
        self._clean_reply = clean_reply
        self._harness_voice = harness_voice
        self._turns = OrderedDict()
        self._closed = OrderedDict()
        self._queue = queue.Queue(maxsize=64)
        self._worker = None
        self._closing = threading.Event()

    def _turn(self, key):
        if not key or key in self._closed:
            return None
        if key not in self._turns:
            if len(self._turns) >= 64:
                old, _ = self._turns.popitem(last=False)
                self.discard(old)
                logger.warning("[live] history dropped incomplete turn at capacity")
            self._turns[key] = {"input": "", "output": "", "interaction": ""}
        return self._turns[key]

    def input(self, key, text, interaction, voice_turn_type=""):
        turn = self._turn(key)
        if turn is not None:
            turn["input"] += text
            turn["interaction"] = interaction or turn["interaction"]
            if voice_turn_type:
                turn["voice_turn_type"] = voice_turn_type
            # Match OS's input limit; never send an incorrectly truncated question.
            if len(turn["input"].encode("utf-8")) > 16 * 1024:
                self.discard(key)
                logger.warning("[live] history input exceeds OS size limit")

    def output(self, key, text):
        turn = self._turn(key)
        if turn is not None:
            limit = config.REALTIME_REPLY_SYNC_MAX_CHARS
            turn["output"] = (turn["output"] + text)[:limit + 1]

    def reset_output(self, key):
        turn = self._turns.get(key)
        if turn is not None:
            turn["output"] = ""

    def discard(self, key):
        if not key:
            return
        self._turns.pop(key, None)
        self._closed[key] = True
        if len(self._closed) > 128:
            self._closed.popitem(last=False)

    def complete(self, key, completed):
        if not completed:
            return
        turn = self._turns.get(key)
        if turn is None:
            return
        self.discard(key)
        turn["output"] = self._clean_reply(turn["output"])
        if not turn["input"].strip() or not turn["output"].strip():
            return
        turn["interaction"] = turn["interaction"] or "live-" + key
        try:
            self._queue.put_nowait(turn)
        except queue.Full:
            logger.error("[live] history notification queue full")
            return
        if self._worker is None:
            self._worker = threading.Thread(target=self._send, name="live-history", daemon=True)
            self._worker.start()

    def _send(self):
        while True:
            try:
                turn = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._closing.is_set():
                    return
                continue
            try:
                reply = turn["output"]
                limit = config.REALTIME_REPLY_SYNC_MAX_CHARS
                if len(reply) > limit:
                    reply = reply[:limit] + " …[truncated]"
                kwargs = {"harness_voice": self._harness_voice} if self._harness_voice is not None else {}
                if turn.get("voice_turn_type"):
                    kwargs["voice_turn_type"] = turn["voice_turn_type"]
                result = self._sender.send(
                    f"[skills: input-branching]\n[HANDLED] {turn['input']}\n[REPLY] {reply}",
                    event_type="voice_agent_handled", skip_echo=True,
                    interaction_id=turn["interaction"], **kwargs,
                )
                if result:
                    voice_metrics.bind_run(turn["interaction"], getattr(result, "run_id", ""))
                    voice_metrics.boundary(
                        voice_metrics.BOUNDARY_AUTO_SUPERSEDE, turn["interaction"],
                        policy_applied=getattr(result, "speech_suppressed", False),
                    )
                else:
                    logger.warning("[live] history notification was not accepted by OS")
            except Exception:
                logger.exception("[live] history notification failed")
            finally:
                self._queue.task_done()

    def close(self):
        self._turns.clear()
        # Do not wait on HTTP from the playback thread. Drain completed turns
        # before stopping, even after live hangup.
        self._closing.set()
