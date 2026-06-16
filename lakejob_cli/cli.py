"""lakejob CLI — BOSS直聘岗位雷达命令行工具."""

import json
import os
import sys
import click

from . import client, output


@click.group()
def main():
    """lakejob — BOSS直聘岗位雷达 v0.1.0

    命令返回结构化 JSON 到 stdout，Agent 友好。
    """


# ── 版本 ──
@main.command("version")
def version_cmd():
    output.emit(output.ok("version", data={"version": "0.1.0"}))


# ── Schema：Agent 工具描述 ──
@main.command("schema")
def schema_cmd():
    path = __file__.replace("cli.py", "schema.json")
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    output.emit(output.ok("schema", data=schema))


# ── 搜索 ──
@main.command("search")
@click.argument("keyword")
@click.option("--city", default="", help="城市名（空则使用设置中的默认城市）")
@click.option("--welfare", default=None, help="福利筛选 如 双休,五险一金")
@click.option("--count", type=int, default=60, help="返回条数上限")
def search_cmd(keyword, city, welfare, count):
    """搜索BOSS直聘岗位。"""
    payload = {"keyword": keyword, "city": city or "", "limit": count}
    if welfare:
        payload["welfare"] = welfare
    resp = client.search(keyword, city, count)
    result = output.ok_or_fail(resp, "search")
    output.emit(result)


# ── 状态 ──
@main.command("status")
def status_cmd():
    resp = client.status()
    result = output.ok_or_fail(resp, "status")
    output.emit(result)


# ── 投递漏斗 ──
@main.command("stats")
def stats_cmd():
    resp = client.stats()
    result = output.ok_or_fail(resp, "stats")
    output.emit(result)


# ── 岗位列表 ──
@main.command("jobs")
@click.option("--status", "filter_status", default=None, help="pending / applied / replied")
@click.option("--limit", type=int, default=50)
def jobs_cmd(filter_status, limit):
    resp = client.jobs(filter_status, limit)
    result = output.ok_or_fail(resp, "jobs")
    output.emit(result)


# ── 投递单个 ──
@main.command("apply")
@click.argument("job_url")
def apply_cmd(job_url):
    resp = client.apply_one(job_url)
    result = output.ok_or_fail(resp, "apply")
    output.emit(result)


# ── 批量投递 ──
@main.command("apply-batch")
@click.option("--status", "filter_status", default="pending", help="pending 等状态")
def apply_batch_cmd(filter_status):
    r = client.jobs(filter_status, limit=200)
    if r.is_error:
        output.emit(output.fail("apply-batch", f"fetch jobs failed: {r.status_code}"))
        return
    jobs_list = r.json().get("jobs", [])
    urls = [j["job_url"] for j in jobs_list if j.get("job_url")]
    if not urls:
        output.emit(output.fail("apply-batch", "no job_urls found"))
        return
    resp = client.apply_batch(urls)
    result = output.ok_or_fail(resp, "apply-batch")
    output.emit(result)


# ── 扫描当前页面 ──
@main.command("scan")
def scan_cmd():
    """扫描当前BOSS搜索结果页，提取所有可见岗位。"""
    resp = client.scan()
    result = output.ok_or_fail(resp, "scan")
    output.emit(result)


# ── 扫描并一键投递 ──
@main.command("scan-apply")
def scan_apply_cmd():
    """扫描当前页面全部岗位并一键批量投递。"""
    resp = client.scan_and_apply()
    result = output.ok_or_fail(resp, "scan-apply")
    output.emit(result)


# ── 会话列表 ──
@main.command("conversations")
def conversations_cmd():
    resp = client.conversations()
    result = output.ok_or_fail(resp, "conversations")
    output.emit(result)


# ── 聊天记录 ──
@main.command("chat")
@click.argument("conv_id", type=int)
def chat_cmd(conv_id):
    resp = client.chat_messages(conv_id)
    result = output.ok_or_fail(resp, "chat")
    output.emit(result)


# ── 手动发消息 ──
@main.command("send")
@click.argument("conv_id", type=int)
@click.option("--msg", required=True, help="消息内容")
def send_cmd(conv_id, msg):
    resp = client.send_message(conv_id, msg)
    result = output.ok_or_fail(resp, "send")
    output.emit(result)


# ── 诊断 ──
@main.command("doctor")
def doctor_cmd():
    resp = client.doctor()
    result = output.ok_or_fail(resp, "doctor")
    output.emit(result)


