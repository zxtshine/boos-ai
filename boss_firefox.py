#!/usr/bin/env python3
"""
BOSS直聘 岗位采集工具

流程:
  1. 搜索列表页 → 提取基本信息（标题、薪资、公司、城市、经验、学历、链接）
  2. 逐个访问详情页 → 提取"岗位技能"原文输出

用法:
  python3 boss_firefox.py                     # 采集+分析
  python3 boss_firefox.py --login             # 首次扫码登录
  python3 boss_firefox.py --headless          # 无头模式
"""

import argparse
import csv
import io
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

_INVALID_COMPANY_RE = re.compile(
    r"^("
    r"\d+[-~]\d+K"
    r"|\d+-\d+\u5143"
    r"|\d+\u5143/[\u65f6\u6708]"
    r"|\d+-\d+\u5e74"
    r"|\d+\u5e74\u4ee5[\u4e0a\u5185]"
    r"|1\u5e74\u4ee5\u5185"
    r"|\u7ecf\u9a8c\u4e0d\u9650|\u5b66\u5386\u4e0d\u9650|\u4e0d\u9650"
    r"|\u5728\u6821|\u5e94\u5c4a|\u5b9e\u4e60"
    r"|\u672c\u79d1|\u7855\u58eb|\u535a\u58eb|\u5927\u4e13|\u4e2d\u4e13|\u4e2d\u6280|\u9ad8\u4e2d|\u521d\u4e2d"
    r"|\u5168\u804c|\u517c\u804c"
    r"|\d+\u5929/\u5468"
    r"|\d+-\d+\u4eba"
    r"|\d+\u4eba\u4ee5\u4e0a"
    r"|\d+\u4eba"
    r")$",
    re.I,
)


def _is_invalid_company(name: str) -> bool:
    if not name or len(name) < 2:
        return True
    name = name.strip()
    if _INVALID_COMPANY_RE.match(name):
        return True
    if "/" in name and len(name) <= 8:
        parts = name.split("/")
        if all(len(p.strip()) <= 4 for p in parts):
            if any(k in name for k in ["\u4e13", "\u6280", "\u79d1", "\u58eb", "\u4e2d", "\u9ad8", "\u5e74", "\u9650"]):
                return True
    return False


from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright

# Windows 编码修复
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ── 配置 ──
TODAY = date.today().isoformat()
DATE_STR = date.today().strftime("%Y-%m-%d")

KEYWORDS = [
    "Python后端",
    "前端开发",
    "产品经理",
    "数据分析",
    "机械设计",
    "电商运营",
]

# BOSS直聘城市代码
CITIES = {
    # 山东省
    "济南": "101120100",
    "青岛": "101120200",
    "淄博": "101120300",
    "德州": "101120400",
    "烟台": "101120500",
    "潍坊": "101120600",
    "济宁": "101120700",
    "泰安": "101120800",
    "临沂": "101120900",
    "菏泽": "101121000",
    "滨州": "101121100",
    "东营": "101121200",
    "威海": "101121300",
    "枣庄": "101121400",
    "日照": "101121500",
    "聊城": "101121700",
    # 一线城市
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    # 新一线城市
    "成都": "101270100",
    "杭州": "101210100",
    "武汉": "101200100",
    "南京": "101190100",
    "重庆": "101040100",
    "西安": "101110100",
    "长沙": "101250100",
    "天津": "101030100",
    "苏州": "101190400",
    "郑州": "101180100",
    "东莞": "101281600",
    "沈阳": "101070100",
    "宁波": "101210400",
    "昆明": "101290100",
    # 其他省会城市
    "合肥": "101220100",
    "福州": "101230100",
    "厦门": "101230200",
    "南昌": "101240100",
    "贵阳": "101260100",
    "南宁": "101300100",
    "太原": "101100100",
    "石家庄": "101090100",
    "哈尔滨": "101050100",
    "长春": "101060100",
    "兰州": "101160100",
    "乌鲁木齐": "101130100",
    "呼和浩特": "101080100",
    "拉萨": "101140100",
    "西宁": "101150100",
    "银川": "101170100",
    "海口": "101310100",
    "三亚": "101310200",
    "全国": "100010000",
}

OUTPUT_DIR = Path.home() / "AI" / "岗位日报"
STATE_FILE = Path(__file__).parent / ".boss_profile" / "firefox_state.json"
PROFILE_DIR = Path(__file__).parent / ".boss_profile" / "firefox_user_data"

ANTI_DETECT = """
// ── 核心：隐藏 webdriver 标记 ──
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
try { delete Object.getPrototypeOf(navigator).webdriver; } catch(e) {}

// ── 语言 ──
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});

// ── 硬件（桌面端典型值）──
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});

// ── screen 与 viewport 保持一致 ──
const fixScreen = () => {
    const w = window.innerWidth || 1280;
    const h = window.innerHeight || 800;
    Object.defineProperty(screen, 'width',  {get: () => w});
    Object.defineProperty(screen, 'height', {get: () => h});
    Object.defineProperty(screen, 'availWidth',  {get: () => w});
    Object.defineProperty(screen, 'availHeight', {get: () => h});
    Object.defineProperty(screen, 'colorDepth', {get: () => 24});
    Object.defineProperty(screen, 'pixelDepth', {get: () => 24});
};
fixScreen();
window.addEventListener('resize', fixScreen);

// ── 时区 ──
if (Intl && Intl.DateTimeFormat) {
    const origResolved = Intl.DateTimeFormat.prototype.resolvedOptions;
    Intl.DateTimeFormat.prototype.resolvedOptions = function() {
        const r = origResolved.call(this);
        r.timeZone = 'Asia/Shanghai';
        return r;
    };
}

// ── canvas 指纹干扰：轻微噪声扰动 ──
try {
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function() {
        const ctx = this.getContext('2d');
        if (ctx && this.width > 10 && this.height > 10) {
            const imgData = ctx.getImageData(0, 0, this.width, this.height);
            for (let i = 0; i < imgData.data.length; i += 4) {
                imgData.data[i] ^= 1;  // R channel ±1 bit
            }
            ctx.putImageData(imgData, 0, 0);
        }
        return origToDataURL.apply(this, arguments);
    };
} catch(e) {}

// ── WebGL 指纹一致性 ──
try {
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        // UNMASKED_VENDOR / UNMASKED_RENDERER
        if (p === 37445) return 'Google Inc. (Intel)';
        if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)';
        return getParam.call(this, p);
    };
} catch(e) {}

// ── 权限通知 ──
if (window.Permissions) {
    const orig = window.Permissions.prototype.query;
    window.Permissions.prototype.query = function(d) {
        if (d.name === 'notifications') return Promise.resolve({state: 'prompt'});
        return orig.call(this, d);
    };
}

// ── navigator.connection ──
try {
    Object.defineProperty(navigator, 'connection', {
        get: () => ({effectiveType: '4g', rtt: 50, downlink: 10, saveData: false})
    });
} catch(e) {}
"""

