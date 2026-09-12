"""Core agent behaviour: the per-turn prompt, context compaction and the turn budget.

These cover what keeps a long turn honest and finite — the model is told what really
exists this turn, the context is compacted instead of the turn being killed, and a
looping or flooding tool call is stopped before it burns the step budget.
"""

import asyncio
import base64
import time
import json
import os
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from connector import agent as agent_module
from connector.agent import IMAGE_PLACEHOLDER, SYSTEM, TOOLS, Agent
from connector.config import Config, password_hash
from connector.prompting import build_system_prompt, goal_summary, tool_inventory, turn_note
from connector.providers import ProviderError
from connector.store import Store


@pytest.fixture
def config_bundle(tmp_path):
    return Config(tmp_path)


@pytest.fixture
def bundle(tmp_path):
    config = Config(tmp_path)
    config.values.update(admin_password=password_hash("admin-password-1"), chat_password=password_hash("chat-pass"))
    store = Store(config.root / "core.db")
    agent = Agent(config, store, AsyncMock(), AsyncMock())
    yield config, store, agent
    store.db.close()


NOW = datetime(2026, 9, 11, 14, 30, 5)


def prompt(config, tools=TOOLS, session=None, root="/workspace"):
    return build_system_prompt(session or {}, tools, config, root, NOW)


# --- dynamic system prompt ----------------------------------------------------------
def test_prompt_lists_exactly_the_tools_of_this_turn(config_bundle):
    config = config_bundle
    from connector.workspace import EXTRA_TOOLS

    text = prompt(config, TOOLS + EXTRA_TOOLS, {"model": "deepseek-flash"})
    for name in ("list_files", "read_file", "apply_patch", "exec_command", "update_plan", "request_user_input"):
        assert f"- {name}:" in text
    assert "Workspace files:" in text and "Execution:" in text and "Planning:" in text

    reduced = prompt(config, [t for t in TOOLS if t["function"]["name"] in {"read_file", "list_files"}],
                     {"model": "deepseek-flash"})
    assert "- read_file:" in reduced
    assert "- apply_patch:" not in reduced and "- run_command:" not in reduced
    assert "Only these tools exist" in reduced


def test_prompt_groups_shared_tools_and_unknown_extensions(config_bundle):
    extra = [{"type": "function", "function": {"name": "shared_image", "description": "Farm image."}},
             {"type": "function", "function": {"name": "ssh_run", "description": "Run over SSH."}}]
    text = prompt(config_bundle, TOOLS + extra, {"model": "deepseek-flash"})
    assert "Shared tools:\n- shared_image:" in text
    assert "Extensions:\n- ssh_run:" in text


def test_prompt_states_the_approval_mode_of_this_chat(config_bundle):
    config = config_bundle
    manual = prompt(config, session={"auto_approve": 0})
    assert "Auto-apply is OFF" in manual and "approve the exact diff" in manual
    auto = prompt(config, session={"auto_approve": 1})
    assert "Auto-apply is ON" in auto and "Auto-apply is OFF" not in auto


def test_prompt_says_when_command_execution_is_disabled(config_bundle):
    config = config_bundle
    assert "Command execution is disabled" in prompt(config)
    config.values["allow_commands"] = True
    assert "Command execution is disabled" not in prompt(config)


def test_vision_note_and_view_image_follow_the_model(config_bundle):
    config = config_bundle
    visual = prompt(config, session={"model": "deepseek-flash"})
    assert "- view_image:" in visual and "Vision input is available" in visual
    blind = prompt(config, session={"model": "deepseek-v4-pro"})
    assert "- view_image:" not in blind and "NOT available" in blind


def test_send_file_is_not_offered_without_a_telegram_chat(config_bundle):
    config = config_bundle
    web = prompt(config, session={"chat_id": None})
    assert "send_file cannot deliver anything" in web
    chat = prompt(config, session={"chat_id": 12345})
    assert "send_file delivers an existing workspace artifact" in chat


def test_prompt_carries_environment_budget_and_guidance(config_bundle):
    text = build_system_prompt({}, TOOLS, config_bundle, "/tmp/project", NOW, "Project guidance from AGENTS.md")
    assert "2026-09-11" not in text, "the date is volatile: it belongs to the turn note, not the cached prefix"
    assert "/tmp/project" in text
    assert f"Step budget for this turn: {config_bundle['max_steps']}" in text
    assert "queued and processed after it" in text
    assert "Project guidance from AGENTS.md" in text
    assert SYSTEM.splitlines()[0] in text


def test_prompt_without_tools(config_bundle):
    assert tool_inventory([]) == []
    assert "No tools are available in this turn" in prompt(config_bundle, [])


def test_tool_inventory_order_does_not_depend_on_the_order_tools_arrive(config_bundle):
    extra = [{"type": "function", "function": {"name": n, "description": n}} for n in ("ssh_run", "shared_image", "shared_video")]
    straight = prompt(config_bundle, TOOLS + extra, {"model": "deepseek-flash"})
    shuffled = prompt(config_bundle, list(reversed(extra)) + list(reversed(TOOLS)), {"model": "deepseek-flash"})
    assert straight == shuffled, "a reordered tool list must not move a single byte of the cached prefix"


