# 开发约定

## 模块职责

- `bot/handlers/`：Telegram 消息、回调和权限入口，负责解析交互及发送响应。
- `bot/core/sources.py`：纯下载源解析，不导入配置、数据库或 Telegram；消息与 RSS 共用。
- `bot/core/download_service.py`：根据下载源和目标节点提交 aria2，返回 gid；不创建数据库记录或发送消息。
- `bot/core/` 其他模块：业务流程、RPC 客户端和展示函数。禁止反向导入 `handlers`，包括函数内导入；`test_architecture.py` 检查此边界。
- `bot/db/`：持久化和迁移，SQL 留在 repository；handler 不直接拼 SQL。
- `bot/web/`：HTTP 接口；可复用 core 服务，不调用 Telegram handler 的私有函数。

公共功能提供有明确输入/返回类型的函数；以下划线开头的函数仅供所属模块使用。
添加下载协议或修改磁力解析时，修改 sources 并分别验证 Telegram、RSS 入口。
下载源 URL 与 RSS 订阅地址是不同边界：后者仍只允许 HTTP(S)，并保留内网地址检查。

## 视频模块

`bot/video/` 隔离解析器进程、凭据和扫码 worker；`bot/db/video.py` 集中视频 SQL，
作为 TaskRepo 的领域 mixin，复用相同数据库。Telegram 和 Web 调用服务/仓库公共接口，
不相互调用 handler。凭据、媒体签名链接、解析器原始输出不得记录到日志。
视频测试安装 `requirements-video.txt`，真实浏览器验证另安装 `requirements-login.txt`。

## 修改范围与格式

Python 最低兼容 3.11，缩进 4 个空格；新公共函数提供类型注解和职责说明。
使用 Ruff 的导入排序和格式化，不同时改写整个仓库的历史格式。
sources、download_service 及其独立测试先纳入格式检查；这两个公共模块额外检查类型注解。
后续按功能逐步扩展，而不是用全仓库格式变更掩盖业务改动。

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt ruff
.venv/bin/ruff check bot tests
.venv/bin/ruff check --select ANN bot/core/sources.py bot/core/download_service.py
.venv/bin/ruff format --check bot/core/sources.py bot/core/download_service.py tests/test_sources.py tests/test_download_service.py tests/test_architecture.py
```

源码安装继续使用 `requirements.txt`；镜像使用包含间接依赖的 `requirements.lock`。
升级直接依赖时同步检查锁文件，并重建镜像验证；不要把开发工具写入运行依赖锁。

## 测试与行为约束

```bash
# 纯解析测试无需 Telegram 配置
.venv/bin/python -m unittest tests.test_sources tests.test_architecture -v

# 其他测试导入配置时需要虚拟凭据；不能使用生产 token。
BOT_TOKEN=test-token API_ID=1 API_HASH=test-hash ARIA2_SECRET=test-secret \
  .venv/bin/python -m unittest discover tests -v
```

纯解析、aria2 提交服务、消息过滤和 RSS 流程分别测试。测试公共行为，优先复用已有
FakeNodePool，不绑定 handler 私有函数作为跨模块补丁点。
不要让测试连接生产服务、使用真实凭据或运行安装脚本修改宿主机。

重构保持数据库字段、callback_data 和来源去重标识兼容。
`callbacks.py`、`task_manager.py`、`repo.py` 仍然较大；以后按独立流程分拆，
每次拆分保持现有测试并避免引入 handler/core 循环依赖。
