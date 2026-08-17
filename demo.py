#!/usr/bin/env python3
"""
GPA Demo — GUI Process Automation
===================================
演示完整流程：录制 → 构建 → 查看 → 重放

场景：用 TextEdit 创建并保存一个文本文件
  Step 1  → 打开 TextEdit
  Step 2  → 截屏 + 解析 UI（YOLO / OCR / IconCLIP）
  Step 3  → 录制：点击文本区域 → 输入内容 → Cmd+S 保存
  Step 4  → LLM 分析 → 生成 Workflow Template（含变量）
  Step 5  → 展示 Workflow 详情
  Step 6  → 重放 Workflow（SMC 定位 + FSM 执行）
"""

import os
import pathlib
import subprocess
import sys
import time
import warnings

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from rich.table import Table

warnings.filterwarnings("ignore")
os.environ["TOKENIZERS_PARALLELISM"] = "false"

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

console = Console()

DEMO_TEXT   = "Hello from GPA!"          # default text (variable)
DEMO_FILE   = "/tmp/gpa_demo.txt"        # save path
WORKFLOW_ID = "demo_textedit_001"

# ── helpers ──────────────────────────────────────────────────────────────────

def step(n, title):
    console.print()
    console.rule(f"[bold cyan]Step {n}: {title}[/bold cyan]")
    time.sleep(0.2)

def ok(msg):   console.print(f"  [bold green]✓[/bold green] {msg}")
def warn(msg): console.print(f"  [yellow]![/yellow] {msg}")
def info(msg): console.print(f"  [dim]{msg}[/dim]")

# ── step 1 ────────────────────────────────────────────────────────────────────

def open_textedit():
    step(1, "打开 TextEdit")
    subprocess.Popen(["open", "-a", "TextEdit", "--new"])
    info("等待 TextEdit 启动 …")
    time.sleep(2.5)
    # 确保新建空白文档（防止弹出 Open 面板）
    subprocess.run(
        ["osascript", "-e",
         'tell application "TextEdit" to activate\n'
         'tell application "System Events" to keystroke "n" using command down'],
        capture_output=True,
    )
    time.sleep(1.0)
    ok("TextEdit 已打开（新建空白文档）")

# ── step 2 ────────────────────────────────────────────────────────────────────

def parse_ui():
    step(2, "截屏 → 解析 UI 元素（YOLO + OCR + IconCLIP）")
    from gpa.core.ui_parser import parse_screenshot
    from gpa.recording.recorder import capture_screenshot

    info("截屏中 …")
    screenshot = capture_screenshot()
    w, h = screenshot.width, screenshot.height
    ok(f"截屏: {w}×{h} px")

    demo_dir = ROOT / "demo_output"
    demo_dir.mkdir(exist_ok=True)
    screenshot.save(demo_dir / "screenshot_textedit.png")
    info("截图 → demo_output/screenshot_textedit.png")

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  console=console, transient=True) as prog:
        t = prog.add_task("解析 UI 元素（YOLO + OCR + IconCLIP）…", total=None)
        graph = parse_screenshot(screenshot)
        prog.update(t, completed=True)

    ok(f"检测到 [bold]{len(graph.nodes)}[/bold] 个元素，"
       f"[bold]{len(graph.edges)}[/bold] 条邻居边")

    # 只显示有文字内容的节点（更有意义）
    text_nodes = [n for n in graph.nodes if n.content]
    icon_nodes = [n for n in graph.nodes if not n.content]
    info(f"其中文字节点 {len(text_nodes)} 个，图标节点 {len(icon_nodes)} 个")

    if text_nodes:
        tbl = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        tbl.add_column("ID",   width=4,  style="dim")
        tbl.add_column("内容", width=20)
        tbl.add_column("位置 (x,y,w,h)")
        for n in text_nodes[:8]:
            tbl.add_row(str(n.id), n.content or "—",
                        f"({n.pos[0]:.0f},{n.pos[1]:.0f},{n.pos[2]:.0f},{n.pos[3]:.0f})")
        if len(text_nodes) > 8:
            tbl.add_row("…", f"共 {len(text_nodes)} 个文字节点", "")
        console.print(tbl)

    return screenshot, graph, w, h

# ── step 3 ────────────────────────────────────────────────────────────────────