# --- DeepSeek prefix cache: a stable prompt is the resource -------------------------
def test_system_prompt_is_byte_identical_across_steps_and_turns(bundle):
    _, store, agent = bundle
    session = store.resolve(50, 0, 1)
    sid = session["id"]
    (agent.workspace(sid) / "AGENTS.md").write_text("Follow the house rules", encoding="utf-8")

    first, guidance_one = agent.system_prompt(sid, TOOLS)
    second, guidance_two = agent.system_prompt(sid, TOOLS)
    assert (first, guidance_one) == (second, guidance_two), "two steps of one turn must send the same bytes"

    agent._project_guidance.pop(sid, None)  # what run() does between turns
    third, guidance_three = agent.system_prompt(sid, TOOLS)
    assert (third, guidance_three) == (first, guidance_one), "an unchanged workspace keeps the cached prefix"

    (agent.workspace(sid) / "AGENTS.md").write_text("Follow the NEW house rules", encoding="utf-8")
    agent._project_guidance.pop(sid, None)
    _, changed = agent.system_prompt(sid, TOOLS)
    assert changed != guidance_one, "a real change to the project rules must reach the model"


def test_only_the_appended_turn_note_carries_volatile_state(bundle):
    _, store, agent = bundle
    session = store.resolve(51, 0, 1)
    sid = session["id"]
    store.message(sid, {"role": "user", "content": "сделай ролик"})
    agent.save_goal(sid, goal="Собрать ролик из кадров", steps=[{"text": "кадры", "status": "completed"},
                                                               {"text": "анимация", "status": "pending"}])

    messages = agent.context(sid)

    assert messages[-1]["role"] == "user" and messages[-1]["content"].startswith("## Turn note")
    assert "Собрать ролик" in messages[-1]["content"] and "Plan 1/2" in messages[-1]["content"]
    assert datetime.now().astimezone().date().isoformat() in messages[-1]["content"]
    assert "Turn note" not in messages[0]["content"] and "Goal (" not in messages[0]["content"]
    assert not [m for m in store.history(sid) if "Turn note" in str(m["content"])], "the note is never persisted"


def test_turn_note_reports_a_still_running_background_job(bundle):
    _, store, agent = bundle
    session = store.resolve(52, 0, 1)
    sid = session["id"]

    class FarmStub:
        active = True

        def running(self, session_id):
            return self.active and session_id == sid

        def tools(self, session):
            return []

    stub = FarmStub()
    agent.extensions.append(stub)
    assert "background job" in agent.turn_note(sid)
    stub.active = False
    assert "background job" not in agent.turn_note(sid)


def test_turn_note_is_pure_and_bounded():
    long_goal = {"goal": "ц" * 900, "status": "active", "steps": [{"text": "т" * 300, "status": "pending"}] * 5}
    assert len(goal_summary(long_goal)) <= 600, "the objective must stay a summary, not a second context"
    assert "Goal (active): ц" in turn_note("2026-09-11", long_goal)
    assert turn_note("2026-09-11", None).count("No goal is recorded yet") == 1


# --- tool-call adjacency (the DeepSeek HTTP 400 crash) ------------------------------
def _assistant_call(cid, name="shared_video"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}]}


def test_context_enforces_tool_call_adjacency_after_an_interleaved_message(bundle):
    """The live crash: a farm reply returns after the owner wrote mid-turn, so the tool message no
    longer immediately follows its assistant tool_calls turn and DeepSeek rejects it with HTTP 400."""
    _, store, agent = bundle
    sid = store.resolve(60, 0, 1)["id"]
    store.message(sid, {"role": "user", "content": "сделай ролик"})
    store.message(sid, _assistant_call("X"))
    store.message(sid, {"role": "tool", "tool_call_id": "X", "content": '{"pending": true}'})
    # The owner writes mid-turn; then the slow farm tool returns a late SECOND reply for the same id.
    store.message(sid, {"role": "user", "content": "поменяй сцену"})
    store.message(sid, {"role": "tool", "tool_call_id": "X", "content": '{"path": "shared/clip.mp4"}'})

    messages = agent.context(sid)

    assert agent.well_formed(messages), "context() must emit a list DeepSeek would accept"
    tools = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    assert len(tools) == 1, "the late duplicate reply is dropped, never sent as an orphan"
    owner = messages[tools[0] - 1]
    assert owner.get("tool_calls") and owner["tool_calls"][0]["id"] == "X", "the reply follows its own turn"
    assert any(m.get("content") == "поменяй сцену" for m in messages), "the mid-turn message survives, after the block"


def test_well_formed_flags_a_split_turn_and_sanitize_repairs_it(bundle):
    _, _, agent = bundle
    split = [_assistant_call("Y", "read_file"),
             {"role": "user", "content": "mid-turn"},
             {"role": "tool", "tool_call_id": "Y", "content": "ok"}]
    assert agent.well_formed(split) is False, "a user message between the turn and its reply is malformed"

    fixed = agent.sanitize(split)
    assert agent.well_formed(fixed)
    assert [m["role"] for m in fixed] == ["assistant", "tool", "user"], "the reply is pulled back beside its turn"
    assert fixed[1]["content"] == "ok", "the real reply is kept, not replaced by a synthetic one"


