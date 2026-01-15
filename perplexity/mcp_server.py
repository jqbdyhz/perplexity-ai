"""
fastmcp-based MCP server exposing Perplexity search and model listing tools.
Provides both stdio (console) and HTTP transports.
Supports multi-token pool with load balancing and dynamic management.
"""

import argparse
import asyncio
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Union

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


# ==================== Heartbeat API 端点 ====================

@mcp.custom_route("/heartbeat/config", methods=["GET"])
async def heartbeat_config(request: Request) -> JSONResponse:
    """获取心跳配置"""
    pool = _get_pool()
    return JSONResponse({
        "status": "ok",
        "config": pool.get_heartbeat_config()
    })


@mcp.custom_route("/heartbeat/config", methods=["POST"])
async def heartbeat_config_update(request: Request) -> JSONResponse:
    """更新心跳配置"""
    from perplexity.config import ADMIN_TOKEN

    if not ADMIN_TOKEN:
        return JSONResponse({
            "status": "error",
            "message": "Admin token not configured. Set PPLX_ADMIN_TOKEN environment variable."
        }, status_code=403)

    provided_token = request.headers.get("X-Admin-Token")
    if not provided_token or provided_token != ADMIN_TOKEN:
        return JSONResponse({
            "status": "error",
            "message": "Invalid or missing admin token."
        }, status_code=401)

    pool = _get_pool()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({
            "status": "error",
            "message": "Invalid JSON body"
        }, status_code=400)

    result = pool.update_heartbeat_config(body)

    # Send Telegram notification if configured
    if result.get("status") == "ok":
        config = result.get("config", {})
        if config.get("tg_bot_token") and config.get("tg_chat_id"):
            await pool._send_telegram_notification("Perplexity config updated")

    return JSONResponse(result)


@mcp.custom_route("/heartbeat/start", methods=["POST"])
async def heartbeat_start(request: Request) -> JSONResponse:
    """启动心跳后台任务"""
    from perplexity.config import ADMIN_TOKEN

    if not ADMIN_TOKEN:
        return JSONResponse({
            "status": "error",
            "message": "Admin token not configured. Set PPLX_ADMIN_TOKEN environment variable."
        }, status_code=403)

    provided_token = request.headers.get("X-Admin-Token")
    if not provided_token or provided_token != ADMIN_TOKEN:
        return JSONResponse({
            "status": "error",
            "message": "Invalid or missing admin token."
        }, status_code=401)

    pool = _get_pool()
    started = pool.start_heartbeat()
    if started:
        return JSONResponse({"status": "ok", "message": "Heartbeat started"})
    elif not pool.is_heartbeat_enabled():
        return JSONResponse({"status": "error", "message": "Heartbeat is disabled in config"})
    else:
        return JSONResponse({"status": "ok", "message": "Heartbeat already running"})


@mcp.custom_route("/heartbeat/stop", methods=["POST"])
async def heartbeat_stop(request: Request) -> JSONResponse:
    """停止心跳后台任务"""
    from perplexity.config import ADMIN_TOKEN

    if not ADMIN_TOKEN:
        return JSONResponse({
            "status": "error",
            "message": "Admin token not configured. Set PPLX_ADMIN_TOKEN environment variable."
        }, status_code=403)

    provided_token = request.headers.get("X-Admin-Token")
    if not provided_token or provided_token != ADMIN_TOKEN:
        return JSONResponse({
            "status": "error",
            "message": "Invalid or missing admin token."
        }, status_code=401)

    pool = _get_pool()
    stopped = pool.stop_heartbeat()
    if stopped:
        return JSONResponse({"status": "ok", "message": "Heartbeat stopped"})
    else:
        return JSONResponse({"status": "ok", "message": "Heartbeat not running"})


@mcp.custom_route("/heartbeat/test", methods=["POST"])
async def heartbeat_test(request: Request) -> JSONResponse:
    """手动触发心跳测试"""
    from perplexity.config import ADMIN_TOKEN

    if not ADMIN_TOKEN:
        return JSONResponse({
            "status": "error",
            "message": "Admin token not configured. Set PPLX_ADMIN_TOKEN environment variable."
        }, status_code=403)

    provided_token = request.headers.get("X-Admin-Token")
    if not provided_token or provided_token != ADMIN_TOKEN:
        return JSONResponse({
            "status": "error",
            "message": "Invalid or missing admin token."
        }, status_code=401)

    pool = _get_pool()
    try:
        body = await request.json()
    except Exception:
        body = {}

    client_id = body.get("id")

    if client_id:
        # Test specific client
        result = await pool.test_client(client_id)
        return JSONResponse(result)
    else:
        # Test all clients
        result = await pool.test_all_clients()
        return JSONResponse(result)


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


def _is_valid_url(url: str) -> bool:
    """Check if a string looks like a valid URL."""
    url = url.strip()
    if not url:
        return False
    # 检查是否包含任何空白字符
    if re.search(r'\s', url):
        return False
    # 必须以 http:// 或 https:// 开头
    if url.lower().startswith(('http://', 'https://')):
        # 确保协议后有内容（至少有域名部分）
        after_scheme = url.split('://', 1)[1] if '://' in url else ''
        if after_scheme and '.' in after_scheme:
            return True
    return False


