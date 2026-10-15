from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import copy
import importlib.util
import inspect
import logging
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "feyumy_context_testpkg"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(f"{PACKAGE}.plugin", ROOT / "plugin.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def plugin():
    instance = module.AffectionPlugin()
    instance._plugin_config_instance = module.AffectionConfig()
    instance.config.evaluation.enabled = False
    instance._ctx = SimpleNamespace(
        logger=logging.getLogger("affection-test"),
        message=SimpleNamespace(get_by_id=AsyncMock(return_value={"success": True, "message": None})),
        llm=SimpleNamespace(generate=AsyncMock()),
        call_capability=AsyncMock(return_value={"success": True, "messages": []}),
    )
    return instance


def message(mid="a", uid="123456", session="group-a", text="测试"):
    return {
        "platform": "qq", "session_id": session, "message_id": mid,
        "processed_plain_text": text,
        "message_info": {"user_info": {"user_id": uid, "user_nickname": "测试用户"}},
    }


def item(kind, text, mid="item"):
    return {
        "item_type": kind,
        "meta": {"item_id": mid, "logical_turn_id": None, "timestamp": "2026-10-15T10:00:00"},
        "parts": [{"type": "text", "text": text}],
    }


def planner_items():
    return [
        item("SystemMessageItem", "system", "system"),
        item("UserMessageItem", '<message msg_id="a" time="10:00" user="甲">正文', "a"),
        item("UserMessageItem", '<message msg_id="b" time="10:01" user="乙">正文', "b"),
        item("AssistantMessageItem", "reminder", "reminder"),
    ]


def test_planner_uses_items_and_preserves_other_kwargs():
    p = plugin()
    asyncio.run(p.index_message_speaker(message=message()))
    asyncio.run(p.index_message_speaker(message=message("b", "654321")))
    items = planner_items()
    originals = copy.deepcopy(items)
    result = asyncio.run(p.inject_affection_before_request(
        items=items, item_schema_version=1, session_id="group-a", tool_definitions=[{"name": "reply"}],
    ))
    payload = result["modified_kwargs"]
    assert payload["item_schema_version"] == 1
    assert payload["tool_definitions"] == [{"name": "reply"}]
    assert items[:-2] + items[-1:] == originals
    hint = items[-2]["parts"][0]["text"]
    assert "QQ=123456，msg_id=a" in hint
    assert "QQ=654321，msg_id=b" in hint
    assert "不创建或取消主人身份" in hint


def test_planner_deduplicates_user_attitude():
    p = plugin()
    asyncio.run(p.index_message_speaker(message=message()))
    asyncio.run(p.index_message_speaker(message=message("b")))
    items = planner_items()
    asyncio.run(p.inject_affection_before_request(items=items, session_id="group-a"))
    hint = items[-2]["parts"][0]["text"]
    assert "msg_id=a,b" in hint
    assert hint.count("【好感度提示】") == 1


def test_replyer_uses_target_not_latest_speaker():
    p = plugin()
    asyncio.run(p.index_message_speaker(message=message()))
    asyncio.run(p.index_message_speaker(message=message("b", "654321")))
    p._get_user_record("123456")["affection"] = -10
    p._get_user_record("654321")["affection"] = 90
    result = asyncio.run(p.inject_affection_into_reply(
        session_id="group-a", reply_message_id="a", extra_prompt="可信身份：主人", reply_args={"x": 1},
    ))
    kwargs = result["modified_kwargs"]
    assert kwargs["extra_prompt"].startswith("可信身份：主人")
    assert "-10" in kwargs["extra_prompt"]
    assert "90" not in kwargs["extra_prompt"]
    assert kwargs["reply_args"] == {"x": 1}
    p.ctx.message.get_by_id.assert_not_awaited()


def test_replyer_after_reload_uses_exact_message_lookup():
    p = plugin()
    p.ctx.message.get_by_id.return_value = {"success": True, "message": message()}
    result = asyncio.run(p.inject_affection_into_reply(
        session_id="group-a", reply_message_id="a", extra_prompt="",
    ))
    assert result is not None
    p.ctx.message.get_by_id.assert_awaited_once_with("a", chat_id="group-a")
    assert list(p._affection_data) == ["123456"]


def test_empty_reply_target_skips_lookup_and_record_creation():
    p = plugin()
    asyncio.run(p.index_message_speaker(message=message("b", "654321")))
    p.ctx.message.get_by_id.return_value = {"success": True, "message": message("b", "654321")}
    result = asyncio.run(p.inject_affection_into_reply(
        session_id="group-a", reply_message_id="", extra_prompt="original",
    ))
    assert result is None
    p.ctx.message.get_by_id.assert_not_called()
    assert not p._affection_data


def test_missing_reply_target_does_not_use_latest_user():
    p = plugin()
    asyncio.run(p.index_message_speaker(message=message("b", "654321")))
    result = asyncio.run(p.inject_affection_into_reply(
        session_id="group-a", reply_message_id="missing", extra_prompt="original",
    ))
    assert result is None
    assert not p._affection_data


def test_qq_key_is_shared_across_groups_and_nicknames_are_rejected():
    p = plugin()
    assert not inspect.iscoroutinefunction(p._resolve_user_id)
    assert p._resolve_user_id("测试用户") == ""
    assert p._resolve_user_id("person-sid") == ""
    assert p._resolve_user_id(" 123456 ") == "123456"
    p._get_user_record("123456")["affection"] = 50
    for session_id in ("group-a", "group-b"):
        asyncio.run(p.index_message_speaker(message=message(session=session_id)))
        items = planner_items()
        asyncio.run(p.inject_affection_before_request(items=items, session_id=session_id))
        assert "QQ=123456，msg_id=a" in items[-2]["parts"][0]["text"]
        assert "好感度为 50 点" in items[-2]["parts"][0]["text"]
    assert len(p._affection_data) == 1


def test_tools_require_explicit_qq_without_exposing_stream_context():
    p = plugin()
    asyncio.run(p.index_message_speaker(message=message()))
    for tool in (p.query_affection, p.adjust_affection):
        parameters = {param.name: param for param in tool.__maibot_component_info__.parameters}
        assert "stream_id" not in parameters
        assert parameters["user_id"].required
        assert "关系索引" in parameters["user_id"].description
        assert "可留空" not in parameters["user_id"].description
        signature = inspect.signature(tool)
        assert "stream_id" not in signature.parameters
        assert signature.parameters["kwargs"].kind is inspect.Parameter.VAR_KEYWORD
        result = asyncio.run(tool(stream_id="group-a", session_id="group-a", message_id="a"))
        assert not result["success"]
    assert not p._affection_data


def test_evaluation_includes_trigger_and_explicit_target(monkeypatch):
    p = plugin()
    trigger = message(text="触发消息尚未入库")
    p.ctx.call_capability.return_value = {"success": True, "messages": [message("b", "654321", text="其他人的话")]}
    p.ctx.llm.generate.return_value = {
        "success": True,
        "response": '{"need_change": true, "direction": "positive", "magnitude": 3, "reason": "主动关心"}',
    }
    update = AsyncMock(return_value={"success": True, "old_affection": 10, "new_affection": 11})
    monkeypatch.setattr(p, "_update_affection", update)
    asyncio.run(p._auto_evaluate("group-a", "123456", trigger))
    args, kwargs = p.ctx.llm.generate.call_args
    assert kwargs["task_name"] == "utils"
    assert "只评价 QQ=123456" in args[0]
    assert "QQ=654321" in args[0]
    assert args[0].index("其他人的话") < args[0].index("触发消息尚未入库")
    assert update.call_args.kwargs["user_id"] == "123456"


def test_evaluation_deduplicates_by_user_not_group(monkeypatch):
    p = plugin()
    p.config.evaluation.enabled = True

    async def exercise():
        gate = asyncio.Event()

        async def pending(*args):
            await gate.wait()

        monkeypatch.setattr(p, "_auto_evaluate", pending)
        p._schedule_auto_evaluation("group-a", "123456", message())
        p._schedule_auto_evaluation("group-b", "123456", message(session="group-b"))
        p._schedule_auto_evaluation("group-a", "654321", message("b", "654321"))
        assert set(p._evaluation_tasks) == {"123456", "654321"}
        tasks = list(p._evaluation_tasks.values())
        gate.set()
        await asyncio.gather(*tasks)

    asyncio.run(exercise())


def test_zero_injection_probability_disables_both_hooks():
    p = plugin()
    p.config.call_behavior.inject_probability = 0
    assert asyncio.run(p.inject_affection_before_request(items=planner_items(), session_id="group-a")) is None
    assert asyncio.run(p.inject_affection_into_reply(session_id="group-a", reply_message_id="a", extra_prompt="")) is None
    p.ctx.message.get_by_id.assert_not_awaited()
