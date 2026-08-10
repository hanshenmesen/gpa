#!/usr/bin/env python3
"""
GPA 实战 Demo：Mail.app 批量邮件草稿生成器
============================================
现实场景：你每周要给不同客户发相似的跟进邮件。
以前：手动一封封写。现在：录制一次，批量重放。

流程：
  Phase A · 录制  → 打开 Mail.app，手动写一封草稿邮件
  Phase B · 构建  → LLM 分析，提取 to/subject/body 为变量
  Phase C · 批量重放 → 用 3 份不同数据自动生成 3 封草稿

运行前确保：
  - Mail.app 已登录账户（能打开"新建邮件"窗口）
  - 屏幕没有全屏遮挡
"""

import subprocess, sys, time, pathlib, warnings, os, json
warnings.filterwarnings("ignore")
os.environ["TOKENIZERS_PARALLELISM"] = "false"

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

import pyautogui
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from rich import box

console = Console()
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05

WORKFLOW_ID = "mail_draft_workflow"

# ── 要批量发送的邮件数据 ───────────────────────────────────────────────────────
BATCH_EMAILS = [
    {
        "to": "alice@example.com",
        "subject": "项目进展同步 - 第 12 周",
        "body": "Hi Alice，本周开发进度顺利，预计下周完成 MVP，请安排评审时间。",
    },
    {
        "to": "bob@acme.corp",
        "subject": "Q4 报告已准备就绪",
        "body": "Hi Bob，Q4 数据报告已整理完毕，附件请查收，如有问题随时联系。",
    },
    {
        "to": "carol@startup.io",
        "subject": "合作提案跟进",
        "body": "Hi Carol，上次会议讨论的合作方案，我们已完善细节，请问您这周有空确认吗？",
    },
]

# 录制时用的占位内容（LLM 会识别并提取为变量）
RECORD_TO      = "demo.recipient@example.com"
RECORD_SUBJECT = "测试邮件主题"
RECORD_BODY    = "这是邮件正文内容，请在此处填写具体信息。"

# ── helpers ───────────────────────────────────────────────────────────────────

def banner(title, subtitle=""):
    lines = f"[bold white]{title}[/bold white]"
    if subtitle:
        lines += f"\n[dim]{subtitle}[/dim]"
    console.print(Panel.fit(lines, border_style="cyan"))

def phase(n, title):
    console.print()
    console.rule(f"[bold magenta]{'─' * 3} Phase {n}: {title} {'─' * 3}[/bold magenta]")
    time.sleep(0.3)

def ok(msg):   console.print(f"  [bold green]✓[/bold green] {msg}")
def info(msg): console.print(f"  [dim]→ {msg}[/dim]")
def warn(msg): console.print(f"  [yellow]![/yellow] {msg}")

def wait(sec, msg=""):
    if msg:
        info(msg)
    time.sleep(sec)

# ── Mail.app helpers ──────────────────────────────────────────────────────────

def activate_mail():
    subprocess.run(
        ["osascript", "-e", 'tell application "Mail" to activate'],
        capture_output=True,
    )
    time.sleep(0.8)

def new_mail_message():
    """Cmd+N 新建邮件窗口。"""
    activate_mail()
    pyautogui.hotkey("command", "n")
    time.sleep(1.2)

def close_draft_save():
    """Cmd+W 关闭并保存为草稿。"""
    pyautogui.hotkey("command", "w")
    time.sleep(0.8)
    # 弹出"存储"对话框时按 S
    pyautogui.press("s")
    time.sleep(0.5)

# ── Phase A: 录制 ─────────────────────────────────────────────────────────────