# ── 技能词库（仅分析用）──
SKILL_MAP = {
    "编程语言": {
        "Python",
        "Java",
        "Go",
        "Golang",
        "Rust",
        "C++",
        "C#",
        "C",
        "PHP",
        "Ruby",
        "Swift",
        "Kotlin",
        "Scala",
        "TypeScript",
        "JavaScript",
        "Node.js",
    },
    "前端": {"React", "Vue", "Angular", "Next.js", "HTML", "CSS", "Tailwind"},
    "AI/ML框架": {
        "PyTorch",
        "TensorFlow",
        "Transformers",
        "vLLM",
        "ONNX",
        "HuggingFace",
        "GGUF",
        "Stable Diffusion",
        "Diffusion",
        "Vision",
        "Multimodal",
    },
    "AI框架/工具": {
        "LangChain",
        "LangGraph",
        "LlamaIndex",
        "AutoGen",
        "CrewAI",
        "Dify",
        "Coze",
        "MCP",
    },
    "大模型技术": {
        "RAG",
        "Fine-tuning",
        "Finetune",
        "微调",
        "SFT",
        "RLHF",
        "LoRA",
        "QLoRA",
        "Prompt",
        "Function Calling",
        "Tool Calling",
        "Agent",
        "Multi-Agent",
        "Embedding",
        "LLM",
        "AI Agent",
        "AIGC",
    },
    "数据库/中间件": {
        "MySQL",
        "PostgreSQL",
        "Redis",
        "MongoDB",
        "Elasticsearch",
        "Milvus",
        "FAISS",
        "Chroma",
        "Qdrant",
        "Pinecone",
        "Weaviate",
        "Kafka",
        "RabbitMQ",
    },
    "部署/架构": {
        "Docker",
        "Kubernetes",
        "K8s",
        "FastAPI",
        "Flask",
        "Django",
        "Spring",
        "Nginx",
        "gRPC",
        "GraphQL",
        "WebSocket",
        "REST",
        "RESTful",
        "CI/CD",
        "GitHub Actions",
        "Linux",
        "GPU",
        "CUDA",
    },
    "云平台": {"AWS", "GCP", "Azure", "阿里云", "腾讯云"},
    "其他": {
        "数据结构",
        "算法",
        "系统设计",
        "架构",
        "微服务",
        "高并发",
        "分布式",
        "设计模式",
        "OOP",
        "TDD",
        "单元测试",
        "测试",
    },
}
ALL_SKILLS = {s for v in SKILL_MAP.values() for s in v}
MY_SKILLS = {
    s.lower()
    for v in {
        "编程语言": {"Python", "TypeScript", "JavaScript"},
        "AI框架/工具": {"LangChain", "LangGraph", "AutoGen", "CrewAI", "Dify", "Coze"},
        "大模型技术": {
            "LLM",
            "AI Agent",
            "RAG",
            "微调",
            "MCP",
            "Prompt Engineering",
            "Function Calling",
            "Tool Calling",
            "Embedding",
        },
        "数据库/向量库": {"MySQL", "Milvus", "FAISS", "Chroma", "Qdrant"},
        "部署/运维": {"Docker", "FastAPI", "Kubernetes"},
        "AI平台/模型": {"Claude", "OpenAI", "GPT"},
    }.values()
    for s in v
}


def decode_salary(text):
    return "".join(str(ord(c) - 0xE030) if 0xE030 <= ord(c) <= 0xE039 else c for c in text)


def salary_ok(text):
    if not text:
        return False
    nums = re.findall(
        r"(\d+)",
        re.sub(r"[^\d-]", "", text.replace("~", "-").replace("K", "").replace("k", "")),
    )
    if len(nums) < 2:
        return False
    l, h = int(nums[0]), int(nums[1])
    if l < 5 and h < 20:
        l *= 10
        h *= 10
    return 15 <= l and h <= 35


def pause(a=1.0, b=3.0):
    time.sleep(random.uniform(a, b))


def parse_skills(text):
    tl = text.lower()
    r = defaultdict(list)
    for cat, skills in SKILL_MAP.items():
        for s in skills:
            if s.lower() in tl:
                r[cat].append(s)
    return dict(r)


# ══════════════════════════════════════
#  浏览器
# ══════════════════════════════════════


