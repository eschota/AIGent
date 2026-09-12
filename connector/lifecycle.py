"""Restarting the server on its own new code, requested from inside a turn, without cutting it.

The agent edits the connector, runs the tests and calls ``restart_server``. Nothing happens
until that turn is over: the request is a flag in the store, and the moment the last running
turn finishes the process queues a verification message for the requesting session and exits
with a code the supervisor reads as "restart me". The supervisor respawns the server on the
current revision, keeps a snapshot of the last one that served traffic and rolls back a child
that dies at once, so a broken self-edit costs a minute, not the IDE. The queued message starts
the agent's next turn on boot: it checks the new revision and closes the goal, or reports what
broke — an update the owner never has to babysit.

Without the supervisor (no ``AIGENT_SUPERVISED`` in the environment) an exit would simply stop
the server, so the tool refuses and says how to run it instead.
"""

import json
import os
import time

from .agent import tool
from .selfheal import RESTART_FLAG

RESTART_EXIT_CODE = 3  # any non-zero code makes the supervisor respawn; 3 names the reason in its log
VERIFY_TEXT = ("Сервер перезапущен на новой ревизии по твоему запросу. Проверь инструментами, что всё "
               "работает: server_status (версия, ревизия, не было ли отката), нужные тесты. Если всё "
               "в порядке — set_goal со status=done; если что-то сломалось — почини или доложи точную ошибку.")
SUPERVISOR_FIELDS = ("state", "revision", "restarts", "failures", "good_revision", "restored", "exit_code", "updated")


class Lifecycle:
    """Extension: the `restart_server` tool and the after-turn check that performs the restart."""

    def __init__(self, agent, store, config, exit=os._exit):
        self.agent, self.store, self.config, self.exit = agent, store, config, exit
        self.started = time.time()

    @staticmethod
    def supervised():
        return bool(os.environ.get("AIGENT_SUPERVISED"))

    # ------------------------------------------------------------------ extension protocol
    def tools(self, session):
        return [tool("server_status",
                     "The running AIGent server: version, uptime, whether a supervisor runs it, and what the "
                     "supervisor recorded (state, revision, restarts, a rollback). Use it to verify a restart.",
                     {}, []),
                tool("restart_server",
                     "Restart the AIGent server on its current code AFTER this turn ends, so an edit to the "
                     "connector takes effect. Call it only after the tests passed. The turn is never cut: "
                     "finish it with a short report; after the restart you get a message asking you to verify "
                     "the new revision (server_status) and close the goal. The supervisor rolls a dead revision back.",
                     {"reason": {"type": "string"}}, ["reason"])]

    def status(self):
        """Safe facts about the process; nothing from the configuration, no secrets."""
        from .version import __version__
        supervisor = None
        path = self.config.root / "supervisor.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                supervisor = {k: data.get(k) for k in SUPERVISOR_FIELDS if k in data}
            except (ValueError, OSError):
                supervisor = {"error": "supervisor.json is unreadable"}
        return {"version": __version__, "uptime_seconds": round(time.time() - self.started),
                "supervised": self.supervised(), "supervisor": supervisor,
                "restart_pending": bool(self.pending())}

    async def execute(self, session, name, args):
        if name == "server_status":
            return self.status()
        if name != "restart_server":
            raise ValueError("Unknown lifecycle tool")
        sid = session["id"]
        if not self.supervised():
            return {"error": "The server is not running under the supervisor (run.py --supervise), so it cannot "
                             "restart itself. Ask the owner to restart it by hand."}
        reason = str(args.get("reason") or "").strip()[:300]
        self.store.set_state(RESTART_FLAG, json.dumps({"sid": sid, "reason": reason, "requested": time.time()}))
        self.store.event(sid, "notice", {"text": "Перезапуск запланирован: сервер перезапустится на новой ревизии "
                                                 "сразу после завершения этого хода.", "restart": "scheduled"})
        return {"scheduled": True,
                "note": "The restart happens right after this turn ends: finish the turn with a short report "
                        "now. A message arrives after the restart asking you to verify the new revision."}

    # ------------------------------------------------------------------ the restart itself
    def pending(self):
        """The recorded request, if any. Accepts the bare timestamp the self-heal flow writes."""
        raw = self.store.get_state(RESTART_FLAG, "")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data
        return {"sid": "", "reason": "self-heal", "requested": raw}

    def after_turn(self, sid):
        """Called when a turn finished: restart now if one was requested and nothing runs any more."""
        request = self.pending()
        if not request:
            return False
        if any(self.agent.busy(other) for other in list(self.agent.jobs)):
            return False  # the next turn already started (a drained queue); try again after it
        target = request.get("sid") or sid
        self.store.set_state(RESTART_FLAG, "")
        if not self.supervised():
            self.store.event(target, "notice", {"text": "Перезапуск невозможен: сервер запущен без супервизора "
                                                        "(run.py --supervise). Перезапустите его вручную."})
            return False
        self.store.queue_message(target, VERIFY_TEXT)
        self.store.event(target, "notice", {"text": "Перезапускаюсь на новой ревизии…", "restart": "now",
                                            "reason": request.get("reason") or ""})
        self.store.db.commit()
        self.exit(RESTART_EXIT_CODE)
        return True
