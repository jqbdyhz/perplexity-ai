
## 更新记录
2025-12-31：增加健康检查endpoint， http://127.0.0.1:8000/health

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
     … +7 lines (ctrl+o to expand)

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
