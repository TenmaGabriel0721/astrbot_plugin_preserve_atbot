# 保留 @Bot 上下文

`astrbot_plugin_preserve_atbot` 是一个 AstrBot 群聊上下文增强插件。

AstrBot 的平台适配器可能在生成纯文本消息时移除用于唤醒机器人的 At 消息段，导致 LLM 只能看到 At 后面的文字。本插件会在 LLM 请求发送前重新识别 `@bot`，并将其转换为 `@机器人名称` 形式的普通文本。这样可以有效解决使用了主动回复类插件后，bot非常容易误将别人随手发的信息当成是对自己说的。

## 功能特性

- 仅处理群聊消息，不影响私聊。
- 只识别指向机器人自身的 At，忽略 `@其他人` 和 `@全体成员`。
- 在 `on_llm_request` 阶段直接更新 `ProviderRequest.prompt`。
- 支持 QQ 官方 API/Webhook 和 OneBot/aiocqhttp。
- 支持 AstrBot `At` 组件、QQ 官方 `mentions`/`<@id>`、OneBot JSON 消息段与 CQ 码回退识别。
- 可自定义机器人显示名称和注入文本模板。
- 可选同步更新 `event.message_str`，方便后续插件读取。
- 自带重复注入保护，避免生成重复的 `@Bot @Bot`。

## 效果示例

用户在群聊中发送：

```text
@珈百璃 你看到我在叫你吗？
```

即使平台生成的纯文本只剩下“你看到我在叫你吗？”，插件也会在 LLM 请求前恢复为：

```text
@珈百璃 你看到我在叫你吗？
```

## 安装

1. 将插件目录放入 AstrBot 的 `data/plugins/` 目录。
2. 在 AstrBot 管理面板中重载插件，或重启 AstrBot。
3. 启用“保留 @Bot 上下文”插件。

插件启用后会自动工作，无需额外命令。

## 配置说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `bot_display_name` | `Bot` | 无法从平台获取昵称时使用的名称 |
| `prefer_detected_name` | `true` | 优先使用平台 `At` 组件提供的机器人昵称 |
| `marker_template` | `@{name}` | 注入文本模板，必须包含 `{name}` |
| `platforms` | QQ 官方与 aiocqhttp | 允许运行的平台名称列表 |
| `sync_event_message` | `true` | 是否同步更新事件纯文本 |
| `debug_log` | `false` | 是否记录每次成功注入的来源与目标 ID |

### 固定显示名称

如果希望始终注入 `@珈百璃`，而不使用平台昵称，可配置：

```json
{
  "bot_display_name": "珈百璃",
  "prefer_detected_name": false,
  "marker_template": "@{name}"
}
```

`marker_template` 也可以改为 `[用户正在@{name}]` 等更明确的提示文本。

## 调试与故障排查

开启 `debug_log` 后，成功注入时会输出类似日志：

```text
[astrbot_plugin_preserve_atbot] 已注入 @珈百璃，platform=aiocqhttp source=message_chain target=123456789
```

如果没有观察到注入，请检查：

1. 消息是否为群聊消息。
2. At 目标是否为当前机器人。
3. 当前平台名称是否在 `platforms` 列表内。
4. AstrBot 日志中是否出现插件加载成功信息。
5. 开启 `debug_log` 后重新发送一条 `@bot` 消息。

## 注意事项

- 本插件只保留 At 的文本语义，不负责决定 AstrBot 是否唤醒或回复消息。
- 开启 `sync_event_message` 后，低优先级的后续插件也可能读到恢复后的 `event.message_str`。
- 启用 `prefer_detected_name` 时，平台提供的昵称会覆盖 `bot_display_name`。

## 作者

[TenmaGabriel0721](https://github.com/TenmaGabriel0721)
