from collections import deque
from pathlib import Path

import asyncio
import copy
import json

import pytest

import test_context_identity as fixture


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("existing_self_record", [False, True])
def test_planner_excludes_self_without_removing_history_or_changing_records(cached, existing_self_record):
    plugin = fixture.plugin()
    messages = {
        "a": fixture.message(),
        "self": fixture.message("self", fixture.BOT),
        "b": fixture.message("b", "654321"),
    }
    if cached:
        for message in messages.values():
            asyncio.run(plugin.index_message_speaker(message=message))
        plugin._session_message_index["group-a"].append(("self", fixture.BOT))
    plugin.ctx.message.get_by_id.side_effect = lambda mid, **kwargs: messages.get(mid)
    plugin._get_user_record("123456")["affection"] = -10.0
    plugin._get_user_record("654321")["affection"] = 90.0
    if existing_self_record:
        plugin._get_user_record(fixture.BOT)["affection"] = 42.0
    records = copy.deepcopy(plugin._affection_data)
    items = [fixture.item("SystemMessageItem", "system")]
    items.extend(fixture.item("UserMessageItem", f'<message msg_id="{mid}" user="同名">原始内容', mid)
                 for mid in messages)
    items.append(fixture.item("AssistantMessageItem", "reminder"))
    original = copy.deepcopy(items)

    result = asyncio.run(plugin.inject_affection_before_request(
        items=items, session_id="group-a", tool_definitions=[{"name": "reply"}],
    ))
    assert result["modified_kwargs"]["tool_definitions"] == [{"name": "reply"}]
    assert items[:-2] + items[-1:] == original
    prompt = items[-2]["parts"][0]["text"]
    assert "QQ=123456，msg_id=a" in prompt and "好感度为 -10" in prompt
    assert "QQ=654321，msg_id=b" in prompt and "好感度为 90" in prompt
    assert f"QQ={fixture.BOT}" not in prompt
    assert plugin._affection_data == records


def test_self_only_planner_history_does_not_create_an_empty_hint_or_record():
    plugin = fixture.plugin()
    plugin.ctx.message.get_by_id.return_value = fixture.message("self", fixture.BOT)
    items = [fixture.item("UserMessageItem", '<message msg_id="self" user="角色">自己的话')]
    original = copy.deepcopy(items)
    assert asyncio.run(plugin.inject_affection_before_request(items=items, session_id="group-a")) is None
    assert items == original
    assert not plugin._affection_data


@pytest.mark.parametrize("cached", [False, True])
def test_replyer_self_target_keeps_existing_prompts_without_creating_record(cached):
    if cached:
        plugin = fixture.plugin()
        plugin._session_message_index["group-a"] = deque([("self", fixture.BOT)], maxlen=100)
    else:
        plugin, message_rpc = fixture.sdk_plugin({"success": True, "message": fixture.message("self", fixture.BOT)})
    kwargs = {"session_id": "group-a", "reply_message_id": "self", "extra_prompt": "保留身份和日程提示"}
    assert asyncio.run(plugin.inject_affection_into_reply(**kwargs)) is None
    assert kwargs["extra_prompt"] == "保留身份和日程提示"
    assert not plugin._affection_data
    if not cached:
        message_rpc.assert_awaited_once()


def test_receive_does_not_schedule_self_evaluation_but_keeps_human_evaluation(monkeypatch):
    plugin = fixture.plugin()
    evaluations = []
    monkeypatch.setattr(plugin, "_schedule_auto_evaluation", lambda *args: evaluations.append(args))
    asyncio.run(plugin.index_message_speaker(message=fixture.message("self", fixture.BOT)))
    assert not plugin._session_message_index
    assert not evaluations
    asyncio.run(plugin.index_message_speaker(message=fixture.message()))
    assert plugin._lookup_message_speaker("group-a", "a") == "123456"
    assert len(evaluations) == 1 and evaluations[0][1] == "123456"
    plugin.ctx.llm.generate.assert_not_awaited()


@pytest.mark.parametrize("bot_id", [None, 0])
@pytest.mark.parametrize("stage", ["planner", "replyer", "receive"])
def test_invalid_bot_account_is_reported_instead_of_injecting_unfiltered(bot_id, stage):
    plugin = fixture.plugin()
    plugin.ctx.config.get.return_value = bot_id
    plugin.ctx.message.get_by_id.return_value = fixture.message()
    with pytest.raises(ValueError, match="无法获取有效的机器人 QQ 号"):
        if stage == "planner":
            asyncio.run(plugin.inject_affection_before_request(items=fixture.planner_items(), session_id="group-a"))
        elif stage == "replyer":
            asyncio.run(plugin.inject_affection_into_reply(
                session_id="group-a", reply_message_id="a", extra_prompt="保留已有提示",
            ))
        else:
            asyncio.run(plugin.index_message_speaker(message=fixture.message()))
    assert not plugin._affection_data
    plugin.ctx.llm.generate.assert_not_awaited()


def test_manifest_declares_readonly_bot_account_lookup():
    manifest = json.loads((Path(__file__).parents[1] / "_manifest.json").read_text())
    assert "config.get" in manifest["capabilities"]
