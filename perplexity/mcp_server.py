"""
fastmcp-based MCP server exposing Perplexity search and model listing tools.
Provides both stdio (console) and HTTP transports.
Supports multi-token pool with load balancing and dynamic management.
"""

import argparse
import asyncio
import os
from typing import Any, Dict, Iterable, List, Literal, Optional, Union

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_http_headers
from starlette.requests import Request
from starlette.responses import JSONResponse

try:
    from .client_pool import ClientPool
    from .config import LABS_MODELS, MODEL_MAPPINGS, SEARCH_LANGUAGES, SEARCH_MODES, SEARCH_SOURCES
    from .exceptions import ValidationError
    from .utils import sanitize_query, validate_file_data, validate_query_limits, validate_search_params
except ImportError:
    from client_pool import ClientPool
    from config import LABS_MODELS, MODEL_MAPPINGS, SEARCH_LANGUAGES, SEARCH_MODES, SEARCH_SOURCES
    from exceptions import ValidationError
    from utils import sanitize_query, validate_file_data, validate_query_limits, validate_search_params

# API 密钥配置（从环境变量读取，默认为 sk-123456）
MCP_TOKEN = os.getenv("MCP_TOKEN", "sk-123456")


class AuthMiddleware(Middleware):
    """Bearer Token 认证中间件"""

    def __init__(self, token: str):
        self.token = token

    async def on_request(self, context: MiddlewareContext, call_next):
        """验证请求的 Authorization header"""
        headers = get_http_headers()
        if headers:  # HTTP 模式下才有 headers
            auth = headers.get("authorization") or headers.get("Authorization")
            if auth != f"Bearer {self.token}":
                raise PermissionError("Unauthorized: Invalid or missing Bearer token")
        return await call_next(context)


mcp = FastMCP("perplexity-mcp")

# 添加认证中间件
mcp.add_middleware(AuthMiddleware(MCP_TOKEN))

# 全局 ClientPool 实例
_pool: Optional[ClientPool] = None


def _get_pool() -> ClientPool:
    """Get or create the singleton ClientPool instance."""
    global _pool
    if _pool is None:
        _pool = ClientPool()
    return _pool


# 健康检查端点 (不需要认证)
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """健康检查接口，用于监控服务状态，包含号池摘要"""
    pool = _get_pool()
    status = pool.get_status()
    return JSONResponse({
        "status": "healthy",
        "service": "perplexity-mcp",
        "pool": {
            "total": status["total"],
            "available": status["available"],
        }
    })


# 号池状态查询端点 (不需要认证)
@mcp.custom_route("/pool/status", methods=["GET"])
async def pool_status(request: Request) -> JSONResponse:
    """号池状态查询接口，返回详细的token池运行时状态"""
    pool = _get_pool()
    return JSONResponse(pool.get_status())


# 号池管理 API 端点 (用于前端管理页面)
@mcp.custom_route("/pool/{action}", methods=["POST"])
async def pool_api(request: Request) -> JSONResponse:
    """号池管理 API 接口，供前端管理页面调用"""
    from perplexity.config import ADMIN_TOKEN

    action = request.path_params.get("action")
    pool = _get_pool()

    try:
        body = await request.json()
    except Exception:
        body = {}

    # 需要认证的操作列表
    protected_actions = {"add", "remove", "enable", "disable", "reset"}

    # 验证 admin token
    if action in protected_actions:
        if not ADMIN_TOKEN:
            return JSONResponse({
                "status": "error",
                "message": "Admin token not configured. Set PPLX_ADMIN_TOKEN environment variable."
            }, status_code=403)

        # 从 header 或 body 中获取 token
        provided_token = request.headers.get("X-Admin-Token") or body.get("admin_token")

        if not provided_token:
            return JSONResponse({
                "status": "error",
                "message": "Authentication required. Provide admin token."
            }, status_code=401)

        if provided_token != ADMIN_TOKEN:
            return JSONResponse({
                "status": "error",
                "message": "Invalid admin token."
            }, status_code=401)

    client_id = body.get("id")
    csrf_token = body.get("csrf_token")
    session_token = body.get("session_token")

    if action == "list":
        return JSONResponse(pool.list_clients())
    elif action == "add":
        if not all([client_id, csrf_token, session_token]):
            return JSONResponse({"status": "error", "message": "Missing required parameters"})
        return JSONResponse(pool.add_client(client_id, csrf_token, session_token))
    elif action == "remove":
        if not client_id:
            return JSONResponse({"status": "error", "message": "Missing required parameter: id"})
        return JSONResponse(pool.remove_client(client_id))
    elif action == "enable":
        if not client_id:
            return JSONResponse({"status": "error", "message": "Missing required parameter: id"})
        return JSONResponse(pool.enable_client(client_id))
    elif action == "disable":
        if not client_id:
            return JSONResponse({"status": "error", "message": "Missing required parameter: id"})
        return JSONResponse(pool.disable_client(client_id))
    elif action == "reset":
        if not client_id:
            return JSONResponse({"status": "error", "message": "Missing required parameter: id"})
        return JSONResponse(pool.reset_client(client_id))
    else:
        return JSONResponse({"status": "error", "message": f"Unknown action: {action}"})