def phase_a_record():
    phase("A", "录制示范操作（Mail.app 写一封草稿邮件）")

    from gpa.recording.recorder import Recording, RecordedEvent, capture_screenshot

    recording = Recording()

    def rec_click(x, y, label):
        pyautogui.click(int(x), int(y))
        time.sleep(0.5)
        sc = capture_screenshot()
        recording.events.append(RecordedEvent(
            event_type="click", x=float(x), y=float(y),
            button="left", screenshot=sc,
            active_app="Mail", pause_before=0.5,
            timestamp=time.time(),
        ))
        ok(f"CLICK  {label}")

    def rec_type(text, label):
        # 用 pyperclip 粘贴以支持中文
        import subprocess as sp
        proc = sp.Popen(["pbcopy"], stdin=sp.PIPE)
        proc.communicate(text.encode("utf-8"))
        pyautogui.hotkey("command", "v")
        time.sleep(0.4)
        sc = capture_screenshot()
        recording.events.append(RecordedEvent(
            event_type="type", value=text, screenshot=sc,
            active_app="Mail", pause_before=0.4,
            timestamp=time.time(),
        ))
        ok(f"TYPE   {label}  →  {text!r}")

    def rec_hotkey(combo, label):
        pyautogui.hotkey(*combo.split("+"))
        time.sleep(0.6)
        sc = capture_screenshot()
        recording.events.append(RecordedEvent(
            event_type="hotkey", value=combo, screenshot=sc,
            active_app="Mail", pause_before=0.6,
            timestamp=time.time(),
        ))
        ok(f"HOTKEY {label}  ({combo})")

    # ── 获取屏幕尺寸供坐标参考 ──
    from gpa.recording.recorder import capture_screenshot as cs
    ss = cs()
    W, H = ss.width, ss.height

    console.print()
    console.print("  [bold]开始打开 Mail.app 并新建邮件 …[/bold]")
    wait(1)

    # 打开 Mail.app
    subprocess.Popen(["open", "-a", "Mail"])
    wait(2.5, "等待 Mail.app 启动")
    activate_mail()
    wait(1)

    # 新建邮件
    rec_hotkey("command+n", "新建邮件")
    wait(1.5, "等待新建邮件窗口")

    # ── 截屏检测 Mail 新建邮件窗口 ──
    from gpa.core.ui_parser import parse_screenshot
    console.print()
    console.print("  [yellow]解析 Mail.app UI 元素 …[/yellow]")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  console=console, transient=True) as prog:
        t = prog.add_task("YOLO + OCR + IconCLIP …", total=None)
        sc = cs()
        graph = parse_screenshot(sc)
        prog.update(t, completed=True)
    ok(f"检测到 {len(graph.nodes)} 个 UI 元素")

    # 显示识别到的文字节点
    text_nodes = [n for n in graph.nodes if n.content]
    if text_nodes:
        tbl = Table(box=box.SIMPLE, pad_edge=False, show_header=False)
        tbl.add_column("", width=4, style="dim")
        tbl.add_column("", width=20)
        tbl.add_column("", style="dim")
        for n in text_nodes[:6]:
            tbl.add_row(f"#{n.id}", n.content[:20], f"({n.pos[0]:.0f},{n.pos[1]:.0f})")
        console.print(tbl)

    # ── 找 To 字段（用 OCR 或坐标回退）──
    def find_field_y(labels, fallback_ratio):
        """在已识别文字节点中找包含 label 的行，返回其 y 中心。"""
        from gpa.core.similarity import text_similarity
        for n in graph.nodes:
            if not n.content:
                continue
            for lab in labels:
                if text_similarity(n.content.lower(), lab.lower()) > 0.6:
                    return n.center[1]
        return H * fallback_ratio

    # Mail.app 新建邮件窗口：To 大概在屏幕 40-50% 高度
    # 用 OCR 定位更准，但也有坐标回退
    to_y    = find_field_y(["To", "收件人", "to:"], 0.43)
    subj_y  = find_field_y(["Subject", "主题", "subject:"], 0.49)
    body_y  = find_field_y([], 0.60)          # body 没有标签，直接回退

    # 新建邮件窗口中心 x 大约在屏幕 55-75%
    field_x = W * 0.62

    console.print()
    console.print(f"  [dim]To 字段 y≈{to_y:.0f}, Subject y≈{subj_y:.0f}[/dim]")

    # ── 填写 To ──
    rec_click(field_x, to_y, "To 字段")
    rec_type(RECORD_TO, "收件人")
    rec_hotkey("tab", "跳到 Subject")

    # ── 填写 Subject ──
    # Tab 可能跳到 CC，多按一次
    time.sleep(0.3)
    rec_hotkey("tab", "跳过 CC → Subject")
    rec_type(RECORD_SUBJECT, "主题")
    rec_hotkey("tab", "跳到正文")

    # ── 填写正文 ──
    rec_type(RECORD_BODY, "正文内容")

    # ── 保存为草稿 ──
    rec_hotkey("command+w", "关闭保存草稿")
    wait(0.8)
    # 确认保存
    pyautogui.press("s")
    wait(0.5)

    ok(f"录制完成：[bold]{len(recording.events)}[/bold] 个事件")
    return recording, graph