# ── 扫码登录 ──
@main.command("login")
def login_cmd():
    resp = client.relogin()
    result = output.ok_or_fail(resp, "login")
    output.emit(result)


# ── AI JD分析 ──
@main.command("analyze")
@click.argument("job_url")
@click.option("--title", default="", help="岗位名称")
@click.option("--company", default="", help="公司名")
@click.option("--desc", default="", help="JD描述")
def analyze_cmd(job_url, title, company, desc):
    resp = client.analyze(job_url, title, company, desc)
    output.emit(output.ok_or_fail(resp, "analyze"))


# ── 候选池 ──
@main.command("shortlist")
@click.argument("action", type=click.Choice(["list", "add", "remove"]))
@click.option("--job-url", help="岗位URL")
@click.option("--title", default="", help="岗位名称")
@click.option("--company", default="", help="公司名")
@click.option("--id", "sid", type=int, help="shortlist ID")
def shortlist_cmd(action, job_url, title, company, sid):
    if action == "list":
        resp = client.get_shortlists()
        output.emit(output.ok_or_fail(resp, "shortlist"))
    elif action == "add":
        if not job_url:
            output.emit(output.fail("shortlist", "--job-url required"))
            return
        resp = client.add_shortlist(job_url, title, company)
        output.emit(output.ok_or_fail(resp, "shortlist"))
    elif action == "remove":
        if not sid:
            output.emit(output.fail("shortlist", "--id required"))
            return
        resp = client.remove_shortlist(sid)
        output.emit(output.ok_or_fail(resp, "shortlist"))


# ── 服务管理 ──
@main.command("server")
@click.option("--start", is_flag=True, help="启动后台服务")
@click.option("--stop", is_flag=True, help="停止后台服务（精确杀 boss_app 进程，不动其他 python）")
@click.option("--port", type=int, default=8010, help="服务端口")
def server_cmd(start, stop, port):
    import subprocess, os

    project_dir = os.path.dirname(os.path.dirname(__file__))
    if not os.path.exists(os.path.join(project_dir, "boss_app.py")):
        project_dir = os.environ.get("LAKEJOB_PROJECT", r"D:\lake\jiaoben\job\lakejobai-job-radar-main")

    if start:
        cmd = ["python", os.path.join(project_dir, "boss_app.py"), "--port", str(port)]
        subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000), cwd=project_dir)
        output.emit(output.ok("server", data={"status": "started", "url": f"http://127.0.0.1:{port}"}))
    elif stop:
        killed = _kill_boss_app()
        output.emit(output.ok("server", data={"status": "stopped", "killed": killed}))
    else:
        resp = client.status()
        if resp.is_error:
            output.emit(output.ok("server", data={"status": "not running"}))
        else:
            output.emit(output.ok("server", data={"status": "running"}))


@main.command("restart")
@click.option("--port", type=int, default=8010, help="端口号")
def restart_cmd(port):
    """杀旧进程 + 起新服务。Windows 用 wmic 精确杀，不动其他 python。"""
    import subprocess, os, time, urllib.request

    project_dir = os.path.dirname(os.path.dirname(__file__))
    if not os.path.exists(os.path.join(project_dir, "boss_app.py")):
        project_dir = os.environ.get("LAKEJOB_PROJECT", r"D:\lake\jiaoben\job\lakejobai-job-radar-main")

    boss_py = os.path.join(project_dir, "boss_app.py")
    if not os.path.exists(boss_py):
        output.emit(output.fail("restart", f"找不到 boss_app.py"))
        return

    killed = _kill_boss_app()
    if killed:
        click.echo(f"  killed {killed} process(es)")
    time.sleep(2)

    log_path = os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "boss_app.log")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    with open(log_path, "w", encoding="utf-8") as lf:
        subprocess.Popen(
            [sys.executable, boss_py, "--port", str(port)],
            stdout=lf,
            stderr=subprocess.STDOUT,
            cwd=project_dir,
            creationflags=flags,
        )

    time.sleep(5)
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/health"), timeout=3)
        output.emit(output.ok("restart", data={"port": port, "url": f"http://127.0.0.1:{port}"}))
    except Exception:
        output.emit(output.ok("restart", data={"port": port, "url": f"http://127.0.0.1:{port}", "note": "稍等再试"}))


