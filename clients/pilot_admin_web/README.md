# 飞书智能体工作台 Web

Web 管理端，复刻 Flutter 工作台的任务运行态、产物和确认节点视图。

## 本地运行

先启动后端：

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 9000
```

再启动 Web 管理端：

```powershell
cd clients/pilot_admin_web
npm install
npm run dev
```

默认通过 Vite proxy 访问 `/api`，代理目标是 `http://127.0.0.1:9000`。

如果要像 Flutter 客户端一样连接线上后端，不要设置 `VITE_WORKBENCH_API_BASE_URL`，否则浏览器会直接跨域请求后端。开发模式请设置代理目标：

```powershell
$env:VITE_WORKBENCH_PROXY_TARGET="http://science.topviewclub.cn"
npm run dev
```

此时浏览器仍然打开 `http://127.0.0.1:5174/`，页面请求同源 `/api`，由 Vite 转发到线上后端。