# ── Phase B: 构建 Workflow ─────────────────────────────────────────────────────

def phase_b_build(recording):
    phase("B", "LLM 分析录制 → 提取变量 → 生成 Workflow")

    from gpa.recording.builder import build_workflow
    import gpa.config as cfg
    import gpa.storage.workflow as wsm

    wf_dir = ROOT / "storage" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    cfg.WORKFLOWS_DIR = wf_dir
    wsm.WORKFLOWS_DIR = wf_dir

    # 清除旧的同名 workflow
    old = wf_dir / WORKFLOW_ID
    if old.exists():
        import shutil; shutil.rmtree(old)

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  console=console, transient=True) as prog:
        t = prog.add_task(f"调用 {cfg.LLM_MODEL} 分析 {len(recording.events)} 个事件 …", total=None)
        result = build_workflow(recording, workflow_id=WORKFLOW_ID)
        prog.update(t, completed=True)

    wf = result.workflow
    console.print()

    # 摘要
    tbl = Table(box=box.SIMPLE, pad_edge=False, show_header=False)
    tbl.add_column("", style="bold cyan", width=16)
    tbl.add_column("")
    tbl.add_row("Workflow 名称", wf.workflow_name)
    tbl.add_row("描述", wf.description)
    tbl.add_row("步骤数", str(len(wf.steps)))
    tbl.add_row("提取变量", ", ".join(f"[cyan]{v.name}[/cyan]" for v in wf.variables) or "（无）")
    tbl.add_row("有子图步骤", f"{len(result.step_subgraphs)} / {len(wf.steps)}")
    console.print(tbl)

    # 步骤表
    console.print()
    stbl = Table(title="Workflow 步骤", box=box.SIMPLE_HEAD, header_style="bold")
    stbl.add_column("#",    width=3,  justify="right")
    stbl.add_column("LLM 生成的动作描述", min_width=45)
    stbl.add_column("类型",   width=8)
    stbl.add_column("子图",   width=5)
    for s in wf.steps:
        sg = "[green]●[/green]" if s.id in result.step_subgraphs else "[dim]○[/dim]"
        stbl.add_row(str(s.step_number), s.action, s.action_type, sg)
    console.print(stbl)

    # 变量说明
    if wf.variables:
        console.print()
        console.print("  [bold]提取出的变量（重放时可按需覆盖）：[/bold]")
        for v in wf.variables:
            console.print(f"    [cyan]--var {v.name}[/cyan]=\"{v.default_value}\"")
            console.print(f"    [dim]    # {v.description}[/dim]")

    from gpa.storage.workflow import WorkflowStorage
    WorkflowStorage().save(wf, result.step_subgraphs)
    ok("Workflow 已保存")
    return result

# ── Phase C: 批量重放 ─────────────────────────────────────────────────────────