def test_sanitize_drops_orphans_and_synthesizes_missing_replies(bundle):
    _, _, agent = bundle
    history = [{"role": "tool", "tool_call_id": "gone", "content": "orphan"},
               {"role": "user", "content": "hi"},
               _assistant_call("Z", "read_file")]

    result = agent.sanitize(history)

    assert agent.well_formed(result)
    assert not any(m.get("tool_call_id") == "gone" for m in result), "a pure orphan is dropped"
    synth = [m for m in result if m.get("tool_call_id") == "Z"][0]
    assert json.loads(synth["content"])["interrupted"] is True, "an unanswered turn gets a synthetic reply"


def test_repair_history_records_a_note_instead_of_orphaning_a_buried_turn(bundle):
    """A tool_calls turn that already has a later message after it must never get a bare tool
    reply appended: the store only appends, so that reply would land out of place and orphan."""
    _, store, agent = bundle
    sid = store.resolve(61, 0, 1)["id"]
    store.message(sid, _assistant_call("W"))
    store.message(sid, {"role": "user", "content": "next thing"})  # a later turn already appended

    agent.repair_history(sid)

    history = store.history(sid)
    assert not any(m.get("role") == "tool" for m in history), "no bare tool message after the interleaved user turn"
    assert history[-1]["role"] == "user" and "фоновой задачи" in history[-1]["content"]
    assert agent.well_formed(agent.sanitize(history)), "the outgoing view is still well-formed"


async def test_a_mid_turn_message_queues_and_never_abandons_a_running_farm_job(bundle):
    """BUG 3: an interleaved owner message steers the next turn; it must not cancel a farm render
    already in flight nor discard its finished result."""
    from connector.shared_tools import SharedTools

    _, store, agent = bundle
    session = store.resolve(62, 0, 1)
    sid = session["id"]
    shared = SharedTools(agent, AsyncMock())
    agent.extensions.append(shared)

    release = asyncio.Event()

    async def farm_job():
        await release.wait()
        return {"path": "shared/clip.mp4"}

    shared.spawn(session, "video", farm_job())

    async def turn():  # an agent turn is in progress, so the new message is interleaved
        await release.wait()

    agent.jobs[sid] = asyncio.create_task(turn())
    await asyncio.sleep(0)
    assert agent.busy(sid) and shared.running(sid)

    result = agent.submit(session, "и добавь звук")
    assert result["accepted"] and result["queued"] and result["position"] == 1
    assert shared.running(sid), "the interleaved message must not cancel the farm task"

    release.set()
    await agent.jobs.pop(sid)
    await asyncio.sleep(0.01)
    assert not shared.running(sid)
    results = [e for e in store.events(sid) if e["kind"] == "tool_result"]
    assert results and results[-1]["payload"]["result"]["path"] == "shared/clip.mp4", "finished work reached the chat"


# --- context compaction -------------------------------------------------------------
def fill(store, sid, results, size=4000, task="task"):
    store.message(sid, {"role": "user", "content": task})
    for index in range(results):
        call = f"call{index}"
        store.message(sid, {"role": "assistant", "content": None, "reasoning_content": "thinking " * 20,
                            "tool_calls": [{"id": call, "type": "function",
                                            "function": {"name": "read_file", "arguments": "{}"}}]})
        store.message(sid, {"role": "tool", "tool_call_id": call, "content": "x" * size})


def test_old_tool_results_are_compacted_before_any_turn_is_dropped(bundle):
    config, store, agent = bundle
    sid = store.resolve(30, 0, 1)["id"]
    config.values["max_context_chars"] = 30000
    config.values["keep_recent_tool_results"] = 2
    fill(store, sid, 10)

    messages = agent.context(sid)

    tools = [m for m in messages if m.get("role") == "tool"]
    assert len(tools) == 10, "a tool result is never removed: the API needs call/result pairing"
    compacted = [m for m in tools if '"compacted": true' in m["content"]]
    assert len(compacted) == 8 and tools[-1]["content"] == "x" * 4000
    assert json.loads(compacted[0]["content"])["summary"].startswith("x" * 300)
    thinking = [i for i, m in enumerate(messages) if m.get("reasoning_content")]
    assert thinking == [len(messages) - 3], "reasoning survives only above the compaction watermark"
    event = [e for e in store.events(sid) if e["kind"] == "context"][-1]
    assert event["payload"]["compacted"] == 8 and event["payload"]["saved_chars"] > 20000
    assert event["payload"]["cache"]["known"] is False, "the cache line reports unknown, never zero"
    assert store.history(sid)[2]["content"] == "x" * 4000, "the store keeps the full history"


def test_whole_old_turns_are_dropped_when_compaction_is_not_enough(bundle):
    config, store, agent = bundle
    sid = store.resolve(31, 0, 1)["id"]
    config.values["max_context_chars"] = 12000
    for _ in range(4):
        # Compaction only shrinks tool output, so oversized user turns force a real drop.
        fill(store, sid, 3, size=3000, task="u" * 5000)

    messages = agent.context(sid)

    assert agent.text_size(messages[2:-1]) <= 12000
    assert any("исключено сообщений" in e["payload"]["text"] for e in store.events(sid) if e["kind"] == "context")
    assert len(store.history(sid)) == 28


