
## 更新记录
2026-01-02：新增多token池支持，支持动态管理号池（列举/新增/删除）
2026-01-02：MCP 响应现在包含 `sources` 字段，返回搜索结果的来源链接
2025-12-31：增加健康检查endpoint， http://127.0.0.1:8000/health

## 启动服务

### 安装依赖
```bash
pip install -e .
# 或使用 uv
uv pip install -e .
```

### HTTP 模式（推荐用于远程访问）
```bash
# 方式一：使用 python -m 直接运行（无需安装）
python -m perplexity.mcp_server --transport http

# 方式二：安装后使用命令行工具
perplexity --transport http

# 自定义 host 和 port
python -m perplexity.mcp_server --transport http --host 0.0.0.0 --port 8080
```
启动后访问 `http://127.0.0.1:8000/mcp` 使用 MCP 服务。

### stdio 模式（用于本地 MCP 客户端直接调用）
```bash
python -m perplexity.mcp_server --transport stdio
# 或安装后
perplexity --transport stdio
```
stdio 模式下，MCP 客户端通过标准输入/输出与服务通信，适用于 Claude Desktop 等本地客户端。

## docker compose 一键部署
> 注意，socks代理没有测试过
```yml
services:
  perplexity-mcp:
    image: shancw/perplexity-mcp:latest
    container_name: perplexity-mcp
    ports:
      - "${MCP_PORT:-8000}:8000"
    environment:
      # MCP 认证密钥
      - MCP_TOKEN=${MCP_TOKEN:-sk-123456}
      # Perplexity 账户凭证 (可选，用于高级功能)
      - PPLX_NEXT_AUTH_CSRF_TOKEN=${PPLX_NEXT_AUTH_CSRF_TOKEN:-}
      - PPLX_SESSION_TOKEN=${PPLX_SESSION_TOKEN:-}
      # SOCKS 代理配置 (可选)
      # 格式: socks5://[user[:pass]@]host[:port][#remark]
      # 示例: socks5://127.0.0.1:1080 或 socks5://user:pass@proxy.example.com:1080
      # - SOCKS_PROXY=${SOCKS_PROXY:-}
    restart: unless-stopped
```
.env环境变量
```bash
# Perplexity MCP Server 环境变量配置
# 复制此文件为 .env 并填入实际值

# ============================================
# MCP 服务配置
# ============================================

# MCP 服务端口
MCP_PORT=8000

# MCP API 认证密钥 (客户端需要在 Authorization header 中携带此密钥)
MCP_TOKEN=sk-123456

# ============================================
# Perplexity 账户凭证 (可选)
# 用于解锁高级功能: Pro 模式、Reasoning 模式、Deep Research
# 不配置则只能使用 auto 模式
# ============================================

# 从 Perplexity 网站 Cookie 中获取
# 打开 perplexity.ai -> F12 开发者工具 -> Application -> Cookies
PPLX_NEXT_AUTH_CSRF_TOKEN=
PPLX_SESSION_TOKEN=
```

## 多token池配置（负载均衡）

支持配置多个 Perplexity 账户 token，实现负载均衡和高可用。

### 配置方式

1. 创建 JSON 配置文件 `token_pool_config.json`：
```json
{
  "tokens": [
    {
      "id": "user1",
      "csrf_token": "your-csrf-token-1",
      "session_token": "your-session-token-1"
    },
    {
      "id": "user2",
      "csrf_token": "your-csrf-token-2",
      "session_token": "your-session-token-2"
    }
  ]
}
```

2. 设置环境变量指向配置文件：
```bash
PPLX_TOKEN_POOL_CONFIG=/path/to/token_pool_config.json
```

### 号池管理 MCP Tool

使用 `pool_manage` 工具动态管理 token 池：

```python
# 列举所有 token
pool_manage(action="list")
# 返回: {"status": "ok", "data": {"clients": [{"id": "user1", "available": true}, ...]}}

# 新增 token
pool_manage(action="add", id="user3", csrf_token="xxx", session_token="yyy")
# 返回: {"status": "ok", "message": "Client 'user3' added successfully"}

# 删除 token
pool_manage(action="remove", id="user2")
# 返回: {"status": "ok", "message": "Client 'user2' removed successfully"}
```

### 号池状态查询 REST API

查询详细的号池状态：
```bash
GET http://127.0.0.1:8000/pool/status
```

返回示例：
```json
{
  "total": 3,
  "available": 2,
  "mode": "pool",
  "clients": [
    {
      "id": "user1",
      "available": true,
      "fail_count": 0,
      "next_available_at": null,
      "request_count": 42
    },
    {
      "id": "user2",
      "available": false,
      "fail_count": 2,
      "next_available_at": "2024-01-02T10:30:00Z",
      "request_count": 38
    }
  ]
}
```

### 特性

- **Round-Robin 负载均衡**：请求在可用 token 间轮询分发
- **指数级回退**：token 失败时自动标记为不可用，2^n 秒后重试（最大64秒）
- **动态管理**：运行时可增删 token，无需重启服务
- **向后兼容**：不配置池时，自动使用单 token 模式或匿名模式


## mcp配置
```json
{
    "perplexity": {
        "type": "http",
        "url": "http://127.0.0.1:8000/mcp",
        "headers": {
            "Authorization": "Bearer sk-123456"
        }
    }
}
```
## 注册为command指令
效果预览:
```bash
> /perplexity 中国农历新年2026 

● 1. Create a todo list.
  2. Call mcp__perplexity-search__search with the query.
  3. Present the result to the user.

● perplexity-search - search (MCP)(query: "中国农历新年2026", language: "zh-CN")
  ⎿  {
       "status": "ok",
       "data": {
         "answer": "...",
         "sources": [
           {"title": "来源标题", "url": "https://..."}
         ]
       }
     }

```

新建 `.claude\commands\perplexity.md`
```
---
description: Use Perplexity to search and answer questions
argument-hint: [query]
---

Please use the Perplexity MCP tool to search for the following query and provide a detailed answer:

$ARGUMENTS
```