def phase_c_batch_replay(build_result):
    phase("C", f"批量重放 × {len(BATCH_EMAILS)} — 自动生成 {len(BATCH_EMAILS)} 封草稿")

    from gpa.execution.executor import Executor

    wf = build_result.workflow
    subgraphs = build_result.step_subgraphs

    # 展示即将发送的邮件
    btbl = Table(title="即将生成的草稿邮件", box=box.SIMPLE_HEAD, header_style="bold yellow")
    btbl.add_column("#",       width=3, justify="right")
    btbl.add_column("收件人",  width=28)
    btbl.add_column("主题",    min_width=20)
    btbl.add_column("正文预览", width=30)
    for i, e in enumerate(BATCH_EMAILS, 1):
        btbl.add_row(str(i), e["to"], e["subject"], e["body"][:28] + "…")
    console.print(btbl)

    console.print()
    console.print("  [yellow]5 秒后开始批量重放 …（请勿移动鼠标）[/yellow]")
    time.sleep(5)

    results = []
    for i, email_data in enumerate(BATCH_EMAILS, 1):
        console.print()
        console.print(f"  [bold cyan]── 草稿 {i}/{len(BATCH_EMAILS)}: {email_data['to']} ──[/bold cyan]")

        activate_mail()
        time.sleep(0.5)

        # 变量映射（用 workflow 的变量名）
        # LLM 可能提取出 to/subject/body 或类似名称
        var_names = {v.name for v in wf.variables}
        variables = {}

        def pick(candidates, value):
            for c in candidates:
                if c in var_names:
                    variables[c] = value
                    return c
            # fallback: 用第一个含关键词的变量
            for v in var_names:
                for c in candidates:
                    if c.lower() in v.lower() or v.lower() in c.lower():
                        variables[v] = value
                        return v
            return None

        pick(["to", "recipient", "email", "address", "收件人"], email_data["to"])
        pick(["subject", "title", "主题", "标题"], email_data["subject"])
        pick(["body", "content", "message", "正文", "内容"], email_data["body"])

        # 未匹配的变量用默认值
        for v in wf.variables:
            if v.name not in variables:
                variables[v.name] = v.default_value

        info(f"变量: {json.dumps(variables, ensure_ascii=False)}")

        executor = Executor(
            wf, subgraphs,
            variables=variables,
            readiness_threshold=0.15,
            max_retries=3,
        )
        result = executor.run()
        results.append((email_data, result))

        if result.success:
            ok(f"草稿 {i} 生成成功 ✓")
        else:
            warn(f"草稿 {i} 部分失败: {result.error}")

        time.sleep(1.5)  # 让 Mail.app 稳定

    # ── 汇总 ──
    console.print()
    console.rule("[bold green]批量执行完成[/bold green]")
    stbl = Table(box=box.SIMPLE_HEAD, header_style="bold")
    stbl.add_column("#",       width=3,  justify="right")
    stbl.add_column("收件人",  width=28)
    stbl.add_column("主题")
    stbl.add_column("结果",    width=8)
    for i, (email_data, res) in enumerate(results, 1):
        tag = "[bold green]✓ 成功[/bold green]" if res.success else "[red]✗ 失败[/red]"
        stbl.add_row(str(i), email_data["to"], email_data["subject"], tag)
    console.print(stbl)

    ok(f"{sum(1 for _, r in results if r.success)}/{len(results)} 封草稿已生成，"
       "请打开 Mail.app → 草稿箱 查看")
    return results

# ── 使用指南 ──────────────────────────────────────────────────────────────────

def show_guide(results):
    console.print()
    console.rule("[bold]现在你可以 …[/bold]")
    console.print("""
[bold cyan]1. 自己录制任意重复操作：[/bold cyan]
   [green]gpa record 邮件草稿[/green]          ← 在终端运行，然后手动操作，Enter 停止

[bold cyan]2. 批量重放（命令行方式）：[/bold cyan]
   [green]gpa run 邮件草稿 --var to=alice@co.com --var subject="周报" --var body="..."[/green]

[bold cyan]3. 让 AI Agent 调用（MCP 方式）：[/bold cyan]
   [green]gpa mcp-serve[/green]                ← 启动 MCP Server
   然后在 Claude / Cursor 等工具里：
   "帮我给销售团队每人发一封跟进邮件"
   → AI 自动调用 GPA 的 mail_draft_workflow

[bold cyan]4. 其他实用场景：[/bold cyan]
   • 填写报销单（SAP/用友/金蝶）
   • 每日更新 Notion / 飞书文档
   • 定期导出报表并发送
   • HR 系统录入候选人信息
""")

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    banner(
        "GPA 实战 Demo：Mail.app 批量邮件草稿生成器",
        "录制一次 → 批量重放 × 3  |  arXiv:2604.01676 本地复现版",
    )

    console.print(Panel(
        "[bold]场景说明[/bold]\n"
        "你每周需要给不同客户发相似的跟进邮件。\n"
        "传统方式：手动一封封写（枯燥、易出错）\n"
        "[green]GPA 方式：录制一次 → 自动批量生成草稿[/green]",
        border_style="dim",
    ))

    # Phase A
    recording, ui_graph = phase_a_record()

    # Phase B
    build_result = phase_b_build(recording)

    # Phase C
    results = phase_c_batch_replay(build_result)

    # 指南
    show_guide(results)


if __name__ == "__main__":
    main()