def test_an_oversized_single_turn_is_compacted_instead_of_killed(bundle):
    config, store, agent = bundle
    sid = store.resolve(32, 0, 1)["id"]
    config.values["max_context_chars"] = 9000
    config.values["keep_recent_tool_results"] = 6
    fill(store, sid, 6, size=4000)

    messages = agent.context(sid)

    tools = [m for m in messages if m.get("role") == "tool"]
    assert len(tools) == 6, "a tool result is never removed, only shrunk"
    assert all('"compacted": true' in m["content"] for m in tools)
    assert agent.text_size(messages[2:-1]) <= 9000


def test_compaction_is_idempotent_and_never_touches_the_frozen_prefix(bundle):
    config, store, agent = bundle
    sid = store.resolve(41, 0, 1)["id"]
    config.values["max_context_chars"] = 30000
    config.values["keep_recent_tool_results"] = 2
    fill(store, sid, 10)

    first = agent.context(sid)
    again = agent.context(sid)
    assert first == again, "recomputing the same history must produce the same bytes"
    watermark = agent.context_state(sid)["mark"]
    assert watermark > 0 and agent.fit_context(sid)[1]["changed"] is False, "a settled watermark stays put"

    # The next turn appends to the history: the compacted prefix must arrive unchanged.
    fill(store, sid, 1, size=100)
    later = agent.context(sid)
    assert later[:len(first) - 1] == first[:-1], "everything before the new messages is byte-identical"
    assert agent.context_state(sid)["mark"] == watermark


def test_compaction_overshoots_the_limit_so_it_does_not_repeat_every_step(bundle):
    config, store, agent = bundle
    sid = store.resolve(42, 0, 1)["id"]
    config.values["max_context_chars"] = 30000
    config.values["keep_recent_tool_results"] = 2
    fill(store, sid, 10)

    _, report = agent.fit_context(sid)

    assert report["chars"] <= config["max_context_chars"] * config["compact_target_ratio"], "hysteresis"
    assert report["changed"] and report["trimmed"] is False


def test_context_size_reports_the_cache_of_the_last_request(bundle):
    _, store, agent = bundle
    sid = store.resolve(43, 0, 1)["id"]
    assert agent.context_size(sid)["cache"] == {"known": False, "hit_tokens": None, "miss_tokens": None,
                                                "prompt_tokens": None, "percent": None}

    store.add_usage(sid, {"prompt_tokens": 1000, "completion_tokens": 10,
                          "cache_hit_tokens": 910, "cache_miss_tokens": 90})

    cache = agent.context_size(sid)["cache"]
    assert cache["known"] and cache["hit_tokens"] == 910 and cache["percent"] == 91.0


def test_context_size_reports_what_would_be_sent_not_the_raw_pile(bundle):
    config, store, agent = bundle
    sid = store.resolve(33, 0, 1)["id"]
    config.values["max_context_chars"] = 20000
    config.values["context_window_tokens"] = 64000
    config.values["keep_recent_tool_results"] = 1
    fill(store, sid, 8)

    size = agent.context_size(sid)

    # Raw history keeps growing, but the meter reports the request the agent WOULD send after
    # compaction — otherwise a trimmed session shows a permanently full budget forever.
    assert size["raw_chars"] == agent.text_size(agent.sanitize(store.history(sid)))
    assert size["compacted_chars"] == size["chars"] < size["raw_chars"]
    assert size["raw_trimmed"] is True and size["raw_percent"] > 100 >= size["percent"]
    # No request has gone out in this fixture, so the token count is an estimate and admits it.
    assert size["provider_known"] is False and size["provider_tokens"] is None and size["estimated"] is True
    assert size["window_tokens"] == 64000
    assert size["window_percent"] == round(100 * size["tokens"] / 64000, 1)
    assert not [e for e in store.events(sid) if e["kind"] == "context"], "the meter must not write events"


def test_context_size_uses_the_tokens_the_provider_really_counted(bundle):
    config, store, agent = bundle
    sid = store.resolve(34, 0, 1)["id"]
    config.values["context_window_tokens"] = 64000
    store.message(sid, {"role": "user", "content": "x" * 4000})

    before = agent.context_size(sid)
    assert before["estimated"] is True and before["tokens"] == round(before["chars"] / 4)

    store.add_usage(sid, {"prompt_tokens": 32000, "completion_tokens": 5})
    after = agent.context_size(sid)

    # The window figure is the provider's own count, not a second guess from characters.
    assert after["provider_known"] is True and after["estimated"] is False
    assert after["provider_tokens"] == 32000 and after["window_percent"] == 50.0
    assert after["provider_created"] and after["tokens"] == round(after["chars"] / 4), "chars stay chars"


# --- loop guard, budget and error hints ---------------------------------------------
class Scripted:
    """A backend replaying prepared assistant messages, like the real streaming provider."""

    def __init__(self, messages):
        self.messages, self.calls = list(messages), []

    async def complete(self, messages, tools, delta):
        self.calls.append((messages, tools))
        item = self.messages.pop(0) if self.messages else {"role": "assistant", "content": "done"}
        return item, {"prompt_tokens": 1, "completion_tokens": 1}


