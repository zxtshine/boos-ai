#!/usr/bin/env python3
"""
招聘网站爬虫 —— 每天自动扫岗位，按条件筛选，生成日报。

默认抓的是 智联招聘，搜 AI 相关岗位。
你也可以改关键词、改薪资范围、改学历要求。

用法:
  python scraper.py                                            # 默认配置
  python scraper.py --keywords "Java,后端开发,Go"               # 自定义搜啥
  python scraper.py --salary-min 20 --salary-max 35            # 钱不够不看
  python scraper.py --no-db                                    # 不存数据库
  python scraper.py --out ./my_reports                         # 日报放哪
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import ssl
from datetime import date

import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ============================================================
# 默认值
# ============================================================

DEFAULT_KEYWORDS = ["Python开发", "前端开发", "产品经理", "数据分析"]
DEFAULT_SALARY_MIN = 15
DEFAULT_SALARY_MAX = 25
DEFAULT_EXP_MIN = 1
DEFAULT_EXP_MAX = 3
DEFAULT_EDUCATION = "bachelor"
DEFAULT_OUTPUT_DIR = "./reports"
DEFAULT_CONFIG_FILE = "config.yaml"

TODAY = date.today().isoformat()
DATE_STR = date.today().strftime("%Y-%m-%d")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


# ============================================================
# 读配置（命令行 > 配置文件 > 默认值）
# ============================================================

def load_config():
    """从命令行参数 + 配置文件合并出最终配置"""
    
    # 1. 先读命令行参数
    parser = argparse.ArgumentParser(description="招聘网站爬虫 — 每天自动扫岗位")
    parser.add_argument("--keywords", help="搜索关键词，逗号分隔，比如: Java,Go,Python")
    parser.add_argument("--salary-min", type=int, help="最低月薪(K)，比如 15 表示 15K")
    parser.add_argument("--salary-max", type=int, help="最高月薪(K)")
    parser.add_argument("--exp-min", type=int, help="最少经验(年)")
    parser.add_argument("--exp-max", type=int, help="最多经验(年)")
    parser.add_argument("--education", choices=["bachelor", "any"], help="学历要求: bachelor=本科及以上, any=不限")
    parser.add_argument("--out", "--output-dir", dest="output_dir", help="日报输出目录")
    parser.add_argument("--no-db", "--no-mysql", dest="no_db", action="store_true", help="不存数据库，只生成日报")
    parser.add_argument("--config", help="配置文件路径，默认 config.yaml")
    args = parser.parse_args()
    
    # 2. 读配置文件
    config_path = args.config or DEFAULT_CONFIG_FILE
    file_config = {}
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            raw = f.read()
            # 替换环境变量 ${VAR}
            raw = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ''), raw)
            file_config = yaml.safe_load(raw) or {}
    
    # 3. 合并：命令行参数优先
    def first(*sources):
        for s in sources:
            if s is not None:
                return s
        return None
    
    # 关键词
    kw_arg = args.keywords
    kw_file = file_config.get("keywords")
    kw = first(kw_arg, kw_file, DEFAULT_KEYWORDS)
    if isinstance(kw, str):
        kw = [k.strip() for k in kw.split(",")]
    
    # 数据库配置
    db_cfg = file_config.get("database", {})
    db_type = db_cfg.get("type", "mysql")
    db_host = db_cfg.get("host", "localhost")
    db_user = db_cfg.get("user", "root")
    db_password = db_cfg.get("password", "") or os.environ.get("DB_PASSWORD", "") or os.environ.get("MYSQL_PWD", "")
    db_name = db_cfg.get("database", "ai_jobs_db")
    no_db = args.no_db or (not db_password and db_type == "mysql")
    
    return {
        "keywords": kw,
        "salary_min": first(args.salary_min, file_config.get("filters", {}).get("salary_min"), DEFAULT_SALARY_MIN),
        "salary_max": first(args.salary_max, file_config.get("filters", {}).get("salary_max"), DEFAULT_SALARY_MAX),
        "exp_min": first(args.exp_min, file_config.get("filters", {}).get("experience_min"), DEFAULT_EXP_MIN),
        "exp_max": first(args.exp_max, file_config.get("filters", {}).get("experience_max"), DEFAULT_EXP_MAX),
        "education": first(args.education, file_config.get("filters", {}).get("education"), DEFAULT_EDUCATION),
        "output_dir": first(args.output_dir, file_config.get("output_dir"), DEFAULT_OUTPUT_DIR),
        "no_db": no_db,
        "db_host": db_host,
        "db_user": db_user,
        "db_password": db_password,
        "db_name": db_name,
    }


# ============================================================
# 智联招聘 爬虫
# ============================================================

def scrape_zhaopin(keyword="Python开发", max_jobs=50):
    """去智联招聘搜岗位，返回列表（多页）"""
    all_jobs = []
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
            )
            page = context.new_page()
            
            kw = urllib.parse.quote(keyword)
            
            # 爬多页
            for page_num in range(1, 4):  # 第1-3页
                if len(all_jobs) >= max_jobs:
                    break
                url = f"https://sou.zhaopin.com/?jl=489&kw={kw}&p={page_num}"
                
                page.goto(url, wait_until="networkidle", timeout=20000)
                page.wait_for_timeout(3000)
                
                html = page.content()
                soup = BeautifulSoup(html, "html.parser")
                
                # 选岗位卡片
                cards = []
                for sel in [
                    "div[class*='joblist-box'] div[class*='item']",
                    "div[class*='positionlist'] div[class*='item']",
                    "div[class*='job-card']",
                    "li[class*='job']",
                ]:
                    cards = soup.select(sel)
                    if cards and len(cards) > 1:
                        break
                
                if cards and len(cards) > 1:
                    for card in cards[:max_jobs]:
                        try:
                            job = _parse_card(card)
                            if job and job["title"]:
                                if job.get("detail_url"):
                                    jd = _fetch_jd_detail(job["detail_url"])
                                    if jd:
                                        job["requirements"] = jd
                                all_jobs.append(job)
                        except:
                            continue
                    print(f"  [智联] 关键词'{keyword}' 第{page_num}页: {len(cards)}个")
                else:
                    print(f"  [智联] 关键词'{keyword}' 第{page_num}页: 没有找到岗位卡片")
            
            context.close()
            browser.close()
            
    except Exception as e:
        print(f"  [智联] 爬失败了: {e}", file=sys.stderr)
    
    return all_jobs


def _parse_card(card):
    """从一个卡片元素里抠出岗位信息"""
    title_el = card.select_one("a.jobinfo__name, .jobinfo__name")
    salary_el = card.select_one(".jobinfo__salary, p.jobinfo__salary")
    company_el = card.select_one("a.companyinfo__name, .companyinfo__name")
    
    exp = ''
    edu = ''
    info_items = card.select(".jobinfo__other-info-item")
    texts = [item.get_text(strip=True) for item in info_items]
    for t in texts:
        if '年' in t and ('经验' in t or re.match(r'\d', t.strip())):
            exp = t
        elif any(x in t for x in ['本科', '硕士', '博士', '大专']):
            edu = t
    
    card_text = card.get_text()
    if not exp:
        m = re.search(r'(\d+-\d+年|\d+年以下|\d+年以上|经验不限)', card_text)
        if m: exp = m.group(1)
    if not edu:
        m = re.search(r'(本科|硕士|博士|大专|学历不限)', card_text)
        if m: edu = m.group(1)
    
    title = title_el.get_text(strip=True) if title_el else ''
    salary = salary_el.get_text(strip=True) if salary_el else ''
    company = company_el.get_text(strip=True) if company_el else ''
    
    if not title:
        return None
    
    # "2-4万" 转成 "20K-40K"
    if salary:
        nums = re.findall(r'(\d+\.?\d*)', salary.replace('万', '').strip())
        if '万' in salary and len(nums) >= 2:
            salary = f"{int(float(nums[0])*10)}K-{int(float(nums[1])*10)}K"
    
    link = title_el.get('href', '') if title_el else ''
    if link and not link.startswith('http'):
        link = 'https://www.zhaopin.com' + link
    
    card_text_full = card.get_text(separator='\n')
    
    return {
        "title": title,
        "company": company,
        "salary": salary,
        "experience": exp,
        "education": edu,
        "requirements": card_text_full[:1000],
        "source": link,
        "detail_url": link,
    }


def _fetch_jd_detail(url):
    """去岗位详情页拉完整 JD"""
    if not url:
        return ""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        })
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        html = resp.read().decode("utf-8", errors="replace")
        
        soup = BeautifulSoup(html, "html.parser")
        for sel in ["div[class*='describtion']", "div[class*='job-description']",
                     "div[class*='detail']", "article", ".job-box"]:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(strip=True, separator="\n")
                if len(text) > 80:
                    return text[:3000]
        return ""
    except:
        return ""


# ============================================================
# 过滤
# ============================================================

def salary_ok(s, lo, hi):
    if not s: return False
    s = s.replace('~','-').replace('—','-').replace('，','-').replace(' ','').replace('k','K').replace('K','')
    mul = 1
    if '万' in s:
        mul = 10
        s = s.replace('万','')
    elif '千' in s:
        s = s.replace('千','')
    n = re.findall(r'(\d+\.?\d*)', s)
    if len(n) >= 2:
        low = float(n[0]) * mul
        high = float(n[1]) * mul
        if mul == 1 and low < 5 and high < 10:
            low *= 10; high *= 10
        return low <= hi and high >= lo
    return False


def exp_ok(s, lo, hi):
    if not s: return False
    n = re.findall(r'(\d+)', s)
    if len(n) >= 2:
        l, h = int(n[0]), int(n[1])
        return l <= hi and h >= lo
    return False


def edu_ok(s, mode):
    if mode == "any":
        return True
    if not s:
        return True
    if any(x in s for x in ['本科', '硕士', '博士']):
        return True
    if '学历不限' in s:
        return True
    return False


# ============================================================
# 分类引擎 —— 自动给岗位打标签
# ============================================================

CATEGORIES = {
    "编程语言": ["python","java","go","golang","rust","c++","typescript","javascript","js","ts"],
    "AI/ML框架": ["langchain","llamaindex","pytorch","tensorflow","transformers","vllm","onnx","huggingface"],
    "大模型技术": ["大模型","llm","gpt","rag","agent","prompt","微调","finetune","embedding","mcp"],
    "数据库": ["mysql","redis","mongodb","elasticsearch","milvus","pinecone","kafka","faiss"],
    "部署运维": ["docker","kubernetes","k8s","gpu","cuda","serving","devops","ci/cd","linux"],
    "架构设计": ["架构","微服务","高并发","分布式","系统设计","工作流"],
}

def classify(text):
    tl = text.lower()
    m = {}
    for c, kw in CATEGORIES.items():
        f = [k for k in kw if k.lower() in tl]
        if f: m[c] = f
    return m


# ============================================================
# 存 MySQL
# ============================================================

def save_to_db(jobs, cfg):
    if cfg["no_db"]:
        return
    for j in jobs:
        cls = classify(j["title"] + " " + j.get("requirements","") + " " + j.get("experience","") + " " + j.get("education",""))
        for cat, kw in cls.items():
            kw_str = ",".join(kw)
            req = (j.get("requirements","") or "")[:500]
            sql = f"""INSERT INTO job_requirements 
