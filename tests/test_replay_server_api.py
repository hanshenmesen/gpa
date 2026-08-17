import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

import demo_web.server as server_module
from gpa.storage.workflow import Workflow, WorkflowStep


class ReplayServerApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        with server_module.STATE_LOCK:
            self.previous_run = dict(server_module.STATE["run"])
            self.previous_client = dict(server_module.STATE["client"])
            self.previous_replay_arm = dict(server_module.STATE["replay_arm"])
            self.previous_package_inspections = dict(server_module.STATE.get("package_inspections", {}))
        self.previous_workflows = server_module.WORKFLOWS_DIR
        self.previous_runs = server_module.RUNS_DIR
        self.previous_spaces = server_module.REPLAY_SPACES_DIR
        self.previous_community = server_module.COMMUNITY_DIR
        self.previous_preview_media = server_module.PREVIEW_MEDIA_DIR
        server_module.WORKFLOWS_DIR = root / "workflows"
        server_module.RUNS_DIR = root / "runs"
        server_module.REPLAY_SPACES_DIR = root / "spaces"
        server_module.COMMUNITY_DIR = root / "community"
        server_module.PREVIEW_MEDIA_DIR = root / "preview_media"
        server_module.WORKFLOWS_DIR.mkdir(parents=True)
        storage = server_module._storage()
        storage.save(
            Workflow(
                workflow_id="api_replay",
                workflow_name="api_replay",
                workflow_title="API Replay",
                description="Replay endpoint fixture",
                task_description="打开网页并输入内容",
                environment={
                    "schema": "gpa.environment/v1",
                    "system": {"name": "darwin", "machine": "arm64"},
                    "browser": {"family": "Chrome"},
                    "screen": {"width": 1000, "height": 800, "pixel_ratio": 2},
                },
                steps=[
                    WorkflowStep(1, "打开网页", action_type="open_url", value="https://example.test", active_app_name="Safari"),
                    WorkflowStep(2, "输入内容", action_type="type", value="hello", active_app_name="Safari"),
                ],
            ),
            {},
        )
        storage.save(
            Workflow(
                workflow_id="unsupported_replay",
                workflow_name="unsupported_replay",
                workflow_title="Unsupported Replay",
                description="Unsupported action fixture",
                task_description="执行不支持的系统动作",
                steps=[WorkflowStep(1, "执行 shell", action_type="shell")],
            ),
            {},
        )
        records = server_module._ensure_demo_community_records()
        self.community_record_id = records[0]["record_id"]
        self.httpd = server_module.ThreadingHTTPServer(("127.0.0.1", 0), server_module.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server_module.WORKFLOWS_DIR = self.previous_workflows
        server_module.RUNS_DIR = self.previous_runs
        server_module.REPLAY_SPACES_DIR = self.previous_spaces
        server_module.COMMUNITY_DIR = self.previous_community
        server_module.PREVIEW_MEDIA_DIR = self.previous_preview_media
        with server_module.STATE_LOCK:
            staged_inspections = list(server_module.STATE.get("package_inspections", {}).values())
            server_module.STATE["run"] = self.previous_run
            server_module.STATE["client"] = self.previous_client
            server_module.STATE["replay_arm"] = self.previous_replay_arm
            server_module.STATE["run_stop_event"] = None
            server_module.STATE["run_thread"] = None
            server_module.STATE["run_process"] = None
            server_module.STATE["run_control_dir"] = None
            server_module.STATE["run_started_monotonic"] = None
            server_module.STATE["preview"] = None
            server_module.STATE["package_inspections"] = self.previous_package_inspections
        for entry in staged_inspections:
            if entry not in self.previous_package_inspections.values():
                server_module._delete_package_inspection_files(entry)
        self.temporary.cleanup()

    def get_json(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read())

    def get_text(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.read().decode()

    def test_get_routes_ignore_unrelated_query_parameters(self):
        status = self.get_json("/api/status?source=test")
        workflows = self.get_json("/api/workflows?source=test")
        workflow = self.get_json("/api/workflows/api_replay?source=test")
        runs = self.get_json("/api/runs?workflow_id=api_replay&source=test")

        self.assertIn("run", status)
        self.assertTrue(any(item["id"] == "api_replay" for item in workflows["workflows"]))
        self.assertEqual(workflow["workflow"]["id"], "api_replay")
        self.assertEqual(runs["runs"], [])

    def test_runtime_setup_page_and_settings_never_return_api_key(self):
        setup = self.get_text("/setup")
        with patch("gpa.config.LLM_API_KEY", "secret-value-1234"):
            settings = self.get_json("/api/settings/runtime")

        self.assertIn("运行前，把权限和模型准备好", setup)
        self.assertIn('type="password"', setup)
        self.assertTrue(settings["llm"]["configured"])
        self.assertEqual(settings["llm"]["api_key_masked"], "••••••••1234")
        self.assertNotIn("secret-value", json.dumps(settings, ensure_ascii=False))

    def test_llm_settings_reject_unsafe_base_urls_before_writing(self):
        for base_url in (
            "http://api.openai.com/v1",
            "https://127.0.0.1/v1",
            "https://user:pass@api.openai.com/v1",
            "https://api.openai.com/v1?secret=value",
        ):
            with self.subTest(base_url=base_url):
                with patch.object(server_module, "LOCAL_ENV_FILE", Path(self.temporary.name) / ".env"):
                    request = urllib.request.Request(
                        self.base + "/api/settings/llm",
                        data=json.dumps({
                            "api_key": "sk-test",
                            "base_url": base_url,
                            "model": "gpt-test",
                        }).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(raised.exception.code, 422)
                    self.assertFalse((Path(self.temporary.name) / ".env").exists())

    def test_custom_llm_provider_requires_exact_host_acknowledgement(self):
        env_path = Path(self.temporary.name) / ".env"
        body = {
            "api_key": "sk-test",
            "base_url": "https://models.example.com/v1",
            "model": "custom-model",
        }
        with patch.object(server_module, "LOCAL_ENV_FILE", env_path):
            request = urllib.request.Request(
                self.base + "/api/settings/llm",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 422)
            self.assertIn("Confirm the custom provider host", json.load(raised.exception)["error"])
            self.assertFalse(env_path.exists())
            with patch("gpa.execution.safe_web.static_public_url_error", return_value=""):
                settings = server_module._validated_llm_settings(
                    {**body, "provider_host_acknowledgement": "models.example.com"},
                    require_key=True,
                )
            self.assertEqual(settings["base_url"], "https://models.example.com/v1")

    def test_desktop_access_can_be_enabled_and_revoked_for_current_session(self):
        previous_enabled = server_module.DESKTOP_AUTOMATION_ENABLED
        previous_requested = server_module.DESKTOP_AUTOMATION_REQUESTED
        previous_recovery = server_module.RECOVERY_SAFE_MODE_ACTIVE
        previous_startup = server_module.os.environ.get(server_module.DESKTOP_STARTUP_ENV)
        try:
            server_module.DESKTOP_AUTOMATION_ENABLED = False
            server_module.DESKTOP_AUTOMATION_REQUESTED = False
            server_module.RECOVERY_SAFE_MODE_ACTIVE = False
            env_path = Path(self.temporary.name) / "desktop.env"
            with patch.object(server_module, "LOCAL_ENV_FILE", env_path):
                _, enabled = self.post_json(
                    "/api/settings/desktop",
                    {"enabled": True, "startup_default_enabled": True},
                )
            self.assertTrue(enabled["desktop"]["enabled"])
            self.assertTrue(enabled["desktop"]["startup_default_enabled"])
            self.assertIn("GPA_DESKTOP_STARTUP_ENABLED=1", env_path.read_text())
            self.assertEqual(server_module.os.environ[server_module.DESKTOP_AUTOMATION_ENV], "1")
            with patch.object(server_module, "LOCAL_ENV_FILE", env_path):
                _, disabled = self.post_json(
                    "/api/settings/desktop",
                    {"enabled": False, "startup_default_enabled": False},
                )
            self.assertFalse(disabled["desktop"]["enabled"])
            self.assertFalse(disabled["desktop"]["startup_default_enabled"])
            self.assertIn("GPA_DESKTOP_STARTUP_ENABLED=0", env_path.read_text())
            self.assertEqual(server_module.os.environ[server_module.DESKTOP_AUTOMATION_ENV], "0")
        finally:
            server_module.DESKTOP_AUTOMATION_ENABLED = previous_enabled
            server_module.DESKTOP_AUTOMATION_REQUESTED = previous_requested
            server_module.RECOVERY_SAFE_MODE_ACTIVE = previous_recovery
            if previous_startup is None:
                server_module.os.environ.pop(server_module.DESKTOP_STARTUP_ENV, None)
            else:
                server_module.os.environ[server_module.DESKTOP_STARTUP_ENV] = previous_startup

    def test_intent_endpoint_rejects_falsey_non_array_steps(self):
        for invalid in ("", {}, 0, False):
            with self.subTest(invalid=invalid):
                request = urllib.request.Request(
                    self.base + "/api/replays/intent?source=test",
                    data=json.dumps({"goal": "test", "steps": invalid}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(raised.exception.code, 422)
                self.assertIn("steps must be a list", json.load(raised.exception)["error"])

    def post_json(self, path, body):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def post_bytes(self, path, body, content_type):
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_replay_manifest_intent_and_space_endpoints(self):
        studio = self.get_text("/")
        store = self.get_text("/store")
        control = self.get_text("/control")
        product_css = self.get_text("/assets/product.css")
        product_js = self.get_text("/assets/product.js")
        environment_js = self.get_text("/assets/environment.js")
        listing = self.get_json("/api/replays?platform=windows")
        detail = self.get_json("/api/replays/api_replay")
        intent_status, intent = self.post_json("/api/replays/intent", {
            "goal": "在浏览器搜索资料",
            "steps": [{"action_type": "type", "app": "Safari", "description": "输入关键词"}],
        })
        plan_status, planned = self.post_json("/api/replays/api_replay/plan", {"platform": "windows"})
        space = self.get_json(f"/api/replay-spaces/{planned['plan']['space_id']}")
        community_detail = self.get_json(f"/api/community/records/{self.community_record_id}")

        self.assertTrue(listing["ok"])
        self.assertIn("Replay Studio", studio)
        self.assertIn('id="stepJump"', studio)
        self.assertIn("function jumpToEditorStep", studio)
        self.assertIn("function stepActionVisual", studio)
        self.assertIn('list="stepActionTypes"', studio)
        self.assertIn('datalist id="stepActionTypes"', studio)
        self.assertIn("STEP_VALUE_REQUIRED_ACTIONS", studio)
        self.assertIn("function stepEditorValidationIssue", studio)
        self.assertIn("坐标必须同时填写", studio)
        self.assertIn("等待秒数必须在 0–300 之间", studio)
        self.assertIn("function duplicateEditorStep", studio)
        self.assertIn("function createEditorStepId", studio)
        self.assertIn("editor_duplicate_source_step_id", studio)
        self.assertIn("data-duplicate-step", studio)
        self.assertIn("data-move-step", studio)
        self.assertIn("data-move-direction", studio)
        self.assertIn("步骤已移动到第", studio)
        self.assertIn("步骤顺序已恢复", studio)
        self.assertIn("已复制为第", studio)
        self.assertIn("复制的步骤已撤销", studio)
        self.assertIn("data-step-kind", studio)
        self.assertIn("product-toast-action", studio)
        self.assertIn("无法撤销：当前 Replay 已切换", studio)
        self.assertIn("WORKSPACE_CONTEXT_KEY", studio)
        self.assertIn("globalThis.productChoice", studio)
        self.assertIn("保存并继续", studio)
        self.assertIn("persistEditorDraft();", studio)
        self.assertIn("workflowListRequestVersion", studio)
        self.assertIn("statusRequestVersion", studio)
        self.assertIn("runHistoryRequestVersion", studio)
        self.assertIn("statusPollGeneration", studio)
        self.assertIn("generation !== statusPollGeneration", studio)
        self.assertIn("if (!isLatestRequest()) return false;", studio)
        self.assertIn("selectionSnapshot !== selectionVersion", studio)
        self.assertIn("if (!statusApplied && !status)", studio)
        self.assertIn("function showWorkflowLoadError", studio)
        self.assertIn("data-retry-workflows", studio)
        self.assertIn("function runExclusiveAction", studio)
        self.assertIn("recording_analysis", studio)
        self.assertIn("收束 ${reducedCount} 个重复或非必要动作", studio)
        self.assertIn("Agent 理解与收束", studio)
        self.assertIn("桌面 Replay 已锁定", studio)
        self.assertIn("agentUnderstandingText", store)
        self.assertIn("个原始动作 →", store)
        self.assertIn("runExclusiveAction('workflow-mutation'", studio)
        self.assertIn("runExclusiveAction('recording-mutation'", studio)
        self.assertIn("runExclusiveAction('replay-mutation'", studio)
        self.assertIn("client_id: CLIENT_ID", studio)
        self.assertIn("client_id: STORE_CLIENT_ID", store)
        self.assertIn("GPAClient.sendJsonBeacon", studio)
        self.assertIn("'/api/client/disconnect'", store)
        self.assertIn("GPAClient.requestJson", studio)
        self.assertIn("timeoutMs = 60000", environment_js)
        self.assertIn("请求等待时间过长，请重试。", environment_js)
        self.assertIn("屏幕录制上传超时，请重试。", studio)
        self.assertIn("['arrowdown', 'arrowup', 'home', 'end', 'j', 'k']", studio)
        self.assertIn("restoreContext", studio)
        self.assertIn("forgetWorkspaceContext", studio)
        self.assertIn("function showEditorFieldError", studio)
        self.assertIn("function showStepEditorError", studio)
        self.assertIn("editor-field-error", studio)
        self.assertIn('id="runRecovery"', studio)
        self.assertIn("function renderRunRecovery", studio)
        self.assertIn("重新运行全部", studio)
        self.assertIn('id="workspaceStatus"', studio)
        self.assertIn("function renderWorkspaceStatus", studio)
        self.assertIn("state-surface is-error", studio)
        self.assertIn("state-message", studio)
        self.assertIn("<title>GPA · 工作台</title>", studio)
        self.assertIn('data-icon="studio" aria-current="page"', studio)
        self.assertIn('class="readiness-task-brief"', studio)
        self.assertIn('data-readiness-action="run"', studio)
        self.assertIn('studio-disclosure step-editor-section', studio)
        self.assertIn('id="openStepEditor"', studio)
        self.assertIn("stepEditorSection.open = true", studio)
        self.assertNotIn('id="serverTime"', studio)
        self.assertNotIn('id="recordChip"', studio)
        self.assertNotIn('id="runChip"', studio)
        self.assertIn('id="storeWorkspaceStatus"', store)
        self.assertIn("function paintStoreWorkspaceStatus", store)
        self.assertIn("<title>GPA · Replay Store</title>", store)
        self.assertIn('data-icon="store" aria-current="page"', store)
        self.assertIn("runtimeUnavailable", store)
        self.assertIn("恢复连接后才能开始验证", store)
        self.assertIn("catalogRequestVersion", store)
        self.assertIn("headerStatusRequestVersion", store)
        self.assertIn("headerStatusInFlight", store)
        self.assertIn("function showStoreLoadError", store)
        self.assertIn("data-retry-detail", store)
        self.assertIn("state-surface is-loading", store)
        self.assertIn("state-surface is-error", store)
        self.assertIn("已保留 ${state.records.length} 个结果", store)
        self.assertIn("data-retry-store", store)
        self.assertIn("function runStoreAction", store)
        self.assertIn("packageInspectionVersion", store)
        self.assertIn("state.packageInspectionRequest?.abort()", store)
        self.assertIn("button.dataset.idleLabel", store)
        self.assertIn("button.setAttribute('aria-busy', 'true')", store)
        self.assertIn("request.timeout = 180000", store)
        self.assertIn("包检查等待时间过长，请重试。", store)
        self.assertNotIn('id="storeServerTime"', store)
        self.assertNotIn('id="storeRecordChip"', store)
        self.assertNotIn('id="storeRunChip"', store)
        self.assertIn('id="controlWorkspaceStatus"', control)
        self.assertIn("function renderControlWorkspaceStatus", control)
        self.assertIn("function renderOverviewUnavailable", control)
        self.assertIn("else if(!lastOverviewToken)renderOverviewUnavailable", control)
        self.assertIn("state-surface is-empty control-state-surface", control)
        self.assertIn("<title>GPA · 运行状态</title>", control)
        self.assertIn('data-icon="control" aria-current="page"', control)
        self.assertIn('id="retryControl"', control)
        self.assertIn("retryControl.addEventListener('click',refresh)", control)
        self.assertIn("Promise.allSettled([json('/api/product/overview'),json('/api/status')])", control)
        self.assertIn("部分状态暂不可用", control)
        self.assertIn("GPAClient.requestJson", control)
        self.assertIn("请求等待时间过长，请重试。", environment_js)
        self.assertIn("const runtimeBadge=document.getElementById('runtimeBadge')", control)
        self.assertIn("const updatedLabel=document.getElementById('updatedLabel')", control)
        self.assertIn("Readability system v4", product_css)
        self.assertIn("Studio focus mode v5", product_css)
        self.assertIn(".readiness-task-brief", product_css)
        self.assertIn(".studio-layout .readiness-run-action", product_css)
        self.assertIn("body .studio-layout :is(input, textarea, select)", product_css)
        self.assertNotIn('id="serverTime"', control)
        self.assertNotIn('id="recordChip"', control)
        self.assertNotIn('id="runChip"', control)
        self.assertIn('id="runHistorySummary"', studio)
        self.assertIn("run-timeline-item", studio)
        self.assertIn("data-history-step", studio)
        self.assertIn('class="record-flow"', studio)
        self.assertIn('class="record-task-card"', studio)
        self.assertIn('class="record-advanced-options"', studio)
        self.assertIn("function setRecordTaskError", studio)
        self.assertIn("完成录制并生成", studio)
        self.assertIn('id="openRecorder"', studio)
        self.assertIn('id="clearWorkflowSearch"', studio)
        self.assertIn('id="workflowSearchStatus"', studio)
        self.assertIn("function openRecorderPanel", studio)
        self.assertIn("data-open-recorder", studio)
        self.assertIn('aria-label="步骤分页"', studio)
        self.assertIn("上传 Replay 插件", store)
        self.assertIn('class="catalog-console"', store)
        self.assertIn('id="storeSearchClear"', store)
        self.assertIn('class="detail-decision is-${escapeHtml(detailDecision.kind)}"', store)
        self.assertIn("可以安全验证", store)
        self.assertIn("environment.capture_surface", store)
        self.assertIn("录屏捕获面", store)
        self.assertIn("录制系统与捕获环境已保存", store)
        self.assertIn("recordedEnvironmentText", store)
        self.assertIn("environmentEvidenceText", store)
        self.assertIn("个安全实录", store)
        self.assertIn("个隔离复现", store)
        self.assertIn("function catalogDecision(record)", store)
        self.assertIn("function catalogPermission(record)", store)
        self.assertIn('class="card-decision is-${escapeHtml(decision.kind)}"', store)
        self.assertIn('class="card-decision-facts"', store)
        self.assertIn('class="detail-purpose"', store)
        self.assertIn("保存到我的 Replay", store)
        self.assertIn("证据链追溯", control)
        self.assertIn('id="controlStatusTitle"', control)
        self.assertIn("runFilterTouched", control)
        self.assertIn("if(issue)", control)
        self.assertIn("Number(run.failed_step||0)", control)
        self.assertIn("rawErrorHtml", control)
        self.assertIn('class="run-issue-glance"', control)
        self.assertIn("没有返回具体错误信息", control)
        self.assertIn("Replay 已归档，记录保留用于审计", control)
        self.assertIn("priorityWorkflowHealth", control)
        self.assertIn("routineWorkflowHealth", control)
        self.assertIn("routineExpanded", control)
        self.assertIn('class="replay-health-clear ', control)
        self.assertIn('class="replay-health-more"', control)
        self.assertIn("repeat(3,minmax(0,1fr))", control)
        self.assertIn("最近成功率", control)
        self.assertIn("completedRuns", control)
        self.assertIn("querySelectorAll('[data-run-filter-trigger]')", control)
        self.assertNotIn("['官方任务'", control)
        self.assertNotIn("['可交接证据'", control)
        self.assertIn(
            ".control-page .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)) !important; }",
            product_css,
        )
        self.assertIn(".step-jump:focus-within", product_css)
        self.assertIn('.step-disclosure[data-step-kind="verify"]', product_css)
        self.assertIn(".step-duplicate-action.secondary", product_css)
        self.assertIn(".step-order-actions", product_css)
        self.assertIn(".step-move-action.secondary", product_css)
        self.assertIn(".step-disclosure.step-just-moved", product_css)
        self.assertIn(".step-type-field > small", product_css)
        self.assertIn(".step-disclosure.step-just-created", product_css)
        self.assertIn(".product-toast.show.has-action", product_css)
        self.assertIn(".editor-field-error", product_css)
        self.assertIn(".run-recovery-card", product_css)
        self.assertIn(".workspace-status", product_css)
        self.assertIn(".run-timeline-item", product_css)
        self.assertIn(".run-step-strip", product_css)
        self.assertIn(".record-task-card", product_css)
        self.assertIn(".record-live-status", product_css)
        self.assertIn(".library-command-bar", product_css)
        self.assertIn(".library-empty", product_css)
        self.assertIn("Canonical visual system v3", product_css)
        self.assertIn("--ui-canvas:", product_css)
        self.assertIn("--ui-focus-ring:", product_css)
        self.assertIn(".store-page .hero {", product_css)
        self.assertIn(".product-choice .choice-actions", product_css)
        self.assertIn("globalThis.productChoice", product_js)
        self.assertIn("product-shortcuts", product_js)
        self.assertIn("createVisibilityPoller", environment_js)
        self.assertIn("environmentSnapshot", environment_js)
        self.assertIn("requestJson", environment_js)
        self.assertIn("postJson", environment_js)
        self.assertIn("sendJsonBeacon", environment_js)
        self.assertIn('/assets/environment.js', studio)
        self.assertIn('/assets/environment.js', store)
        self.assertIn('/assets/environment.js', control)
        self.assertEqual(studio.count("function clientEnvironmentSnapshot()"), 0)
        self.assertEqual(store.count("function clientEnvironmentSnapshot()"), 0)
        self.assertIn("event.key === '?'", product_js)
        self.assertEqual(product_js.count("commandList.innerHTML = '';"), 1)
        self.assertIn("shortcutDialog.addEventListener('close', restoreShortcutFocus)", product_js)
        self.assertIn("replayCommandsError", product_js)
        self.assertIn("GPAClient.requestJson('/api/workflows'", product_js)
        self.assertIn("timeoutMs: 10000", product_js)
        self.assertIn("loadReplayCommands({ refresh: true })", product_js)
        self.assertIn("if (event.key !== 'Tab') return", product_js)
        self.assertIn("enqueueProductDialog", product_js)
        self.assertIn("productDialogQueue", product_js)
        self.assertIn("restoreDialogFocus", product_js)
        self.assertIn("isDialogBackdropClick(confirmDialog, event)", product_js)
        self.assertIn("isDialogBackdropClick(choiceDialog, event)", product_js)
        self.assertIn("shortcutDialog.addEventListener('cancel'", product_js)
        self.assertIn("data-shortcut-scope=\"save\"", product_js)
        self.assertIn("syncShortcutGuide", product_js)
        self.assertIn("route-chord-hint", product_js)
        self.assertIn("{ s: '/store', p: '/setup', r: '/control', w: '/' }", product_js)
        self.assertGreaterEqual(product_js.count("hideRouteChord();"), 5)
        self.assertIn("data-choice-save", product_js)
        self.assertIn("data-choice-discard", product_js)
        self.assertIn("data-choice-cancel", product_js)
        self.assertIn("scrollbar-color:", product_css)
        self.assertIn(".store-page .catalog-grid { grid-template-columns: repeat(3, minmax(0, 1fr)) !important; }", product_css)
        self.assertIn(".detail-decision-facts", product_css)
        self.assertIn("Store decision marketplace v6", product_css)
        self.assertIn(".card-decision-facts", product_css)
        self.assertNotIn("先看用途、适配结论和权限边界", store)
        self.assertIn('class="publisher publisher-dialog"', store)
        self.assertIn("openPublisherPanel", store)
        self.assertIn("client_environment: clientEnvironmentSnapshot()", store)
        self.assertIn("Publisher and guide system v10", product_css)
        self.assertIn("product-guide", product_js)
        self.assertIn("label: '打开使用说明'", product_js)
        self.assertIn("guideTrigger.addEventListener('click', openGuide)", product_js)
        self.assertIn("guideDialog.addEventListener('close', restoreGuideFocus)", product_js)
        self.assertIn("Unified application shell v7", product_css)
        self.assertIn("Shared state language v8", product_css)
        self.assertIn("Modal and touch system v9", product_css)
        self.assertIn("body:has(.editor-command-dock) .product-toast", product_css)
        self.assertIn("@media (hover: none) and (pointer: coarse)", product_css)
        self.assertIn("runLauncherReturnFocus", studio)
        self.assertIn("runtimeConsoleClickIsBackdrop", studio)
        self.assertIn("trapDetailFocus", store)
        self.assertIn(".state-message.is-error", product_css)
        self.assertIn(".control-state-metric", product_css)
        self.assertIn("--app-rail-expanded: 184px", product_css)
        self.assertIn("calc(100vw - var(--app-rail-expanded)", product_css)
        self.assertNotIn(".handoff-entry", store)
        self.assertNotIn(".card-flags", store)
        self.assertIn(".control-page .control-overview[data-state=\"issue\"]", product_css)
        self.assertIn("Control command center v5", product_css)
        self.assertIn(".control-page .control-metrics", product_css)
        self.assertIn(".control-page .run-issue-glance", product_css)
        self.assertIn(".control-page .diagnostic-archive-note", product_css)
        self.assertIn(".replay-health-clear", product_css)
        self.assertIn(".replay-health-more-list", product_css)
        self.assertEqual(listing["replays"][0]["compatibility"]["status"], "degraded")
        self.assertEqual(detail["replay"]["schema"], "gpa.replay/v1")
        self.assertEqual(intent_status, 200)
        self.assertEqual(intent["intent"]["apps"], ["Safari"])
        self.assertEqual(plan_status, 201)
        self.assertEqual(planned["plan"]["steps"][0]["app"], "Microsoft Edge")
        self.assertEqual(space["space"]["state"], "planned")
        self.assertEqual(community_detail["record"]["record_id"], self.community_record_id)
        self.assertIn("safe_web", community_detail["record"])
        self.assertIn("runnable", community_detail["record"]["safe_web"])

    def test_run_history_exposes_failed_step_for_recovery(self):
        run_state = {
            "status": "failed",
            "success": False,
            "started_at": "2026-08-12 10:00:00",
            "finished_at": "2026-08-12 10:00:02",
            "steps_run": 2,
            "steps_failed": 1,
            "error": "Step 2 failed: target not found",
            "execution_mode": "desktop",
        }
        result = {
            "steps": [
                {"step_number": 1, "state": "done", "error": ""},
                {"step_number": 2, "state": "failed", "error": "target not found"},
            ],
        }
        server_module._save_run_history("api_replay", "failed-run", run_state, result)
        history = server_module._list_run_history("api_replay")

        self.assertEqual(server_module._failed_step_number(result), 2)
        self.assertEqual(history[0]["failed_step"], 2)

    def test_duplicate_step_preserves_visual_subgraph_under_new_id(self):
        workflow, _ = server_module._storage().load("api_replay")
        source_step = workflow.steps[0]
        source_graph = server_module._coordinate_subgraph(source_step.id, [120, 180])
        payload = server_module._workflow_payload(workflow, {source_step.id: source_graph})
        duplicate = dict(payload["steps"][0])
        duplicate["id"] = "duplicated-step"
        duplicate["metadata"] = {
            **dict(duplicate.get("metadata") or {}),
            "editor_duplicate_source_step_id": source_step.id,
        }
        payload["steps"].insert(1, duplicate)

        updated, subgraphs = server_module._apply_workflow_payload(
            workflow,
            {source_step.id: source_graph},
            payload,
        )

        self.assertEqual(updated.steps[1].id, "duplicated-step")
        self.assertNotIn("editor_duplicate_source_step_id", updated.steps[1].metadata)
        self.assertIn("duplicated-step", subgraphs)
        self.assertIsNot(subgraphs["duplicated-step"], source_graph)
        self.assertEqual(subgraphs["duplicated-step"].click_coordinates, [120, 180])

    def test_reordered_steps_keep_visual_subgraphs_attached_to_step_ids(self):
        workflow, _ = server_module._storage().load("api_replay")
        first_id, second_id = workflow.steps[0].id, workflow.steps[1].id
        original_subgraphs = {
            first_id: server_module._coordinate_subgraph(first_id, [100, 140]),
            second_id: server_module._coordinate_subgraph(second_id, [220, 260]),
        }
        payload = server_module._workflow_payload(workflow, original_subgraphs)
        payload["steps"] = list(reversed(payload["steps"]))

        updated, subgraphs = server_module._apply_workflow_payload(
            workflow,
            original_subgraphs,
            payload,
        )

        self.assertEqual([step.id for step in updated.steps], [second_id, first_id])
        self.assertEqual([step.step_number for step in updated.steps], [1, 2])
        self.assertEqual(set(subgraphs), {first_id, second_id})
        self.assertEqual(subgraphs[first_id].click_coordinates, [100, 140])
        self.assertEqual(subgraphs[second_id].click_coordinates, [220, 260])

    def test_streamed_package_inspection_and_duplicate_publish(self):
        package = server_module._community_repository().package_path(self.community_record_id).read_bytes()

        inspect_status, inspected = self.post_bytes(
            "/api/community/inspect",
            package,
            "application/zip",
        )
        self.assertEqual(inspect_status, 200)
        self.assertEqual(inspected["inspection"]["package_bytes"], len(package))
        self.assertGreater(inspected["inspection"]["step_count"], 0)
        self.assertIn("has_environment", inspected["inspection"]["evidence"])
        self.assertIn("success_criteria_count", inspected["inspection"]["evidence"])
        self.assertFalse(inspected["inspection"]["reproduction_contract"]["publishable_as_verified"])
        self.assertRegex(inspected["inspection"]["inspection_token"], r"^[a-f0-9]{32}$")
        self.assertRegex(inspected["inspection"]["package_sha256"], r"^[a-f0-9]{64}$")

        request = urllib.request.Request(
            self.base + "/api/community/publish-inspection",
            data=json.dumps({
                "inspection_token": inspected["inspection"]["inspection_token"],
                "privacy_reviewed": True,
                "publication_mode": "verified_replay",
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(rejected.exception.code, 422)

        publish_status, published = self.post_json(
            "/api/community/publish-inspection",
            {
                "inspection_token": inspected["inspection"]["inspection_token"],
                "author": "Stream Tester",
                "tags": ["upload", "package"],
                "record_license": "CC-BY-4.0",
                "privacy_reviewed": True,
            },
        )
        self.assertEqual(publish_status, 200)
        self.assertTrue(published["record"]["duplicate"])
        self.assertFalse(any((server_module.COMMUNITY_DIR / ".uploads").glob(".upload-*")))
        self.assertFalse(any((server_module.COMMUNITY_DIR / ".inspections").glob("*")))

    def test_streamed_package_inspection_rejects_invalid_zip(self):
        request = urllib.request.Request(
            self.base + "/api/community/inspect",
            data=b"not-a-zip",
            headers={"Content-Type": "application/zip"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 422)
        self.assertFalse(any((server_module.COMMUNITY_DIR / ".uploads").glob(".upload-*")))

    def test_inspected_recording_is_previewable_and_publishes_exact_snapshot(self):
        from gpa.community.package import export_workflow_package
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV is not installed")

        storage = server_module._storage()
        workflow = Workflow(
            workflow_id="recorded_inspection",
            workflow_name="recorded_inspection",
            workflow_title="Recorded inspection",
            description="Inspect a package with real media evidence.",
            task_description="Verify the recording before publishing.",
                environment={
                    "schema": "gpa.environment/v1",
                    "system": {"name": "darwin", "machine": "arm64"},
                    "browser": {"family": "Chrome"},
                    "screen": {"width": 1512, "height": 982, "pixel_ratio": 2},
                },
            understanding={
                "schema": "gpa.agent-understanding/v1",
                "goal": "Verify the recording before publishing.",
                "required_environment": {"applications": ["Chrome"], "web_hosts": ["example.com"]},
                "interaction_profile": {"step_count": 1, "action_counts": {"assert_text": 1}},
                "success_criteria": [{"step": 1, "type": "assert_text", "expected": "done"}],
                "risk_controls": {"read_only": True},
            },
            steps=[WorkflowStep(1, "Verify done", action_type="assert_text", value="done")],
        )
        workflow_dir = storage.save(workflow, {})
        media_path = workflow_dir / "recording.mp4"
        writer = cv2.VideoWriter(
            str(media_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            12.0,
            (64, 48),
        )
        if not writer.isOpened():
            self.skipTest("OpenCV MP4 writer is unavailable")
        try:
            for index in range(12):
                frame = np.zeros((48, 64, 3), dtype=np.uint8)
                frame[:, :, index % 3] = 40 + index * 8
                writer.write(frame)
        finally:
            writer.release()
        media = media_path.read_bytes()
        workflow.artifacts = {
            "recording": {
                "kind": "screen-recording",
                "path": "recording.mp4",
                "mime_type": "video/mp4",
                "bytes": len(media),
                "sha256": hashlib.sha256(media).hexdigest(),
                "duration_seconds": 1.0,
                "width": 64,
                "height": 48,
                "capture_scope": "browser-tab",
                "capture_method": "browser-tab-frame-capture",
                "privacy_review": {
                    "status": "passed",
                    "other_apps_visible": False,
                    "scope_confirmed": "browser-tab",
                },
            }
        }
        storage.save(workflow, {})
        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = export_workflow_package(
                workflow.workflow_id,
                Path(tmpdir) / "recorded.gpa-record.zip",
                storage=storage,
            )
            package = package_path.read_bytes()

        inspect_status, inspected = self.post_bytes(
            "/api/community/inspect",
            package,
            "application/zip",
        )
        self.assertEqual(inspect_status, 200)
        inspection = inspected["inspection"]
        self.assertTrue(inspection["evidence"]["recording_container_verified"])
        self.assertEqual(inspection["evidence"]["agent_understanding"]["goal"], workflow.task_description)
        self.assertTrue(inspection["evidence"]["recording_media_verified"])
        self.assertGreaterEqual(
            inspection["evidence"]["recording_media_probe"]["decoded_sample_count"],
            2,
        )
        self.assertTrue(inspection["reproduction_contract"]["publishable_as_verified"])
        self.assertEqual(inspection["reproduction_contract"]["score"], 100)
        preview_url = inspection["recording_preview_url"]
        with urllib.request.urlopen(self.base + preview_url, timeout=5) as response:
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
            self.assertEqual(response.read(), media)

        publish_status, published = self.post_json(
            "/api/community/publish-inspection",
            {
                "inspection_token": inspection["inspection_token"],
                "author": "Evidence Tester",
                "tags": ["recording", "evidence"],
                "record_license": "CC-BY-4.0",
                "privacy_reviewed": True,
                "publication_mode": "verified_replay",
            },
        )
        self.assertEqual(publish_status, 201)
        self.assertEqual(published["record"]["package_sha256"], inspection["package_sha256"])
        with self.assertRaises(urllib.error.HTTPError) as expired:
            urllib.request.urlopen(self.base + preview_url, timeout=5)
        self.assertEqual(expired.exception.code, 404)

    def test_package_inspection_reports_environment_difference_and_scale_hint(self):
        from gpa.community.package import export_workflow_package

        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = export_workflow_package(
                "api_replay",
                Path(tmpdir) / "environment.gpa-record.zip",
                storage=server_module._storage(),
            )
            package = package_path.read_bytes()
        query = urllib.parse.urlencode({
            "screen_width": 1600,
            "screen_height": 1200,
            "pixel_ratio": 1,
            "browser_family": "Edge",
            "viewport_width": 1280,
            "viewport_height": 720,
            "language": "zh-CN",
            "timezone": "Asia/Shanghai",
        })

        status, inspected = self.post_bytes(
            f"/api/community/inspect?{query}",
            package,
            "application/zip",
        )

        self.assertEqual(status, 200)
        evidence = inspected["inspection"]["evidence"]
        self.assertEqual(evidence["environment_diff"]["status"], "degraded")
        dimensions = next(
            item
            for item in evidence["environment_diff"]["differences"]
            if item["field"] == "screen.dimensions"
        )
        self.assertEqual(dimensions["scale_hint"], {"x": 1.6, "y": 1.5})
        self.assertEqual(evidence["current_environment"]["browser"]["family"], "Edge")

    def test_product_overview_reports_execution_maturity(self):
        overview = self.get_json("/api/product/overview")

        self.assertTrue(overview["ok"])
        self.assertEqual(overview["summary"]["workflow_count"], 2)
        self.assertGreaterEqual(overview["summary"]["store_record_count"], 1)
        self.assertIn("isolated_reproduction_passed_count", overview["summary"])
        self.assertIn("isolated_reproduction_coverage", overview["summary"])
        self.assertIn("source_linked_isolated_reproduction_count", overview["summary"])
        self.assertIn("source_linked_reproduction_coverage", overview["summary"])
        self.assertIn(
            "isolated_reproduction_coverage",
            {item["id"] for item in overview["slo_gates"]},
        )
        self.assertIn(
            "source_linked_reproduction_coverage",
            {item["id"] for item in overview["slo_gates"]},
        )
        self.assertIn("assert_url", overview["capabilities"]["semantic_checkpoints"])
        self.assertIn("assert_clipboard", overview["capabilities"]["action_types"])
        self.assertEqual(overview["workflows"][0]["steps"], 2)
        self.assertFalse(overview["stability"]["global_input_hooks_active"])
        self.assertIn("no background keyboard listener", overview["stability"]["input_permission_probe"])
        expected_backend = "quartz" if server_module.sys.platform == "darwin" else "pynput"
        self.assertEqual(overview["stability"]["recording_input_backend"], expected_backend)
        if server_module.sys.platform == "darwin":
            self.assertFalse(overview["stability"]["text_input_sources_translation"])
        self.assertTrue(overview["stability"]["normal_client_disconnects_suppressed"])
        model_policy = overview["runtime"]["model_policy"]
        self.assertTrue(model_policy["text_primary"])
        self.assertTrue(model_policy["vision_primary"])
        self.assertGreaterEqual(model_policy["timeout_seconds"], 5)
        self.assertNotIn("api_key", json.dumps(model_policy).casefold())
        self.assertNotIn("sk-", json.dumps(model_policy).casefold())
        crash = overview["stability"]["python_crash_diagnostics"]
        self.assertIsInstance(crash["new_reports_since_start"], int)
        self.assertIsInstance(crash["crash_free_since_start"], bool)
        self.assertNotIn(str(Path.home()), json.dumps(crash))

    def test_workflow_detail_compares_the_requesting_browser_environment(self):
        detail = self.get_json(
            "/api/workflows/api_replay?browser_family=Edge&screen_width=1600&screen_height=1200"
            "&viewport_width=1200&viewport_height=800&pixel_ratio=1"
        )["workflow"]
        diff = detail["environment_diff"]
        self.assertEqual(diff["status"], "degraded")
        fields = {item["field"] for item in diff["differences"]}
        self.assertIn("browser.family", fields)
        self.assertIn("screen.dimensions", fields)
        strategies = {item["strategy"] for item in diff["adaptation_plan"]}
        self.assertIn("semantic_browser_navigation", strategies)
        self.assertIn("scale_then_relocalize", strategies)
        gate = detail["reproduction_gate"]
        self.assertEqual(gate["schema"], "gpa.reproduction-gate/v1")
        self.assertEqual(len(gate["decision_id"]), 24)
        self.assertTrue(gate["can_execute"])
        self.assertEqual(gate["execution_mode"], "desktop")
        self.assertEqual(
            detail["reproduction_contract"]["schema"],
            "gpa.reproduction-contract/v1",
        )

    def test_stale_reproduction_gate_is_rejected_without_consuming_arm(self):
        workflow_id = "safe_web_gate_replay"
        server_module._storage().save(
            Workflow(
                workflow_id=workflow_id,
                workflow_name=workflow_id,
                workflow_title="Safe Web Gate Replay",
                description="Verify gate freshness before public Web execution",
                task_description="Verify a published fact from a public source",
                steps=[
                    WorkflowStep(1, "Open report", action_type="open_url", value="https://example.test/report"),
                    WorkflowStep(2, "Verify fact", action_type="assert_text", value="Published fact"),
                ],
            ),
            {},
        )
        detail = self.get_json(f"/api/workflows/{workflow_id}")["workflow"]
        decision_id = detail["reproduction_gate"]["decision_id"]
        self.post_json("/api/client/heartbeat", {"client_id": "gate-test-client"})
        _, armed = self.post_json("/api/run/arm", {"workflow_id": workflow_id})
        request = urllib.request.Request(
            self.base + f"/api/workflows/{workflow_id}/run",
            data=json.dumps({
                "arm_token": armed["arm_token"],
                "execution_mode": "safe_web",
                "gate_decision_id": "stale-decision",
                "countdown_seconds": 0,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 409)
        rejected = json.loads(raised.exception.read())
        self.assertEqual(rejected["reproduction_gate"]["decision_id"], decision_id)

        with patch(
            "gpa.execution.safe_web.fetch_public_page",
            return_value=("https://example.test/report", "Published fact", 200),
        ):
            status, started = self.post_json(
                f"/api/workflows/{workflow_id}/run",
                {
                    "arm_token": armed["arm_token"],
                    "execution_mode": "safe_web",
                    "gate_decision_id": decision_id,
                    "countdown_seconds": 0,
                    "max_runtime_seconds": 30,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                started["reproduction_gate"]["decision_id"],
                decision_id,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                run = self.get_json("/api/status")["run"]
                if not run["active"]:
                    break
                time.sleep(0.02)
            else:
                self.fail("gate-validated safe Web replay did not finish")
        self.assertEqual(run["status"], "succeeded")

    def test_invalid_arm_token_does_not_create_replay_space(self):
        workflow_id = "safe_web_invalid_arm"
        server_module._storage().save(
            Workflow(
                workflow_id=workflow_id,
                workflow_name=workflow_id,
                workflow_title="Invalid Arm Space Guard",
                description="Reject invalid authorization before allocating a Space",
                task_description="Verify a public fact",
                steps=[
                    WorkflowStep(
                        1,
                        "Open report",
                        action_type="open_url",
                        value="https://example.test/report",
                    ),
                    WorkflowStep(
                        2,
                        "Verify fact",
                        action_type="assert_text",
                        value="Published fact",
                    ),
                ],
            ),
            {},
        )
        self.post_json("/api/client/heartbeat", {"client_id": "invalid-arm-client"})
        spaces_before = set(server_module.REPLAY_SPACES_DIR.glob("space_*"))

        request = urllib.request.Request(
            self.base + f"/api/workflows/{workflow_id}/run",
            data=json.dumps({
                "arm_token": "invalid-token",
                "client_id": "invalid-arm-client",
                "execution_mode": "safe_web",
                "countdown_seconds": 0,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)

        self.assertEqual(raised.exception.code, 409)
        self.assertEqual(
            set(server_module.REPLAY_SPACES_DIR.glob("space_*")),
            spaces_before,
        )

    def test_agent_handoff_capsule_is_machine_readable_and_target_specific(self):
        query = (
            "browser_family=Edge&screen_width=1600&screen_height=1200"
            "&viewport_width=1200&viewport_height=800&pixel_ratio=1"
        )
        request = urllib.request.Request(
            self.base + f"/api/community/records/{self.community_record_id}/handoff?{query}"
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            capsule = json.loads(response.read())
            disposition = response.headers.get("Content-Disposition", "")

        self.assertEqual(capsule["schema"], "gpa.agent-handoff/v1")
        self.assertEqual(capsule["record"]["record_id"], self.community_record_id)
        self.assertEqual(capsule["evidence"]["target_environment"]["browser"]["family"], "Edge")
        self.assertIn("reproduction_contract", capsule["evidence"])
        self.assertIn(capsule["execution"]["recommended_mode"], {"safe_web", "agent_first"})
        self.assertNotIn(str(Path.home()), json.dumps(capsule))
        self.assertIn(".gpa-handoff.json", disposition)

    def test_preview_media_upload_and_playback_is_binary_and_bounded(self):
        workflow, subgraphs = server_module._storage().load("api_replay")
        preview_id = "preview-media-fixture"
        server_module.STATE["preview"] = {
            "preview_id": preview_id,
            "created_at": "2026-08-10 00:00:00",
            "workflow": workflow,
            "subgraphs": subgraphs,
        }
        recording = b"webm-recording-fixture"

        status, uploaded = self.post_bytes(
            f"/api/preview/media?preview_id={preview_id}&capture_scope=browser"
            "&capture_method=media-recorder&capture_width=1280"
            "&capture_height=720&capture_frame_rate=12",
            recording,
            "video/webm",
        )
        self.assertEqual(status, 201)
        self.assertEqual(uploaded["bytes"], len(recording))
        self.assertEqual(uploaded["capture"]["capture_scope"], "browser")
        self.assertEqual(uploaded["capture"]["capture_method"], "media-recorder")
        self.assertEqual(uploaded["capture"]["width"], 1280)
        self.assertEqual(server_module.STATE["preview"]["media_capture"]["height"], 720)

        with urllib.request.urlopen(
            self.base + f"/api/preview/media?preview_id={preview_id}", timeout=5
        ) as response:
            self.assertEqual(response.headers["Content-Type"], "video/webm")
            self.assertTrue(response.headers["Content-Disposition"].startswith("inline;"))
            self.assertEqual(response.read(), recording)

        ranged_request = urllib.request.Request(
            self.base + f"/api/preview/media?preview_id={preview_id}",
            headers={"Range": "bytes=5-11"},
        )
        with urllib.request.urlopen(ranged_request, timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
            self.assertEqual(response.headers["Content-Range"], f"bytes 5-11/{len(recording)}")
            self.assertEqual(response.read(), recording[5:12])

    def test_privacy_quarantined_record_cannot_leave_store(self):
        from gpa.community.package import export_workflow_package

        storage = server_module._storage()
        workflow = Workflow(
            workflow_id="private_recording",
            workflow_name="private_recording",
            workflow_title="Private recording",
            description="A recording that contains another application.",
            steps=[WorkflowStep(1, "Verify", action_type="assert_text", value="done")],
        )
        workflow_dir = storage.save(workflow, {})
        recording = b"\x00\x00\x00\x18ftypmp42private-recording"
        (workflow_dir / "recording.mp4").write_bytes(recording)
        workflow.artifacts = {
            "recording": {
                "path": "recording.mp4",
                "mime_type": "video/mp4",
                "bytes": len(recording),
                "sha256": hashlib.sha256(recording).hexdigest(),
                "capture_scope": "monitor",
                "privacy_review": {
                    "status": "failed",
                    "other_apps_visible": True,
                    "scope_confirmed": "monitor",
                    "note": "Private application content is visible.",
                },
            }
        }
        storage.save(workflow, {})
        with tempfile.TemporaryDirectory() as tmpdir:
            package = export_workflow_package(
                workflow.workflow_id,
                Path(tmpdir) / "private.gpa-record.zip",
                storage=storage,
            )
            record = server_module._community_repository().publish_package(
                package,
                author="Privacy Test",
                tags=["privacy-test"],
                license_id="CC-BY-4.0",
                privacy_reviewed=True,
            )

        for suffix in ("recording", "download", "handoff"):
            with self.assertRaises(urllib.error.HTTPError) as blocked:
                urllib.request.urlopen(
                    self.base + f"/api/community/records/{record['record_id']}/{suffix}",
                    timeout=5,
                )
            self.assertEqual(blocked.exception.code, 423)

        for suffix, body in (("import", {}), ("audit", {"client_environment": {}})):
            request = urllib.request.Request(
                self.base + f"/api/community/records/{record['record_id']}/{suffix}",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as blocked:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(blocked.exception.code, 423)

        persisted = server_module._community_repository().get_record(record["record_id"])
        self.assertEqual(persisted["stats"]["downloads"], 0)

    def test_deterministic_semantic_replay_does_not_require_llm(self):
        workflow, _ = server_module._storage().load("api_replay")
        self.assertFalse(server_module._workflow_replay_requires_llm(workflow))

        workflow.steps.append(WorkflowStep(3, "点击按钮", action_type="click"))
        self.assertTrue(server_module._workflow_replay_requires_llm(workflow))

    def test_safe_web_replay_runs_with_desktop_automation_disabled(self):
        workflow_id = "safe_web_api_replay"
        server_module._storage().save(
            Workflow(
                workflow_id=workflow_id,
                workflow_name=workflow_id,
                workflow_title="Safe Web API Replay",
                description="Read and verify a public source without desktop input",
                task_description="Verify a published fact from a public Web source",
                steps=[
                    WorkflowStep(1, "Open report", action_type="open_url", value="https://example.test/report"),
                    WorkflowStep(2, "Verify URL", action_type="assert_url", value="example.test/report"),
                    WorkflowStep(3, "Verify fact", action_type="assert_text", value="Published fact"),
                    WorkflowStep(4, "Remember answer", action_type="set_clipboard", value="42"),
                    WorkflowStep(5, "Verify answer", action_type="assert_clipboard", value="42"),
                ],
            ),
            {},
        )
        self.post_json("/api/client/heartbeat", {"client_id": "safe-web-test-client"})
        _, armed = self.post_json("/api/run/arm", {"workflow_id": workflow_id})

        with patch.object(server_module, "DESKTOP_AUTOMATION_ENABLED", False), patch.object(
            server_module,
            "_abort_desktop_actions",
            side_effect=AssertionError("safe Web replay must not touch desktop actions"),
        ), patch(
            "gpa.execution.safe_web.fetch_public_page",
            return_value=("https://example.test/report", "Published fact", 200),
        ):
            status, started = self.post_json(
                f"/api/workflows/{workflow_id}/run",
                {
                    "arm_token": armed["arm_token"],
                    "execution_mode": "safe_web",
                    "countdown_seconds": 0,
                    "max_runtime_seconds": 30,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(started["execution_mode"], "safe_web")
            self.assertFalse(started["desktop_input"])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                run = self.get_json("/api/status")["run"]
                if not run["active"]:
                    break
                time.sleep(0.02)
            else:
                self.fail("safe Web replay did not finish")

        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["execution_mode"], "safe_web")
        self.assertFalse(run["desktop_input"])
        self.assertEqual(run["steps_run"], 5)

    def test_safe_web_replay_stop_is_interruptible_and_never_touches_desktop(self):
        workflow_id = "safe_web_interruptible_replay"
        server_module._storage().save(
            Workflow(
                workflow_id=workflow_id,
                workflow_name=workflow_id,
                workflow_title="Interruptible Safe Web Replay",
                description="Prove that a running read-only Web task can be stopped safely",
                task_description="Open a public source, wait, then verify",
                steps=[
                    WorkflowStep(1, "Open report", action_type="open_url", value="https://example.test/report"),
                    WorkflowStep(2, "Wait for review", action_type="wait", value="5"),
                    WorkflowStep(3, "Verify fact", action_type="assert_text", value="Published fact"),
                ],
            ),
            {},
        )
        self.post_json("/api/client/heartbeat", {"client_id": "safe-web-stop-test-client"})
        _, armed = self.post_json("/api/run/arm", {"workflow_id": workflow_id})

        with patch.object(server_module, "DESKTOP_AUTOMATION_ENABLED", False), patch.object(
            server_module,
            "_abort_desktop_actions",
            side_effect=AssertionError("stopping Safe Web must not touch desktop actions"),
        ), patch(
            "gpa.execution.safe_web.fetch_public_page",
            return_value=("https://example.test/report", "Published fact", 200),
        ):
            _, started = self.post_json(
                f"/api/workflows/{workflow_id}/run",
                {
                    "arm_token": armed["arm_token"],
                    "execution_mode": "safe_web",
                    "countdown_seconds": 0,
                    "max_runtime_seconds": 30,
                },
            )
            self.assertFalse(started["desktop_input"])
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                run = self.get_json("/api/status")["run"]
                if run["active"] and int(run.get("steps_run") or 0) >= 1:
                    break
                time.sleep(0.02)
            else:
                self.fail("safe Web replay did not enter its interruptible wait")
            stop_status, stopped = self.post_json("/api/run/stop", {})
            self.assertEqual(stop_status, 200)
            self.assertEqual(stopped["run_id"], started["run_id"])
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                run = self.get_json("/api/status")["run"]
                if not run["active"]:
                    break
                time.sleep(0.02)
            else:
                self.fail("safe Web replay did not stop promptly")

        self.assertEqual(run["status"], "cancelled")
        self.assertFalse(run["desktop_input"])
        self.assertTrue(run["stop_requested"])

    def test_explicit_safe_web_mode_rejects_desktop_steps(self):
        self.post_json("/api/client/heartbeat", {"client_id": "safe-web-reject-client"})
        _, armed = self.post_json("/api/run/arm", {"workflow_id": "api_replay"})
        request = urllib.request.Request(
            self.base + "/api/workflows/api_replay/run",
            data=json.dumps({
                "arm_token": armed["arm_token"],
                "execution_mode": "safe_web",
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 422)

    def test_desktop_replay_rejects_cross_platform_coordinates_before_input_gate(self):
        request = urllib.request.Request(
            self.base + "/api/workflows/api_replay/run",
            data=json.dumps({
                "arm_token": "unused-because-preflight-must-fail-first",
                "execution_mode": "desktop",
                "client_environment": {
                    "screen": {"width": 1440, "height": 900, "pixel_ratio": 1},
                    "browser": {"family": "Edge"},
                },
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with patch("gpa.replay.environment.platform.system", return_value="Windows"):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 422)
        payload = json.loads(raised.exception.read())
        self.assertFalse(payload["environment_diff"]["safe_to_attempt"])
        self.assertEqual(payload["environment_diff"]["status"], "blocked")

    def test_active_replay_health_does_not_probe_keyboard_input_source(self):
        original_checker = server_module._check_input_monitoring_permission
        original_run = dict(server_module.STATE["run"])
        original_cache = dict(server_module.HEALTH_CACHE)
        server_module._check_input_monitoring_permission = lambda: self.fail(
            "active replay must not start a concurrent keyboard listener"
        )
        try:
            server_module.STATE["run"]["active"] = True
            server_module.HEALTH_CACHE.update({"value": None, "expires_at": 0.0})
            health = server_module._cached_dependency_health()
            self.assertTrue(health["permissions"]["deferred"])
        finally:
            server_module._check_input_monitoring_permission = original_checker
            server_module.STATE["run"] = original_run
            server_module.HEALTH_CACHE.clear()
            server_module.HEALTH_CACHE.update(original_cache)

    def test_foreign_origin_cannot_heartbeat_or_arm_replay(self):
        for path, body in (
            ("/api/client/heartbeat", {"client_id": "foreign"}),
            ("/api/run/arm", {"workflow_id": "api_replay"}),
        ):
            request = urllib.request.Request(
                self.base + path,
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://attacker.example",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 403)

    def test_supplied_space_cannot_bypass_compatibility(self):
        service = server_module._replay_service()
        plan = service.plan("unsupported_replay")
        self.assertFalse(plan.compatibility.runnable)

        with self.assertRaisesRegex(ValueError, "incompatible"):
            server_module._prepare_replay_space("unsupported_replay", plan.space_id)

        original_desktop_gate = server_module._require_desktop_automation
        original_dependencies = server_module._ensure_dependencies
        original_permissions = server_module._ensure_permissions
        server_module._require_desktop_automation = lambda *args, **kwargs: True
        server_module._ensure_dependencies = lambda *args, **kwargs: None
        server_module._ensure_permissions = lambda *args, **kwargs: None
        try:
            request = urllib.request.Request(
                self.base + "/api/workflows/unsupported_replay/run",
                data=json.dumps({"arm_token": "unused", "space_id": plan.space_id}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 422)
        finally:
            server_module._require_desktop_automation = original_desktop_gate
            server_module._ensure_dependencies = original_dependencies
            server_module._ensure_permissions = original_permissions


if __name__ == "__main__":
    unittest.main()
