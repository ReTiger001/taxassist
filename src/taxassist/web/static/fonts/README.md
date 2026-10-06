# 自托管字体

`sarasa-mono-sc-regular.woff2` / `sarasa-mono-sc-bold.woff2` 是
**Sarasa Mono SC（更纱黑体 等距 SC）** 的子集，由 `tools/build_web_font.py`
生成，用于界面中英混排的等宽显示。

## 为什么自托管，而不是只靠系统安装

实测：把字体装进系统（用户字体目录 + 注册表都写对了），**已经在运行的浏览器
进程看不到它** —— 在 canvas 里量 `"Sarasa Mono SC"` 的宽度，与量一个乱写的
字体名、以及量 `sans-serif` 三者完全相等（105.56px），也就是静默回退。

所以界面必须自备字体文件，不能依赖系统状态。系统安装仍然保留（重启浏览器后
系统层面也能用），但**不是**页面能正常显示的前提。

## 子集化

| | 原始 TTF | 子集 woff2 |
|---|---|---|
| Regular | 24.4 MB | ~800 KB |
| Bold | 24.2 MB | ~810 KB |

字符集 = ASCII + GB2312 一级汉字（3755 个）+ 全角标点符号 +
模板与政策标题里实际出现过的字（共约 4000 字）。

缺字（GB2312 二级生僻字）会自动回退到字体栈后面的系统字体，不会显示方块。

重新生成：

```bash
.venv/Scripts/python.exe tools/build_web_font.py
```

脚本从系统字体目录读源 TTF。若报「缺源文件」，说明系统里还没装 Sarasa Mono SC
（装法见该脚本的文件头注释）。

## 许可

Sarasa Gothic 以 **SIL Open Font License 1.1** 发布，允许再分发与嵌入。

上游：https://github.com/be5invis/Sarasa-Gothic

本目录下的是**子集**，不是上游原始文件；需要完整字体请从上游获取。
