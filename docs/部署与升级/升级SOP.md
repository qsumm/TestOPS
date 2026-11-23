# TestOPS 升级 SOP（标准操作流程）

> 适用：TestOPS 平台自身的版本升级（v1.8.x → v1.9.0 及以后）。
> 原则：**先备份、后升级；每一步有验证点；任何一步失败都有回滚路径。**

---

## 0. 升级前必读

| 硬性要求 | 怎么确认 |
|---|---|
| 磁盘空闲 **>15GB**（SeekDB 数据盘要求） | `df -h /` |
| 可用内存 **≥2GB**（SeekDB 启动要求） | `free -m` 看 available |
| 宿主机系统在 SeekDB 兼容列表 | CentOS 7/9、Ubuntu 20.04+、Anolis 8+（Alibaba Cloud Linux 3 官方兼容） |
| `.env` 里的三个密钥有记录（JWT_SECRET / KEY_STORE_SECRET / DB_PASSWORD） | `grep -E "^(JWT_SECRET|KEY_STORE_SECRET|DB_PASSWORD)=" .env` |

> SeekDB 对资源不足**不报错、直接退出**——升级前把资源确认清楚，能省掉 90% 的"升级失败"。

---

## 1. 升级前检查（Pre-check，约 5 分钟）

### 1.1 记录当前状态（回滚依据）

```bash
cd <部署目录>
cat VERSION
docker ps --format "{{.Names}} | {{.Status}}" | grep -E "aiops|seekdb"
curl -s http://127.0.0.1:8848/api/health
```

**记录三个值**：版本号、容器状态（应全 healthy/up）、health 返回 `status:up`。

### 1.2 确认数据可登录（升级后对照）

- 用当前管理员账号登录一次，确认能进
- 记下用户列表（升级后对照）：`系统设置 → 用户`

### 1.3 检查本地改动（避免升级被 git 冲突挡住）

```bash
git status -s
```

> 有输出 = 有本地改动（改过 compose 端口等）。升级脚本会自动 stash 并在结束时提示恢复；
> 更推荐把端口/镜像源这类定制**挪进 `.env`**（`PLATFORM_PORT` / `SEEKDB_IMAGE` / `BASE_IMAGE`，
> 这些变量升级不影响）。

---

## 2. 执行升级（约 3~5 分钟）

### 步骤 0：老版本（v1.8.x）先拿升级脚本（一次性）

`upgrade.sh` 是 v1.9.0 才加入的文件——**v1.8.x 的部署目录里没有**。先取脚本（一次性，之后升级都有）：

```bash
cd <部署目录>
curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/qsumm/TestOPS/main/deploy/upgrade.sh -o deploy/upgrade.sh
curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/qsumm/TestOPS/main/deploy/wait-ready.sh -o deploy/wait-ready.sh
chmod +x deploy/upgrade.sh deploy/wait-ready.sh
```

> GitHub 直连可达也可以：`git fetch origin && git show origin/main:deploy/upgrade.sh > deploy/upgrade.sh`。
> 完全断网：下载新版压缩包覆盖代码目录（**保留 .env**）。详见《从旧版本升级》第 0 步。

### 方式 A：一键脚本（推荐）

```bash
bash deploy/upgrade.sh
```

脚本按序执行 **5 步**，每步都有输出可对照：

| 步骤 | 动作 | 成功标志 |
|---|---|---|
| 0/5 | 记录当前版本 | `当前版本：v1.8.x` |
| 1/5 | **备份数据卷** → `/root/testops-backup-<时间戳>.tar.gz` | `✓ 数据卷已备份` |
| 2/5 | 口令兼容性提示 | 有明确提示即正常 |
| 3/5 | 拉取新代码（detached HEAD 自动切回 main；本地改动自动 stash） | `新版本：v1.9.0` |
| 4/5 | **先 build/pull 镜像 → 最后 up -d**（失败不留半死状态） | 容器 Started |
| 5/5 | **等就绪**（轮询 health，最多 5 分钟） | `✓ 平台已就绪（耗时 Xs）` |

### 方式 B：手工（脚本不可用时）

```bash
# 1) 备份数据卷
VOL=$(docker volume ls -q | grep -i seekdb | head -1)
docker compose --env-file .env -f deploy/docker-compose.yaml stop seekdb
sudo tar czf /root/testops-backup-$(date +%F).tar.gz -C /var/lib/docker/volumes/$VOL/_data .

# 2) 更新代码（保留 .env）
git fetch --all --tags && git pull --ff-only

# 3) 重建启动（迁移自动执行）
docker compose --env-file .env -f deploy/docker-compose.yaml up -d --build
```

