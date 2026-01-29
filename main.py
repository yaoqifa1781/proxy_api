import os
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect
from starlette.background import BackgroundTask

# 从环境变量获取目标地址
TARGET_URL = os.getenv("TARGET_URL", "http://httpbin.org")

app = FastAPI()

# 创建一个全局的 HTTP 客户端
# timeout 设置为 None 表示不限制超时
client = httpx.AsyncClient(base_url=TARGET_URL, timeout=None)

@app.on_event("shutdown")
async def shutdown_event():
    await client.aclose()
	
@app.get("/health")
async def health_check():
    return JSONResponse(content={"status": "ok"})

async def _proxy(request: Request, path: str):
    # 1. 构建目标 URL
    url = httpx.URL(path=path, query=request.url.query.encode("utf-8"))

    # 2. 处理请求头 (移除 Host 和 Content-Length)
    req_headers = dict(request.headers)
    req_headers.pop("host", None)
    req_headers.pop("content-length", None) 

    # 3. 智能处理请求体 (核心修改部分)
    content = None
    
    # 只有非 GET/HEAD/OPTIONS 方法才尝试读取 Body
    if request.method not in ["GET", "HEAD", "OPTIONS"]:
        async def req_body_iterator():
            try:
                async for chunk in request.stream():
                    yield chunk
            except ClientDisconnect:
                # 如果客户端断开连接，优雅停止，不抛出异常
                pass
        
        content = req_body_iterator()

    # 4. 构建并发送请求
    try:
        rp_req = client.build_request(
            request.method,
            url,
            headers=req_headers,
            content=content, # 这里可能是 None 或者 是一个安全的迭代器
        )
        
        rp_resp = await client.send(rp_req, stream=True)
    except httpx.ConnectError:
        return StreamingResponse(iter([b"Target connection failed"]), status_code=502)
    except Exception as e:
        return StreamingResponse(iter([f"Proxy error: {str(e)}".encode()]), status_code=500)

    # 5. 处理响应头
    excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    resp_headers = {
        k: v for k, v in rp_resp.headers.items() 
        if k.lower() not in excluded_headers
    }

    # 6. 返回流式响应
    # 使用 BackgroundTask 确保响应结束后关闭上游连接
    return StreamingResponse(
        rp_resp.aiter_bytes(),
        status_code=rp_resp.status_code,
        headers=resp_headers,
        background=BackgroundTask(rp_resp.aclose) 
    )

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(path: str, request: Request):
    return await _proxy(request, path)