def call_message(name, arguments, index=0):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"c{index}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


async def test_identical_calls_are_stopped_by_the_loop_guard(bundle):
    _, store, agent = bundle
    session = store.resolve(34, 0, 1)
    sid = session["id"]
    (agent.workspace(sid) / "a.txt").write_text("content", encoding="utf-8")
    agent.deepseek = Scripted([call_message("read_file", {"path": "a.txt"}, i) for i in range(4)]
                              + [{"role": "assistant", "content": "готово"}])

    await agent.run(session, "прочитай файл")

    results = [e["payload"]["result"] for e in store.events(sid) if e["kind"] == "tool_result"]
    assert results[0]["text"] == "content"
    assert results[1]["unchanged"] is True
    assert results[2].get("loop_guard") and "Repeated identical call" in results[2]["error"]
    assert any("защитой от цикла" in e["payload"]["text"] for e in store.events(sid) if e["kind"] == "notice")


async def test_tool_output_budget_truncates_and_notices_once(bundle):
    config, store, agent = bundle
    config.values["max_turn_tool_chars"] = 5000
    session = store.resolve(35, 0, 1)
    sid = session["id"]
    for name in ("one.txt", "two.txt", "three.txt"):
        (agent.workspace(sid) / name).write_text("y" * 20000, encoding="utf-8")
    agent.deepseek = Scripted([call_message("read_file", {"path": name}, i)
                               for i, name in enumerate(("one.txt", "two.txt", "three.txt"))]
                              + [{"role": "assistant", "content": "готово"}])

    await agent.run(session, "прочитай три файла")

    payloads = [json.loads(m["content"]) for m in store.history(sid) if m.get("role") == "tool"]
    assert "truncated" not in payloads[0]
    assert payloads[-1]["truncated"] is True and len(payloads[-1]["output"]) == 4000
    notices = [e for e in store.events(sid) if e["kind"] == "notice" and "обрезаются" in e["payload"]["text"]]
    assert len(notices) == 1, "the budget is announced once, not on every result"


async def test_repeated_identical_error_gets_an_actionable_hint(bundle):
    _, store, agent = bundle
    session = store.resolve(36, 0, 1)
    sid = session["id"]
    agent.deepseek = Scripted([call_message("read_file", {"path": "missing.txt"}, i) for i in range(2)]
                              + [{"role": "assistant", "content": "нет файла"}])

    await agent.run(session, "прочитай отсутствующий файл")

    results = [e["payload"]["result"] for e in store.events(sid) if e["kind"] == "tool_result"]
    assert "list_files" in results[0]["hint"]
    assert "Same error twice" in results[1]["hint"]


async def test_bad_json_arguments_are_reported_with_the_schema_hint(bundle):
    _, store, agent = bundle
    session = store.resolve(37, 0, 1)
    sid = session["id"]
    broken = {"role": "assistant", "content": None,
              "tool_calls": [{"id": "bad", "type": "function",
                              "function": {"name": "read_file", "arguments": "{path: a.txt"}}]}
    agent.deepseek = Scripted([broken, {"role": "assistant", "content": "исправлюсь"}])

    await agent.run(session, "прочитай файл")

    result = [e["payload"]["result"] for e in store.events(sid) if e["kind"] == "tool_result"][0]
    assert "valid JSON" in result["hint"]


async def test_path_escape_error_explains_the_expected_path(bundle):
    _, store, agent = bundle
    session = store.resolve(38, 0, 1)
    agent.deepseek = Scripted([call_message("read_file", {"path": "../../etc/passwd"}),
                               {"role": "assistant", "content": "ок"}])

    await agent.run(session, "прочитай системный файл")

    result = [e["payload"]["result"] for e in store.events(session["id"]) if e["kind"] == "tool_result"][0]
    assert "relative path with forward slashes" in result["hint"]


async def test_exhausted_step_budget_ends_with_a_summary(bundle):
    config, store, agent = bundle
    config.values["max_steps"] = 2
    session = store.resolve(39, 0, 1)
    sid = session["id"]
    (agent.workspace(sid) / "a.txt").write_text("content", encoding="utf-8")
    backend = Scripted([call_message("read_file", {"path": "a.txt"}, 0),
                        call_message("list_files", {"path": "."}, 1),
                        {"role": "assistant", "content": "Сделано: прочитан a.txt. Осталось: проверить тесты."}])
    agent.deepseek = backend

    await agent.run(session, "поработай")

    assert len(backend.calls) == 3, "exactly one extra toolless call closes the turn"
    assert backend.calls[-1][1] == []
    assert backend.calls[-1][0][-1]["content"].startswith("Step budget for this turn is exhausted")
    texts = [e["payload"]["text"] for e in store.events(sid) if e["kind"] in ("assistant", "notice")]
    assert any("Осталось" in t for t in texts)
    # The goal is still active, so the turn says it continues instead of asking for a nudge.
    assert any("имит шагов" in t for t in texts)


