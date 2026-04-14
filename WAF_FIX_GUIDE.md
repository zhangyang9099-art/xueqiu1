# 雪球爬虫 WAF 修复指南

## 问题原因

雪球启用了**阿里云 WAF（Web Application Firewall）**，当请求到达时，WAF 会返回一段 JavaScript 挑战代码，
要求浏览器执行后才能访问真正的 API。`curl_cffi` 虽然能模拟 TLS 指纹，但**不能执行 JavaScript**，
所以 WAF 挑战永远无法通过——所有请求要么拿到 HTML 拦截页，要么 403。

## 解决方案

把 HTTP 客户端从 `curl_cffi` 换成 **Playwright**（真实 Chromium 浏览器内核）。
浏览器会自动执行 WAF 的 JS 挑战，拿到合法 cookie 后，后续 API 请求全部通过浏览器的 `fetch()` 发出。

## 操作步骤

### 第一步：安装 Playwright

```bash
cd ~/Desktop/xueqiu-scraper

# 激活虚拟环境
source venv/bin/activate

# 安装 Playwright Python 包
pip install playwright

# 下载 Chromium 浏览器内核（约 150MB，只需下载一次）
playwright install chromium
```

### 第二步：运行诊断脚本（可选但推荐）

把 `test_playwright.py` 复制到项目根目录，然后运行：

```bash
python test_playwright.py
```

这个脚本会：
1. 用 Playwright 打开雪球主页，通过 WAF 挑战
2. 访问贵州茅台讨论页
3. 自动捕获浏览器发出的所有 API 请求
4. 打印每个 API 的 URL、状态码、返回数据预览

看输出就能确认哪些接口能正常返回数据。

### 第三步：替换核心客户端

**备份原文件：**
```bash
cp core/client.py core/client_old_curlffi.py
```

**用新文件覆盖：**
把本次提供的 `core/client.py` 复制到项目的 `core/` 目录下，覆盖原文件。

### 第四步：更新 requirements.txt

编辑 `requirements.txt`，把 `curl_cffi` 相关的行注释掉，加上 playwright：

```
# curl_cffi>=0.5.0    # 已弃用，被 playwright 替代
playwright>=1.40.0
PyYAML>=6.0
schedule>=1.2.0
```

### 第五步：确认 api_endpoints.py 中的接口

运行诊断脚本后，根据捕获到的真实 API URL 确认 `scrapers/api_endpoints.py` 中的接口地址是否正确。
如果不一致，修改对应的 URL。

### 第六步：测试运行

```bash
python main.py test-cookie
python main.py run
```

## 注意事项

1. **Playwright 比 curl_cffi 慢一些**，因为它要启动完整浏览器内核。
   但由于爬取频率低（每天一次），速度差异可以忽略。

2. **内存占用**比 curl_cffi 高约 100-200MB（Chromium 引擎），
   对现代 MacBook 来说不是问题。

3. 在服务器（如 OpenClaw）上运行时，确保：
   - 已安装 Playwright 和 Chromium：`playwright install chromium --with-deps`
   - `headless=True`（默认就是）

4. 如果某天 WAF 策略更新了，Playwright 方案的适应性远好于 curl_cffi，
   因为它本身就是真正的浏览器，和人手动操作没有区别。