class BossScraper:
    def __init__(self, headless=False):
        self.headless = headless
        self._pw = self._br = self._ctx = None
        self.page = None

    def start(self):
        self._pw = sync_playwright().start()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        kw = {
            "headless": self.headless,
            "viewport": {"width": 1280, "height": 800},
            "locale": "zh-CN",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        }
        self._ctx = self._pw.firefox.launch_persistent_context(str(PROFILE_DIR), **kw)
        self._br = None

        # 持久化 profile 自动管理 cookies，不额外 add_cookies 避免冲突
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                cookies = state.get("cookies") or []
                if cookies:
                    for c in cookies:
                        try:
                            self._ctx.add_cookies([c])
                        except Exception:
                            pass
            except Exception:
                pass

        self._ctx.add_init_script(ANTI_DETECT)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.set_default_timeout(30000)

    def close(self):
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:
                pass
        elif self._br:
            self._br.close()
        if self._pw:
            self._pw.stop()

    def _save_state(self):
        """持久化浏览器 cookie / localStorage 到文件，防止关窗丢失登录态。"""
        try:
            if not self._ctx:
                return
            state = self._ctx.storage_state()
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception:
            pass

    def _body_text(self, limit=1500):
        try:
            return self.page.inner_text("body")[:limit]
        except Exception:
            return ""

    def _login_prompt_visible(self):
        """判断当前页面是否真的落在登录/扫码态，避免误判普通详情页。"""
        try:
            url = (self.page.url or "").lower()
        except Exception:
            url = ""

        # 只看路径，不看 query 参数
        url_path = url.split("?")[0] if url else ""
        explicit_login_paths = (
            "/web/user/",
            "/login/",
            "/passport/",
            "security.html",
        )
        if any(path in url_path for path in explicit_login_paths):
            return True

        body = self._body_text(4000)

        # 详情页/聊天页的已登录特征，优先级高于任意“登录”字样。
        authenticated_indicators = (
            "职位描述",
            "岗位职责",
            "任职要求",
            "公司介绍",
            "竞争力分析",
            "立即沟通",
            "立即聊",
            "已沟通",
            "继续沟通",
            "聊天",
            "消息",
            "沟通中",
            "发简历",
        )
        if any(text in body for text in authenticated_indicators):
            return False

        strong_prompts = (
            "请登录",
            "扫码登录",
            "密码登录",
            "验证码登录",
            "微信扫码",
            "登录BOSS直聘",
        )
        if not any(text in body for text in strong_prompts):
            return False

        try:
            return self.page.evaluate("""() => {
                const visible = el => {
                    if (!el) return false;
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const ariaHidden = el.getAttribute('aria-hidden');
                    return style
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && style.opacity !== '0'
                        && ariaHidden !== 'true'
                        && rect.width > 0
                        && rect.height > 0;
                };

                const selectors = [
                    'input[placeholder*="手机号"]',
                    'input[placeholder*="验证码"]',
                    'input[type="password"]',
                    '.qrcode-img',
                    'img[class*="qrcode"]',
                    '[class*="login-panel"]',
                    '[class*="login-modal"]',
                    '[class*="sign-form"]',
                    '[class*="user-sign"]',
                ];

                return selectors.some(sel =>
                    Array.from(document.querySelectorAll(sel)).some(visible)
                );
            }""")
        except Exception:
            # 页面内容已明确出现强登录提示，但 JS 检测失败时宁可保守返回 False，
            # 避免误把普通详情页当成掉线。
            return False

    def is_logged_in_page(self):
        """当前页面是否能作为已登录态使用；about:blank 属于未知，不当作过期。"""
        try:
            url = self.page.url
        except Exception:
            return False
        if url == "about:blank":
            return True
        return not self._login_prompt_visible()

    def login(self):
        self.page.goto("https://www.zhipin.com/web/user/?ka=header-login")
        pause(2, 4)
        self.page.bring_to_front()
        print("\n🔓 浏览器已打开，请扫码登录")
        last = self.page.url
        logged_in = False
        for i in range(600):
            time.sleep(1)
            try:
                url = self.page.evaluate("window.location.href")
            except:
                continue
            if (
                any(p in url for p in ["/web/geek", "/web/geek/chat", "/job_detail"])
                and not self._login_prompt_visible()
            ):
                print("✅ 登录成功")
                logged_in = True
                break
            if "security" in url or "passport" in url:
                print("⚠️ 检测到安全验证页面，请在浏览器中完成验证")
            last = url
            if i > 0 and i % 30 == 0:
                print("  ⏳ %ds" % i)
        if not logged_in:
            raise TimeoutError("扫码登录超时或未确认进入已登录页面")
        self._save_state()
        print("✅ 登录状态已保存")

        # 预热：导航到聊天页验证 session 稳定性，确保 token 生效
        try:
            self.page.goto("https://www.zhipin.com/web/geek/chat", wait_until="load", timeout=30000)
            pause(3, 5)
            if not self._login_prompt_visible():
                print("✅ 会话预热成功")
            else:
                print("⚠️ 预热时仍检测到登录提示，可能需要手动刷新页面")
        except Exception as e:
            print(f"⚠️ 会话预热失败: {e}")

    # ── 搜索列表页 ──

    def search(self, keyword, city_code="100010000", districts=None, district="", company_size=None, areas=None):
        """搜索关键词，返回岗位列表。

        参数：
          - districts: 多区列表（推荐）。元素可传区名（如"增城区"）或区 code（如"440118"）。
            后端调用 boss_geo 解析为区 code 后拼成 multiBusinessDistrict=a&multiBusinessDistrict=b
            （BOSS URL 用重复参数表示多区）。
          - areas: 多工作区域列表（如淄博→["张店区", "临淄区"]）。
            拼成 multiAreaDistrict=张店区&multiAreaDistrict=临淄区
            （BOSS 用重复参数表示多区域，值是区名本身）。
          - district: 单区字符串（兼容旧接口）。如果同时传了 districts，则忽略。
          - district: 单区字符串（兼容旧接口）。如果同时传了 districts，则忽略。
          - company_size: 公司规模过滤。可传：
              * None / "" / []：不拼 scale
              * str（"20-99人" 或 "302"）：单值
              * List[str] / tuple：多值（拼为 scale=301,302,...）
            中文人名按本地映射查 code，未识别则跳过该条。

        BOSS URL 实际过滤参数（参考真实请求）：
          city=101280100
          multiBusinessDistrict=440118&multiBusinessDistrict=440113 (可重复, 多区用 & 重复参数)
          scale=302,301,303,304,305,306 (多值逗号分隔)
        """
        scale_name_to_code = {
            "0-20人": "301",
            "20-99人": "302",
            "100-499人": "303",
            "500-999人": "304",
            "1000-9999人": "305",
            "10000人以上": "306",
            "不限": "",
        }

        if company_size is None:
            company_size = ""
        if isinstance(company_size, (list, tuple, set)):
            size_items = [str(s).strip() for s in company_size if s]
        else:
            s = str(company_size).strip()
            size_items = [x.strip() for x in s.split(",") if x.strip()] if s else []

        scale_codes: list[str] = []
        for item in size_items:
            if item.isdigit():
                if item not in scale_codes:
                    scale_codes.append(item)
                continue
            code = scale_name_to_code.get(item, "")
            if code and code not in scale_codes:
                scale_codes.append(code)

        # 收集区列表：districts 优先，回退到 district
        district_inputs: list[str] = []
        if districts:
            if isinstance(districts, (list, tuple, set)):
                district_inputs = [str(d).strip() for d in districts if d]
            else:
                s = str(districts).strip()
                district_inputs = [x.strip() for x in s.split(",") if x.strip()] if s else []
        elif district:
            s = str(district).strip()
            district_inputs = [x.strip() for x in s.split(",") if x.strip()] if s else [s]

        district_codes: list[str] = []
        if district_inputs:
            try:
                from boss_geo import resolve_district_code
            except Exception:
                resolve_district_code = None  # type: ignore
            for ds in district_inputs:
                if ds.isdigit() and len(ds) >= 3:
                    if ds not in district_codes:
                        district_codes.append(ds)
                    continue
                if resolve_district_code is None:
                    continue
                code = resolve_district_code(city_code, ds) or ""
                if code and code not in district_codes:
                    district_codes.append(code)

        params = [f"query={quote_plus(keyword)}", f"city={city_code}"]
        if scale_codes:
            params.append(f"scale={','.join(scale_codes)}")
        for dc in district_codes:
            params.append(f"multiBusinessDistrict={dc}")

        # 处理「工作区域」（如淄博→张店区/临淄区）
        # BOSS 把工作区域和商圈合并到同一个 multiBusinessDistrict 参数里(6 位 code=行政区域,4-8 位=商圈)
        # 所以这里把 areas 解析为 code 后合并到 district_codes,统一走 multiBusinessDistrict
        area_inputs: list[str] = []
        if areas:
            if isinstance(areas, (list, tuple, set)):
                area_inputs = [str(a).strip() for a in areas if a]
            else:
                s = str(areas).strip()
                area_inputs = [x.strip() for x in s.split(",") if x.strip()] if s else []
        if area_inputs:
            try:
                from boss_geo import resolve_area_code

                for an in area_inputs:
                    code = resolve_area_code(city_code, an) or an
                    if code and code not in district_codes:
                        district_codes.append(code)
            except Exception:
                for an in area_inputs:
                    if an and an not in district_codes:
                        district_codes.append(an)

        url = "https://www.zhipin.com/web/geek/job?" + "&".join(params)
        try:
            self.page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            pass  # Boss 可能先跳安全页再跳回来导致 interrupted，忽略

        # 检查最终着陆页 — 只在真正掉到 passport/login 时才报错
        current_url = self.page.url
        if "/passport/" in current_url or "/login" in current_url.split("?")[0]:
            raise RuntimeError(f"BOSS 重定向到安全/登录页: {current_url}\n请先在浏览器中完成安全验证/登录。")
        if self._login_prompt_visible():
            raise RuntimeError("登录态已过期，请重新登录。")

        pause(3, 5)
        self._scroll_all()

        # 优先：列表 XHR 拉取 + 详情 XHR 补全（拿 JD/真实 HR 名/可投递权限）
        xhr_jobs = self._fetch_jobs_via_xhr(
            keyword, city_code, district_codes, scale_codes, max_pages=1, area_names=area_inputs
        )
        if xhr_jobs:
            # 收集本次结果中的工作区域到缓存,供 UI 下次使用
            try:
                from boss_geo import collect_areas_from_jobs

                collect_areas_from_jobs(city_code, xhr_jobs)
            except Exception:
                pass
            self._save_state()
            return xhr_jobs

        dom_jobs = self._extract_job_cards()
        if dom_jobs:
            self._save_state()
            return dom_jobs

        lines = [l.strip() for l in self.page.inner_text("body").split("\n") if l.strip()]

        # 薪资行定位
        sal_idx = [i for i, l in enumerate(lines) if re.search(r"\d+[-~]\d+K", decode_salary(l), re.I)]

        jobs = []
        for n, si in enumerate(sal_idx):
            if n > 0 and si - sal_idx[n - 1] < 3:
                continue
            if si == 0:
                continue
            title = lines[si - 1]
            if not (2 < len(title) < 60):
                continue

            salary = decode_salary(lines[si])
            company = exp = edu = city = ""
            end = sal_idx[n + 1] if n + 1 < len(sal_idx) else min(si + 10, len(lines))
            for j in range(si + 1, min(end, len(lines))):
                ln = lines[j]
                if "经验" in ln or "应届" in ln:
                    exp = ln
                elif re.search(r"本科|硕士|博士|大专|学历不限", ln):
                    edu = ln
                elif "·" in ln and len(ln) < 30:
                    city = ln
                elif (
                    not company
                    and len(ln) > 2
                    and len(ln) < 40
                    and not re.search(r"年|学历|大专|本科|硕士|博士|不限|应届|·", ln)
                ):
                    company = ln

            jobs.append(
                {
                    "title": title,
                    "salary": salary,
                    "company": company,
                    "experience": exp,
                    "education": edu,
                    "city": city,
                    "url": "",
                    "description": "",
                    "hr_name": "",
                    "hr_title": "",
                }
            )

        # 合并链接
        links = self._extract_links()
        if links:
            lm = {l["title"][:12]: l["href"] for l in links if l["title"][:12]}
            for j in jobs:
                if not j["url"] and j["title"][:12] in lm:
                    j["url"] = lm[j["title"][:12]]
        return jobs

    def _filter_by_welfare(self, jobs, welfare_keywords):
        """福利筛选：AND逻辑，所有关键词都必须匹配。"""
        if not welfare_keywords:
            return jobs
        filtered = []
        for j in jobs:
            tags = " ".join(j.get("welfareList", []) or [])
            if not tags:
                tags = j.get("description", "") or ""
            if all(kw in tags for kw in welfare_keywords):
                filtered.append(j)
        return filtered

    def _extract_job_cards(self):
        """优先从岗位卡片 DOM 提取，避免正文行号变化导致链接和岗位错配。"""
        try:
            rows = self.page.evaluate("""() => {
                const pickText = (root, selectors) => {
                    for (const sel of selectors) {
                        const el = root.querySelector(sel);
                        const text = (el && el.innerText || '').trim();
                        if (text) return text;
                    }
                    return '';
                };
                const linesOf = (root) => (root.innerText || '')
                    .split('\\n')
                    .map(s => s.trim())
                    .filter(Boolean);
                const cards = [];
                const seen = new Set();
                document.querySelectorAll('a[href*="/job_detail/"]').forEach(a => {
                    const href = a.href || a.getAttribute('href') || '';
                    if (!href || seen.has(href)) return;
                    // 锁定岗位卡：必须用 [class*="job-card"] / [class*="search-job"] / .job-primary，
                    // 不能用裸 li（会命中左侧筛选侧栏的 <li> 选项）
                    const card = a.closest('.job-card-wrapper, .job-card-body, .job-primary, [class*="job-card-wrapper"], [class*="search-job-result"], [class*="job-list-box"]') || a;
                    const lines = linesOf(card);
                    let title = pickText(card, [
                        '.job-name', '.job-title', '.job-card-left .job-name',
                        '[class*="job-name"]', '[class*="job-title"]'
                    ]) || (a.innerText || '').trim().split('\\n')[0] || lines[0] || '';
                    let salary = pickText(card, ['.salary', '.red', '[class*="salary"]'])
                        || lines.find(x => /\\d+[-~]\\d+K/i.test(x)) || '';
                    let company = pickText(card, [
                        '.company-name', '.brand-name', '.company-text',
                        '[class*="company-name"]', '[class*="brand-name"]'
                    ]);
                    // 兜底：card 里没 .company-name 时，从最近的 [class*="company-info"] 找
                    if (!company) {
                        const infoBlock = card.querySelector('[class*="company-info"]');
                        if (infoBlock) company = clean(infoBlock.innerText);
                    }
                    // 脏数据过滤：company 不应包含经验/学历/薪资关键词（这些是其他字段）
                    if (company && /经验|应届|在校|不限|学历|本科|硕士|博士|大专|中专|高中|元\\\/时|元\\\/月|\\\\d+[-~]\\\\d+K|\\\\d+-\\\\d+元|\\\\d+人以上|\\\\d+人/.test(company)) {
                        company = '';
                    }
                    // 兜底：长度过短或纯数字也当作脏数据
                    if (company && (company.length < 2 || /^\\d+$/.test(company))) {
                        company = '';
                    }
                    // companyId: 从公司名链接的 href 里取 /gongsi/<id>.html
                    let companyId = '';
                    const brandHref = card.querySelector('a[href*="/gongsi/"]')?.getAttribute('href') || '';
                    const m = brandHref.match(/\/gongsi\/(?:job\/)?([0-9a-zA-Z]+~?)/);
                    if (m) companyId = m[1];
                    let city = pickText(card, ['.job-area', '[class*="job-area"]'])
                        || lines.find(x => x.includes('·') && x.length < 40) || '';
                    let experience = lines.find(x => /经验|应届|在校|不限/.test(x) && x.length < 30) || '';
                    let education = lines.find(x => /本科|硕士|博士|大专|学历不限|中专|高中/.test(x) && x.length < 30) || '';
                    if (!company) {
                        company = lines.find(x =>
                            x !== title && x !== salary && x !== city &&
                            !/经验|应届|在校|不限|本科|硕士|博士|大专|学历|·|\\d+[-~]\\d+K/i.test(x) &&
                            x.length > 1 && x.length < 40
                        ) || '';
                    }
                    title = title.replace(/\\s+/g, ' ').trim();
                    // HR: extract from card
                    let hrName = '';
                    let hrTitle = '';
                    // 1) 尝试多种选择器
                    const hrEl = card.querySelector('[class*="info-public"], [class*="job-info"], [class*="hr-"], [class*="recruiter"], [class*="boss-name"], [class*="boss-title"], [class*="publisher"], [class*="boss-info"]');
                    let hrText = hrEl ? (hrEl.innerText || '').trim() : '';
                    // 2) 选择器没命中 → 从所有子元素找含HR关键词的元素
                    if (!hrText) {
                        const hrKeywords = ['人事','招聘','经理','主管','总监','HRBP','HR','负责人','助理','专员','猎头','顾问','HR','招聘者'];
                        for (const el of card.querySelectorAll('*')) {
                            const t = (el.innerText || '').trim();
                            if (t.length > 1 && t.length < 50 && hrKeywords.some(k => t.includes(k))) {
                                // 排除太长的文本（可能是整个卡片）
                                if (t.split(/\\n/).length <= 3) { hrText = t; break; }
                            }
                        }
                    }
                    // 3) 从文本行中正则匹配
                    if (!hrText) {
                        hrText = lines.find(x => /^[一-龥]{2,4}[\\s·•·]+/.test(x) && /人事|招聘|经理|主管|总监|HRBP|HR|负责人|助理|专员|猎头|顾问/.test(x)) || '';
                    }
                    // 4) 尝试匹配 "XXX·YYY" 或 "XXX YYY" 格式（HR名字·职位）
                    if (!hrText) {
                        for (const line of lines) {
                            const m = line.match(/^([一-龥]{2,4})[·•\\s]+(.{2,20})$/);
                            if (m && !/经验|应届|在校|不限|学历|本科|硕士|博士|大专|中专|高中|K|元|月薪|年薪/.test(line)) {
                                hrText = line; break;
                            }
                        }
                    }
                    // 5) 最后兜底：找含"立即沟通"附近的文字
                    if (!hrText) {
                        for (const el of card.querySelectorAll('[class*="btn"], [class*="chat"], [class*="start-chat"]')) {
                            const parent = el.parentElement;
                            if (parent) {
                                const sibs = parent.querySelectorAll(':scope > *');
                                for (const s of sibs) {
                                    const t = (s.innerText || '').trim();
                                    if (t && t !== (s.textContent||'').trim() && t.length < 30 && /[一-龥]{2,4}/.test(t)) {
                                        hrText = t; break;
                                    }
                                }
                            }
                        }
                    }
                    if (hrText) {
                        const hm = hrText.match(/^([一-龥]{2,4})[\\s·•·]+(.+)$/);
                        if (hm) { hrName = hm[1]; hrTitle = hm[2].trim(); }
                        else { hrName = hrText.substring(0, 4).replace(/[·•\\s]/g, ''); }
                    }
                    if (title && salary) {
                        seen.add(href);
                        cards.push({title, salary, company, company_id: companyId, city, experience, education, url: href, hr_name: hrName, hr_title: hrTitle});
                    }
                });
                return cards;
            }""")
        except Exception:
            return []

        jobs = []
        seen = set()
        for row in rows or []:
            url = (row.get("url") or "").strip()
            title = (row.get("title") or "").strip()
            if not url or not title or url in seen:
                continue
            seen.add(url)
            company = (row.get("company") or "").strip()
            if company and _is_invalid_company(company):
                company = ""
            jobs.append(
                {
                    "title": title,
                    "salary": decode_salary((row.get("salary") or "").strip()),
                    "company": company,
                    "company_id": (row.get("company_id") or row.get("companyId") or "").strip(),
                    "experience": (row.get("experience") or "").strip(),
                    "education": (row.get("education") or "").strip(),
                    "city": (row.get("city") or "").strip(),
                    "url": url,
                    "description": "",
                    "hr_name": (row.get("hr_name") or "").strip(),
                    "hr_title": (row.get("hr_title") or "").strip(),
                    "hr_active": "",
                    "company_size": "",
                    "industry": "",
                    "area_district": "",
                    "business_district": "",
                }
            )
        return jobs

    # ══════════════════════════════════════
    #  XHR 抓取（列表 + 详情），替代部分 DOM 抓取
    # ══════════════════════════════════════

    def _extract_zp_headers(self) -> dict:
        """从当前页面提取 BOSS 接口所需的 zp_token / token。失败时返回空 dict。

        三路提取：
        1) JS 全局对象 (window.zpToken / window.token)
        2) document.cookie 中的 bst / __a
        3) Playwright context cookies（处理 HttpOnly cookie 场景）
        """
        result = {}
        try:
            result = (
                self.page.evaluate(
                    r"""
                () => {
                    // 1) 全局对象
                    let zp_token = '', token = '';
                    for (const k of Object.keys(window)) {
                        try {
                            const v = window[k];
                            if (v && typeof v === 'object') {
                                if (!zp_token && typeof v.zpToken === 'string') zp_token = v.zpToken;
                                if (!token && typeof v.token === 'string') token = v.token;
                            }
                        } catch (e) {}
                    }
                    // 2) Cookie
                    if (!zp_token) {
                        const m = document.cookie.match(/(?:^|;\s*)bst=([^;]+)/);
                        if (m) zp_token = decodeURIComponent(m[1]);
                    }
                    if (!token) {
                        const m = document.cookie.match(/(?:^|;\s*)__a=([^;]+)/);
                        if (m) token = m[1].split('.')[0];
                    }
                    return { zp_token, token };
                }
                """
                )
                or {}
            )
        except Exception:
            result = {}

        # 3) 回退：通过 Playwright context cookies 提取（处理 HttpOnly）
        if not result.get("zp_token") or not result.get("token"):
            try:
                all_cookies = self._ctx.cookies()
                cookie_map = {c["name"]: c["value"] for c in all_cookies if c.get("name")}
                if not result.get("zp_token"):
                    zp = cookie_map.get("bst") or cookie_map.get("__zp_stoken__") or ""
                    if zp:
                        result["zp_token"] = zp
                if not result.get("token"):
                    t = cookie_map.get("__a", "")
                    if t:
                        result["token"] = t.split(".")[0] if "." in t else t
            except Exception:
                pass

        return result

    def _fetch_jobs_via_xhr(
        self,
        keyword: str,
        city_code: str,
        district_codes: list,
        scale_codes: list,
        max_pages: int = 1,
        area_names: list = None,
    ) -> list:
        """通过 BOSS XHR 抓取岗位列表 + 详情。

        列表接口：POST /wapi/zpgeek/search/joblist.json
        详情接口：GET  /wapi/zpgeek/job/detail.json?securityId=...&lid=...

        抓取失败（zp_token 缺失、接口非 0、网络错误等）一律返回空 list，
        调用方回退到 DOM 抓取。
        """
        try:
            headers = self._extract_zp_headers()
            zp_token = headers.get("zp_token") or ""
            token = headers.get("token") or ""

            detail_headers = {
                "zp_token": zp_token,
                "token": token,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.zhipin.com",
                "Referer": self.page.url,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            }

            all_jobs: list = []
            for page_num in range(1, max_pages + 1):
                form: dict = {
                    "page": str(page_num),
                    "pageSize": "15",
                    "city": str(city_code),
                    "query": keyword or "",
                    "expectInfo": "",
                    "multiSubway": "",
                    "position": "",
                    "jobType": "",
                    "salary": "",
                    "experience": "",
                    "degree": "",
                    "industry": "",
                    "stage": "",
                    "scene": "1",
                    "encryptExpectId": "",
                }
                if scale_codes:
                    form["scale"] = ",".join(str(s) for s in scale_codes)

                # multiBusinessDistrict 需要在 URL querystring 中重复出现
                import urllib.parse as _urlparse

                _ts = int(time.time() * 1000)
                _qs_parts = [f"_={_ts}"]
                for dc in district_codes or []:
                    _qs_parts.append(f"multiBusinessDistrict={_urlparse.quote(str(dc))}")
                for an in area_names or []:
                    _qs_parts.append(f"multiAreaDistrict={_urlparse.quote(str(an))}")
                _post_url = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json?" + "&".join(_qs_parts)

                # 浏览器内 fetch（自动带 cookie/referer，避免风控 code 37）
                _form_body = "&".join(f"{_urlparse.quote(str(k))}={_urlparse.quote(str(v))}" for k, v in form.items())
                _fetch_js = (
                    """async () => {
                    const r = await fetch("""
                    + json.dumps(_post_url)
                    + """, {
                        method: "POST",
                        headers: {"Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "XMLHttpRequest"},
                        body: """
                    + json.dumps(_form_body)
                    + """,
                        credentials: "include"
                    });
                    return await r.json();
                }"""
                )
                try:
                    payload = self.page.evaluate(_fetch_js)
                except Exception:
                    return []
                if not isinstance(payload, dict) or payload.get("code") != 0:
                    return []
                job_list = (payload.get("zpData") or {}).get("jobList") or []

                if not job_list:
                    break

                for j in job_list:
                    security_id = j.get("securityId") or ""
                    lid = j.get("lid") or ""
                    item = {
                        "title": j.get("jobName", ""),
                        "salary": j.get("salaryDesc", ""),
                        "company": j.get("brandName", ""),
                        "experience": j.get("jobExperience", ""),
                        "education": j.get("jobDegree", ""),
                        "city": j.get("cityName", ""),
                        "area_district": j.get("areaDistrict", ""),
                        "business_district": j.get("businessDistrict", ""),
                        "company_size": j.get("brandScaleName", ""),
                        "industry": j.get("brandIndustry", ""),
                        "url": f"https://www.zhipin.com/job_detail/{j.get('encryptJobId', '')}"
                        if j.get("encryptJobId")
                        else "",
                        "description": "",
                        "hr_name": j.get("bossName") or j.get("hrName") or (j.get("bossInfo") or {}).get("name") or "",
                        "hr_title": j.get("bossTitle")
                        or j.get("hrTitle")
                        or (j.get("bossInfo") or {}).get("title")
                        or "",
                        "security_id": security_id,
                        "lid": lid,
                        "encrypt_job_id": j.get("encryptJobId", ""),
                        "encrypt_brand_id": j.get("encryptBrandId", ""),
                        "encrypt_boss_id": j.get("encryptBossId", ""),
                        "hr_active": (
                            ("在线" if j.get("bossOnline") is True else "")
                            or j.get("activeTimeDesc")
                            or (j.get("bossInfo") or {}).get("activeTimeDesc")
                            or ""
                        ),
                        "legal_rep": "",
                    }
                    # 详情：拿 JD 补充（浏览器内 fetch，避免风控）
                    if security_id and lid:
                        try:
                            _detail_url = f"https://www.zhipin.com/wapi/zpgeek/job/detail.json?securityId={_urlparse.quote(security_id)}&lid={_urlparse.quote(lid)}"
                            p2 = self.page.evaluate(
                                """async () => {
                                try {
                                    const r = await fetch("""
                                + json.dumps(_detail_url)
                                + """, {
                                        headers: {"X-Requested-With": "XMLHttpRequest"},
                                        credentials: "include"
                                    });
                                    return await r.json();
                                } catch(e) { return {}; }
                            }"""
                            )
                        except Exception:
                            p2 = {}
                        if isinstance(p2, dict) and p2.get("code") == 0:
                            zd = p2.get("zpData") or {}
                            ji = zd.get("jobInfo") or {}
                            bi = zd.get("bossInfo") or {}
                            bc = zd.get("brandComInfo") or {}
                            ok = zd.get("oneKeyResumeInfo") or {}
                            item["description"] = ji.get("postDescription", "") or ""
                            item["hr_name"] = bi.get("name", "") or ""
                            item["hr_title"] = bi.get("title", "") or ""
                            item["hr_active"] = bi.get("activeTimeDesc", "") or ""
                            # detail API的activeTimeDesc更准确，直接取即可；无detail时用搜索结果的兜底
                            if not item["company"]:
                                item["company"] = bc.get("brandName", "") or ""
                            if not item["company_size"]:
                                item["company_size"] = bc.get("scaleName", "") or ""
                            if not item["industry"]:
                                item["industry"] = bc.get("industryName", "") or ""
                            _legal = bc.get("legalPerson") or bc.get("legalPersonName") or ""
                            if _legal and not item.get("legal_rep"):
                                item["legal_rep"] = _legal
                            item["encrypt_brand_id"] = bc.get("encryptBrandId", "") or item["encrypt_brand_id"]
                            item["can_send_resume"] = bool(ok.get("canSendResume", False))
                            item["can_send_wechat"] = bool(ok.get("canSendWechat", False))
                            item["can_send_phone"] = bool(ok.get("canSendPhone", False))
                            item["already_apply"] = bool(
                                (zd.get("atsOnlineApplyInfo") or {}).get("alreadyApply", False)
                            )
                    all_jobs.append(item)

            return all_jobs
        except Exception:
            return []

    def _scroll_all(self, max_scrolls: int = 60, stable_rounds: int = 3):
        """持续滚动直到没有新内容加载，或达到最大滚动次数。"""
        try:
            last_height = 0
            stable_count = 0
            for _ in range(max_scrolls):
                h = self.page.evaluate("document.body.scrollHeight")
                if h == last_height:
                    stable_count += 1
                    if stable_count >= stable_rounds:
                        break
                else:
                    stable_count = 0
                    last_height = h
                self.page.evaluate("window.scrollTo(0,%d)" % h)
                time.sleep(random.uniform(0.5, 1.0))
            # 滚回顶部，确保 DOM 完整渲染
            self.page.evaluate("window.scrollTo(0,0)")
            time.sleep(random.uniform(0.3, 0.5))
        except:
            pass

    def _extract_links(self):
        try:
            return self.page.evaluate("""()=>{
                const r=[];const s=new Set();
                document.querySelectorAll('a[href*="/job_detail/"]').forEach(a=>{
                    const h=a.href,t=(a.innerText||'').trim();
                    if(h&&t&&!s.has(h)&&t.length<60){s.add(h);r.push({href:h,title:t.substring(0,60)});}
                });return r;
            }""")
        except:
            return []

    # ── 详情页 ──

    def fetch_detail(self, url):
        """访问详情页，提取岗位描述 + HR/招聘者信息"""
        result = {"description": "", "hr_name": "", "hr_title": ""}
        try:
            self.page.goto(url, wait_until="load", timeout=45000)
            pause(2, 4)

            # ── 提取招聘者信息 ──
            try:
                hr_info = self.page.evaluate("""() => {
                    const body = document.body.innerText || '';
                    const lines = body.split('\\n').map(l => l.trim()).filter(Boolean);
                    let hrName = '', hrTitle = '';
                    for (let i = 0; i < lines.length; i++) {
                        const l = lines[i];
                        // BOSS直聘招聘者区域: 通常 "HR" "招聘者" "经理" 等标识
                        if (l.includes('HR') || l.includes('招聘者') || l.includes('招聘经理') ||
                            l.includes('人事') || l.includes('HRBP') || l.includes('猎头')) {
                            // 上一行或当前行可能是名字
                            if (i > 0 && lines[i-1].length <= 6 && !/\\d|省|市|区|路|号|招聘|公司|BOSS/.test(lines[i-1])) {
                                hrName = lines[i-1];
                            }
                            hrTitle = l;
                            break;
                        }
                    }
                    // 也尝试用选择器找招聘者信息区域
                    const bossSelectors = [
                        '.boss-info-attr', '.boss-info', '.recruiter-info',
                        '.boss-name', '.recruiter-name', '[class*="boss"]',
                    ];
                    for (const sel of bossSelectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText.trim()) {
                            const t = el.innerText.trim();
                            if (t.length <= 15) {
                                if (!hrName) hrName = t;
                                break;
                            }
                        }
                    }
                    return {hrName, hrTitle};
                }""")
                result["hr_name"] = (hr_info.get("hrName") or "").strip()
                result["hr_title"] = (hr_info.get("hrTitle") or "").strip()
            except:
                pass

            # ── 提取岗位描述 ──
            body = self.page.inner_text("body")
            lines = [l.strip() for l in body.split("\n") if l.strip()]

            skill_lines = []
            capture = False
            for l in lines:
                if "职位描述" in l or "岗位职责" in l:
                    capture = True
                    continue
                if capture:
                    if any(
                        stop in l
                        for stop in [
                            "公司介绍",
                            "工商信息",
                            "BOSS 安全提示",
                            "竞争力分析",
                        ]
                    ):
                        break
                    skill_lines.append(l)
            result["description"] = "\n".join(skill_lines) if skill_lines else ""

            # 如果 JS 没抓到招聘者信息，从文本中尝试解析
            if not result["hr_name"]:
                for i, l in enumerate(lines):
                    if l in ("HR", "招聘者", "招聘经理", "HRBP", "人事", "猎头"):
                        if i > 0 and len(lines[i - 1]) <= 6:
                            result["hr_name"] = lines[i - 1]
                            result["hr_title"] = l
                            break

        except Exception:
            pass
        return result


