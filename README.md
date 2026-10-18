# astrochat

一个可扩展的 Zulip chatbot。目前支持聊天命令，以及 **GitHub Projects V2 → Zulip** 的单向讨论同步。

- [普通用户指南](#普通用户指南)：在 Zulip 中使用命令、配置项目和管理同步。
- [开发者指南](#开发者指南)：部署 bot、理解代码与数据存储、维护和扩展功能。

本文介绍当前版本的功能，版本变动记录见 [CHANGELOG.md](CHANGELOG.md)。

## 普通用户指南

本部分面向使用已部署 AstroChat 的用户。你只需要在 Zulip 中与 bot 对话，并通过授权网页提交 GitHub token。

### AstroChat 能做什么

- 将个人或组织的 GitHub Project 同步到指定的 Zulip Channel，每个 Issue、PR 或 DraftIssue 对应一个 Topic。
- 首次导入任务正文和历史讨论，之后定时同步新增、编辑和删除；PR 讨论包括普通评论、已提交的 Review 和代码行讨论。
- GitHub 任务标题变化时同步修改 Topic 名称，原有消息和 Zulip 本地回复一起保留。
- 在对应 Topic 发送 Project 状态变化、分配与取消分配负责人的通知，保留操作者和原始时间。
- Project 状态变为 `Done` 时自动将 Topic 标记为已解决；改回其他状态时取消已解决标记。
- 在消息正文中保留作者、原始时间和 GitHub 链接；消息实际由 bot 发送。
- 转换 GitHub Markdown，包括只读任务清单、提示块和数学公式；简单折叠区展开显示。
- 将 GitHub 附件中的截图下载并上传到 Zulip，让读者直接查看私有仓库 Issue 中的图片；同一配置和频道复用已上传图片。
- 每个用户可以配置多个 Project，分别设置目标 Channel、token 和同步周期。
- 目标 Channel 不存在时，私信询问是否创建公有频道；确认后自动加入用户和 bot。
- 默认查看自己的项目，添加 `--all` 可查看所有用户的项目及创建者；只有创建者能管理自己的配置。

同步方向是 GitHub → Zulip。在 Zulip 中的回复不会写回 GitHub，也不会更新 GitHub Project 的状态字段。

### 任务状态与负责人

每个任务的首条镜像消息会显示当前 Project 状态和负责人。Issue / PR 的历史状态与分配事件会一起导入，
后续每轮同步补充新的事件，例如 `Backlog → In progress`、`In progress → Done`、`Assigned to alice`。
自动化产生的变化也会记录，例如由 `github-project-automation` 移动到 `Done`。
这些通知发送到该任务的 Topic，不会自动提及同名 Zulip 用户；收到提醒的方式由你的频道和 Topic 通知设置决定。

当前读取 Project 中名为 `Status` 的字段。当其值为 `Done`（忽略大小写及首尾空格）时，
Topic 会变为 `✔ 标题 · hash`，即 Zulip 的已解决状态；离开 `Done` 时会移除该标记。
关闭 Issue 本身不会直接标记 Topic，是否已解决以 Project 的 `Status` 为准。
改名会保留原有消息及本地回复，bot 需要有修改该 Topic 的权限。

Issue / PR 通过 GitHub 事件历史补齐轮询期间的中间变化。DraftIssue 没有此历史接口：首次同步记录当前值，
以后比较轮询结果并通知变化；草稿通知注明观察时间，不推断操作者，不能还原两次轮询之间的中间状态。

### 图片与附件

同步会自动处理正文和评论中的 GitHub 附件图片，包括 Markdown 图片和粘贴截图生成的 `<img>`。
支持 PNG、JPEG、GIF、WebP，单张图片最多 10 MiB，还需满足所在 Zulip 的上传大小与存储配额限制。
私有图片使用该 Project 配置的 GitHub token 获取；token 必须有权访问原图片，组织要求 SSO 时还需完成相应授权。
不需要额外发送图片同步命令。第一次同步会上传图片，以后的同步及 bot 重启会复用已保存的图片地址。

**上传后，图片按 Zulip 消息的访问权限分享。** 能阅读目标频道消息的人可以查看截图，无需 GitHub 仓库权限；
如果目标是公有频道，截图也会随消息分享给能访问该频道的组织成员。请将包含私有资料的 Project 配置到合适的频道。
GitHub 权限收回或原图删除不会自动撤回已经上传到 Zulip 的副本。

其他网站的图片、仓库文件图片链接以及 PDF、压缩包、视频等附件仍保留原链接，不自动转存。
GitHub 附件图片下载或上传失败时，该任务本轮暂停消息更新，下一轮重试；其他任务继续同步。
SVG 等暂未支持的图片格式也会使该任务报错，可改用上述格式或普通文件链接。

### 从哪里发送命令

所有 `project ...` 命令都需要在与 AstroChat 的**一对一私信**中发送。
在 Channel 或群聊中提及 bot 并发送 Project 命令，只会收到私聊提示。

`help` 和普通聊天可以通过私信发送，也可以在消息开头提及 bot，例如 `@AstroChat help`。
提及时请从 Zulip 的候选列表中选择实际的 bot 账号。

### 命令速查

`NAME` 是你为同步配置起的名字，`PROJECT_URL` 是 GitHub Project 的链接，`CHANNEL` 是目标 Channel 的名称。
方括号表示可选参数，发送命令时不需要输入方括号；包含空格的 Channel 名称需要用引号括起来。

| 命令 | 作用 |
| --- | --- |
| `help` | 显示 bot 的基本帮助 |
| `project help` | 显示 Project 命令说明 |
| `project setup NAME PROJECT_URL "CHANNEL" [SECONDS]` | 创建同步配置，并获取授权链接；同步周期默认 900 秒 |
| `project list` | 列出自己的全部配置 |
| `project list --all` | 列出所有用户的配置，注明创建者名称及 Zulip 用户 ID |
| `project status [NAME]` | 查看自己的全部或指定名称配置的状态 |
| `project status [NAME] --all` | 查看所有用户的状态；提供名称时显示所有同名配置 |
| `project sync NAME` | 请求后台尽快同步自己的指定项目 |
| `project auth NAME` | 获取新授权链接，也用于更换 token |
| `project channel NAME "CHANNEL"` | 更改目标 Channel |
| `project confirm CODE` | 确认创建公有频道，并继续刚才的配置操作；`CODE` 使用 bot 提供的确认码 |
| `project cancel CODE` | 取消本次频道创建，不改变配置 |
| `project interval NAME SECONDS` | 修改同步周期，范围为 60–86400 秒 |
| `project pause NAME` | 暂停同步 |
| `project resume NAME` | 恢复同步 |
| `project remove NAME` | 删除配置和已保存的 token，保留 Channel 和已发送到 Zulip 的消息 |

### 创建 GitHub PAT

PAT（Personal Access Token，个人访问令牌）是 GitHub 的访问凭据，AstroChat 用它读取你的 Project 和相关讨论。
请使用能够访问目标 Project 及其 Issue / PR 的 GitHub 账号创建；token 不会赋予账号原本没有的访问权限。

下面以 **classic PAT** 为例，适用于个人 Project，也适用于允许 classic PAT 的组织 Project。
GitHub 目前的 fine-grained PAT（细粒度令牌）尚不支持个人账户的 Projects。类型限制见
[GitHub PAT 说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens-limitations)。

1. 登录 GitHub，依次打开右上角头像 → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**。
   也可以直接打开 [Tokens (classic) 设置页](https://github.com/settings/tokens)。
2. 点击 **Generate new token**，选择 **Generate new token (classic)**。
3. 在 **Note** 中填写便于识别的名称，例如 `astrochat-research`。这只是备注名称，不是 token 本身。
4. 在 **Expiration** 中设置有效期，例如 30 天。到期后需要创建新 token，并通过 `project auth NAME` 更新授权。
5. 在权限列表中按下表勾选所需范围。
6. 点击 **Generate token**，复制生成的 token。可以先保存在密码管理器中，再按下一节创建同步配置，
   把 token 粘贴到 bot 提供的独立授权网页中。不要把 token 发送到 Zulip 私信或 Channel。

以上界面步骤参照 [GitHub 创建 classic PAT 的说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-personal-access-token-classic)。

| 同步内容 | 需要勾选的权限 |
| --- | --- |
| 读取 Project；其中的 Issue / PR 均来自公开仓库 | `read:project` |
| 还需要读取私有仓库中的 Issue / PR 和讨论 | `read:project` 和 `repo` |

`read:project` 是 Project 的读取权限，AstroChat 不需要用于修改 Project 的完整 `project` 权限。
参见 [GitHub Projects API 的权限要求](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects#authentication)。
虽然 AstroChat 只读取 GitHub，classic PAT 的 `repo` 权限本身仍包含仓库写权限；只同步公开仓库时无需勾选它。
参见 [GitHub scopes 说明](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps#available-scopes)。

如果组织使用 SAML 单点登录（SSO），创建 token 后还需要在 token 列表中点击 **Configure SSO**，
找到目标组织并点击 **Authorize**。如果没有此入口，先通过组织的 SSO 登录一次，再返回设置页。
详见 [GitHub 的 SSO 授权步骤](https://docs.github.com/en/enterprise-cloud@latest/authentication/authenticating-with-single-sign-on/authorizing-a-personal-access-token-for-use-with-single-sign-on)。
如果组织禁止 classic PAT，请联系组织管理员或 bot 维护者确认可用的授权方式；
这是 [GitHub 组织策略](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-personal-access-token-classic) 的限制。

建议先准备好 PAT，再执行 `project setup`，以免创建 token 时 bot 的 10 分钟授权链接已经过期。
如果链接已失效，使用 `project auth NAME` 获取新链接即可。

### 配置第一个 Project

如果使用已有 Channel，需要先让你和 bot 都加入该 Channel。频道不存在时，bot 会询问是否创建公有频道，流程见下文。
私有仓库的讨论也会发送到目标 Channel，请选择具有对应访问范围的 Channel。

**第一步：私信 bot，创建配置。**

```text
project setup research https://github.com/users/YOUR_USERNAME/projects/5 "research-channel"
```

将 `YOUR_USERNAME`、Project 编号 `5` 和 `research-channel` 替换为实际值。
组织 Project 使用 `https://github.com/orgs/ORG/projects/5` 形式的链接。

`research` 是你自己的配置名称，不要求与 GitHub 上的 Project 标题一致。
名称以字母开头，支持字母、数字、`_` 和 `-`，最长 40 个字符，不区分大小写。
同一个用户的配置名称不能重复，不同用户可以使用相同名称。

命令末尾可以指定同步周期。例如，每 300 秒同步一次：

```text
project setup research https://github.com/users/YOUR_USERNAME/projects/5 "research-channel" 300
```

以上是两种创建方式，选择一种执行即可。已有配置的周期可以通过 `project interval research 300` 修改。

如果 `research-channel` 不存在，bot 会先询问是否创建，并给出类似以下两条命令：

```text
project confirm a1b2c3d4
project cancel a1b2c3d4
```

选择一条发送，确认码以 bot 实际回复的内容为准。确认后创建**公有频道**，把你和 bot 加入频道并设为频道管理员，
然后保存同步配置、返回 GitHub 授权链接。频道中的同步内容对组织内有公有频道访问权限的成员可见，不开放匿名网页访问。
如果希望使用私有频道，请先自行创建私有频道并加入 bot，再执行 `project setup`。

确认有效期为 10 分钟，只能由发起请求的用户使用一次。取消、超时或 bot 重启都不会保存这次配置；
重新发送原命令即可再次操作。每人最多保留一个待确认请求，新提示会替换自己的旧提示。
如果 bot 提示服务器版本或权限不支持创建频道，请联系管理员，或手动创建频道、加入自己和 bot 后重试。

**第二步：打开授权链接，在网页上提交 token。**

bot 会返回一条有效期 10 分钟、只接受一次提交的私密链接。打开链接，确认项目和目标 Channel，
填写能够读取该 Project 的 GitHub personal access token（PAT）。这是独立的 token 提交页，需要自行创建 PAT。
不要将 token 发送到 Zulip，也不要转发授权链接。

如果还没有 token，请先按[创建 GitHub PAT](#创建-github-pat)中的步骤准备，再返回授权页提交。

**第三步：查看同步状态。**

验证成功后，bot 会开始同步，并按配置的周期继续更新。首次导入历史讨论可能需要一些时间。
在私信中查看进度或最近一次同步结果：

```text
project status research
```

需要提前同步时，发送 `project sync research`。它会请求后台尽快执行，完成情况仍通过 `project status research` 查看。
如果项目已暂停，需要先发送 `project resume research`。

### 查看自己的项目与所有人的项目

`project list` 和 `project status` 默认只展示自己的配置。
添加 `--all` 可查看所有用户的配置，即使自己尚未创建项目也可以使用：

```text
project list --all
project status --all
```

结果包括配置名称、创建者、GitHub Project 链接、目标 Channel、同步周期和状态，不包含 token 或授权链接。
创建者名称无法获取时，仍会显示其 Zulip 用户 ID。

例如，Alice 和 Bob 都创建了 `research`：Alice 执行 `project status research` 只会看到自己的配置；
执行 `project status research --all` 会看到两份配置，并注明各自的创建者。`--all` 也可以放在名称之前。

`sync`、`auth`、`channel`、`interval`、`pause`、`resume`、`remove` 只操作命令发送者自己创建的配置。
例如，Bob 执行 `project pause research` 只会暂停 Bob 的配置；如果 Bob 没有该名称的配置，命令会被拒绝。

### 添加多个 Project

同一个用户可以创建多份同步配置，每份配置需要使用不同的名称。例如，已经创建 `research` 后，
可以用 `teaching` 配置第二个 GitHub Project：

```text
project setup teaching https://github.com/users/YOUR_USERNAME/projects/6 "teaching-channel"
```

然后打开 bot 返回的新授权链接，为 `teaching` 提交能读取这个 Project 的 token。
以后使用配置名称指定项目，例如 `project sync teaching` 只会请求同步 `teaching`。
创建第二份配置不会覆盖第一份配置；每份配置分别保存自己的 token、目标 Channel 和同步进度。

### 更换已有 Project 的 token

如果 `research` 的 token 过期或需要替换，在与 bot 的私信中发送：

```text
project auth research
```

打开 bot 返回的新授权链接，在网页上提交新的 token。验证通过后，AstroChat 会替换 `research` 保存的 token。
新的 token 需要能够读取原来的 GitHub Project；如果多份配置使用了同一个旧 token，需要分别为这些配置更新授权。

AstroChat 会记住“GitHub 的哪条任务正文或评论对应 Zulip 的哪条消息”，这就是消息映射。
更换 token 会保留这些记录，后续同步仍会更新已有消息，不会仅仅因为更换 token 就把历史内容重新发送一遍。
更换 token 时直接使用 `project auth`，无需先删除配置再重新 `setup`；删除后重建会作为全新配置导入历史内容。

新授权链接会使旧链接失效。验证失败时原有 token 保持不变，需要重新执行 `project auth NAME` 获取链接后再尝试。
如果授权页无法打开，请联系 bot 的部署维护者。

### 修改设置时会发生什么

- 更改 Channel 会在新目标导入历史内容，旧 Channel 中的消息保留；切换回原 Channel 时会继续使用原有同步记录。
- `project channel NAME "CHANNEL"` 指定的频道不存在时，也会先询问是否创建公有频道。确认前保留原来的目标和同步设置；
  取消或过期不会中断原有同步。等待期间修改配置或重新授权后，需要重新发送更改频道的命令。
- 暂停或删除配置会停止后续同步，Channel 和已经发送到 Zulip 的消息保留，包括 bot 创建的频道。
  恢复暂停的配置使用 `project resume NAME`。
- 修改设置会使尚未完成的授权失效，需要重新获取授权链接。当前正在进行的同步请求可能先完成，随后停止按旧配置同步。
- bot 只编辑或删除自己同步的消息正文。GitHub 标题变化时会给整个 Topic 改名，本地回复随之保留，回复正文不变。
  改名因权限或时间限制失败时，该任务本轮同步会停止并在之后重试，可用 `project status NAME` 查看失败计数。

## 开发者指南

本部分面向部署、维护或修改 AstroChat 的人员，介绍运行环境、代码结构、持久化和同步边界。

### 安装与本机启动

需要 Python 3.10+，当前常驻进程支持 macOS / Linux。在项目根目录安装依赖：

```sh
uv sync --locked
```

在 Zulip 中创建 **Generic bot**，下载凭据保存为项目根目录的 `zuliprc`，并将 bot 加入目标 Channel。
同步历史消息的编辑、删除受 Zulip 服务器权限和时间窗口限制，需要允许 bot 编辑和删除自己之前发送的消息。
同步标题还需要允许 bot 在同一 Channel 内移动整个 Topic 的消息，包括用户回复，并配置合适的历史消息移动时间限制。
相关权限为 `can_move_messages_between_topics_group`，时间限制为 `move_messages_within_stream_limit_seconds`，
参见 [Zulip Topic 修改权限与时间限制](https://zulip.com/api/update-message)。

通过 bot 创建频道需要 Zulip 11.0、feature level 417 或更新版本，并允许 bot 创建公有频道及添加成员。
这是所用 [Zulip 创建频道接口](https://zulip.com/api/create-channel) 的最低要求；旧服务器仍可配置已存在的频道。

本机开发时，浏览器与 bot 在同一台机器上，执行：

```sh
uv run python -m astrochat --zulip-config zuliprc
```

这会同时运行聊天处理、后台同步和授权网页服务。默认授权地址为 `http://localhost:8765`，数据目录为 `.astrochat/`。
Project、token、目标 Channel 和同步周期由用户通过私信命令及授权网页配置，不需要在本地配置文件中填写。

`zulip-run-bot astrochat/astrochat.py` 只提供基础聊天功能；运行完整功能应使用上面的 `python -m astrochat` 入口。

### 服务器部署与启动参数

为授权页配置一个用户浏览器可访问的 HTTPS 地址，然后启动 bot：

```sh
uv run python -m astrochat --zulip-config zuliprc \
  --auth-url https://astrochat.example.org
```

授权服务使用 Flask / Waitress，只监听 bot 所在服务器的回环地址 `127.0.0.1:8765`。
在该服务器上配置 HTTPS 反向代理，把 `https://astrochat.example.org` 的请求转发到这个本地地址，由反向代理终止 TLS。
浏览器访问的是 HTTPS 地址，反向代理负责把请求送到本机授权服务。

`--auth-url` 应只包含协议、主机名和可选端口，不包含路径或查询参数；仅本机回环地址允许 HTTP。
代理应保留浏览器的 `Origin` 请求头，请勿配置请求正文日志，以免记录用户提交的 token。

| 参数 | 含义 |
| --- | --- |
| `--zulip-config PATH` | bot 的 Zulip 凭据文件路径 |
| `--data-dir PATH` | 配置、加密密钥和同步状态的目录，默认为 `.astrochat/`，首次启动自动创建 |
| `--auth-url URL` | 用户浏览器访问授权页的地址；也可通过 `ASTROCHAT_AUTH_URL` 设置，默认 `http://localhost:8765` |
| `--auth-port PORT` | 授权服务本地监听端口，默认 `8765`；修改后需相应调整代理或本机访问地址 |
| `--once` | 对已保存、已授权且未暂停的配置执行一次同步后退出 |
| `--dry-run` | 预览上述配置的同步操作后退出，不修改 Zulip 消息或同步检查点 |

省略 `--zulip-config` 时，Zulip SDK 也可以从 `ZULIP_EMAIL`、`ZULIP_API_KEY`、`ZULIP_SITE` 读取 bot 凭据。
服务器上可由 systemd 等进程管理器运行同一命令，并设置工作目录、凭据环境变量和自动重启。
SIGINT / SIGTERM 会等待正在进行的授权验证、同步请求和状态提交完成后退出。

### 模块职责与运行流程

| 模块 | 职责 |
| --- | --- |
| [__main__.py](astrochat/__main__.py) | `python -m astrochat` 的入口，解析参数、初始化资源、启动服务并处理退出 |
| [astrochat.py](astrochat/astrochat.py) | 处理聊天消息、帮助命令和一对一私信路由 |
| [project_commands.py](astrochat/project_commands.py) | 解析 Project 命令、检查操作权限和 Channel 成员资格，构造同步任务 |
| [channel_creation.py](astrochat/channel_creation.py) | 区分频道不存在与查询失败，保存确认请求的数据结构，通过 Zulip API 创建公有频道 |
| [authorization.py](astrochat/authorization.py) 与 [static/](astrochat/static/) | 提供 token 提交页面、验证授权链接和 GitHub Project 读取权限 |
| [project_store.py](astrochat/project_store.py) | 管理用户配置、加密 token、授权链接摘要和调度状态 |
| [sync_worker.py](astrochat/sync_worker.py) | 按各配置的周期调度同步，处理手动请求、取消和失败重试 |
| [config.py](astrochat/config.py) | 定义单次同步使用的运行时配置，由已保存的 Project 配置构建 |
| [github.py](astrochat/github.py) | 通过 GitHub GraphQL API 分页读取项目、任务及讨论 |
| [markdown.py](astrochat/markdown.py) | 解析并转换 GitHub Markdown，转义显示字段，按块拆分长消息 |
| [image_sync.py](astrochat/image_sync.py) | 授权解析 GitHub 附件图片、限量下载并上传到 Zulip，复用图片映射 |
| [project_sync.py](astrochat/project_sync.py) | 组装消息来源信息、调用格式转换，比较内容哈希并执行增删改 |
| [sync_state.py](astrochat/sync_state.py) | 保存各配置与 Channel 的 Topic、消息 ID、内容哈希和图片上传映射 |

常驻入口打开配置库后，启动后台同步线程和授权服务，再进入 Zulip 消息事件循环。
聊天命令、后台同步使用独立的 API 会话；授权验证为每次请求建立 GitHub 会话。

`project sync NAME` 在配置库中记录同步请求并唤醒后台线程，聊天处理不会等待同步完成。
后台只调度已授权且未暂停的配置，按各自周期依次执行，并保存结果供 `project status` 查询。
单个任务或 Project 失败会保留已有记录并在后续轮询重试，其余任务和 Project 继续处理。

### 用户权限与网页授权

配置所有权取自 Zulip 消息发送者的用户 ID，用户显示名称变化或重名不会改变所有权。
查询默认限定当前用户，`--all` 开放查看所有配置；修改类命令始终按发送者查找自己的配置。
创建者名称通过 Zulip 用户列表接口批量获取，每次非空查询只请求一次；无法获取时回退为用户 ID。

用户和 bot 都必须是目标 Channel 的有效成员，保存配置、更改目标和每轮同步都会检查。
`setup` / `channel` 遇到不存在的频道时发起确认，确认后才创建公有频道并加入双方，再检查成员资格、保存配置。
GitHub 授权验证 token 能否读取目标 Project，不要求用户具有 GitHub Project 管理员权限。
程序只执行 GitHub 查询。

频道确认请求只保存在聊天事件循环的内存中，按 Zulip 发送者 ID 隔离，有效期 10 分钟，消费后不可重放。
更改频道的确认还绑定原配置 ID 和版本；确认前及保存前检查，避免旧请求覆盖已修改或删除重建的配置。
后台轮询不会创建频道。已存在的频道仍需用户和 bot 预先加入，bot 不自动改变其成员或可见性。

创建时使用 `POST /channels/create`，显式设置 `invite_only=false`、`is_web_public=false`，
将用户和 bot 同时设为初始成员及频道管理员。确认期间出现同名频道时要求重新执行原命令；
不使用会同时处理已有频道订阅的旧接口作为回退。创建失败不会更改原配置；请求超时会提示先到 Zulip 检查结果。
如果频道创建成功，但随后的成员检查或配置保存失败，会保留频道并明确提示，用户可修正后重新执行原命令。

授权链接绑定到具体配置及其版本，有效期为 10 分钟。随机凭据放在 URL 的 `#` 片段中，页面读取后清除地址栏片段，
通过 POST 提交；数据库只保存其 SHA-256 摘要，不保存可直接使用的链接。
提交前检查 JSON 格式和 `Origin`，调用 GitHub 验证前先消费链接，防止并发重复使用。
验证通过后才加密保存 token，验证失败保留旧 token；配置版本检查阻止过期的授权结果覆盖新设置。

授权页设置禁止缓存、禁止被嵌入和限制资源来源的响应头，不向页面或日志返回 token、授权凭据及上游错误正文。
持有授权链接的人可以给其绑定的配置提交 token，因此链接仍需保密。

### SQLite 持久化与备份

SQLite 数据库自动创建，无需运行额外数据库服务。默认目录结构：

```text
.astrochat/
  projects.sqlite3       # 用户配置、加密 token、授权链接摘要、调度状态
  token.key              # token 加密密钥，文件权限为 0600
  states/*.sqlite3       # Topic、消息/事件映射、哈希、图片映射和草稿观察记录
```

配置库由聊天、授权和调度线程共享，通过锁保护访问，并使用短事务；网络请求不在数据库事务内执行。
消息检查点按配置与 Channel 分开保存，按任务查询映射，每次成功修改一条 Zulip 消息后提交对应记录，
不需要整体重写状态文件。重启后继续使用已保存的配置、token、调度状态和消息映射。
图片上传成功后立即提交原地址 SHA-256 → Zulip 附件地址的映射；随后消息发送失败也能复用已上传的图片。
同步数据库使用 schema 3，自动升级 schema 1 / 2：补齐图片表和草稿观察记录表，保留原 Topic、消息和图片映射。
Issue / PR 事件使用 GitHub 事件 ID 保存映射；草稿变化使用持久化序号，通知成功保存后才推进观察记录。
预览模式仅升级内存副本，不更改磁盘上的旧数据库。

每个数据目录只允许一个进程使用，并绑定 Zulip 站点和 bot 身份；同步检查点另外绑定 GitHub Project 和目标 Channel。
配置库约束已授权的 GitHub Project 与 Channel 组合不能重复，避免重复镜像。

Token 使用 Fernet 加密，读取配置或状态不会返回 token。`projects.sqlite3` 和 `token.key` 的文件权限均为 `0600`。
正常停止进程后备份整个数据目录，并妥善保管 `token.key`；丢失密钥无法解密已有 token。
密钥与数据库均可被服务器运行账户读取，加密不提供服务器被攻破后的保护。
`.gitignore` 排除 bot 凭据和默认数据目录。

### 单次同步与预览

停止常驻进程后，运维人员可以预览或执行所有已授权且未暂停的配置：

```sh
uv run python -m astrochat --zulip-config zuliprc --dry-run
uv run python -m astrochat --zulip-config zuliprc --once
```

这两种模式不启动聊天或授权服务，两个选项不能同时使用。
预览仍会读取两端 API，但不修改 Zulip 消息或同步检查点；同步数据库使用内存副本，本地可能创建目录和锁文件。
预览不下载或上传图片：已有映射直接复用，尚未上传的图片使用内存中的占位地址估算消息更新，不验证图片下载权限或上传配额。
单次模式出现同步失败返回非零退出码。

### 同步规则与实现边界

- 首次导入任务正文及全部历史讨论。PR 包括普通评论、已提交的 Review、代码行讨论和线程回复，不发布尚未提交的 Review 草稿。
- 读取任务的 `Status` 和完整负责人列表；Issue / PR 分页读取 `ProjectV2ItemStatusChangedEvent`、
  `AssignedEvent`、`UnassignedEvent`，状态事件只保留当前 Project 的记录，避免混入同一 Issue 在其他 Project 的变化。
  事件与评论按 GitHub 时间排序后导入，升级后补导入的历史事件会出现在已有 Zulip 消息之后，正文保留原时间。
  已保存的事件通知作为历史记录保留，不随评论删除；重复轮询和重启通过事件 ID 去重。
- Topic 名采用“标题 · hash”格式，hash 由 Project ID 和任务 ID 计算生成，取前 10 位。
  `Status=Done` 时加上 Zulip 的 `✔ ` 前缀，其他值不加；包括前缀在内最长 60 字符，超长时截短标题并保留完整后缀。
  每次同步比较生成的名称与 SQLite 中记录的 Topic，
  不同时使用 Zulip `update_message` 的 `propagate_mode=change_all` 给整个 Topic 改名，包括 bot 有权移动的本地回复。
  消息 ID 和用户回复正文保持不变，后续评论进入新名称下；已有旧命名格式的 Topic 也会在后续同步时对齐。
- 只有 Zulip 确认改名后才更新 SQLite 中的 Topic，再继续同步消息正文和评论。
  改名失败保留旧检查点并停止该任务本轮写入，避免把讨论拆到两个 Topic；其他任务继续同步。
  若改名成功但响应或本地提交失败，下次通过已记录的消息 ID 核对远端名称；名称已符合预期时补存检查点。
  状态中的 `renamed` 是本轮完成名称对齐的 Topic 数，包括标记或取消已解决；事件通知计入 `sent`。
  预览模式只统计计划操作，不修改 Zulip 或检查点。
- 改名前检查目标 Topic 是否已存在，发现同名讨论时停止该任务以避免合并；仅大小写变化允许改名。
  用于定位 Topic 的镜像消息被人工移到其他 Channel / Topic 或被删除时，会报错，需维护者检查日志并处理后再重试。
- 消息正文保留作者、原始时间和 GitHub 链接；Zulip 中的实际发送者和消息时间仍然是 bot 和导入时间。
- 正文编辑、删除只操作数据库中记录的镜像消息，Topic 改名会连同本地回复一起移动；不修改或删除人工回复的正文。
  GitHub 评论被删除时删除对应镜像；
  编辑或删除失败会记录错误、保留映射，并在下一轮重试。
- 每次成功写入 Zulip 后，通过独立 SQLite 事务提交对应的消息记录。任务的全部讨论分页读取成功后，才判断评论删除。
  Project 中被移除、不可访问或被隐藏的任务保留已导入的历史，不自动清理 Topic。
- 消息映射按配置与 Channel 隔离，不会把另一个用户或 Project 的消息当作自己的镜像。
  更改 Channel 会在新目标导入历史，旧目标消息保持不变；切换回原 Channel 时复用原检查点。
- 删除配置后再 setup 会视为全新导入。删除配置不会清理磁盘上的同步检查点或已有 Zulip 消息。
- Zulip 和本地数据库之间没有跨服务事务。如果 Zulip 已接收新消息，但响应丢失或进程在保存 ID 前崩溃，
  重试可能出现重复消息。丢失数据库也会重新导入，无法保证这些故障场景下每条消息只发送一次。
- API 限流或网络故障在后续轮询重试。当前使用一个同步线程，Project 依次执行，大量历史导入会延迟其他 Project。
  每轮完整读取当前讨论，适合小到中型 Project。
- DraftIssue 没有评论或事件时间线，通过 SQLite 中的状态和负责人快照记录首次同步后观察到的变化；
  转换为 Issue 后 GitHub 内容 ID 改变，会作为新任务导入。
- Markdown 经过独立模块转换后再计算内容哈希，具体支持范围与拆分规则见下一节。
  GitHub 附件图片先转存到 Zulip，其他图片与非图片附件保留原链接。
- 当前不支持 Zulip → GitHub 回写（包括修改 GitHub 状态字段）、Webhook、Reaction 或非图片附件转存。

### 图片下载、上传与复用

[image_sync.py](astrochat/image_sync.py) 在修改一个任务的消息及 Topic 前，先收集其全部讨论中实际显示的图片引用。
代码片段、普通文件链接、注释以及转换为源码显示的复杂 HTML 不触发图片下载。
仅处理 GitHub 附件地址：`github.com/user-attachments/assets/...`、旧版 `github.com/OWNER/REPO/assets/...`、
图片语法引用的 GitHub `/files/...` 附件，以及 `user-images.githubusercontent.com`、`private-user-images.githubusercontent.com`。
第三方图片及仓库 `blob` / `raw` 文件链接保持原有引用行为。

对 `github.com` 附件，用当前配置的 PAT 请求 GitHub Markdown API，以 `gfm` 模式和仓库上下文解析临时图片地址；
若返回原附件地址，再通过带授权的请求获取图片或下载重定向。临时签名仅用于下载，不写入 Zulip 消息或检查点。
下载使用独立会话，PAT 只用于首次访问 `github.com` 的附件端点，不随重定向传递给 CDN。
每次跳转都检查 HTTPS 和明确的 GitHub 图片域名白名单，最多允许 5 次重定向；不请求任意外部网站或内网地址。
下载会话不读取 `.netrc` 或复用已有登录 Cookie；保留 Requests 对环境代理和 CA 设置的支持。

流式下载同时检查响应类型、图片文件头和实际累计字节数，上限为 10 MiB。
连接 / 读取超时分别为 10 / 30 秒，并在跳转和读取数据块之间检查 60 秒总时限及任务取消标记。
图片只在内存中暂存，不写本地文件；使用 Zulip `upload_file` 上传，成功后保存稳定的 `/user_uploads/...` 地址。
下载、上传或映射保存失败会停止该任务本轮消息修改和删除；其他任务继续处理，下一轮重试。
错误信息不包含 token、签名下载地址或上游响应正文。

缓存以原图片地址的 SHA-256 为键，保存在当前配置、当前 Channel 的独立数据库中，不跨用户、配置或频道共享。
图片地址视为不可变附件：相同地址复用已有上传，不在每轮重新下载比较图片字节。
更换 token 不清除映射；更换频道会建立新映射，切回原频道复用旧映射。
删除源图片引用时更新镜像消息，但不自动删除 Zulip 已上传文件，以免影响引用同一图片的其他消息；
手动删除 Zulip 文件不会自动修复缓存。上传成功但响应丢失或检查点提交失败时，重试可能留下重复文件。

接口参考：[GitHub Markdown API](https://docs.github.com/en/rest/markdown/markdown)、
[GitHub 私有图片访问说明](https://github.com/orgs/community/discussions/54551) 和
[Zulip 文件上传](https://zulip.com/api/upload-file)。

### Markdown 转换与消息拆分

[markdown.py](astrochat/markdown.py) 使用 Mistune 先解析 Markdown，再生成 Zulip Markdown；该模块不访问网络或数据库。
`markdown_images(body, source_url)` 收集待处理图片，`convert_github_markdown(body, source_url, image_urls)`
负责正文转换及根据已准备的映射替换图片地址；`escape_text(text)` 负责标题、作者等显示字段的转义，
`split_message(content, limit=10000)` 负责消息长度限制。下载上传在图片模块中执行，转换模块保持纯文本处理。

| GitHub 内容 | Zulip 中的处理方式 |
| --- | --- |
| 标题、段落、粗体、斜体、删除线、引用、列表、Markdown 表格 | 保留格式含义，统一 Markdown 写法；不承诺源码空白和分隔符逐字相同 |
| `- [ ]` / `- [x]` 任务清单 | 转为 `☐` / `☑` 的只读列表，不创建可在 Zulip 中修改的任务组件 |
| `[!NOTE]`、`[!TIP]`、`[!IMPORTANT]`、`[!WARNING]`、`[!CAUTION]` 引用提示块 | 转为带粗体类型名称的引用 |
| 行内 `$...$`、GitHub 的行内反引号公式写法 | 转为 Zulip 的 `$$...$$` |
| `$$...$$` 块公式、`math` 代码块 | 转为 Zulip 的 `math` 围栏块；公式能否渲染仍取决于 Zulip 支持的数学语法 |
| 行内代码、缩进代码和围栏代码 | 不对代码正文应用任务清单、公式或提及转换；保留语言标记并补齐代码围栏。`quote`、`spoiler`、`poll`、`todo` 语言名改为普通文本代码，避免生成 Zulip 组件 |
| 普通链接、图片和引用式链接 | 输出明确的行内链接；相对地址以原讨论 URL 为基准解析；已转存图片替换为 Zulip 附件地址，包裹图片的同源点击链接也替换 |
| 脚注 | 保留编号引用，正文末尾列出脚注说明 |
| 简单 `<details>` / `<summary>`、`<br>`、HTML 图片 | 折叠区展开为标题和正文，换行及图片转为 Markdown；图片尺寸等 HTML 布局属性不保留 |
| 其他 HTML、与代码混在同一 HTML 块中的复杂内容 | 显示原始源码；不模拟 HTML 布局或交互 |

正文中的 Zulip 提及语法会被中和，避免同步内容触发点名通知；代码中的文本保持原样。
普通括号、花括号和反斜杠不额外转义；需要显示为字面的 Markdown 符号使用字符实体，避免 Zulip 显示多余的反斜杠。
GitHub 用户名、Issue 编号简写不会自动映射为 Zulip 用户或 GitHub 链接。
相对地址转换不推断仓库的默认分支，因此仓库文件链接建议在 GitHub 原文中使用完整 URL。

拆分优先选择完整段落或代码块，每条消息（含分段编号）不超过 10,000 字符。
超长普通代码块在每段补齐围栏并保留语言标记；单行代码过长时会分段显示，复制后可能需要拼接换行。
无法放进单条消息的表格、嵌套结构、行内链接和公式会降级为带围栏的源码片段，避免发送残缺的格式。
若 Zulip 实例设置了更小的消息上限，需要调整 `split_message` 的 `limit`。

内容哈希基于替换图片地址、转换和拆分后的实际消息计算。转换规则调整后，后续同步会更新已有镜像；分段数减少时删除多余镜像，
保留其他用户的本地回复。规则和语法参考 [Mistune 插件](https://mistune.lepture.com/en/latest/plugins.html)、
[Zulip Markdown](https://zulip.com/help/format-your-message-using-markdown) 和
[GitHub 数学表达式](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/writing-mathematical-expressions)。

### 开发检查与接口参考

```sh
uv run python -m unittest discover -s tests -v
uv run ruff check astrochat tests
uv run ruff format --check astrochat tests
```

测试使用模拟 API，覆盖默认查询与 `--all` 查询、创建者显示、同名配置与操作权限隔离、多 Project 调度、
授权过期与重放、token 加密、Channel 权限、公有频道创建确认及取消、确认过期和配置变更、
分页与 PR 讨论、消息增删改、Topic 整体改名与本地回复保留、改名失败恢复、重启与失败恢复、SQLite 事务回滚、
状态锁、后台线程、预览模式、聊天路由、Markdown 转换、长代码块分段和转换后的增量更新，
以及图片授权、重定向限制、上传复用、失败重试和 schema 升级；
状态与分配事件分页、跨 Project 隔离、已解决标记、草稿快照及事件去重也有覆盖。
测试不会连接真实 GitHub / Zulip，也不需要凭据。

GitHub 读取接口参照 [Projects schema](https://docs.github.com/en/graphql/reference/projects)
、[Issues schema](https://docs.github.com/en/graphql/reference/issues)
和 [Pull requests schema](https://docs.github.com/en/graphql/reference/pulls)；Channel 成员检查使用
[Zulip channel subscribers](https://zulip.com/api/get-subscribers)，消息同步使用
[发送消息](https://zulip.com/api/send-message)、[编辑消息](https://zulip.com/api/update-message)和
[删除消息](https://zulip.com/api/delete-message)接口；创建者名称通过
[用户列表](https://zulip.com/api/get-users)接口获取。
