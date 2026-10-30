# ForkIQ SafeFork Controller

自托管的单人 GitHub 保存面板：追踪上游更新，但不让已有分支后退、不移动已有标签、不执行被保存项目的 CI。

基于 [zzusec/forkiq](https://github.com/zzusec/forkiq) 的独立改造，保留原 MIT 许可。原项目的镜像强推、合并、回滚、自动发现和工作流保活功能已移除。

## 保存规则

- 同步所有 `refs/heads/*`、原始 `refs/tags/*` 对象及可达历史；只允许创建和快进，不生成额外 merge commit。
- 上游删除分支或标签时，保留目标已有引用；上游消失、撤权、改名、同名重建或身份变化时停写，不自动更换来源。
- 分支回退、分叉、标签冲突、空文件树或异常大量删除时，整批停止。没有强推、删除引用或绕过按钮。
- 可显式登记 `main → upstream-main`，旧 `main` 保持不动。目标独有分支、归档和 `sync-control` 保留。
- 已审核标签差异只能按“标签名、目标原始 SHA、上游原始 SHA”固定登记；旧标签不动，任一 SHA 变化后重新停写。
- 新登记仓库保持暂停。先只读预览，10 分钟内确认后才启用；每次写入仍重新检查。失败、结果不确定或写入时崩溃后不会自动重试。

这些规则保存的是 Git 历史，不是整个 GitHub 项目的全量备份：不包含 Git LFS 实体、子模块仓库、Issues、Release 附件、Actions 数据或 GitHub 设置。普通快进也可能含不良代码；本工具不审查代码质量或保证项目内容安全。

## 公开与私有来源

| 来源 | 允许的目标 | 读取凭据 |
| --- | --- | --- |
| 公开仓库 | 身份固定的公开原生 Fork，建议名为 `原仓库名-SafeFork` | Git 对象匿名读取 |
| 私有仓库 | 经授权的独立私有仓库，不能是原生 Fork | 单独挂载的只读来源凭据 |

私有复制和上游删除、撤权后的留存均须有权授权者允许；访问权限本身不等于留存授权。私有来源不会降级发布到公开目标。独立仓库不依赖原生私有 Fork 网络，但仍受账号、平台规则与法律约束，不提供“永不删除”保证。

## 权限和 CI 隔离

被管理目标的 Actions 必须**持续全局关闭**，且不能有在途任务。控制器只检查，不会帮你打开、关闭或触发 Actions。迁移前还须停用其他同步写入口，保持单写者。

专用 GitHub App 仅安装到明确批准的目标仓库：

- `Contents: write`、`Workflows: write`：保真推送 Git 内容，包括工作流文件；不意味着运行工作流。
- `Administration: read`、`Actions: read`、`Metadata: read`：核对开关、任务和身份；不授予上述两类写权限。
- 私有来源使用另一个只读凭据。不要导出本机 GitHub 登录令牌，也不要将凭据写入仓库或聊天。

本应用开发仓库自己的 CI 只测试、构建本应用，无生产凭据，和被管理仓库的 CI 是两回事。真实 GitHub App 权限组合及含工作流文件的写入，须在合成仓库试点通过后再接入生产。

## Docker 启动

需要可用的 Docker Engine / Compose、GitHub 出站网络，以及你控制的 HTTPS 根地址。容量取决于各仓库完整历史大小；同步串行进行，每次使用临时裸仓库，至少保留 1 GiB 可用空间，且还要容纳源、目标历史及 Git 运算峰值。Compose 的 512 MiB 内存限制不是适用所有仓库的容量承诺。

```sh
export FORKIQ_PUBLIC_URL=https://fork.example.com
docker compose build
docker compose up -d
docker compose ps
```

默认仅绑定 `127.0.0.1:8792`，非 root、只读根文件系统、单 worker，无 Docker socket。首次设置完成前不要公开管理入口；先通过受控本机/SSH 隧道访问，设置至少 12 位密码，再接入自己的 HTTPS 反向代理。代理须正确设置 HTTPS 转发头，并限制管理访问；不要直接暴露容器端口。

1. 登录面板，在 GitHub 连接页创建**专用** App，仅选择批准的目标仓库，完成安装并检测连接。
2. 在 GitHub 人工完成目标 Actions 隔离并等候在途任务结束。
3. 登记已有来源与目标，核对固定 ID、分支映射、标签例外与私有授权。
4. 点击“只读预览”，刷新查看全部引用计划；确认后再启用同步。

默认每轮完成后等待 60 分钟，设置范围 1–1440 分钟；不依赖 GitHub schedule，也不保证每小时必定成功。网络、限流、容量、长任务和停机均可能延迟。没有上游 webhook 授权时不能保证“上游一推即同步”。

私有凭据以只读文件挂载到 `/run/secrets/<文件名>`，面板只填写文件名，不填写 Token。凭据需可由 UID `10001` 读取，仅授权来源 `Contents: read`；禁止与目标写入凭据相同。挂载配置及真实值不得提交。

默认 Compose 不挂载私有凭据。需要私有来源时，先在主机上准备受限目录及凭据文件，再使用可选覆盖配置：

```sh
export FORKIQ_SOURCE_SECRETS_DIR_HOST=/absolute/path/to/source-secrets
docker compose -f compose.yaml -f compose.private.example.yaml up -d
```

路径必须已存在，配置不会自动创建目录；不要把整个用户主目录或其他无关密钥目录挂载进去。

持久状态在 `state` 卷；App 私钥默认位于其中。首次连接后可将私钥迁至单独受控的只读挂载，并设置 `FORKIQ_PEM_PATH`；之后不能直接从页面覆写只读私钥，轮换须停写并受控更换。备份状态应停机或使用 SQLite 一致性备份，不能只复制运行中的 `.db` 而忽略 WAL；含私钥的备份须加密、限制访问。

## 故障处理

检查仓库“需关注”状态、事件和最近检查时间；`/healthz` 同时检查 worker 活性，不只是 Web 进程。失败后先核对来源、权限和目标引用，再运行只读预览。不要用强推、移动标签、清库或重新开启 Actions 来“修复”。

暂停阻止后续推送，但不能撤回已发出的 Git 请求。GitHub Actions 开关、仓库可见性与 Git push 不是一个原子事务：管理员在检查之后改变这些设置仍可能破坏隔离。同步期间必须保持设置稳定，禁止并行管理操作；详见 [安全边界](SECURITY.md)。

从原版迁移应使用**独立状态目录**，逐库重新登记和审核，不直接复用启用状态。若误打开旧数据库，迁移会留存第一份 `.before-safefork` 副本、暂停全部旧记录并清除旧任务；不会自动迁移来源、私钥权限或创建远程仓库。历史备份表和文件保留，但不再提供回滚入口。

## 本地测试

Python 3.11/3.12、Git、Node.js：

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_ui.js
python deploy/container_smoke.py --image forkiq-safefork-controller:local
```

Windows 将 `.venv/bin/` 替换为 `.venv\Scripts\`。单元和真实 Git 测试仅使用合成数据；容器验收创建临时本地卷，不注入 GitHub 凭据。测试不能替代真实 GitHub App 的受控试点、24 小时自然调度观察和独立异地备份。

## 许可

[MIT](LICENSE)。保留原作者 zzusec 的版权和许可声明。被保存的各上游项目仍适用各自许可与授权；本应用许可不授予复制、留存或公开它们的额外权利。