def _normalize_url(value: Any, _depth: int = 0) -> Optional[str]:
    """
    Normalize a URL value to a valid string URL.
    Handles: string, list (recursively), nested dict with href/url/link.
    Returns None if not a valid URL.

    Args:
        value: The value to normalize
        _depth: Internal recursion depth counter (max 10 to prevent infinite loops)
    """
    # 防止无限递归
    if _depth > 10:
        return None

    # 直接字符串
    if isinstance(value, str):
        url = value.strip()
        if _is_valid_url(url):
            return url
        return None

    # 列表形式：递归处理每个元素，取首个有效 URL
    if isinstance(value, list) and value:
        for item in value:
            url = _normalize_url(item, _depth + 1)
            if url:
                return url
        return None

    # 嵌套 dict 形式
    if isinstance(value, dict):
        # 先尝试提取标准字段 href/url/link
        nested = value.get("href") or value.get("url") or value.get("link")
        if nested:
            url = _normalize_url(nested, _depth + 1)
            if url:
                return url
        # 如果标准字段不存在或无效，遍历所有 values 递归查找
        for v in value.values():
            url = _normalize_url(v, _depth + 1)
            if url:
                return url
        return None

    return None


def _extract_sources(response: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Extract source links from the search response.
    Returns deduplicated sources with stable ordering, containing only url and short title.
    """
    sources = []
    seen_urls = set()

    # 方法1: 从 text 字段的 SEARCH_RESULTS 步骤中提取 web_results
    if "text" in response and isinstance(response["text"], list):
        for step in response["text"]:
            if isinstance(step, dict) and step.get("step_type") == "SEARCH_RESULTS":
                content = step.get("content", {})
                web_results = content.get("web_results", [])
                for web_result in web_results:
                    if isinstance(web_result, dict):
                        url = _normalize_url(web_result.get("url"))
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            source = {"url": url}
                            if "name" in web_result:
                                # 截断过长的 title（最多 80 字符）
                                title = web_result["name"][:80]
                                if len(web_result["name"]) > 80:
                                    title += "..."
                                source["title"] = title
                            sources.append(source)

    # 方法2: 备用 - 从 chunks 字段提取
    if not sources and "chunks" in response and isinstance(response["chunks"], list):
        for chunk in response["chunks"]:
            if isinstance(chunk, dict):
                url = _normalize_url(chunk.get("url"))
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    source = {"url": url}
                    title_raw = chunk.get("title") or chunk.get("name", "")
                    if title_raw:
                        title = title_raw[:80]
                        if len(title_raw) > 80:
                            title += "..."
                        source["title"] = title
                    sources.append(source)

    # 方法3: Fallback - 从顶层 sources 字段提取（兼容未来响应结构变化）
    if not sources and "sources" in response and isinstance(response["sources"], list):
        for src in response["sources"]:
            # 统一使用 _normalize_url 处理任意类型（string/list/dict/嵌套）
            title_raw = ""

            if isinstance(src, dict):
                # 对于 dict，直接递归处理（_normalize_url 会优先检查 href/url/link）
                url = _normalize_url(src)
                title_raw = src.get("title") or src.get("name", "")
            else:
                # 对于其他类型（string/list/嵌套），直接递归处理
                url = _normalize_url(src)

            if url and url not in seen_urls:
                seen_urls.add(url)
                source = {"url": url}
                if title_raw:
                    title = title_raw[:80]
                    if len(title_raw) > 80:
                        title += "..."
                    source["title"] = title
                sources.append(source)

    return sources


def _clean_citation_markers(text: str) -> str:
    """
    Remove citation markers from text to avoid confusion when sources are not included.
    Handles: [1], [1,2], [1-3], [^1], 【1】, （1）, <sup>1</sup>, etc.
    Note: Superscript numbers are NOT cleaned to avoid damaging units like m² or x².
    Code blocks (``` and `) are preserved to avoid damaging code examples.
    """
    # 保护代码块：先提取并替换为占位符
    code_blocks = []

    def save_code_block(match):
        code_blocks.append(match.group(0))
        return f"\x00CODE_BLOCK_{len(code_blocks) - 1}\x00"

    # 保护多行代码块 ```...```
    text = re.sub(r'```[\s\S]*?```', save_code_block, text)
    # 保护行内代码 `...`
    text = re.sub(r'`[^`]+`', save_code_block, text)

    # 英文方括号: [1], [12], [1,2], [1-3], [1 - 3], [1–3], [1—3] (允许分隔符前后空格)
    text = re.sub(r'\[\d+(?:\s*[,\-–—]\s*\d+)*\]', '', text)
    # 脚注格式: [^1], [^12]
    text = re.sub(r'\[\^\d+\]', '', text)
    # 中文方括号: 【1】, 【12】
    text = re.sub(r'【\d+】', '', text)
    # 中文圆括号: （1）, （12）
    text = re.sub(r'（\d+）', '', text)
    # HTML 上标格式: <sup>1</sup>, <sup>12</sup>
    text = re.sub(r'<sup>\d+</sup>', '', text, flags=re.IGNORECASE)
    # 注意：不清理上标数字 ¹²³，因为可能误伤 m²、x² 等单位/幂次
    # 清理多余空格（连续空格变单个）
    text = re.sub(r' +', ' ', text)
    # 英文标点前空格删除
    text = re.sub(r' ([.,;:!?])', r'\1', text)
    # 中文标点前空格删除
    text = re.sub(r' ([，。；：！？])', r'\1', text)

    # 恢复代码块
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODE_BLOCK_{i}\x00", block)

    return text.strip()


def _extract_clean_result(response: Dict[str, Any], include_sources: bool = False) -> Dict[str, Any]:
    """
    Extract the final answer from the search response.

    Args:
        response: Raw response from Perplexity API
        include_sources: If True, include source links in response

    Returns:
        Dict with 'answer' and 'sources' (empty list if include_sources=False)

    Note:
        Citation markers like [1][2] are ALWAYS removed from answer because
        the order of returned sources may not match the original citation numbers.
        This prevents user confusion about which source corresponds to which number.
    """
    result = {}

    if "answer" in response:
        answer = response["answer"]
        # 始终清理引用标记，因为 sources 顺序可能与原始标号不一致
        answer = _clean_citation_markers(answer)
        result["answer"] = answer

    if include_sources:
        result["sources"] = _extract_sources(response)
    else:
        result["sources"] = []  # 兼容性：始终返回 sources 字段

    return result


def _run_query(
    query: str,
    mode: str,
    model: Optional[str] = None,
    sources: Optional[List[str]] = None,
    language: str = "en-US",
    incognito: bool = False,
    files: Optional[Union[Dict[str, Any], Iterable[str]]] = None,
    include_sources: bool = False,
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
        clean_result = _extract_clean_result(response, include_sources=include_sources)
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
    include_sources: bool = False,
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
        sources: 搜索来源列表 (注意：此参数指定搜索范围，非返回的引用链接)
            - 'web': 网页搜索 (默认)
            - 'scholar': 学术论文
            - 'social': 社交媒体
        language: 响应语言代码 (默认 'en-US'，中文用 'zh-CN')
        incognito: 隐身模式，不保存搜索历史
        files: 上传文件 (用于分析文档内容)
        include_sources: 是否返回引用来源链接 (默认 False，不返回以节省 token)

    Returns:
        {
            "status": "ok",
            "data": {
                "answer": "搜索结果...",
                "sources": []  # 默认为空列表；若 include_sources=True 则包含引用链接
            }
        }
        或 {"status": "error", "error_type": "...", "message": "..."}
    """
    # 限制 search 只能使用 auto 或 pro 模式
    if mode not in ["auto", "pro"]:
        mode = "pro"
    # 使用 asyncio.to_thread 避免阻塞事件循环
    return await asyncio.to_thread(
        _run_query,
        query=query,
        mode=mode,
        model=model,
        sources=sources,
        language=language,
        incognito=incognito,
        files=files,
        include_sources=include_sources,
    )


@mcp.tool
async def research(
    query: str,
    mode: str = "reasoning",
    model: Optional[str] = "gemini-3.0-pro",
    sources: Optional[List[str]] = None,
    language: str = "en-US",
    incognito: bool = False,
    files: Optional[Union[Dict[str, Any], Iterable[str]]] = None,
    include_sources: bool = False,
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
        sources: 搜索来源列表 (注意：此参数指定搜索范围，非返回的引用链接)
            - 'web': 网页搜索 (默认)
            - 'scholar': 学术论文 (学术研究推荐)
            - 'social': 社交媒体
        language: 响应语言代码 (默认 'en-US'，中文用 'zh-CN')
        incognito: 隐身模式，不保存搜索历史
        files: 上传文件 (用于分析文档内容)
        include_sources: 是否返回引用来源链接 (默认 False，不返回以节省 token)

    Returns:
        {
            "status": "ok",
            "data": {
                "answer": "研究结果...",
                "sources": []  # 默认为空列表；若 include_sources=True 则包含引用链接
            }
        }
        或 {"status": "error", "error_type": "...", "message": "..."}
    """
    # 限制 research 只能使用 reasoning 或 deep research 模式
    if mode not in ["reasoning", "deep research"]:
        mode = "reasoning"
    # 使用 asyncio.to_thread 避免阻塞事件循环
    return await asyncio.to_thread(
        _run_query,
        query=query,
        mode=mode,
        model=model,
        sources=sources,
        language=language,
        incognito=incognito,
        files=files,
        include_sources=include_sources,
    )


def run_server(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Start the MCP server with the requested transport."""
    # Initialize the pool on startup
    pool = _get_pool()

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
