<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="see 为任何不支持多模态的模型补充原生图片与视频理解">
</p>

`see` 让任何不支持多模态的模型直接查看图片和视频。图片默认交给 Qwen3.7 Plus；视频优先交给 Gemini 3.1 Flash-Lite，平台不可用时使用 Qwen3.7 Plus。

## 安装

把下面这句话发给 Codex：

```text
安装 https://github.com/oil-oil/see-skill skill
```

安装完成后说：

```text
帮我配置 see
```

Codex 会启动 onboard。选择供应商后，在隐藏输入框中填写 API Key；Key 不需要发到聊天里，也不会写进 Skill 或项目仓库。

没有多模态 Key 也能使用，onboard 时选择 `local` 即可。

## 直接使用

像平常一样告诉 AI 图片在哪里、想看什么：

```text
看看 /path/to/screenshot.png
```

```text
识别 /path/to/error.png 里的报错，并告诉我怎么修
```

```text
并行查看 a.png、b.png、c.png
```

```text
比较 before.png 和 after.png 的界面变化
```

```text
总结 /path/to/demo.mp4 的内容
```

AI 只需要调用一个脚本：

```bash
# 单图
see/scripts/see.sh screenshot.png

# 多图独立分析，默认并行
see/scripts/see.sh a.png b.png c.png

# 多图联合理解
see/scripts/see.sh --together before.png after.png --task "比较界面变化"

# 完整视频理解
see/scripts/see.sh demo.mp4
```

## 为什么更接近原生视觉

- 图片原图直接发送，不预先 OCR、缩放或压缩。
- 视频保持完整时间线和音频，直接使用模型原生视频能力，不在 Skill 内抽帧。
- 4K、高帧率或大体积视频自动压缩为清晰的 H.264 MP4，减少上传时间。
- 用户问题通过 `--task` 原样交给视觉模型，不套固定报告模板。
- 视觉模型同时理解对象、布局、空间关系、界面状态和文字。
- 云端结果直接返回给主模型，减少“先泛化描述、再二次推理”的信息损失。

纯文本主模型最终仍然接收文字结果，因此不可能和同一个模型原生拥有视觉完全相同；但这是外接视觉模型时信息损失最少的方式。

## 并行与联合

| 场景 | 模式 | 行为 |
|---|---|---|
| 多张图片互不相关 | 默认 | 并行请求，结果按输入顺序汇总 |
| 前后对比、连续截图、组合证据 | `--together` | 所有原图进入同一次多模态请求 |
| 只有本地能力可用 | 自动降级 | 多张图片继续并行分析 |

## 供应商

| 供应商 | 默认模型 | Key 变量 |
|---|---|---|
| ZenMux | `qwen/qwen3.7-plus` | `ZENMUX_API_KEY` |
| 百炼 | `qwen3.7-plus` | `DASHSCOPE_API_KEY` |
| OpenRouter | `qwen/qwen3.7-plus` | `OPENROUTER_API_KEY` |
| TokenDance | `qwen3.7-plus` | `TOKENDANCE_API_KEY` |
| 本地 | 系统视觉 / OCR | 不需要 |

图片会按配置顺序尝试供应商，全部失败才进入本地视觉分析；视频使用下方的独立路由。

视频模型自动选择：

| 供应商 | 默认视频模型 | 输入 |
|---|---|---|
| ZenMux | `google/gemini-3.1-flash-lite` | 完整视频 + 音频 |
| OpenRouter | `google/gemini-3.1-flash-lite` | 完整视频 + 音频 |
| 百炼 | `qwen3.7-plus` | 完整视频 |
| TokenDance | `qwen3.7-plus` | 完整视频 |

视频不会降级为抽帧；没有支持视频的云端 Key 时会直接提示配置。

## Onboard 与 Key 保存

Onboard 可重复运行，用于添加供应商、更换默认路由或切换成本地模式：

```bash
python3 see/scripts/onboard.py
python3 see/scripts/onboard.py --status
```

私有配置位置：

- macOS / Linux：`~/.config/see/config.env`
- Windows：`%APPDATA%\see\config.env`

配置文件以明文环境变量格式保存在本机，并限制为仅当前用户可读写。环境变量优先级最高，适合 CI 或不希望落盘的用户；项目 `.env.local` 其次，用户私有配置最后。

高级用户也可以直接设置：

```bash
export SEE_PROVIDER=zenmux
export ZENMUX_API_KEY=你的Key
```

不要把真实 Key 提交到 Git。

## 本地降级

```text
macOS   → Vision 图像分析 + OCR → Tesseract
Windows → Windows OCR  → Tesseract
Linux   → Tesseract
```

macOS Vision 和 Windows OCR 使用系统内置能力，不需要额外 Key。Tesseract 是最后兜底，需要用户自行安装。

macOS 还会返回场景分类、人物/人脸、条码和基础图形结构；这些线索仍不能替代多模态模型的完整语义理解。Windows 和 Linux 目前以 OCR 为主。

## 参数

日常使用只需要图片路径。其余参数按需使用：

| 参数 | 用途 |
|---|---|
| `--task "问题"` | 原样发送给视觉模型 |
| `--together` | 多图放进同一次请求 |
| `--jobs 4` | 多图并发数 |
| `--provider NAME` | 临时指定供应商 |
| `--model NAME` | 临时覆盖当前媒体模型 |
| `--ocr-backend system` | 指定本地系统能力 |
| `-o result.md` | 指定结果文件 |

成功后 stdout 只输出：

```text
output_path=/absolute/path/result.md
```

结果 Markdown 会记录实际后端、模型、单图/并行/联合模式，以及每次路由是否成功，方便 AI 判断有没有发生降级。

## 支持范围

- 支持本地图片、视频和 HTTP / HTTPS URL。
- 支持多文件并行；`--together` 用于多图联合理解。
- 视频自动压缩为最长边 1920、2 fps、H.264 和 AAC；超出上传预算时自动切换紧凑档。
- 下载上限为 512 MB。
- 不负责网页视频提取。
- 需要 Python 3；云端调用不依赖第三方 Python 包。
- 视频压缩需要 FFmpeg。

## 文件结构

```text
see/
├── SKILL.md
├── agents/
│   └── openai.yaml
└── scripts/
    ├── see.sh
    ├── onboard.py
    ├── parse_media.py
    ├── ocr_macos.swift
    └── ocr_windows.ps1
```

## License

[MIT](./LICENSE) © 2026 oil-oil
