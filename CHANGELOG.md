# 更新日志

记录各版本的功能新增、行为变更和问题修复，按版本从新到旧排列。尚未发布的变动记录在“未发布”中。
当前功能与使用说明见 [README.md](README.md)。

## 0.1.1

### 修复

- 修复普通正文和元数据被过度转义、在 Zulip 中显示多余反斜杠的问题，例如 `)` 被显示为 `\)`。
  普通标点保持原样，字面 Markdown 符号改用字符实体；保留代码中的原始字符和提及防护，脚注引用一并修正。
  后续同步按原消息 ID 更新受影响的镜像，无需重新导入。

## 0.1.0

### 新增

- GitHub 附件图片自动转存到 Zulip：支持正文和评论中的 Markdown / HTML 图片，覆盖 PNG、JPEG、GIF、WebP。
  私有图片通过当前配置的 PAT 获取，解析临时下载地址，按域名限制重定向并限制下载大小；不向 CDN 传递 PAT。
  SQLite schema 2 新增图片映射表，自动升级原 schema 1 并保留消息记录；重启及消息发送失败后复用已上传图片。
  图片准备失败时保留该任务的原消息和 Topic，后续轮询重试；预览不下载上传图片或修改磁盘检查点。
  README 补充图片格式、权限范围、缓存和存储限制；非图片附件及第三方图片仍保留原链接。
- 独立 `markdown.py` 模块和 Mistune 解析依赖，负责 GitHub → Zulip Markdown 转换及长消息拆分。
  支持只读任务清单、提示块、数学公式、脚注、引用式链接和简单 HTML 转写；代码正文不参与提及等文本转换。
- `project setup` / `project channel` 在目标频道不存在时，通过私信询问是否创建公有频道；
  用户发送 `project confirm CODE` 后创建并加入用户和 bot，发送 `project cancel CODE` 则取消。
  确认按用户隔离、10 分钟有效且只能使用一次，配置变更后旧确认不可覆盖新设置；确认前保留原配置。
  使用 Zulip 11.0（feature level 417）新增的创建接口，旧服务器提示手动创建；后台同步不会自动创建频道。
- Zulip 私信中的多用户、多 Project 配置管理：setup、list、status、sync、auth、channel、interval、pause、resume、remove。
  按消息发送者的 Zulip 用户 ID 检查所有权，用户仅能操作自己创建的配置。
- 独立 token 授权网页：私信下发 10 分钟有效的一次性链接，验证 GitHub Project 读取权限后加密保存 PAT；不通过聊天提交 token。
  授权链接仅保存哈希，支持过期、撤销、重复提交保护及配置变更期间的并发检查。
- SQLite 用户配置库、多 Project 调度、独立的配置 / Channel 消息检查点；定期检查创建者和 bot 的 Channel 成员资格。
- 新增 Flask / Waitress 授权服务及 cryptography 依赖，支持 HTTPS 反向代理、token 更换和本地密钥持久化。
- GitHub Projects V2 → Zulip 单向讨论同步，支持个人及组织 Project，每个任务映射到独立 Topic。
- 首次导入任务正文与历史讨论，后续同步新增、编辑和删除；覆盖 Issue 评论、PR 普通评论、已提交的 Review 和代码行讨论，以及 DraftIssue 正文。
- 分页读取完整讨论、拆分长消息，并保留原始作者、时间和 GitHub 链接。
- 持久化消息 ID 映射和内容哈希，支持重启后继续同步、原子保存状态、进程锁及失败重试。
- `python -m astrochat` 启动入口，支持聊天与后台定时同步同时运行，以及 `--once` 单次同步和 `--dry-run` 预览模式。
- `help` 帮助命令、手动触发同步和查询同步状态的聊天命令。
- 同步配置示例，以及分页读取、消息增删改、失败恢复、状态管理和聊天路由测试。

### 变更

- 长消息改为优先按段落和代码块拆分，跨消息的普通代码块补齐围栏；无法完整展示的超长复杂块显示源码片段。
  Markdown 转换结果参与同步内容哈希，已有消息仍按原映射更新，多余分段按原流程删除。
  README 用明确的支持范围和限制替换“Markdown 原文尽量保留”的笼统说明。
- GitHub 任务标题变化时同步更新整个 Zulip Topic 名称，保留原消息 ID 和本地回复，更新镜像正文中的标题。
  改名成功后才保存 SQLite 检查点；失败停止该任务本轮写入并重试，支持响应丢失后的核对恢复及预览。
  同步状态增加 `renamed` 计数，目标同名讨论及镜像消息被人工移动时拒绝自动合并。
- Topic 名改为“标题 · hash”，移除仓库名和任务编号前缀；保留原有 10 位 hash 算法和 60 字符上限，已有 Topic 在后续同步时对齐名称。
- 普通用户指南补充 GitHub classic PAT 的创建步骤、Project 与私有仓库权限选择、组织 SSO 授权及提交方式。
- README 按普通用户指南和开发者指南重写：前者集中说明命令和使用步骤，后者整理部署、模块职责、授权、持久化与同步边界。
- `project list` 和 `project status [NAME]` 默认只展示自己的配置和状态，添加 `--all` 可查看所有用户的项目，
  注明创建者的 Zulip 名称及用户 ID；按名称查询加 `--all` 会显示所有同名配置。
  `sync`、`auth`、`channel`、`interval`、`pause`、`resume`、`remove` 仍仅允许创建者操作，
  公开查询不返回 token 或授权链接。
- 移除本地 `astrochat.conf` 配置入口及示例，Project、token、Channel 和周期改为通过私信命令及授权网页配置。
  启动参数改用 `--data-dir`、`--auth-url`、`--auth-port`；bot 自身仍需要 Zulip 凭据。
- `project sync` 改为 `project sync NAME`，`project status [NAME]` 按用户隔离；所有 Project 命令限一对一私信。
  `--once` / `--dry-run` 处理所有已授权且未暂停的配置。
- 新配置库位于 `.astrochat/projects.sqlite3`，检查点位于 `.astrochat/states/`；旧单 Project 数据库保留，不自动推断所有者或导入配置。
- Topic 的稳定后缀包含 GitHub Project ID，区分同一 Issue 出现在多个 Project 的情况。
- 同步状态由 JSON 文件改为 SQLite，按任务读取消息映射，每次成功同步后以独立事务提交对应记录，不再整体重写状态文件。
  默认路径调整为 `.astrochat/project-sync.sqlite3`；不提供旧 JSON 状态迁移。
- SQLite 状态保留配置绑定、任务 Topic、消息 ID 和内容哈希；支持后台同步线程使用，预览模式使用内存副本，不写入磁盘数据库。
- 聊天命令 `github sync` 和 `github status` 更名为 `project sync` 和 `project status`，帮助文本和使用示例同步更新。
- README 仅描述当前版本的功能、配置、使用方式和限制；版本变动集中记录在本文件中。
