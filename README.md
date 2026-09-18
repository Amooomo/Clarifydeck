# ClarifyDeck

> Steam Deck 游戏内文字 OCR 辅助插件 / An OCR accessibility plugin for in-game text on Steam Deck.

[中文](#中文) · [English](#english)

---

# 中文

## 简介

**ClarifyDeck** 是一款面向 Steam Deck 的 Decky Loader 插件，用于识别游戏画面中的小字、字幕、菜单文字等内容，并将识别结果以更清晰的文本框覆盖显示在游戏画面上。

你可以自行设置需要识别的屏幕区域，并为不同游戏或场景保存不同的 Profile / Region。ClarifyDeck 会在后台对指定区域进行 OCR 识别，并通过高对比度文本框显示结果，帮助提升 Steam Deck 小屏幕下的文字可读性。

### 主要功能

- 自定义 OCR 识别区域
- 支持多个 Profile 和多个 Region
- 支持设置 Primary Region
- Region Preview 区域预览
- 深色 / 浅色文本框
- 文本框透明度调节
- 文本大小调节
- Persistent Overlay 持续显示识别结果
- 在 Steam Deck QAM 中直接控制 OCR 和 Overlay
- 当前 OCR 方案基于 RapidOCR + PP-OCRv6 + ONNX Runtime

---

## 安装

### 1. 安装 Decky Loader

ClarifyDeck 需要 **Decky Loader** 才能运行。

如果尚未安装 Decky Loader，请先按照 Decky Loader 官方说明完成安装。

### 2. 下载 ClarifyDeck

进入本项目 GitHub 的 **Releases** 页面，下载：

```text
ClarifyDeck-v0.1.0-SteamDeck.zip
```

> 请不要使用 GitHub 自动生成的 `Source code (zip)`，它只包含源码，不是完整的 Steam Deck 可安装版本。

### 3. 安装插件

1. 在 Steam Deck 中切换到桌面模式。
2. 解压 `ClarifyDeck-v0.1.0-SteamDeck.zip`。
3. 将插件文件放入 Decky Loader 的插件目录，通常为：

```text
/home/deck/homebrew/plugins/ClarifyDeck
```

最终应类似：

```text
/home/deck/homebrew/plugins/ClarifyDeck/
├── main.py
├── plugin.json
├── dist/
├── runtime/
├── models/
└── ...
```

4. 重载 Decky Loader，或重新启动 Steam Deck。
5. 返回游戏模式，在 QAM 的 Decky 插件列表中打开 **ClarifyDeck**。

---

## 使用方法

### OCR 页面

打开 ClarifyDeck 后，可以在 OCR 页面：

- 启动 / 停止 OCR
- 启用 / 禁用 Persistent Overlay
- 查看当前 OCR 运行状态

### Regions 页面

在 Regions 页面可以：

- 新建、删除和切换 Profile
- 新建、删除和切换 Region
- 设置 Primary Region
- 开启 / 关闭 Region Preview
- 调整识别区域的 X / Y / W / H
- 开启或关闭某个 Region
- 选择 Dark / Light 文本框
- 调整 Panel Opacity
- 调整 Text Size
- 点击 **Save Changes** 保存设置

建议先开启 **Show Region Preview**，确认识别区域覆盖了需要识别的文字位置，再开始 OCR。

---

## 研发测试说明

ClarifyDeck 当前仍处于 **研发测试阶段**。

虽然当前版本已在 Steam Deck 实机上完成多轮功能和稳定性测试，但仍可能存在：

- 个别游戏或 SteamOS / Decky Loader 版本兼容问题
- OCR 识别准确率差异
- 特殊字体、动态字幕或复杂背景下识别效果下降
- 未发现的 UI 或运行时问题

本项目并非商业软件，也不提供任何商业级稳定性保证。

如果你发现 Bug、兼容性问题或有功能建议，欢迎通过 GitHub Issues 反馈。

---

## 非商业使用声明

本项目仅供：

- 学习
- 研究
- 个人使用
- 非商业测试

**未经作者明确书面许可，禁止利用本项目及其修改版本进行商业销售、付费分发、收费服务或其他以商业获利为目的的使用。**

如果需要进行商业合作或商业使用，请先联系项目作者并获得许可。

> README 中的声明用于说明项目使用要求；正式发布时建议同时以仓库中的 `LICENSE` 文件为准。

---

## 支持项目

如果 ClarifyDeck 对你有帮助，欢迎点击 GitHub 右上角的 **⭐ Star** 支持本项目。

你的 Star、Bug 反馈和建议都会帮助 ClarifyDeck 继续完善。

感谢支持！

---

# English

## About

**ClarifyDeck** is a Decky Loader plugin for Steam Deck that helps make small in-game text easier to read.

You can define custom OCR regions for subtitles, menus, dialogue, UI text, or other parts of the game screen. ClarifyDeck recognizes text from those regions in the background and displays the result through a high-contrast persistent overlay.

Different Profiles and Regions can be created for different games or situations.

### Features

- Custom OCR regions
- Multiple Profiles and Regions
- Primary Region support
- Region Preview
- Dark / Light text panels
- Adjustable panel opacity
- Adjustable text size
- Persistent OCR overlay
- OCR and Overlay controls directly from the Steam Deck QAM
- Current OCR stack: RapidOCR + PP-OCRv6 + ONNX Runtime

---

## Installation

### 1. Install Decky Loader

ClarifyDeck requires **Decky Loader**.

If Decky Loader is not installed yet, install it first by following the official Decky Loader instructions.

### 2. Download ClarifyDeck

Go to the **Releases** page of this GitHub repository and download:

```text
ClarifyDeck-v0.1.0-SteamDeck.zip
```

> Do not use GitHub's automatically generated `Source code (zip)`. It contains the source code only and is not the complete Steam Deck build.

### 3. Install the plugin

1. Switch your Steam Deck to Desktop Mode.
2. Extract `ClarifyDeck-v0.1.0-SteamDeck.zip`.
3. Place the plugin files in the Decky Loader plugin directory, usually:

```text
/home/deck/homebrew/plugins/ClarifyDeck
```

The final structure should look similar to:

```text
/home/deck/homebrew/plugins/ClarifyDeck/
├── main.py
├── plugin.json
├── dist/
├── runtime/
├── models/
└── ...
```

4. Reload Decky Loader or restart your Steam Deck.
5. Return to Gaming Mode and open **ClarifyDeck** from the Decky section of the QAM.

---

## How to Use

### OCR page

From the OCR page you can:

- Start / Stop OCR
- Enable / Disable the Persistent Overlay
- Check the current OCR status

### Regions page

From the Regions page you can:

- Create, delete, and switch Profiles
- Create, delete, and switch Regions
- Set a Primary Region
- Show / Hide Region Preview
- Adjust X / Y / W / H
- Enable or disable individual Regions
- Choose a Dark or Light panel
- Adjust Panel Opacity
- Adjust Text Size
- Use **Save Changes** to save the configuration

It is recommended to enable **Show Region Preview** first and make sure the region covers the in-game text you want to recognize.

---

## Development & Testing Status

ClarifyDeck is currently a **research and testing project**.

The current version has gone through multiple rounds of real-device testing on Steam Deck, but issues may still exist, including:

- Compatibility differences between games, SteamOS versions, or Decky Loader versions
- OCR accuracy differences between fonts and languages
- Reduced recognition quality with complex backgrounds or moving text
- Undiscovered UI or runtime bugs

ClarifyDeck is not commercial software and does not provide commercial-grade reliability guarantees.

If you find a bug, compatibility issue, or have a feature request, please open a GitHub Issue.

---

## Non-Commercial Use

This project is intended only for:

- Learning
- Research
- Personal use
- Non-commercial testing

**Commercial sale, paid redistribution, paid services, or any other use intended to generate commercial profit from this project or modified versions of it is prohibited without explicit written permission from the author.**

For commercial cooperation or commercial use, please contact the project author first and obtain permission.

> This section explains the intended usage restrictions. For formal distribution, the repository's `LICENSE` file should be treated as the authoritative license document.

---

## Support ClarifyDeck

If ClarifyDeck is useful to you, please consider giving the project a **⭐ Star** on GitHub.

Stars, bug reports, and feedback all help the project improve.

Thank you for your support!