# --- the goal, and how the agent keeps pursuing it ----------------------------------
async def test_the_first_user_message_becomes_the_goal_and_the_model_refines_it(bundle):
    _, store, agent = bundle
    session = store.resolve(44, 0, 1)
    sid = session["id"]
    agent.deepseek = Scripted([call_message("set_goal", {"goal": "Сделать 5 кадров и склеить ролик",
                                                         "status": "active", "note": "ферма"}),
                               {"role": "assistant", "content": "принял"}])

    await agent.run(session, "Сгенерируй кадры на ферме и собери из них ролик")

    goal = agent.goal(sid)
    assert goal["goal"] == "Сделать 5 кадров и склеить ролик" and goal["source"] == "model"
    events = [e["payload"] for e in store.events(sid) if e["kind"] == "goal"]
    assert events[0]["goal"].startswith("Сгенерируй кадры"), "a deterministic goal exists before any model call"
    assert events[0]["source"] == "user" and events[-1]["source"] == "model"


async def test_update_plan_is_mirrored_into_the_goal_steps(bundle):
    _, store, agent = bundle
    from connector.workspace import WorkspaceService

    session = store.resolve(45, 0, 1)
    sid = session["id"]
    agent.workspace_service = WorkspaceService(agent)
    plan = [{"step": "кадры", "status": "completed"}, {"step": "видео", "status": "in_progress"}]

    result = await agent.execute(session, "update_plan", {"plan": plan})

    assert result == {"updated": True}, "the plan tool keeps working exactly as before"
    assert agent.goal(sid)["steps"] == [{"text": "кадры", "status": "completed"},
                                        {"text": "видео", "status": "in_progress"}]
    assert "Plan 1/2" in agent.turn_note(sid)


def test_a_bad_goal_is_refused_without_touching_the_stored_one(bundle):
    _, store, agent = bundle
    session = store.resolve(46, 0, 1)
    agent.save_goal(session["id"], goal="Рабочая цель")
    for args in ({"goal": "   "}, {"goal": "ok", "status": "нечто"}):
        with pytest.raises(ValueError):
            agent.apply_goal(session, args)
    assert agent.goal(session["id"])["goal"] == "Рабочая цель"


