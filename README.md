# Life Companion for AstrBot

一个独立的 AstrBot 生活状态插件。它每天生成一份连续、可执行的生活安排，并把早上、中午、下午、晚上的分时段穿搭、时间线和主要活动提供给对话与生图插件使用。

## 它解决什么问题

- 以日期、节日、人格、历史日程和近期会话为上下文生成每日状态。
- 强制模型返回结构化时间线，Bot 可以根据当前时间回答“现在在做什么”。
- 自动生成 `image_prompt`，用于参考照自拍或今日生活照。
- 每天生成四套分时段穿搭，按当前时间向对话和生图插件提供对应穿搭。
- 生成失败时不污染缓存；重写会自动保留历史版本，支持回滚。
- `get_life_context(allow_generate=False)` 是严格只读接口，生图插件读取缓存时不会额外消耗 LLM。
- 独立使用 `astrbot_plugin_life_companion` 数据目录，不覆盖其他日程插件。

分时段穿搭的时间范围为：早上 05:00-10:59，中午 11:00-13:59，下午
14:00-17:59，晚上 18:00-次日 04:59。生图插件继续读取
`get_life_context()["outfit"]`，该字段会根据调用时刻返回当前时段穿搭；完整四段数据在
`get_life_context()["outfits"]` 中。

## 安装

将本目录放入 AstrBot 的 `data/plugins/astrbot_plugin_life_companion`，安装依赖后重启 AstrBot：

```bash
pip install -r requirements.txt
```

如果使用的是 Docker，插件目录通常对应宿主机的 `/root/data/plugins/astrbot_plugin_life_companion`。

## 与 gitee_aiimg 自拍适配

本插件已提供 `今日生活照` 命令，调用已安装的 `astrbot_plugin_gitee_aiimg` 自拍链路。使用前请先在生图插件中配置自拍参考照。

为了让生图插件的普通 `/自拍` 也自动读取本插件的状态，请应用仓库中的：

```text
integrations/gitee_aiimg_life_companion.patch
```

该补丁针对 `gitee_aiimg v5.1.30`，修改范围只有生活上下文发现和自拍提示词构造；生图插件不存在或不兼容时仍会自动降级。插件升级后如果补丁冲突，应重新检查对应方法，而不是覆盖整个生图插件。服务器中应用时在容器内执行：`patch -p0 < /tmp/gitee_aiimg_life_companion.patch`。

## 命令

| 命令 | 权限 | 作用 |
| --- | --- | --- |
| `查看日程` | 所有人 | 查看今日穿搭、日程和结构化时间线 |
| `查看日程 YYYY-MM-DD` | 所有人 | 查看已缓存的历史日期 |
| `重写日程 [补充要求]` | 管理员 | 重写今日状态，补充要求优先级最高 |
| `今日生活照` | 所有人 | 使用今日穿搭和活动场景调用生图插件自拍 |
| `日程历史 [数量]` | 所有人 | 查看今日可回滚版本 |
| `回滚日程 <编号>` | 管理员 | 回滚到历史版本 |
| `导入旧日程 [覆盖]` | 管理员 | 从旧插件数据目录导入兼容的 JSON 日程 |
| `日程时间 HH:MM` | 管理员 | 设置每日自动生成时间 |
| `参考会话 [set\|show\|clear]` | 管理员 | 设置生成时参考的默认会话 |

## 数据与配置

数据保存在 AstrBot 的插件数据目录：

- `schedule_data.json`：当前每日状态。
- `schedule_history.json`：每个日期最多保留 10 个旧版本。

配置项包括自动生成时间、历史参考天数、近期会话数量、LLM 供应商、创意池和 Prompt 模板。默认模板要求模型返回：

```json
{
  "outfit_style": "...",
  "outfit": "全天穿搭概览...",
  "outfits": {
    "morning": {"style": "...", "description": "..."},
    "noon": {"style": "...", "description": "..."},
    "afternoon": {"style": "...", "description": "..."},
    "evening": {"style": "...", "description": "..."}
  },
  "schedule": "...",
  "timeline": [{"time": "08:00", "activity": "...", "location": "..."}],
  "image_prompt": "..."
}
```

旧数据可以通过命令导入；导入不会默认覆盖当天已有的新数据。新旧插件同时安装时，建议确认新插件工作正常后停用旧插件，避免两个插件同时注入不同生活状态。

## 开发检查

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```
