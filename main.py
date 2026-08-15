"""检测纯五位数字并通过 OneBot v11 修改 QQ 群名称。"""

import asyncio
import json
import os
import re
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


FIVE_DIGITS = re.compile(r"^[0-9]{5}$")


@register(
    "astrbot_plugin_pjsk_autochange_groupname",
    "Glaceon471",
    "检测纯五位数字并自动修改 OneBot QQ 群名",
    "1.0.0",
)
class AutoChangeGroupNamePlugin(Star):
    """每个群可独立记录原群名，并由白名单和黑名单决定是否启用。"""


    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._rename_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._state_path = os.path.join(
            get_astrbot_plugin_data_path(),
            "astrbot_plugin_pjsk_autochange_groupname",
            "original_group_names.json",
        )
        self._original_group_names = self._load_original_names()


    @staticmethod
    def _is_group_message(event: AstrMessageEvent) -> bool:
        return bool(event.get_group_id()) and not event.is_private_chat()


    @staticmethod
    def _is_exact_plain_text(event: AstrMessageEvent) -> bool:
        """只接受单一纯文本消息，避免图片、@ 等消息链意外触发。"""
        messages = event.get_messages()
        return (
            len(messages) == 1
            and isinstance(messages[0], Plain)
            and messages[0].text == event.message_str
        )


    async def _save_config(self) -> None:
        """兼容旧版 AstrBot 没有异步保存接口的情况。"""
        save_async = getattr(self.config, "save_config_async", None)
        if save_async:
            await save_async()
        else:
            self.config.save_config()


    def _load_original_names(self) -> dict[str, str]:
        try:
            with open(self._state_path, encoding="utf-8") as file:
                content = json.load(file)
            if isinstance(content, dict):
                return {
                    str(group_id): name
                    for group_id, name in content.items()
                    if isinstance(name, str) and name
                }
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取原群名记录失败，将使用空记录：%s", exc)
        return {}


    async def _save_original_names(self) -> None:
        def save() -> None:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            temp_path = f"{self._state_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as file:
                json.dump(self._original_group_names, file, ensure_ascii=False, indent=2)
            os.replace(temp_path, self._state_path)

        await asyncio.to_thread(save)


    def _get_original_name(self, group_id: str) -> str:
        saved_name = self._original_group_names.get(str(group_id), "")
        if isinstance(saved_name, str) and saved_name:
            return saved_name
        default_name = self.config.get("default_original_group_name", "")
        return default_name if isinstance(default_name, str) else ""


    def _group_id_list(self, config_key: str) -> list[str]:
        """读取并规范化面板中的群号列表。"""
        value = self.config.get(config_key, [])
        if not isinstance(value, list):
            return []
        return [str(group_id) for group_id in value if str(group_id).strip()]


    def _is_group_enabled(self, group_id: str) -> bool:
        """黑名单优先；其余群必须显式位于白名单中。"""
        return (
            str(group_id) in self._group_id_list("enabled_group_ids")
            and str(group_id) not in self._group_id_list("disabled_group_ids")
        )


    async def _can_manage(self, event: AstrMessageEvent) -> bool:
        """允许 AstrBot 管理员、QQ 群主和 QQ 群管理员操作。"""
        if event.is_admin():
            return True
        if not self._is_group_message(event) or event.get_platform_name() != "aiocqhttp":
            return False
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "api"):
            return False
        args: dict[str, Any] = {
            "group_id": int(event.get_group_id()),
            "user_id": int(event.get_sender_id()),
            "no_cache": True,
        }
        if event.get_self_id():
            args["self_id"] = event.get_self_id()
        try:
            info = await bot.api.call_action("get_group_member_info", **args)
            if isinstance(info, dict) and isinstance(info.get("data"), dict):
                info = info["data"]
            return isinstance(info, dict) and info.get("role") in {"owner", "admin"}
        except Exception:
            logger.warning("无法确认群管理权限，群号=%s", event.get_group_id())
            return False


    async def _require_enabled_group(self, event: AstrMessageEvent):
        if not self._is_group_message(event):
            return event.plain_result("请在 QQ 群内使用此命令。")
        if not self._is_group_enabled(str(event.get_group_id())):
            return event.plain_result("本群尚未启用，请先发送：/enable 改群名。请注意单群里不要有多个bot同时开启此插件，避免冲突")
        return None


    def _custom_pattern(self, group_id: str) -> str:
        rules = self.config.get("custom_match_rules", [])
        if not isinstance(rules, list):
            return ""
        for rule in rules:
            if not isinstance(rule, dict) or str(rule.get("group_id", "")) != str(group_id):
                continue
            pattern = rule.get("pattern", "")
            if isinstance(pattern, str) and pattern.count("<>") == 1:
                return pattern
        return ""


    async def _rename_group(self, event: AstrMessageEvent, new_name: str) -> str | None:
        """调用 OneBot 改群名；失败时返回可展示的错误信息。"""
        if event.get_platform_name() != "aiocqhttp":
            return "自动改群名仅支持 OneBot v11（aiocqhttp）QQ 适配器。"
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "api"):
            return "无法取得 OneBot API，未修改群名。"
        args: dict[str, Any] = {"group_id": int(event.get_group_id()), "group_name": new_name}
        if event.get_self_id():
            args["self_id"] = event.get_self_id()
        try:
            async with self._rename_lock:
                await bot.api.call_action("set_group_name", **args)
        except Exception:
            logger.exception("修改群名失败，群号=%s", event.get_group_id())
            return "修改群名失败。请确认机器人在该群拥有管理员或群主权限，且 OneBot 实现支持 set_group_name。"
        return None


    @filter.command("enable", alias={"启用改群名"})
    async def enable(self, event: AstrMessageEvent, feature: str = ""):
        """用法：/enable 改群名。将当前群加入白名单。"""
        if not await self._can_manage(event):
            yield event.plain_result("只有群主或群管理员可以操作。")
            return
        if feature != "改群名":
            yield event.plain_result("自动改群名启动用法：/enable 改群名")
            return
        if not self._is_group_message(event):
            yield event.plain_result("请在需要启用的 QQ 群内使用此命令。")
            return
        group_id = str(event.get_group_id())
        whitelist = self._group_id_list("enabled_group_ids")
        blacklist = self._group_id_list("disabled_group_ids")
        if group_id not in whitelist:
            whitelist.append(group_id)
        self.config["enabled_group_ids"] = whitelist
        self.config["disabled_group_ids"] = [item for item in blacklist if item != group_id]
        await self._save_config()
        yield event.plain_result("本群自动改群名已启用。")


    @filter.command("disable", alias={"禁用改群名"})
    async def disable(self, event: AstrMessageEvent, feature: str = ""):
        """用法：/disable 改群名。将当前群加入黑名单。"""
        if not await self._can_manage(event):
            yield event.plain_result("只有群主或群管理员可以操作。")
            return
        if feature != "改群名":
            yield event.plain_result("自动改群名禁用用法：/disable 改群名")
            return
        disabled_message = await self._require_enabled_group(event)
        if disabled_message:
            yield disabled_message
            return
        group_id = str(event.get_group_id())
        whitelist = self._group_id_list("enabled_group_ids")
        blacklist = self._group_id_list("disabled_group_ids")
        if group_id not in blacklist:
            blacklist.append(group_id)
        self.config["enabled_group_ids"] = [item for item in whitelist if item != group_id]
        self.config["disabled_group_ids"] = blacklist
        await self._save_config()
        yield event.plain_result("本群自动改群名已禁用。")


    @filter.command("原群名")
    async def set_original_group_name(self, event: AstrMessageEvent, group_name: GreedyStr):
        """用法：/原群名 <群名>。在当前群记录原群名。"""
        if not await self._can_manage(event):
            yield event.plain_result("只有群主或群管理员可以操作。")
            return
        disabled_message = await self._require_enabled_group(event)
        if disabled_message:
            yield disabled_message
            return
        async with self._state_lock:
            self._original_group_names[str(event.get_group_id())] = group_name
            await self._save_original_names()
        yield event.plain_result(f"已记录本群原群名：{group_name}")


    @filter.command("自定义匹配")
    async def set_custom_match(self, event: AstrMessageEvent, pattern: GreedyStr):
        """用法：/自定义匹配 那<>天的啤酒烧烤起来。<> 将替换为五位数字。"""
        if not await self._can_manage(event):
            yield event.plain_result("只有群主或群管理员可以操作。")
            return
        disabled_message = await self._require_enabled_group(event)
        if disabled_message:
            yield disabled_message
            return
        if pattern.count("<>") != 1:
            yield event.plain_result("自定义匹配必须且只能包含一个 <> 占位符，例如：/自定义匹配 那<>天的啤酒烧烤起来")
            return
        group_id = str(event.get_group_id())
        rules = self.config.get("custom_match_rules", [])
        if not isinstance(rules, list):
            rules = []
        rules = [rule for rule in rules if not isinstance(rule, dict) or str(rule.get("group_id", "")) != group_id]
        rules.append({"__template_key": "group_rule", "group_id": group_id, "pattern": pattern})
        self.config["custom_match_rules"] = rules
        await self._save_config()
        yield event.plain_result(f"已设置本群自定义匹配：{pattern}")


    @filter.command("还原")
    async def restore_group_name(self, event: AstrMessageEvent):
        """将当前群名还原为记录的原群名。"""
        if not await self._can_manage(event):
            yield event.plain_result("只有群主或群管理员可以操作。")
            return
        disabled_message = await self._require_enabled_group(event)
        if disabled_message:
            yield disabled_message
            return
        original_name = self._get_original_name(str(event.get_group_id()))
        if not original_name:
            yield event.plain_result("尚未设置原群名，请先发送：/原群名 <群名>")
            return
        error = await self._rename_group(event, original_name)
        yield event.plain_result(error or f"已还原群名：{original_name}")


    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def change_group_name_on_five_digits(self, event: AstrMessageEvent):
        """收到严格匹配的五位 ASCII 数字时修改当前 QQ 群名。"""
        if not self._is_exact_plain_text(event) or not FIVE_DIGITS.fullmatch(event.message_str):
            return
        if not self._is_group_enabled(str(event.get_group_id())):
            yield event.plain_result("本群尚未启用，请先发送：/enable 改群名")
            return

        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("自动改群名仅支持 OneBot v11（aiocqhttp）QQ 适配器。")
            return

        custom_pattern = self._custom_pattern(str(event.get_group_id()))
        original_name = self._get_original_name(str(event.get_group_id()))
        if not custom_pattern and not original_name:
            yield event.plain_result("尚未设置原群名，请群主或群管理员先发送：/原群名 <群名>")
            return
        new_name = custom_pattern.replace("<>", event.message_str) if custom_pattern else f"{event.message_str} ({original_name})"
        error = await self._rename_group(event, new_name)
        yield event.plain_result(error or f"已将群名修改为：{new_name}")