> **首次升级到 v1.9.0** 会自动执行数据库迁移（`alert_rules.algorithm` 追加 `forecast`），
> 无需手工干预；迁移只加列/加枚举值，旧代码在新库上仍可运行。

---

## 3. 升级后验证（Post-check，约 5 分钟）

### 3.1 版本与健康

```bash
curl -s http://127.0.0.1:8848/api/health
```

期望：`"status":"up","version":"<新版本>"`；`docker ps` 四个容器 Up/healthy。

### 3.2 数据保留

- 用**升级前的管理员口令**登录（口令不会被重置）
- 对照 1.2 记录的用户列表：`系统设置 → 用户`
- 大盘打开正常：`curl -s "http://127.0.0.1:8848/api/ops/dashboard/overview?range=24h" ...`（需要 token）

### 3.3 冒烟测试（有 Python3 即可）

```bash
python3 tools/smoke_mainflow.py --base http://127.0.0.1:8848 --no-llm
```

期望：**退出码 0**（写操作全部幂等自清理）。

### 3.4 Agent 状态

`主机管理` 里看各主机状态；若出现「需重新接入」：重新生成令牌并重跑安装命令
（Agent 协议向后兼容，老 Agent 不升级也能继续上报）。

---

## 4. 失败排查（按症状对号入座）

| 症状 | 根因 | 处理 |
|---|---|---|
| seekdb 反复 Restarting，日志 `SeekDB.Password.Incorrect` | 旧卷口令 ≠ 新 `.env` 口令 | a) `.env` 改回旧口令再 up；或 b) `bash deploy/upgrade.sh --reset-db`（清库） |
| 日志停在 `server_start 3/14`，无任何错误，Exit=1 | **资源不足**（数据盘空闲 >15GB / 可用内存 ≥2GB） | `df -h /`、`free -m`；清磁盘或停同机其它数据库服务 |
| `git pull` 报 local changes / untracked 冲突 | 本地改动或下载的脚本同名 | 脚本已自动 stash；手动：`git stash -u` → pull → 需要时 `git stash pop` |
| 构建时拉基础镜像超时（daocloud/Cloudflare 不通） | 出网受限 | `.env` 设 `BASE_IMAGE` / `SEEKDB_IMAGE` 换华为云等可达源（见《自建部署加速指引》） |
| 构建报 `--mount option requires BuildKit` | 老 Docker | `export DOCKER_BUILDKIT=1`，或 Docker ≥23 默认已开 |
| 大盘 500 / `ob_query_timeout` 超时 | 老版本已知缺陷（v1.9.0 已修） | 完成本次升级即可 |

> 详细排障表见《快速开始》常见问题与《自建部署加速指引》。

---

## 5. 回滚（Rollback）

```bash
cd <部署目录>
docker compose --env-file .env -f deploy/docker-compose.yaml down

# 还原数据卷
VOL=$(docker volume ls -q | grep -i seekdb | head -1)
docker volume rm $VOL
docker volume create $VOL
sudo tar xzf /root/testops-backup-<时间戳>.tar.gz -C /var/lib/docker/volumes/$VOL/_data

# 回到旧版本代码
git checkout <旧版本 tag>          # 如 v1.8.1-oss 对应的提交

# 启动
docker compose --env-file .env -f deploy/docker-compose.yaml up -d --build
```

> 数据库迁移是**加列/加枚举值**式的（不删不改既有结构），旧版本代码在新库上仍可运行——
> 所以"升级失败回滚"不需要还原数据库结构。

---

## 6. Agent 侧（通常不需要动）

- 平台升级**不影响**已接入的 Agent：协议向后兼容，老 Agent 继续上报
- 想让 Agent 用新版本（如指标名限长修复——K8s 节点必升）：平台界面
  「主机管理 → 直接升级」，或重跑安装命令；主机 ident / 探活 / 历史数据不变
- K8s 节点上的 Agent **建议升级**（v1.9.0 修复了 kubelet 子路径导致指标落库失败的问题）

---

## 7. 升级窗口建议

- 低峰期执行（升级中平台不可用约 1~2 分钟，数据不丢）
- 预留 30 分钟（含备份、构建、验证）
- 生产环境首次升级先在测试机演练一遍（`upgrade.sh` 支持在测试机重复执行）