# ══════════════════════════════════════
#  分析
# ══════════════════════════════════════


def skill_gap(jobs):
    c = Counter()
    for j in jobs:
        text = (j.get("description") or "") + " " + (j.get("title") or "")
        seen = set()
        for cat, skills in parse_skills(text).items():
            for s in skills:
                if s.lower() not in seen:
                    seen.add(s.lower())
                    c[s] += 1
    have, miss = [], []
    for s, n in c.most_common():
        (have if s.lower() in MY_SKILLS else miss).append({"skill": s, "count": n})
    return {"have": have, "missing": miss, "total": len(jobs)}


# ══════════════════════════════════════
#  输出
# ══════════════════════════════════════


def output_report(jobs):
    lines = ["# 招聘日报 · %s\n" % DATE_STR]
    lines.append("> 来源：**BOSS直聘** · 无薪资限制 · 共 %d 条\n---\n" % len(jobs))

    for i, j in enumerate(jobs, 1):
        lines.append("### %d. %s %s" % (i, j["title"], j["salary"]))
        lines.append("- 公司: %s" % (j.get("company") or "未显示"))
        if j.get("city"):
            lines.append("- 城市: %s" % j["city"])
        if j.get("experience"):
            lines.append("- 经验: %s" % j["experience"])
        if j.get("education"):
            lines.append("- 学历: %s" % j["education"])
        if j.get("hr_name"):
            lines.append("- 👤 招聘者: %s (%s)" % (j["hr_name"], j.get("hr_title") or ""))
        if j.get("url"):
            lines.append("- 链接: %s" % j["url"])
        desc = j.get("description", "")
        if desc:
            lines.append("- 岗位技能：%s" % desc[:600])
        lines.append("---\n")
    lines.append("\n*数据采集于 %s，BOSS直聘*\n" % DATE_STR)
    return "\n".join(lines)


