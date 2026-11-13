# 表情包感知

> 还原被丢弃的 sub_type，纠正模型把表情包当真实照片

## 这是什么

NapCat 在 CQ 码里带 sub_type（1=表情包/7=贴图）与 summary，但 AstrBot 的 Image 组件只保留 file/url/path。本插件从原始事件捞回这两个字段，判定「这是表情包」，并以临时内容注入本轮请求，明确禁止模型追问拍摄地点、衣着、气温等只适用于真实照片的问题。可选开启视觉描述。不接管上下文、不改人格。

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/xiaoli0412/astrbot_plugin_sticker_sense.git
```

然后在 WebUI 插件管理里启用，或重启 AstrBot。

## 配置

全部配置都能在 **WebUI → 插件 → 表情包感知 → 配置** 里改，不用手改文件。

| 键 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `True` | 关闭后不捕获也不注入。 |
| `hint_enabled` | bool | `True` | 开启后，本轮含表情包时会告诉模型「这是表情包不是照片」，并禁止追问拍摄地点/衣着/天气。这是修复「把表情包当照片」的核心开关。 |
| `vision_enabled` | bool | `False` | 默认关闭以节省内存与 token。开启后会用当前会话的多模态模型描述表情包情绪，同一张图 10 分钟内不重复调用。 |
| `log_detect` | bool | `True` | 在日志里记录每张图片的 sub_type 与判定结果，便于排查。 |

## 依赖

无第三方依赖（纯标准库）

## 兼容性

- 平台：aiocqhttp
- AstrBot：`>=4.24,<5`

## 隐私说明

插件只在本机运行，不上传任何数据到第三方。配置与运行时数据存在 `AstrBot/data/` 下。

## 许可

MIT，见 [LICENSE](LICENSE)。