(collected_date,title,company,salary,experience,education,requirement_category,requirement_text,source_url)
VALUES ('{TODAY}','{_e(j['title'])}','{_e(j['company'])}','{_e(j['salary'])}',
'{_e(j['experience'])}','{_e(j['education'])}','{_e(cat)}',
'{_e(kw_str+' | '+req[:200])}','{_e(j['source'])}');"""
            subprocess.run(
                f'mysql -h {cfg["db_host"]} -u {cfg["db_user"]} '
                f'-p\'{cfg["db_password"]}\' {cfg["db_name"]} -e "{sql}" 2>/dev/null',
                shell=True, capture_output=True)


def save_summary(jobs, cfg):
    if cfg["no_db"]:
        return
    subprocess.run(
        f'mysql -h {cfg["db_host"]} -u {cfg["db_user"]} -p\'{cfg["db_password"]}\' '
        f'{cfg["db_name"]} -e "DELETE FROM job_requirements WHERE '
        f"collected_date='{TODAY}' AND requirement_category='__summary__';\" 2>/dev/null",
        shell=True, capture_output=True)
    cc = {}
    for j in jobs:
        for c in classify(j["title"] + " " + j.get("requirements","")):
            cc[c] = cc.get(c, 0) + 1
    for c, n in sorted(cc.items(), key=lambda x: -x[1]):
        subprocess.run(
            f'mysql -h {cfg["db_host"]} -u {cfg["db_user"]} -p\'{cfg["db_password"]}\' '
            f'{cfg["db_name"]} -e "INSERT INTO job_requirements '
            f"(collected_date,title,company,salary,experience,education,"
            f"requirement_category,requirement_text,source_url) VALUES "
            f"('{TODAY}','【汇总】{DATE_STR}','','','','','__summary__',"
            f"'{_e(f'类别: {c}, 出现: {n}次')}','');\" 2>/dev/null",
            shell=True, capture_output=True)


def clean_db(cfg):
    if cfg["no_db"]:
        return
    subprocess.run(
        f'mysql -h {cfg["db_host"]} -u {cfg["db_user"]} -p\'{cfg["db_password"]}\' '
        f'{cfg["db_name"]} -e "DELETE FROM job_requirements WHERE '
        f"collected_date='{TODAY}';\" 2>/dev/null",
        shell=True, capture_output=True)


def _e(s):
    if not s: return ""
    return (s.replace("\\","\\\\\\\\").replace("'","\\'")
            .replace('"','\\"').replace("\n"," ").replace("\r"," "))


# ============================================================
# 生成日报 Markdown
# ============================================================

def render_md(jobs, cfg):
    lines = []
    edu_label = "本科及以上" if cfg["education"] == "bachelor" else "不限"
    lines.append(f"# 招聘日报 · {DATE_STR}\n")
    lines.append(
        f"> 来源：**智联招聘** · "
        f"薪资 **{cfg['salary_min']}K-{cfg['salary_max']}K** · "
        f"经验 **{cfg['exp_min']}-{cfg['exp_max']}年** · "
        f"学历 **{edu_label}** · "
        f"共 {len(jobs)} 条\n---\n")
    
    lines.append("## 概览\n")
    ss = sorted(set(j["salary"] for j in jobs if j["salary"]))
    cs = sorted(set(j["company"] for j in jobs if j["company"]))
    if ss: lines.append(f"- 💰 薪资: {' | '.join(ss[:8])}")
    if cs: lines.append(f"- 🏢 公司: {'、'.join(cs[:10])}")
    lines.append("")
    
    cc = {}
    for j in jobs:
        for c in classify(j["title"] + " " + j.get("requirements","")):
            cc[c] = cc.get(c, 0) + 1
    if cc:
        lines.append("### 技能要求分布\n")
        for c, n in sorted(cc.items(), key=lambda x: -x[1]):
            bar = "■" * min(n, 15)
            lines.append(f"- {c}: {bar} ({n}条)")
    
    lines.append("\n---\n## 岗位列表\n")
    for i, j in enumerate(jobs, 1):
        lines.append(f"### {i}. {j['title']}")
        lines.append(f"- 公司: {j['company']}")
        lines.append(f"- 薪资: {j['salary']}")
        lines.append(f"- 经验: {j['experience']}")
        lines.append(f"- 学历: {j['education']}")
        lines.append(f"- 链接: {j['source']}\n")
        
        req = j.get("requirements","").strip()
        if req:
            lines.append("**岗位要求：**\n")
            lines.append("```\n" + req[:2000] + "\n```\n")
        
        cl = classify(req + " " + j["title"])
        if cl:
            lines.append("  " + " ".join(f"`{c}`" for c in cl))
        lines.append("\n---\n")
    
    lines.append(f"\n---\n*数据采集于 {DATE_STR}*\n")
    return "\n".join(lines)


# ============================================================
# 主流程
# ============================================================

def main():
    cfg = load_config()
    
    kw_list = cfg["keywords"]
    print(f"搜: {', '.join(kw_list)}")
    print(f"筛: 薪资{cfg['salary_min']}K-{cfg['salary_max']}K, "
          f"经验{cfg['exp_min']}-{cfg['exp_max']}年, "
          f"学历{'本科及以上' if cfg['education']=='bachelor' else '不限'}")
    print(f"日报放: {cfg['output_dir']}")
    if cfg["no_db"]:
        print("数据库: 跳过")
    else:
        print(f"数据库: mysql://{cfg['db_host']}/{cfg['db_name']}")
    print()
    
    os.makedirs(cfg["output_dir"], exist_ok=True)
    
    # 爬
    all_jobs = []
    for kw in kw_list:
        jobs = scrape_zhaopin(kw)
        all_jobs.extend(jobs)
        if len(all_jobs) >= 100:
            break
    
    # 去重
    seen = set()
    unique = []
    for j in all_jobs:
        key = (j["title"], j["company"])
        if key not in seen and j["title"]:
            seen.add(key)
            unique.append(j)
    
    # 过滤
    filtered = [j for j in unique
                if salary_ok(j["salary"], cfg["salary_min"], cfg["salary_max"])
                and exp_ok(j["experience"], cfg["exp_min"], cfg["exp_max"])
                and edu_ok(j.get("education",""), cfg["education"])]
    
    # 如果太少，放宽
    if len(filtered) < 10:
        extra = [j for j in unique
                 if salary_ok(j["salary"], cfg["salary_min"]-5, cfg["salary_max"]+5)
                 and exp_ok(j["experience"], cfg["exp_min"], cfg["exp_max"]+2)
                 and edu_ok(j.get("education",""), cfg["education"])]
        seen_f = set((j["title"], j["company"]) for j in filtered)
        for j in extra:
            k = (j["title"], j["company"])
            if k not in seen_f and len(filtered) < 15:
                seen_f.add(k)
                filtered.append(j)
    
    filtered = filtered[:100]
    
    if not filtered:
        print("一条都没捞到，可能是网站改版了或者关键词没匹配上")
        return
    
    print(f"\n搞定 {len(filtered)} 条\n")
    
    # 出日报
    md = render_md(filtered, cfg)
    md_path = os.path.join(cfg["output_dir"], f"招聘日报_{DATE_STR}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"日报: {md_path}")
    
    # 存库
    if not cfg["no_db"]:
        try:
            clean_db(cfg)
            save_to_db(filtered, cfg)
            save_summary(filtered, cfg)
            print("已存 MySQL")
        except Exception as e:
            print(f"存 MySQL 失败了（不影响日报）: {e}")
    
    # 打出来看看
    print(f"\n{'─'*50}")
    for j in filtered:
        print(f"  {j['title'][:25]:25s} | {str(j['company'])[:15]:15s} | {j['salary']}")
    print(f"{'─'*50}")


if __name__ == "__main__":
    main()