# 管理页面路由
@mcp.custom_route("/admin", methods=["GET"])
async def admin_page(request: Request):
    """管理页面"""
    from starlette.responses import FileResponse
    import pathlib
    static_path = pathlib.Path(__file__).parent / "static" / "admin.html"
    return FileResponse(static_path, media_type="text/html")


def _normalize_files(files: Optional[Union[Dict[str, Any], Iterable[str]]]) -> Dict[str, Any]:
    """
    Accept either a dict of filename->data or an iterable of file paths,
    and normalize to the dict format expected by Client.search.
    """
    if not files:
        return {}

    if isinstance(files, dict):
        normalized = files
    else:
        normalized = {}
        for path in files:
            filename = os.path.basename(path)
            with open(path, "rb") as fh:
                normalized[filename] = fh.read()

    validate_file_data(normalized)
    return normalized


def list_models_tool() -> Dict[str, Any]:
    """Return supported modes, model mappings, and Labs models."""
    return {
        "modes": SEARCH_MODES,
        "model_mappings": MODEL_MAPPINGS,
        "labs_models": LABS_MODELS,
    }


def _extract_clean_result(response: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the final answer and source links from the search response."""
    result = {}

    # 提取最终答案
    if "answer" in response:
        result["answer"] = response["answer"]

    # 提取来源链接
    sources = []

    # 方法1: 从 text 字段的 SEARCH_RESULTS 步骤中提取 web_results
    if "text" in response and isinstance(response["text"], list):
        for step in response["text"]:
            if isinstance(step, dict) and step.get("step_type") == "SEARCH_RESULTS":
                content = step.get("content", {})
                web_results = content.get("web_results", [])
                for web_result in web_results:
                    if isinstance(web_result, dict) and "url" in web_result:
                        source = {"url": web_result["url"]}
                        if "name" in web_result:
                            source["title"] = web_result["name"]
                        sources.append(source)

    # 方法2: 备用 - 从 chunks 字段提取（如果 chunks 包含 URL）
    if not sources and "chunks" in response and isinstance(response["chunks"], list):
        for chunk in response["chunks"]:
            if isinstance(chunk, dict):
                source = {}
                if "url" in chunk:
                    source["url"] = chunk["url"]
                if "title" in chunk:
                    source["title"] = chunk["title"]
                if "name" in chunk and "title" not in source:
                    source["title"] = chunk["name"]
                if "url" in source:
                    sources.append(source)

    result["sources"] = sources

    return result


def _run_query(
    query: str,
    mode: str,
    model: Optional[str] = None,
    sources: Optional[List[str]] = None,
    language: str = "en-US",
    incognito: bool = False,
    files: Optional[Union[Dict[str, Any], Iterable[str]]] = None,
) -> Dict[str, Any]:
    """Execute a Perplexity query (non-streaming) and return the final response."""
    pool = _get_pool()
    client_id, client = pool.get_client()

    if client is None:
        # All clients are in backoff
        earliest = pool.get_earliest_available_time()
        return {
            "status": "error",
            "error_type": "NoAvailableClients",
            "message": f"All clients are currently unavailable. Earliest available at: {earliest}",
        }

    try:
        clean_query = sanitize_query(query)
        chosen_sources = sources or ["web"]

        if language not in SEARCH_LANGUAGES:
            raise ValidationError(
                f"Invalid language '{language}'. Choose from: {', '.join(SEARCH_LANGUAGES)}"
            )

        validate_search_params(mode, model, chosen_sources, own_account=client.own)
        normalized_files = _normalize_files(files)
        validate_query_limits(client.copilot, client.file_upload, mode, len(normalized_files))

        response = client.search(
            clean_query,
            mode=mode,
            model=model,
            sources=chosen_sources,
            files=normalized_files,
            stream=False,
            language=language,
            incognito=incognito,
        )

        # Mark success
        pool.mark_client_success(client_id)

        # 只返回精简的最终结果
        clean_result = _extract_clean_result(response)
        return {"status": "ok", "data": clean_result}
    except ValidationError as exc:
        # Pro mode specific failures (like quota exhausted) - reduce weight
        if mode == "pro" and "pro" in str(exc).lower():
            pool.mark_client_pro_failure(client_id)
        else:
            pool.mark_client_failure(client_id)
        return {
            "status": "error",
            "error_type": exc.__class__.__name__,
            "message": str(exc),
        }
    except Exception as exc:
        # Check if it's a pro-related failure
        error_msg = str(exc).lower()
        if mode == "pro" and any(kw in error_msg for kw in ["pro", "quota", "limit", "remaining"]):
            pool.mark_client_pro_failure(client_id)
        else:
            # Mark general failure for exponential backoff
            pool.mark_client_failure(client_id)
        return {
            "status": "error",
            "error_type": exc.__class__.__name__,
            "message": str(exc),
        }


@mcp.tool
def list_models() -> Dict[str, Any]:
    """
    获取 Perplexity 支持的所有搜索模式和模型列表

    当你需要了解可用的模型选项时调用此工具。

    Returns:
        包含 modes (搜索模式)、model_mappings (模型映射) 和 labs_models (实验模型) 的字典
    """
    return list_models_tool()


@mcp.tool
async def search(
    query: str,
    mode: str = "pro",
    model: Optional[str] = None,
    sources: Optional[List[str]] = None,
    language: str = "en-US",
    incognito: bool = False,
    files: Optional[Union[Dict[str, Any], Iterable[str]]] = None,
) -> Dict[str, Any]:
    """
    Perplexity 快速搜索 - 用于获取实时网络信息和简单问题解答

    ⚡ 特点: 速度快，适合需要实时信息的简单查询

    Args:
        query: 搜索问题 (清晰、具体的问题效果更好)
        mode: 搜索模式
            - 'auto': 快速模式，使用 turbo 模型，不消耗额度
            - 'pro': 专业模式，更准确的结果 (默认)
        model: 指定模型 (仅 pro 模式生效)
            - None: 使用默认模型 (推荐)
            - 'sonar': Perplexity 自研模型
            - 'gpt-5.2': OpenAI 最新模型
            - 'claude-4.5-sonnet': Anthropic Claude
            - 'grok-4.1': xAI Grok
        sources: 搜索来源列表
            - 'web': 网页搜索 (默认)
            - 'scholar': 学术论文
            - 'social': 社交媒体
        language: 响应语言代码 (默认 'en-US'，中文用 'zh-CN')
        incognito: 隐身模式，不保存搜索历史
        files: 上传文件 (用于分析文档内容)

    Returns:
        {"status": "ok", "data": {"answer": "搜索结果...", "sources": [{"title": "...", "url": "..."}]}}
        或 {"status": "error", "error_type": "...", "message": "..."}
    """
    # 限制 search 只能使用 auto 或 pro 模式
    if mode not in ["auto", "pro"]:
        mode = "pro"
    # 使用 asyncio.to_thread 避免阻塞事件循环
    return await asyncio.to_thread(_run_query, query, mode, model, sources, language, incognito, files)


@mcp.tool
async def research(
    query: str,
    mode: str = "reasoning",
    model: Optional[str] = "gemini-3.0-pro",
    sources: Optional[List[str]] = None,
    language: str = "en-US",
    incognito: bool = False,
    files: Optional[Union[Dict[str, Any], Iterable[str]]] = None,
) -> Dict[str, Any]:
    """
    Perplexity 深度研究 - 用于复杂问题分析和深度调研

    🧠 特点: 使用推理模型，会进行多步思考，结果更全面准确，但耗时较长

    Args:
        query: 研究问题 (问题越具体，研究结果越有针对性)
        mode: 研究模式
            - 'reasoning': 推理模式，多步思考分析 (默认)
            - 'deep research': 深度研究，最全面但最耗时
        model: 指定推理模型 (仅 reasoning 模式生效)
            - 'gemini-3.0-pro': Google Gemini Pro (默认，推荐)
            - 'gpt-5.2-thinking': OpenAI 思考模型
            - 'claude-4.5-sonnet-thinking': Claude 推理模型
            - 'kimi-k2-thinking': Moonshot Kimi
            - 'grok-4.1-reasoning': xAI Grok 推理
        sources: 搜索来源列表
            - 'web': 网页搜索 (默认)
            - 'scholar': 学术论文 (学术研究推荐)
            - 'social': 社交媒体
        language: 响应语言代码 (默认 'en-US'，中文用 'zh-CN')
        incognito: 隐身模式，不保存搜索历史
        files: 上传文件 (用于分析文档内容)

    Returns:
        {"status": "ok", "data": {"answer": "研究结果...", "sources": [{"title": "...", "url": "..."}]}}
        或 {"status": "error", "error_type": "...", "message": "..."}
    """
    # 限制 research 只能使用 reasoning 或 deep research 模式
    if mode not in ["reasoning", "deep research"]:
        mode = "reasoning"
    # 使用 asyncio.to_thread 避免阻塞事件循环
    return await asyncio.to_thread(_run_query, query, mode, model, sources, language, incognito, files)


@mcp.tool
async def pool_manage(
    action: Literal["list", "add", "remove", "info", "enable", "disable", "reset", "status"],
    id: Optional[str] = None,
    csrf_token: Optional[str] = None,
    session_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    号池管理工具 - 用于动态管理 token 池

    Args:
        action: 操作类型
            - 'list': 列举所有 token（返回 id、可用状态和权重）
            - 'add': 新增 token 到池中
            - 'remove': 从池中删除指定 token
            - 'info': 获取用户会话信息（如指定 id 则获取单个，否则获取全部）
            - 'enable': 启用指定 token
            - 'disable': 禁用指定 token
            - 'reset': 重置指定 token 的失败状态和权重
            - 'status': 获取号池详细状态
        id: token 标识（add/remove/enable/disable/reset 操作必需，info 操作可选）
        csrf_token: CSRF token（add 操作必需）
        session_token: Session token（add 操作必需）

    Returns:
        list 操作: {"status": "ok", "data": {"clients": [{"id": "...", "available": true, "weight": 100}, ...]}}
        add 操作: {"status": "ok", "message": "Client 'xxx' added successfully"}
        remove 操作: {"status": "ok", "message": "Client 'xxx' removed successfully"}
        info 操作: {"status": "ok", "data": {...用户会话信息...}}
        enable/disable/reset 操作: {"status": "ok", "message": "..."}
        status 操作: {"status": "ok", "data": {...详细状态...}}
        错误: {"status": "error", "message": "..."}
    """
    pool = _get_pool()

    if action == "list":
        return pool.list_clients()

    elif action == "add":
        if not all([id, csrf_token, session_token]):
            return {
                "status": "error",
                "message": "Missing required parameters: id, csrf_token, session_token",
            }
        return pool.add_client(id, csrf_token, session_token)

    elif action == "remove":
        if not id:
            return {
                "status": "error",
                "message": "Missing required parameter: id",
            }
        return pool.remove_client(id)

    elif action == "info":
        # info 操作会发起网络请求获取用户信息，使用 to_thread 避免阻塞
        if id:
            return await asyncio.to_thread(pool.get_client_user_info, id)
        return await asyncio.to_thread(pool.get_all_clients_user_info)

    elif action == "enable":
        if not id:
            return {"status": "error", "message": "Missing required parameter: id"}
        return pool.enable_client(id)

    elif action == "disable":
        if not id:
            return {"status": "error", "message": "Missing required parameter: id"}
        return pool.disable_client(id)

    elif action == "reset":
        if not id:
            return {"status": "error", "message": "Missing required parameter: id"}
        return pool.reset_client(id)

    elif action == "status":
        return {"status": "ok", "data": pool.get_status()}

    else:
        return {
            "status": "error",
            "message": f"Invalid action: {action}. Valid actions are: list, add, remove, info, enable, disable, reset, status",
        }


def run_server(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Start the MCP server with the requested transport."""
    # Initialize the pool on startup
    _get_pool()

    if transport == "http":
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Perplexity MCP server (fastmcp).")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport to use for MCP server.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (when transport=http).")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (when transport=http).")
    args = parser.parse_args()
    run_server(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