def simulate_recording(w, h):
    step(3, "模拟录制：点击文本区 → 输入内容 → Cmd+S")
    import pyautogui

    from gpa.recording.recorder import RecordedEvent, Recording, capture_screenshot

    recording = Recording()

    def rec_click(x, y, label):
        pyautogui.click(int(x), int(y))
        time.sleep(0.4)
        sc = capture_screenshot()
        recording.events.append(RecordedEvent(
            event_type="click", x=float(x), y=float(y),
            button="left", screenshot=sc,
            active_app="TextEdit", pause_before=0.4,
            timestamp=time.time(),
        ))
        ok(f"CLICK  {label} ({x:.0f},{y:.0f})")

    def rec_type(text, label):
        pyautogui.typewrite(text, interval=0.04)
        time.sleep(0.3)
        sc = capture_screenshot()
        recording.events.append(RecordedEvent(
            event_type="type", value=text, screenshot=sc,
            active_app="TextEdit", pause_before=0.3,
            timestamp=time.time(),
        ))
        ok(f"TYPE   {label} → {text!r}")

    def rec_hotkey(combo, label):
        pyautogui.hotkey(*combo.split("+"))
        time.sleep(0.5)
        sc = capture_screenshot()
        recording.events.append(RecordedEvent(
            event_type="hotkey", value=combo, screenshot=sc,
            active_app="TextEdit", pause_before=0.5,
            timestamp=time.time(),
        ))
        ok(f"HOTKEY {label} ({combo})")

    # 激活 TextEdit
    subprocess.run(["osascript", "-e",
                    'tell application "TextEdit" to activate'],
                   capture_output=True)
    time.sleep(0.6)

    # 点击文本区（屏幕中央下方，TextEdit 文本区域）
    text_area_x = w // 2
    text_area_y = int(h * 0.55)
    rec_click(text_area_x, text_area_y, "TextEdit 文本区")

    # 输入文字（使用变量占位符格式）
    rec_type(DEMO_TEXT, "正文内容 {{content}}")

    # Cmd+Shift+S 另存为，然后输入文件名
    rec_hotkey("command+shift+s", "另存为")
    time.sleep(1.2)  # 等待对话框

    # 先清除默认文件名再输入
    pyautogui.hotkey("command", "a")
    time.sleep(0.2)
    rec_type(DEMO_FILE, "文件路径 {{file_path}}")

    rec_hotkey("enter", "确认保存")
    time.sleep(0.8)

    ok(f"录制完成：[bold]{len(recording.events)}[/bold] 个事件")
    return recording

# ── step 4 ────────────────────────────────────────────────────────────────────

def build_wf(recording):
    step(4, "LLM 分析录制 → 生成 Workflow Template")
    import gpa.config as cfg
    import gpa.storage.workflow as wsm
    from gpa.recording.builder import build_workflow

    wf_dir = ROOT / "storage" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    cfg.WORKFLOWS_DIR = wf_dir
    wsm.WORKFLOWS_DIR = wf_dir

    info(f"调用 LLM ({cfg.LLM_MODEL}) 分析录制序列 …")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  console=console, transient=True) as prog:
        t = prog.add_task("LLM 提取变量 & 生成步骤描述 …", total=None)
        result = build_workflow(recording, workflow_id=WORKFLOW_ID)
        prog.update(t, completed=True)

    wf = result.workflow
    ok(f"workflow_name : [bold]{wf.workflow_name}[/bold]")
    ok(f"workflow_title: {wf.workflow_title}")
    ok(f"description   : {wf.description}")
    ok(f"steps         : {len(wf.steps)}")
    ok(f"variables     : {[v.name for v in wf.variables]}")
    ok(f"subgraphs     : {len(result.step_subgraphs)} 个（有 SMC 上下文的步骤）")

    # 保存
    from gpa.storage.workflow import WorkflowStorage
    stor = WorkflowStorage()
    saved = stor.save(wf, result.step_subgraphs)
    ok(f"已保存 → {saved.name}")
    return result

# ── step 5 ────────────────────────────────────────────────────────────────────

def show_wf(build_result):
    step(5, "Workflow 详情")
    wf = build_result.workflow

    tbl = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    tbl.add_column("#",    width=3,  justify="right")
    tbl.add_column("动作描述",       min_width=40)
    tbl.add_column("类型", width=8)
    tbl.add_column("子图", width=6)
    for s in wf.steps:
        has_sg = "[green]● 有[/green]" if s.id in build_result.step_subgraphs else "[dim]○ 无[/dim]"
        tbl.add_row(str(s.step_number), s.action, s.action_type, has_sg)
    console.print(tbl)

    if wf.variables:
        console.print()
        console.print("  [bold]变量（可在 run 时用 --var 覆盖）：[/bold]")
        for v in wf.variables:
            console.print(f"    [cyan]{v.name}[/cyan] = {v.default_value!r}  # {v.description}")

