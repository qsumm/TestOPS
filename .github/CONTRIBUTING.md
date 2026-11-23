# 贡献指南（CONTRIBUTING）

> 这个项目最需要的不是「提个功能」，而是**把你在真机上踩到的坑修掉**——
> 每一条真实故障、每一个误报、每一次护栏误拦，都是最有价值的贡献。

---

## 1. 缺陷：先开 Issue，再动手

**不要直接提一个只有代码的 PR。** 缺陷类改动一律走：

```
① 开 Issue（说明现象/根因/影响）  →  ② 建分支修复  →  ③ 提 PR（关联 Issue）  →  ④ 合并
```

### 1.1 开 Issue

点 [新建 Issue](https://github.com/qsumm/TestOPS/issues/new/choose)，用内置模板填五段：

| 段 | 要点 |
|---|---|
| **现象** | 你看到什么。**贴真实日志**（`docker logs aiops-platform --tail 30`），去掉 IP / 密钥 |
| **期望 vs 实际** | 期望的行为，实际的输出 |
| **复现步骤** | 部署方式 → 做了什么 → 多久出现。能稳定复现最好 |
| **环境** | `/api/health` 的 version、`uname -r`、CPU/内存/磁盘空闲、Docker 版本 |
| **严重度** | S1 数据丢失/不可用 · S2 核心功能不可用（有绕过）· S3 体验问题 |

> **报之前先排除环境问题**：SeekDB 要求**数据盘空闲 >15GB、可用内存 ≥2GB**，
> 不满足时它会**静默退出**（`Exit=1`、无错误日志、停在 `server_start 3/14`）。
> 见《快速开始》的排障表。

### 1.2 建修复分支

命名：`fix/<issue号>-<短描述>`，例如：

```bash
git checkout main && git pull
git checkout -b fix/123-metric-name-overflow
```

一个分支**只修一个缺陷**（或一组强相关的）。修多个不相关的东西会让 review 无法进行。

### 1.3 提 PR

```bash
git push -u origin fix/123-metric-name-overflow
gh pr create --base main --title "[fix] 一句话说清改了什么" --body "..."
```

PR 正文必须包含：

```markdown
## 现象
（贴日志）

## 根因
（定位到文件:行号，说清为什么会这样）

## 修复
（改了什么、为什么这样改）

## 验证
（测试命令 + 结果；有真机复现的贴前后对比）

Closes #123
```

`Closes #123` 会让 Issue 在合并时**自动关闭**。

### 1.4 提交信息

```
<类型> <一句话结论>

<正文：为什么这么改、踩了什么坑、怎么验证的>
```

类型：`fix` / `feat` / `docs` / `test` / `refactor` / `perf`。
**正文比标题重要**——半年后回来看，只有正文能告诉你当时为什么这么写。

---

## 2. 硬性约定（会被自动化守卫拦住）

| 约定 | 守卫 |
|---|---|
| **前端免构建**：`static/js/pages/*.js` 是 ESM 直载，改完刷新即生效。不要引入打包器/构建链 | — |
| **改了前端必过语法门禁**：一个括号错会让整个模块图加载失败、页面白屏且**浏览器不报错** | `pytest backend/tests/test_d3_frontend_assets.py` |
| **改了后端补测试**：新增/修复逻辑都要有对应用例，按 `test_d<序号>_<主题>.py` 命名 | `pytest backend/tests -q` |
| **不要提交真实资产**：真实 IP、主机名、API Key、告警截图、内部文档一律不入库；演示数据用 `demo-*` 假名 | `python tools/oss_export.py` 敏感扫描（命中即拒绝导出） |
| **新增前端 class 必须在 CSS 里定义**：否则渲染成裸元素 | `test_d64_guards.py` |
| **文档代码块里不要写 `#` 注释**：macOS 默认 zsh 不把 `#` 当注释，用户复制粘贴会执行失败 | `test_docs_code_blocks_are_zsh_safe` |
| **手册标题不能带行内 markdown**（`**` 会原样显示在目录里） | `test_manual_headings_have_no_inline_markdown` |

提交前跑一遍：

```bash
./.venv/bin/python -m pytest backend/tests -q        # 全量（必须全绿，除已知 d143 环境项）
python3 tools/oss_export.py                          # 敏感扫描（可只看扫描结果）
```

---

## 3. 安全漏洞

**不要开公开 Issue。** 走 [GitHub 安全通告](https://github.com/qsumm/TestOPS/security/advisories/new)
私下报告，我们会在修复后公开致谢。

---

## 4. 文档与手册

用户手册是**单一来源**：`docs/部署与升级/用户手册.md`。改了手册正文后必须重新生成交付件：

```bash
python3 tools/gen_manual_docs.py    # 生成 docx + pdf（d66 守卫会检查一致性）
python3 tools/sync_manual.py        # 同步进平台内置手册
```

---

## 5. 发布与开源同步（维护者）

```bash
# 1) 开发分支（main）合并修复
# 2) 导出开源快照（白名单裁剪 + 敏感扫描 + 孤儿提交强推）
git checkout opensource-baseline && git reset --hard main
OSS_VERSION_TAG=vX.Y.Z-oss python3 tools/oss_export.py
git checkout main
```

> `tools/oss_export.py` 会：按白名单裁剪、剥离手册/视频 COPY 行、扫敏感词、生成
> 单提交快照并强推 `qsumm/TestOPS` 的 `main` 与版本分支/tag。

---

## 6. 其他

功能建议、文档改进、翻译、样例数据集都欢迎。**在 Issue 里先聊一句**能省掉很多无效 PR。