def skill_report(gap):
    lines = ["# 技能差距分析报告 · %s\n" % DATE_STR]
    lines.append("> 基于 BOSS 直聘 %d 个岗位\n---\n" % gap["total"])
    lines.append("## 一、✅ 你已拥有的技能\n")
    for item in gap["have"]:
        lines.append("- **%s**: %d个岗位" % (item["skill"], item["count"]))
    lines.append("\n## 二、🔍 需要查漏补缺\n")
    for item in gap["missing"][:30]:
        p = "🔴" if item["count"] >= 10 else "🟡" if item["count"] >= 5 else "🟢"
        lines.append("- %s **%s**: %d个岗位" % (p, item["skill"], item["count"]))
    return "\n".join(lines)


# ══════════════════════════════════════
#  主流程
# ══════════════════════════════════════


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--headless", action="store_true", default=False)
    ap.add_argument("--keywords")
    ap.add_argument("--output", default=str(OUTPUT_DIR))
    ap.add_argument("--max-jobs", type=int, default=64)
    args = ap.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    keywords = [k.strip() for k in args.keywords.split(",")] if args.keywords else KEYWORDS

    if not STATE_FILE.exists() and not args.login:
        print("⚠️ 请先运行: python3 boss_firefox.py --login")
        sys.exit(1)

    sc = BossScraper(headless=args.headless)
    sc.start()
    try:
        if args.login:
            sc.login()
            return

        # Phase 1: 搜索列表（关键词 × 城市）
        all_jobs = []
        seen = set()
        for city_name, city_code in CITIES.items():
            for kw in keywords:
                if len(all_jobs) >= args.max_jobs:
                    break
                print("\n📌 搜索: 「%s」@ %s" % (kw, city_name))
                try:
                    jobs = sc.search(kw, city_code)
                except Exception as e:
                    print("  ⚠️ 失败: %s" % e)
                    continue
                ok = []
                for j in jobs:
                    key = j["title"] + j["salary"] + j.get("company", "")
                    if key not in seen:
                        seen.add(key)
                        j["city"] = city_name  # 标记城市
                        ok.append(j)
                print("  %d条, 去重后%d条(累计%d)" % (len(jobs), len(ok), len(all_jobs)))
                all_jobs.extend(ok)
                if len(all_jobs) >= args.max_jobs:
                    print("  📊 已达上限%d条" % args.max_jobs)
                    break
                pause(2, 4)
            if len(all_jobs) >= args.max_jobs:
                break

        print("\n📊 共%d条" % len(all_jobs))
        if not all_jobs:
            return

        # Phase 2: 逐个访问详情页，提取岗位技能 + 招聘者信息
        print("\n🔍 开始采集岗位详情（共%d条）..." % len(all_jobs))
        success = 0
        for i, j in enumerate(all_jobs):
            if not j.get("url"):
                continue
            print(
                "  [%d/%d] %s" % (i + 1, len(all_jobs), j["title"][:25]),
                end=" ",
                flush=True,
            )
            detail = sc.fetch_detail(j["url"])
            if detail["description"]:
                j["description"] = detail["description"]
                success += 1
            j["hr_name"] = detail.get("hr_name", "")
            j["hr_title"] = detail.get("hr_title", "")
            if detail["description"]:
                print("✅ %d字 | HR: %s" % (len(detail["description"]), j["hr_name"] or "未识别"))
            else:
                print("⚠️ 无描述 | HR: %s" % (j["hr_name"] or "未识别"))
            time.sleep(random.uniform(1.5, 3.0))

        print("📊 详情采集: %d/%d条成功" % (success, len(all_jobs)))

        # 分析输出到终端即可
        gap = skill_gap(all_jobs)
        print("\n" + "=" * 60)
        print("📊 技能差距分析")
        print("=" * 60)
        for item in gap["have"][:10]:
            print("  ✅ %s: %d个岗位" % (item["skill"], item["count"]))
        for item in gap["missing"][:15]:
            p = "🔴" if item["count"] >= 10 else "🟡" if item["count"] >= 5 else "🟢"
            print("  %s %s: %d个岗位" % (p, item["skill"], item["count"]))

        # 输出——招聘日报 + CSV 数据文件
        with open(out_dir / ("招聘日报_%s.md" % DATE_STR), "w", encoding="utf-8") as f:
            f.write(output_report(all_jobs))
        print("📄 日报: %s/招聘日报_%s.md" % (out_dir, DATE_STR))

        # CSV 格式，方便 Excel 打开
        csv_path = out_dir / ("招聘数据_%s.csv" % DATE_STR)
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "title",
                    "company",
                    "salary",
                    "city",
                    "experience",
                    "education",
                    "hr_name",
                    "hr_title",
                    "url",
                    "description",
                ],
            )
            writer.writeheader()
            for j in all_jobs:
                writer.writerow({k: j.get(k, "") for k in writer.fieldnames})
        print("📊 数据: %s/招聘数据_%s.csv" % (out_dir, DATE_STR))
        print("\n✅ 完成！")

    finally:
        sc.close()


if __name__ == "__main__":
    main()