# ── step 6 ────────────────────────────────────────────────────────────────────

def replay_wf(build_result):
    step(6, "重放 Workflow（SMC 定位 + 坐标回退 + FSM）")
    import gpa.config as cfg
    import gpa.storage.workflow as wsm
    from gpa.execution.executor import Executor

    wf_dir = ROOT / "storage" / "workflows"
    cfg.WORKFLOWS_DIR = wf_dir
    wsm.WORKFLOWS_DIR = wf_dir

    wf = build_result.workflow
    subgraphs = build_result.step_subgraphs

    info("准备重放：先清空 TextEdit 文档 …")
    subprocess.run(["osascript", "-e",
                    'tell application "TextEdit" to activate'],
                   capture_output=True)
    time.sleep(0.5)
    import pyautogui
    pyautogui.hotkey("command", "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    time.sleep(0.3)

    info("3 秒后开始重放 …")
    time.sleep(3)

    # 低阈值：SMC 尽力定位，失败则直接用录制坐标
    executor = Executor(
        wf, subgraphs,
        variables={"content": DEMO_TEXT + " (replay)", "file_path": DEMO_FILE},
        readiness_threshold=0.15,
        max_retries=3,
    )

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  console=console, transient=True) as prog:
        t = prog.add_task("重放中 …", total=None)
        result = executor.run()
        prog.update(t, completed=True)

    # 汇报结果
    for r in result.step_results:
        loc = r.localization
        if loc:
            method_tag = f"[cyan]{loc.method}[/cyan]  conf={loc.confidence:.2f}"
        else:
            method_tag = "[dim]无定位（type/hotkey）[/dim]"
        state_tag = "[green]✓[/green]" if str(r.state).endswith("DONE") else "[red]✗[/red]"
        console.print(f"  {state_tag} Step {r.step_number:2d}  {method_tag}")

    console.print()
    if result.success:
        console.print(Panel(
            "[bold green]✓ Workflow 重放成功！[/bold green]\n"
            f"共 {result.n_steps} 步，{result.n_failed} 步失败\n"
            f"[dim]文件已保存到 {DEMO_FILE}[/dim]",
            border_style="green",
        ))
        # 验证文件
        if pathlib.Path(DEMO_FILE).exists():
            content = pathlib.Path(DEMO_FILE).read_text(errors="replace").strip()
            ok(f"验证文件内容: {content!r}")
    else:
        console.print(Panel(
            f"[yellow]! 部分步骤失败[/yellow]\n{result.error}\n"
            "[dim]SMC 定位置信度不足时自动降级为坐标执行，属正常情况[/dim]",
            border_style="yellow",
        ))

# ── 使用指南 ──────────────────────────────────────────────────────────────────

def show_guide():
    console.print()
    console.rule("[bold]接下来：自己录制任意 GUI 工作流[/bold]")
    guide = Syntax("""\
# 1. 录制一个新工作流（终端里运行，然后操作你的 App，按 Enter 停止录制）
gpa record my_task

# 2. 查看所有已录 workflow
gpa list

# 3. 查看某个 workflow 的步骤
gpa show my_task

# 4. 重放（使用默认变量）
gpa run my_task

# 5. 重放时覆盖变量（如邮件地址、搜索词）
gpa run my_task --var email=foo@bar.com --var subject="Hello"

# 6. 作为 MCP tool 启动，供 Claude / Agentforce 等 AI Agent 调用
gpa mcp-serve
""", "bash", theme="monokai", line_numbers=False)
    console.print(guide)

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    console.print(Panel.fit(
        "[bold white]GPA · GUI Process Automation[/bold white]\n"
        "[dim]arXiv:2604.01676  ·  Salesforce AI Research  ·  本地复现版[/dim]\n"
        "[dim]场景：TextEdit 创建并保存文本文件（含变量）[/dim]",
        border_style="cyan",
    ))

    open_textedit()
    screenshot, graph, w, h = parse_ui()
    recording = simulate_recording(w, h)
    build_result = build_wf(recording)
    show_wf(build_result)
    replay_wf(build_result)
    show_guide()


if __name__ == "__main__":
    main()
