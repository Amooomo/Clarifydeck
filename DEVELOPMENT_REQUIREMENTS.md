# Clarifydeck 开发规范与约束

1. **技术栈限制**：前端必须使用 React + TypeScript (`src/index.tsx`)，后端必须使用异步 Python (`main.py`)。
2. **构建链路约束**：任何前端修改必须保证 `pnpm build`（通过 Rollup 打包）能够无错通过。禁止引入未在 `package.json` 中注册的第三方前端裸库。
3. **功能核心**：
   - 后端负责定时截取特定坐标的 Gamescope 画面，并使用图像差异比对算法（如哈希或 MSE）减少无效 OCR。
   - 后端调用本地 Tesseract 二进制引擎进行文本识别，通过 `decky.emit` 广播。
   - 前端负责渲染 4 个区域框选滑块，并以最高 z-index 渲染悬浮字幕条。