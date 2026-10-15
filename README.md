# ForkIQ

管理自己的 GitHub fork，让它们跟上上游；同步失败必须看得见。

ForkIQ 是一个自托管的单人面板：用 GitHub App 连接账号、自动发现你的 fork、按仓库决定「镜像」或「手工 fork」同步方式，并把每一次同步结果写进事件记录。

## 功能

- **自动发现**：安装 GitHub App 后扫描账号下的 fork，一小时一次，也可手动触发。
- **镜像同步**：严格跟随上游，分支始终停在与上游相同的提交；fork 不留自有提交，上游改写历史时直接对齐。要保留自己的改动请用手工 fork 模式，或另建仓库。
- **手工 fork 同步**：保留 fork 自有提交，用服务端 merge 把上游更新合进来。
- **版本对比**：每个仓库同时显示本地与上游的最新 tag、短 SHA、提交日期，并直接回答两边是不是同一个版本：同一版本 / 本地领先 N 个提交（上游更新已全部并入，差异来自 fork 自有提交）/ 落后上游 N 个提交 / 与上游分叉 / 检查失败。
- **上游消失不删本地**：上游被删除或改名时只标记待处理，fork 与历史原样保留。
- **自更新保活**（可选）：上游 Actions 静默超过 24 小时时，触发 fork 自己的 workflow。
- **统计下钻**：概览五项数字可点击进入对应仓库列表，待同步与需关注使用一致的分组筛选。
- **操作可追溯**：每次检查、合并、阻塞、错误都进事件流；列表保留状态，异常全文在详情浮层或仓库详情查看，不撑坏表格。
- **后台刷新**：只替换数据区域，revision 未变化直接返回 204，跳过内容构建与渲染；阅读详情或编辑表单时不打断。带内容哈希的 CSS/JS 使用长期缓存。

## 快速开始

需要 Python 3.10+，以及一个公网可访问的域名（GitHub App 回调要用）。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export FORKIQ_STATE_DIR="$PWD/state"      # 数据库与 App 私钥（默认 /var/lib/forkiq）
export FORKIQ_PEM_PATH="$FORKIQ_STATE_DIR/app.pem"
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8792
```

首次打开会跳转 `/setup` 设置管理员密码，然后到 `/github/connect` 创建 GitHub App。页面上的按钮会带着预填配置跳到 GitHub，授权后回调自动写入 App id 与私钥，再点「检测」绑定 installation。

App 需要的权限：`contents: write`、`administration: write`、`workflows: write`、`actions: write`、`metadata: read`。不需要 webhook 事件。

## 部署

`deploy/bootstrap.sh` 在干净的 Linux 主机上创建 `forkiq` 服务账号与目录，`deploy/forkiq.service` 是 systemd 单元（只监听 `127.0.0.1:8792`，由同机 HTTPS 反向代理访问）。把代码放到 `/opt/forkiq`、装好依赖后：

```sh
systemctl daemon-reload && systemctl enable --now forkiq
```

面板不做 TLS，请在反向代理终止 HTTPS 并传下 `X-Forwarded-Proto`，否则登录 Cookie 不会带 `Secure`。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # 引擎与 Web 层
node --test tests/test_ui.js                                    # 前端脚本
```

`tests/test_engine.py` 用脚本化的假 GitHub 客户端覆盖同步状态机；`tests/test_web.py` 走 FastAPI TestClient 渲染真实模板；`tests/test_ui.js` 在隔离的浏览器原语里执行 `app/static/app.js`。

## 结构

| 路径 | 作用 |
| --- | --- |
| `app/main.py` | 路由、会话与 CSRF、模板渲染、后台刷新片段 |
| `app/engine.py` | 同步引擎与后台 Worker |
| `app/github.py` | GitHub App / REST / GraphQL 客户端（标准库 + PyJWT） |
| `app/db.py` | SQLite（WAL）存储与增量迁移 |
| `app/versions.py` | 本地与上游版本对比判定 |
| `app/templates/` | Jinja 模板（含 `partials/` 刷新片段） |
| `app/static/` | 一份 CSS、一份脚本、内联图标 sprite |
| `PRODUCT.md` / `DESIGN.md` | 产品与设计约束（各自附同名 HTML 版本） |

## 说明

- 单人使用：一个管理员密码、一个 GitHub installation，不做多租户。
- 密码用 scrypt 加盐哈希；所有写操作校验 CSRF；会话 7 天过期。
- 上游被删除、改名、默认分支不一致时一律停止自动推送并提示，不猜也不覆盖。

## 许可

MIT，见 [LICENSE](LICENSE)。