def _kill_boss_app():
    """精确杀死所有 boss_app.py 主进程（python 解释器执行 boss_app.py 的）。
    只匹配 cmdline 中包含 'boss_app.py' 的 python 进程，避免误杀任何包含 'boss_app' 字样的 shell。
    返回杀死数。
    """
    killed = 0
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None

    if psutil is not None:
        my_pid = os.getpid()
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                pid = p.info.get("pid")
                if pid == my_pid:
                    continue
                cmd = p.info.get("cmdline") or []
                if not isinstance(cmd, list):
                    continue
                name = (p.info.get("name") or "").lower()
                if "python" not in name and not any("python" in (a or "").lower() for a in cmd[:1]):
                    continue
                if not any("boss_app.py" in (a or "") for a in cmd):
                    continue
                p.kill()
                killed += 1
            except Exception:
                continue
        return killed

    # 兜底：用 wmic（旧 Windows 系统），但限定 commandline 必须含 boss_app.py
    import subprocess

    try:
        r = subprocess.run(
            "wmic process where \"name='python.exe' and commandline like '%%boss_app.py%%'\" get processid",
            capture_output=True,
            shell=True,
            text=True,
            timeout=10,
        )
        for line in r.stdout.split("\n"):
            line = line.strip()
            if line.isdigit() and int(line) != os.getpid():
                try:
                    subprocess.run(f"taskkill /F /PID {line}", capture_output=True, shell=True, timeout=5)
                    killed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return killed


# ── 智能投递 ──
@main.command("smart-send")
@click.option("--keyword", default="", help="搜索关键词")
@click.option("--city", default="", help="城市")
@click.option("--greeting", default="", help="自定义招呼语")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
@click.option("--districts", default="", help="多区 code 列表，逗号分隔，如 440118,440113")
@click.option("--company-size", default="", help="多规模 code 列表，逗号分隔，如 302,303")
def smart_send_cmd(keyword, city, greeting, yes, districts, company_size):
    """智能投递：搜索→按公司分组→挑最高HR→批量投递。"""
    if not keyword:
        output.emit(output.fail("smart-send", "--keyword 必填"))
        return
    ds_list = [x.strip() for x in districts.split(",") if x.strip()] or None
    cs_list = [x.strip() for x in company_size.split(",") if x.strip()] or None
    try:
        resp = client.company_preview(
            keyword=keyword,
            city=city,
            districts=ds_list,
            company_size=cs_list,
        )
        data = resp.json() if not resp.is_error else None
    except Exception as e:
        output.emit(output.fail("smart-send", f"preview 失败: {e}"))
        return
    if resp.is_error or not data or not data.get("ok"):
        output.emit(output.fail("smart-send", f"preview 失败: {(data or {}).get('message', '')}"))
        return

    companies = data.get("companies") or []
    output.emit(output.ok("smart-send-preview", data={"total_companies": len(companies), "keyword": keyword}))

    targets = []
    for c in companies[:20]:
        if c.get("already_applied"):
            continue
        tj = c.get("target_job") or {}
        if not tj.get("url"):
            continue
        top = c.get("top_hr") or {}
        targets.append(
            {
                "company": c["company"],
                "job_url": tj["url"],
                "hr_name": top.get("name", ""),
                "hr_title": top.get("title", ""),
                "is_boss": top.get("is_boss", False),
                "boss_confidence": top.get("boss_confidence", ""),
            }
        )

    if not targets:
        output.emit(output.fail("smart-send", "没有可投递的公司"))
        return

    if not yes:
        click.echo(f"\n  共 {len(targets)} 家公司待投递：")
        for t in targets:
            boss_tag = ""
            if t.get("is_boss"):
                conf = {"high": "★老板", "medium": "疑似老板", "low": "可能老板?"}.get(
                    t.get("boss_confidence", ""), "疑似老板"
                )
                boss_tag = f"  [{conf}]"
            click.echo(f"    {t['company']}  →  {t.get('hr_name') or 'HR'} ({t.get('hr_title', '')}){boss_tag}")
        click.echo("\n  确认？[y/N] ", nl=False)
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes"):
            output.emit(output.ok("smart-send", data={"cancelled": True}))
            return

    resp2 = client.smart_send(company="", job_url="", targets=targets, confirm=True)
    result = output.ok_or_fail(resp2, "smart-send")
    try:
        payload = resp2.json()
        if isinstance(result.get("data"), dict) and isinstance(payload, dict):
            result["data"].update(payload)
    except Exception:
        pass
    output.emit(result)


if __name__ == "__main__":
    main()