async def settle_started(agent, expected, timeout=2.0):
    """The continuation is scheduled, not inline: let its task queue and start the next turn."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(agent.run.call_args_list) >= expected:
            return True
        await asyncio.sleep(0.01)
    return False


async def test_auto_continue_submits_a_follow_up_only_while_the_goal_is_unfinished(bundle):
    """One unified continuation: the goal drives it, `max_auto_continues` bounds it."""
    config, store, agent = bundle
    config.values["max_auto_continues"] = 2
    agent_module.AUTO_CONTINUE_GRACE = 0  # the grace window is what the owner reads in; not needed here
    agent.run = AsyncMock()  # the continuation now starts the turn itself; running it is another test
    session = store.resolve(47, 0, 1)
    sid = session["id"]
    agent.save_goal(sid, goal="Собрать ролик", auto_continue=True,
                    steps=[{"text": "кадры", "status": "completed"}, {"text": "видео", "status": "pending"}])

    for used in range(3):
        agent.continues[sid] = used
        agent._delivered[sid] = True  # the turn answered the owner; only then may it continue
        assert agent.plan_continuation(session) is (used < 2), "never more than the configured limit"
        await settle_started(agent, min(used + 1, 2))
        agent.auto_pending.discard(sid)
    started = [call.args[1] for call in agent.run.call_args_list]
    assert len(started) == 2 and all("Продолжай цель: «Собрать ролик»" in text for text in started)
    assert "Автопродолжение 2/2" in started[1]
    notices = [e["payload"]["text"] for e in store.events(sid) if e["kind"] == "notice"]
    assert len([t for t in notices if "Продолжу сам через" in t]) == 2
    assert any("Автопродолжение остановлено" in t for t in notices)

    agent.continues[sid] = 0
    agent._delivered[sid] = True
    agent.save_goal(sid, status="done")
    assert agent.plan_continuation(session) is False, "a finished goal is not continued"
    agent.save_goal(sid, status="blocked")
    assert agent.plan_continuation(session) is False, "a blocked goal waits for the owner"


async def test_auto_continue_waits_for_a_pending_approval_and_stops_after_a_failed_turn(bundle):
    _, store, agent = bundle
    session = store.resolve(48, 0, 1)
    sid = session["id"]
    agent.save_goal(sid, goal="Собрать ролик", steps=[{"text": "видео", "status": "pending"}])

    agent.approvals["a1"] = {"future": asyncio.get_running_loop().create_future(), "session": session,
                             "admin_only": False, "name": "write_file", "detail": "diff"}
    agent._delivered[sid] = True
    assert agent.plan_continuation(session) is False, "a waiting approval is not a stall to push through"
    agent.approvals.pop("a1")

    agent._turn_failed.add(sid)
    agent._delivered[sid] = True
    assert agent.plan_continuation(session) is False, "an error or a cancellation ends the pursuit"
    agent._turn_failed.discard(sid)
    agent._delivered[sid] = True
    assert agent.plan_continuation(session) is True
    store.drop_queued(sid)
    agent.auto_pending.discard(sid)

    store.update_session(sid, auto_continue=0)
    agent._delivered[sid] = True
    assert agent.plan_continuation(session) is False, "the owner can switch the behaviour off per chat"


def test_the_ide_reads_and_edits_the_goal_over_the_api(tmp_path):
    from fastapi.testclient import TestClient

    from connector.app import create_app

    app = create_app(tmp_path, polling=False)
    app.state.config.values.update(admin_password=password_hash("admin-password-1"),
                                   chat_password=password_hash("chat-pass"))
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "admin-password-1"})
        client.headers["X-Requested-With"] = "DeepSeekIDE"
        sid = client.post("/api/sessions", json={"title": "Ролик"}).json()["id"]

        assert client.get(f"/api/sessions/{sid}/goal").json()["goal"] == ""
        saved = client.post(f"/api/sessions/{sid}/goal", json={
            "goal": "Собрать ролик из кадров", "auto_continue": True,
            "steps": [{"text": "кадры", "status": "completed"}, {"text": "видео", "status": "pending"}]}).json()
        assert saved["auto_continue"] is True and saved["source"] == "user"
        assert client.get(f"/api/sessions/{sid}/goal").json()["steps"][1]["text"] == "видео"
        assert client.post(f"/api/sessions/{sid}/goal", json={"status": "нечто"}).status_code == 422
        assert "Собрать ролик" in app.state.agent.turn_note(sid), "the edit reaches the next turn note"


async def test_a_tool_that_waited_for_a_background_job_is_not_a_loop(bundle, monkeypatch):
    from connector import agent as agent_module

    _, store, agent = bundle
    session = store.resolve(49, 0, 1)
    sid = session["id"]
    monkeypatch.setattr(agent_module, "LONG_TOOL_SECONDS", 0)

    class FarmStub:
        def tools(self, session):
            return [{"type": "function", "function": {"name": "shared_video", "description": "render",
                                                      "parameters": {"type": "object", "properties": {}}}}]

        async def execute(self, session, name, args):
            await asyncio.sleep(0.01)
            return {"path": "shared/clip.mp4"}

        def running(self, session_id):
            return False

    agent.extensions.append(FarmStub())
    agent.deepseek = Scripted([call_message("shared_video", {"path": "a.png"}, i) for i in range(3)]
                              + [{"role": "assistant", "content": "готово"}])

    await agent.run(session, "сделай три ролика подряд")

    results = [e["payload"]["result"] for e in store.events(sid) if e["kind"] == "tool_result"]
    assert len(results) == 3 and not any(r.get("loop_guard") for r in results), "waiting is work, not a repeat"
    assert any("не считается повтором" in e["payload"]["text"] for e in store.events(sid) if e["kind"] == "notice")


async def test_cancellation_and_provider_errors_still_end_the_turn_cleanly(bundle):
    _, store, agent = bundle
    session = store.resolve(40, 0, 1)
    sid = session["id"]

    class Hanging:
        async def complete(self, messages, tools, delta):
            await asyncio.sleep(30)

    agent.deepseek = Hanging()
    task = asyncio.create_task(agent.run(session, "долгая задача"))
    await asyncio.sleep(0.05)
    task.cancel()
    await task

    kinds = [e["kind"] for e in store.events(sid)]
    assert "notice" in kinds and kinds[-1] == "turn_completed"
    assert store.session(sid)["status"] == "idle"


# --- the byte budget: the DeepSeek HTTP 413 crash ----------------------------------
def image_message(size, tag="img"):
    """A vision-result user message whose base64 payload is `size` bytes long."""
    return {"role": "user", "content": [
        {"type": "text", "text": tag},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * size}}]}


def _image_count(messages):
    return sum(1 for m in messages if isinstance(m.get("content"), list)
               for p in m["content"] if p.get("type") == "image_url")


def test_byte_budget_sheds_oldest_images_first_and_keeps_the_newest(bundle):
    """text_size ignores base64, so a handful of vision frames pass the char budget yet blow the
    real HTTP body. The byte budget sheds the OLDEST images, keeps the newest, and stays under."""
    config, store, agent = bundle
    sid = store.resolve(70, 0, 1)["id"]
    config.values["max_request_bytes"] = 300000
    store.message(sid, {"role": "user", "content": "task"})
    for i in range(5):
        store.message(sid, image_message(200000, f"img{i}"))

    messages = agent.context(sid)

    assert agent.well_formed(messages), "shedding must not orphan a tool message or split a turn"
    assert agent.request_bytes(messages) <= config["max_request_bytes"], "outgoing body fits the budget"
    survivors = [m for m in messages if isinstance(m.get("content"), list)
                 and any(p.get("type") == "image_url" for p in m["content"])]
    assert len(survivors) == 1, "only the newest image survives"
    assert any(p.get("text") == "img4" for p in survivors[0]["content"]), "the newest image is the one kept"
    assert any(IMAGE_PLACEHOLDER in str(m.get("content")) for m in messages), "old frames become placeholders"
    stored = [m for m in store.history(sid) if isinstance(m.get("content"), list)]
    assert len(stored) == 5 and _image_count(stored) == 5, "the stored history is never touched"
    assert any("Убрано изображений" in e["payload"]["text"]
               for e in store.events(sid) if e["kind"] == "context"), "the user is told images were shed"


def test_image_shedding_is_deterministic(bundle):
    """Same input twice → byte-identical output, so the DeepSeek prefix cache is not needlessly broken."""
    config, store, agent = bundle
    sid = store.resolve(72, 0, 1)["id"]
    config.values["max_request_bytes"] = 300000
    store.message(sid, {"role": "user", "content": "task"})
    for i in range(5):
        store.message(sid, image_message(200000, f"img{i}"))

    first, second = agent.context(sid), agent.context(sid)
    assert first == second, "recomputing the same history sheds the same images"
    assert agent.request_bytes(first) == agent.request_bytes(second)


class ByteTransport:
    """A fake DeepSeek raising HTTP 413 for its first `fail_times` calls, then succeeding."""

    def __init__(self, fail_times=0):
        self.fail_times, self.calls = fail_times, []

    async def complete(self, messages, tools, delta):
        self.calls.append(_image_count(messages))
        if len(self.calls) <= self.fail_times:
            raise ProviderError("DeepSeek HTTP 413: запрос слишком большой.", too_large=True)
        return {"role": "assistant", "content": "ok"}, {"prompt_tokens": 1, "completion_tokens": 1}


class TooLargeWhileImagesPresent:
    """A fake DeepSeek that 413s while ANY image is present, forcing the text-only fallback."""

    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools, delta):
        n = _image_count(messages)
        self.calls.append(n)
        if n > 0:
            raise ProviderError("DeepSeek HTTP 413.", too_large=True)
        return {"role": "assistant", "content": "text ok"}, {"prompt_tokens": 1, "completion_tokens": 1}


class AlwaysTooLarge:
    async def complete(self, messages, tools, delta):
        raise ProviderError("DeepSeek HTTP 413.", too_large=True)


async def test_a_413_despite_the_budget_triggers_shedding_and_the_step_succeeds(bundle):
    config, store, agent = bundle
    sid = store.resolve(73, 0, 1)["id"]
    config.values["max_request_bytes"] = 5_000_000
    store.message(sid, {"role": "user", "content": "task"})
    for i in range(3):
        store.message(sid, image_message(200000, f"img{i}"))
    backend = ByteTransport(fail_times=1)

    message, usage = await agent.complete_with_retry(backend, sid, TOOLS, agent._silent)

    assert message["content"] == "ok" and len(backend.calls) == 2, "the retry after 413 succeeds"
    assert any("уменьшаю бюджет" in e["payload"]["text"]
               for e in store.events(sid) if e["kind"] == "notice"), "the budget reduction is announced"


async def test_a_413_falls_back_to_a_text_only_request(bundle):
    config, store, agent = bundle
    sid = store.resolve(74, 0, 1)["id"]
    config.values["max_request_bytes"] = 2_000_000
    store.message(sid, {"role": "user", "content": "task"})
    for i in range(4):
        store.message(sid, image_message(500000, f"img{i}"))
    backend = TooLargeWhileImagesPresent()

    message, usage = await agent.complete_with_retry(backend, sid, TOOLS, agent._silent)

    assert message["content"] == "text ok"
    assert backend.calls[-1] == 0, "the successful retry carried no images at all"
    assert any("убираю все изображения" in e["payload"]["text"]
               for e in store.events(sid) if e["kind"] == "notice")


async def test_a_413_that_persists_even_text_only_surfaces_a_clear_error(bundle):
    config, store, agent = bundle
    sid = store.resolve(75, 0, 1)["id"]
    config.values["max_request_bytes"] = 2_000_000
    store.message(sid, {"role": "user", "content": "task"})
    store.message(sid, image_message(500000, "img0"))

    with pytest.raises(ProviderError, match="слишком большой даже без изображений"):
        await agent.complete_with_retry(AlwaysTooLarge(), sid, TOOLS, agent._silent)


def test_a_single_oversized_image_is_downscaled_at_ingest(tmp_path):
    from connector.vision import image_content

    big = tmp_path / "big.png"
    Image.frombytes("RGB", (1500, 1500), os.urandom(1500 * 1500 * 3)).save(big, format="PNG")
    assert big.stat().st_size > 2_000_000, "an incompressible PNG really is oversized"

    parts = image_content(big, max_bytes=500_000)

    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,"), "PNG that busts the cap falls back to JPEG"
    data = base64.b64decode(url.split("base64,", 1)[1])
    assert len(data) <= 500_000, "the encoded image is capped at ingest"
    # No cap given → the lossless PNG path is preserved unchanged.
    assert image_content(big)[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_context_size_reports_the_request_byte_budget(bundle):
    config, store, agent = bundle
    sid = store.resolve(76, 0, 1)["id"]
    config.values["max_request_bytes"] = 4_000_000
    store.message(sid, {"role": "user", "content": "task"})
    store.message(sid, image_message(120000, "img0"))

    size = agent.context_size(sid)

    assert size["max_request_bytes"] == 4_000_000
    assert size["request_bytes"] > 120000 and size["request_bytes"] > size["chars"], "bytes count base64"
    assert size["request_percent"] == round(100 * size["request_bytes"] / 4_000_000, 1)
