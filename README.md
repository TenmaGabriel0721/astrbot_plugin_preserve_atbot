# 保留 @Bot 与引用上下文

`astrbot_plugin_preserve_atbot` 是一个 AstrBot 群聊上下文增强插件。

AstrBot 的平台适配器可能在生成纯文本消息时移除用于唤醒机器人的 At 消息段，导致 LLM 只能看到 At 后面的文字。本插件会在 LLM 请求发送前重新识别 `@bot`，并将其转换为 `@机器人名称` 形式的普通文本。

对于 OneBot/aiocqhttp，若引用消息退化为仅包含消息 ID、缺少发送人信息，插件还会通过 `get_msg` 补全 `Reply.sender_id`、`Reply.sender_nickname` 及可恢复的消息链，使纯图片引用也能向 LLM 同时展示被引用消息发送人的昵称和 QQ 号，例如 `(Alice, QQ: 10001): [Image]`。

## 功能特性

- 仅处理群聊消息，不影响私聊。
- 只识别指向机器人自身的 At，忽略 `@其他人` 和 `@全体成员`。
- 在 `on_llm_request` 阶段直接更新 `ProviderRequest.prompt`。
- 支持 QQ 官方 API/Webhook 和 OneBot/aiocqhttp。
- 支持 AstrBot `At` 组件、QQ 官方 `mentions`/`<@id>`、OneBot JSON 消息段与 CQ 码回退识别。
- 可自定义机器人显示名称和注入文本模板。
- 可选同步更新 `event.message_str`，方便后续插件读取。
- 自带重复注入保护，避免生成重复的 `@Bot @Bot`。
- 在 OneBot/aiocqhttp 的降级路径中，通过 `get_msg` 补全引用消息发送人的 ID 和昵称。
- 可恢复引用消息中的文本及图片消息链，纯图片引用也能保留发送人语义。
- 在 `on_llm_request` 阶段同步修正已经构建好的 `<Quoted Message>` 内容，不需要修改 AstrBot 核心代码。

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
| `marker_template` | `@{name}` | 注入文本模板；`{name}` 会包含机器人昵称和 QQ 号 |
| `platforms` | QQ 官方与 aiocqhttp | 允许运行的平台名称列表 |
| `sync_event_message` | `true` | 是否同步更新事件纯文本 |
| `debug_log` | `false` | 是否记录每次成功注入的来源与目标 ID |
| `enhance_quote_sender` | `true` | 是否通过 OneBot `get_msg` 补全引用消息发送人 |
| `quote_sender_fallback_name` | `Unknown User` | `get_msg` 未提供昵称和 ID 时使用的回退名称；留空则不强制补全 |
| `quote_sender_timeout` | `5.0` | 单次 `get_msg` 调用超时，范围限制为 0.1～30 秒 |

### 固定显示名称

如果希望始终使用昵称 `珈百璃`，同时保留平台检测到的机器人 QQ 号，可配置：

```json
{
  "bot_display_name": "珈百璃",
  "prefer_detected_name": false,
  "marker_template": "@{name}"
}
```

`marker_template` 也可以改为 `[用户正在@{name}]` 等更明确的提示文本。默认格式的实际效果为：

```text
@珈百璃(QQ: 123456789) 消息正文
```

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
6. 若引用发送人没有补全，确认 OneBot 实现支持 `get_msg`，并检查日志中的 `reply_id` 与调用错误。

引用补全只在 `Reply.sender_nickname` 与有效 `sender_id` 都缺失时发起远程查询，不会重复拉取适配器已经完整解析的引用消息。

## 注意事项

- 本插件只保留 At 的文本语义，不负责决定 AstrBot 是否唤醒或回复消息。
- 开启 `sync_event_message` 后，低优先级的后续插件也可能读到恢复后的 `event.message_str`。
- 启用 `prefer_detected_name` 时，平台提供的昵称会覆盖 `bot_display_name`。

## 作者

[TenmaGabriel0721](https://github.com/TenmaGabriel0721)
