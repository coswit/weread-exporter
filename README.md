# weread-exporter

微信读书全本/选章导出工具 — Playwright + Canvas Hook 提取完整**图文**内容,导出为按**章节标题命名**的 Markdown。支持选择章节、断点续传、图文按阅读顺序精确交错。

## 原理

微信读书网页版用 Canvas 渲染文字(而非 DOM 文本),插图是 DOM `<img>`。本工具:

1. **Playwright 自动化** — 持久化登录(扫码一次,后续复用)
2. **Canvas fillText Hook** — 拦截每次文字绘制,捕获字符 (x, y) 坐标
3. **目录全量扫描** — 增量滚动目录面板,收集全部章节并编号
4. **选章导出** — `--chapters` 或交互选择;目录点击定点跳转,分段导出
5. **图文交错** — 文字行与视口内图片按屏幕 y 坐标排序,插图落在正确段落间
6. **页内小节切分** — 画布中的小节标题行作为章内边界,小节粒度切分文件
7. **自适应翻页** — 渲染稳定即翻页(约 0.3–0.6 秒/页),卡死自动重开续传

## 安装

```bash
pip install -r requirements.txt   # playwright + pytest
playwright install chromium
```

## 使用

```bash
# 交互式:打开书 → 列出目录 → 输入范围(如 5-12,18)或回车全选
python export.py <reader_url_or_book_id>

# 指定章节(1-based 目录编号)
python export.py <book_id> --chapters 5-12,18

# 只扫描目录不导出
python export.py <book_id> --scan-only

# 只补下缺失图片(不开浏览器)
python export.py <book_id> --download-only
```

- 首次运行弹浏览器扫码,会话存 `cache/browser_profile/` 自动复用
- 中途中断(崩溃/Ctrl+C)后**重跑同一命令**即续传;半章会整章重导,不丢内容
- 重跑已完成的书不弹浏览器,直接补图+合并
- 不传 `--chapters` 时沿用上次的选择(改选用 `--chapters` 重新指定)

## 输出

```
output/<book_id>/
├── _catalog.json        # 目录标题数组(位置即编号)
├── _selection.json      # 上次选章与书名(续传/合并用)
├── chapters/第1章 xxx.md       # 章节按标题命名(重复标题自动加 (2))
├── images/              # chXXXX_imgNN.jpg 并发下载
├── raw/                 # 每章元数据(catalog_idx/finished/图片URL)
└── 书名.md              # 选中章节按目录序合并
```

> v3 旧版导出目录(数字命名 md)与新格式不互通:旧 raw 无 `catalog_idx` 会被忽略,建议旧目录改名备份后重新导出。

## 限制

- 需微信读书账号且对目标书有阅读权限
- 部分出版社限制网页端阅读("去 App 阅读"),无法导出
- 章节切分粒度受渲染页限制:**不足一页的小节**会并入前一章的页面(标题仍在目录与合并文件中,内容零丢失)
- 纯图廊章节图片密集时,图注与图的配对偶尔差一位;正文图片位置准确
- 极少数页面的文字行恰与相邻目录标题完全相同时,可能被误判为小节边界

## 测试

```bash
python -m pytest tests/test_pure.py -v   # 纯函数单元测试(选章解析/命名/切分/去重等)
```

## 声明

仅供个人学习研究使用。请勿用于商业用途或大规模传播,请尊重著作权。
