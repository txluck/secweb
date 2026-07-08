/* secweb 前端逻辑 */
const { createApp, reactive, computed, ref, onMounted, watch, nextTick } = Vue;

const STATUS_ZH = {
  queued: '排队', running: '运行中', done: '完成',
  failed: '失败', stopped: '已停止', needs_input: '待补充',
  awaiting_login: '等待登录', paused: '已暂停',
};

createApp({
  setup() {
    // 路由: 'home' = 项目列表; 'detail' = 项目详情
    const route = ref('home');
    const currentProjectId = ref(null);
    const currentProject = ref(null);

    const projects = ref([]);
    const tasks = ref([]);
    const stats = ref({});
    const selectedId = ref(null);
    const events = ref([]);
    const filter = ref('all');
    const tab = ref('log');
    const urlsRaw = ref('');
    // 提示词预设: 启动时从 /api/skills 动态加载, 加载失败退回写死的 fallback.
    // 用户在 ~/.claude/skills/<name>/ 加 SKILL.md 就会自动出现, 无需改前端代码.
    // "自定义 …" 永远是最后一项, 选中时清空 prompt 让用户自由编辑.
    const PROMPT_PRESETS_FALLBACK = [
      { label: '/hack {url} auto (推荐)',     value: '/hack {url} auto' },
      { label: '/hack {url} (简版)',          value: '/hack {url}' },
      { label: '/bug-bounty {url}',           value: '/bug-bounty {url}' },
      { label: '/src-hunt {url}',             value: '/src-hunt {url}' },
      { label: '/recon {url}',                value: '/recon {url}' },
      { label: '/pentest {url}',              value: '/pentest {url}' },
      { label: '/js-audit {url}',             value: '/js-audit {url}' },
      { label: '/idor {url}',                 value: '/idor {url}' },
      { label: '/sqli {url}',                 value: '/sqli {url}' },
      { label: '/xss {url}',                  value: '/xss {url}' },
      { label: '/ssrf {url}',                 value: '/ssrf {url}' },
      { label: '/auth-bypass {url}',          value: '/auth-bypass {url}' },
      { label: '/business-logic {url}',       value: '/business-logic {url}' },
      { label: '/known-cve {url}',            value: '/known-cve {url}' },
      { label: '/miniprogram-audit {url}',    value: '/miniprogram-audit {url}' },
      { label: '自定义 …',                     value: '' },
    ];
    const PROMPT_PRESETS = ref([...PROMPT_PRESETS_FALLBACK]);

    async function loadSkillPresets() {
      try {
        const r = await fetchJSON('/api/skills');
        const skills = r.skills || [];
        if (!skills.length) return;  // 后端没扫到, 保留 fallback

        // 主流水线 hack 加 "auto" 推荐变体
        const presets = [];
        const hackSkill = skills.find(s => s.name === 'hack');
        if (hackSkill) {
          presets.push({
            label: '/hack {url} auto (推荐)',
            value: '/hack {url} auto',
            title: hackSkill.description || '完整流水线全自动 (recon → js-audit → ... → report)',
          });
        }
        // 所有 skill 按后端排序 (主流水线置顶 + 字母序)
        // label 保持简洁 (只 /name {url}), description 放 title 让鼠标悬停看 — 下拉框不会被长描述撑爆
        for (const s of skills) {
          presets.push({
            label: `/${s.name} {url}`,
            value: `/${s.name} {url}`,
            title: s.description || '',
          });
        }
        // 自定义永远在末尾
        presets.push({ label: '自定义 …', value: '', title: '选这个后下方输入框可任意编辑' });
        PROMPT_PRESETS.value = presets;
      } catch (e) {
        // /api/skills 不存在或失败 → 保留 fallback, 不影响主流程
      }
    }

    const prompt = ref(PROMPT_PRESETS.value[0].value);
    const promptPreset = ref(PROMPT_PRESETS.value[0].value);
    const onPromptPresetChange = () => {
      // 选 "自定义" 时不覆盖, 让用户在 input 里自由编辑
      if (promptPreset.value !== '') prompt.value = promptPreset.value;
    };
    const concurrency = ref(3);
    // 新增目标抽屉: 任务为空时默认展开, 否则收起 (避免遮挡任务列表)
    const submitOpen = ref(false);
    const submitting = ref(false);
    const report = ref('');
    const reportLoading = ref(false);
    const files = ref([]);
    const answerModal = ref(null);
    const answerText = ref('');
    const body = ref(null);
    const logStream = ref(null);
    const autoFollow = ref(true);
    const showThinking = ref(true);
    const showSystem = ref(false);
    // 思考状态计时器: 跟踪最近一次"实质"事件 (排除 thinking) 的时间.
    // 模型在 think / 1M ctx compaction / 长 stream 解析时, stdout 自然停顿,
    // 用户会以为日志卡住. 顶部计时器明确告知"AI 思考中, 不是卡死".
    const lastSubstantiveTs = ref(0);
    const nowTick = ref(Date.now());
    setInterval(() => { nowTick.value = Date.now(); }, 1000);
    const createProjectModal = ref(false);
    // 系统设置 (TEMP 目录等)
    const systemModal = ref(false);
    const systemSaving = ref(false);
    const systemForm = reactive({ temp_dir: '', env_default: '' });
    // .env 文件编辑器
    const envModal = ref(false);
    const envSaving = ref(false);
    const envForm = reactive({
      content: '', path: '', exists: false, hot_keys: [],
      last_hot_applied: [], last_restart_required: [],
    });
    // 邮件设置
    const mailModal = ref(false);
    const mailSaving = ref(false);
    const mailTesting = ref(false);
    const mail = reactive({
      enabled: false, smtp_host: '', smtp_port: 465, use_ssl: true,
      username: '', password: '', password_set: false,
      from_addr: '',
      notify_on_finding: true, notify_on_failure: false,
    });
    const mailToRaw = ref('');
    // 修改密码
    const passwordModal = ref(false);
    const passwordIsDefault = ref(false);
    const pwdForm = reactive({ current: '', next: '', confirm: '' });
    const pwdSaving = ref(false);
    const pwdError = ref('');
    const newProject = reactive({ name: '', description: '', default_prompt: '', auth_payload: '' });
    const reports = ref([]);
    const reportsLoading = ref(false);
    const reportsProjectId = ref(null);

    const reportsScope = computed(() => {
      if (!reportsProjectId.value) return '// 全局';
      const p = projects.value.find(x => x.id === reportsProjectId.value);
      return p ? `@ ${p.name}` : '';
    });

    let lastEventId = 0;
    let ws = null;
    let pollTimer = null;
    let logsEverywhereVersion = 0;
    let globalDefaultPrompt = '/hack {url} auto';

    const selected = computed(() =>
      tasks.value.find(t => t.id === selectedId.value) || null
    );

    const filteredTasks = computed(() => {
      if (filter.value === 'all') return tasks.value;
      if (filter.value === 'findings') return tasks.value.filter(t => t.has_finding);
      if (filter.value === 'rerun') return tasks.value.filter(t => (t.rerun_count || 0) > 0);
      return tasks.value.filter(t => t.status === filter.value);
    });

    const visibleEvents = computed(() => events.value.filter(e => {
      if (e.kind === 'thinking' && !showThinking.value) return false;
      if (e.kind === 'system' && !showSystem.value) return false;
      return true;
    }));

    // 思考状态: 当前任务在跑, 但超过 N 秒没有实质工具事件 → 显示"AI 思考中"计时
    // 触发阈值 5 秒 — 短到能即时安抚用户, 长到不会因正常工具间隔误触
    const thinkingStatus = computed(() => {
      const sel = tasks.value.find(t => t.id === selectedId.value);
      if (!sel || sel.status !== 'running') return null;
      if (!lastSubstantiveTs.value) return null;
      const idleMs = nowTick.value - lastSubstantiveTs.value;
      if (idleMs < 5000) return null;
      const totalSec = Math.floor(idleMs / 1000);
      const mm = String(Math.floor(totalSec / 60)).padStart(2, '0');
      const ss = String(totalSec % 60).padStart(2, '0');
      return `${mm}:${ss}`;
    });

    function statusLabel(s) { return STATUS_ZH[s] || s; }

    // 探索深度: 单元格颜色 (达标 / 偏低 / 严重不足)
    function depthCellLevel(value, threshold) {
      const v = Number(value || 0);
      if (v >= threshold) return 'depth-ok';
      if (v >= Math.ceil(threshold / 2)) return 'depth-warn';
      return 'depth-bad';
    }
    // 综合等级: 任一硬指标 bad → bad; 任一 warn → warn; 全部 ok → ok
    function depthLevel(t) {
      if (!t) return '';
      const cells = [
        depthCellLevel(t.nav_calls, 3),
        depthCellLevel(t.unique_routes, 3),
        depthCellLevel(t.interaction_calls, 5),
        depthCellLevel(t.network_req_calls, 3),
        depthCellLevel(t.mcp_calls, 5),
      ];
      if (cells.includes('depth-bad')) return 'depth-bad';
      if (cells.includes('depth-warn')) return 'depth-warn';
      return 'depth-ok';
    }
    function depthLabel(t) {
      const level = depthLevel(t);
      return level === 'depth-ok' ? '充分' : level === 'depth-warn' ? '偏浅' : '严重不足';
    }
    function bashClass(t) {
      // python web 降级 (py_web_calls > 0) 严重; mcp == 0 也严重
      const mcp = Number(t?.mcp_calls || 0);
      const py = Number(t?.py_web_calls || 0);
      if (py > 0 || (mcp === 0 && (t?.mcp_calls !== null))) return 'depth-bad';
      return 'depth-ok';
    }

    // ---------- 契约执行 (skill 强制门控) ----------
    // 后端在 runner 收尾时把 ~/.claude/skills/<name>/SKILL.md 抽出的 N 条契约
    // 与 report.md "## 契约执行清单" 段落对账, 写到 tasks.contract_*。
    // 这里只是把数字渲染成颜色 + ID 列表, 不做反向校验。
    function contractPct(t) {
      const total = Number(t?.contract_total || 0);
      if (!total) return 0;
      const c = Number(t?.contract_covered || 0);
      return Math.max(0, Math.min(100, Math.round((c / total) * 100)));
    }
    function contractLevel(t) {
      const total = Number(t?.contract_total || 0);
      if (!total) return 'depth-ok';
      const pct = contractPct(t);
      // 阈值与 runner.case_c_contract 的 missing_ratio > 0.3 对齐:
      //   覆盖率 ≥ 70% 视为达标, 50-70% 偏低, < 50% 严重不足
      if (pct >= 70) return 'depth-ok';
      if (pct >= 50) return 'depth-warn';
      return 'depth-bad';
    }
    function contractLabel(t) {
      const total = Number(t?.contract_total || 0);
      if (!total) return '无契约';
      const lv = contractLevel(t);
      return lv === 'depth-ok' ? '达标' : lv === 'depth-warn' ? '偏低' : '严重不足';
    }
    function contractMissingIds(t) {
      const raw = t?.contract_missing_json;
      if (!raw) return [];
      try {
        const arr = typeof raw === 'string' ? JSON.parse(raw) : raw;
        return Array.isArray(arr) ? arr : [];
      } catch {
        return [];
      }
    }
    // 缺漏 chip 的 hover 标题 — 后端没把契约原文带过来, 这里只能给一个通用提示;
    // 用户要看具体内容直接打开 ~/.claude/skills/<contract_skill>/SKILL.md
    function contractItemHint(id) {
      return `契约 C${id} 未在 report.md 中显式声明 [done]/[N/A]/[skip]`;
    }

    function formatTime(ts) {
      if (!ts) return '';
      const d = new Date(ts * 1000);
      const pad = n => String(n).padStart(2, '0');
      return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }

    function formatClock(ts) {
      if (!ts) return '';
      const d = new Date(ts * 1000);
      const pad = n => String(n).padStart(2, '0');
      return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }

    function humanSize(n) {
      if (n < 1024) return n + 'B';
      if (n < 1024*1024) return (n/1024).toFixed(1) + 'K';
      return (n/1024/1024).toFixed(1) + 'M';
    }

    function formatPayload(ev) {
      let p = ev.payload;
      if (typeof p === 'string') {
        try { const j = JSON.parse(p); if (j && typeof j === 'object') p = j; }
        catch {}
      }
      if (typeof p === 'string') return p;
      if (p && typeof p === 'object' && p.event) {
        return `[${p.event}] ${p.error || p.message || JSON.stringify(p.data || '').slice(0, 200)}`;
      }
      return JSON.stringify(p);
    }

    function renderMd(s) {
      const esc = (x) => x.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      let html = esc(s);
      html = html.replace(/```([\s\S]*?)```/g, (_, c) => `<pre><code>${c}</code></pre>`);
      html = html.replace(/^### (.*)$/gm, '<h3>$1</h3>');
      html = html.replace(/^## (.*)$/gm, '<h2>$1</h2>');
      html = html.replace(/^# (.*)$/gm, '<h1>$1</h1>');
      html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
      html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      html = html.replace(/\n\n/g, '</p><p>');
      return '<p>' + html + '</p>';
    }

    async function fetchJSON(url, opts) {
      const r = await fetch(url, opts);
      if (r.status === 401) { location.href = '/login'; throw new Error('401'); }
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    }

    // ---------- 路由 ----------

    function parseHash() {
      const h = location.hash || '#/';
      const m = h.match(/^#\/p\/([a-z0-9]+)$/i);
      const r = h.match(/^#\/reports(?:\/([a-z0-9]+))?$/i);
      if (m) {
        route.value = 'detail';
        currentProjectId.value = m[1];
      } else if (r) {
        route.value = 'reports';
        reportsProjectId.value = r[1] || null;
      } else {
        route.value = 'home';
        currentProjectId.value = null;
        currentProject.value = null;
        selectedId.value = null;
      }
    }

    function goHome() { location.hash = '#/'; }
    function goReports() { location.hash = '#/reports'; }
    function goProjectReports() {
      if (currentProjectId.value) location.hash = `#/reports/${currentProjectId.value}`;
    }

    function openProject(pid) {
      location.hash = `#/p/${pid}`;
    }

    function openReportTask(r) {
      location.hash = `#/p/${r.project_id}`;
      // 等路由切换后再选中任务并打开报告 tab
      setTimeout(() => {
        selectTask(r.id).then(() => { tab.value = 'report'; loadReport(); });
      }, 250);
    }

    function progressPct(p, kind) {
      const t = p.task_count || 0;
      if (!t) return 0;
      return Math.max(0, Math.min(100, ((p[kind + '_count'] || 0) / t) * 100));
    }

    // ---------- 全局加载 ----------

    async function loadGlobalConfig() {
      const c = await fetchJSON('/api/config');
      globalDefaultPrompt = c.default_prompt;
      concurrency.value = c.concurrency;
      // 模型选择 (顶部下拉)
      try {
        const m = await fetchJSON('/api/model');
        modelPresets.value = m.presets || [];
        currentModel.value = m.current || '';
      } catch (e) {
        // 模型 API 不在不影响主流程
      }
      // 动态加载用户 skill 列表填充提示词预设
      await loadSkillPresets();
    }

    const currentModel = ref('');
    const modelPresets = ref([]);

    async function onModelChange() {
      try {
        const r = await fetchJSON('/api/model', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: currentModel.value }),
        });
        currentModel.value = r.current || '';
        // 给个轻量提示, 不打断当前操作
        console.log('[model] 已切到', currentModel.value || '(默认)');
      } catch (e) {
        alert('切换模型失败: ' + e.message);
      }
    }

    async function loadProjects() {
      const r = await fetchJSON('/api/projects');
      projects.value = r.projects;
    }

    async function loadReports(pid) {
      reportsLoading.value = true;
      try {
        const url = pid ? `/api/projects/${pid}/reports` : '/api/reports';
        const r = await fetchJSON(url);
        reports.value = r.reports || [];
      } catch (e) {
        alert('加载报告失败: ' + e.message);
        reports.value = [];
      } finally { reportsLoading.value = false; }
    }

    // ---------- 项目详情 ----------

    async function loadProjectDetail(pid) {
      try {
        currentProject.value = await fetchJSON(`/api/projects/${pid}`);
        prompt.value = currentProject.value.default_prompt || globalDefaultPrompt;
        projectConcurrency.value =
          currentProject.value.concurrency_effective ||
          currentProject.value.concurrency || 3;
        cookieDraft.value = currentProject.value.auth_payload || currentProject.value.cookies || '';
        await loadTasks();
        // 任务为空 -> 自动展开新增目标抽屉; 否则收起, 任务列表占据主视野
        submitOpen.value = tasks.value.length === 0;
      } catch (e) {
        alert('加载项目失败: ' + e.message);
        goHome();
      }
    }

    async function loadTasks() {
      if (!currentProjectId.value) return;
      const r = await fetchJSON(`/api/tasks?project_id=${currentProjectId.value}`);
      tasks.value = r.tasks;
      stats.value = r.stats;
    }

    async function selectTask(id) {
      selectedId.value = id;
      tab.value = 'log';
      events.value = [];
      lastEventId = 0;
      report.value = '';
      files.value = [];
      autoFollow.value = true;
      const myVer = ++logsEverywhereVersion;
      const r = await fetchJSON(`/api/tasks/${id}/events?after_id=0&limit=2000`);
      if (myVer !== logsEverywhereVersion) return;
      events.value = r.events.map(normalizeEvent);
      if (events.value.length) lastEventId = events.value[events.value.length - 1].id;
      scrollLogToEnd(true);
    }

    async function reloadEvents() {
      if (!selectedId.value) return;
      events.value = [];
      lastEventId = 0;
      const r = await fetchJSON(`/api/tasks/${selectedId.value}/events?after_id=0&limit=2000`);
      events.value = r.events.map(normalizeEvent);
      if (events.value.length) lastEventId = events.value[events.value.length - 1].id;
      scrollLogToEnd(true);
    }

    function normalizeEvent(ev) {
      let p = ev.payload;
      if (typeof p === 'string') {
        const t = p.trim();
        if (t.startsWith('{') || t.startsWith('[')) {
          try { p = JSON.parse(p); } catch {}
        }
      }
      return { ...ev, payload: p };
    }

    function scrollLogToEnd(force) {
      nextTick(() => {
        if (!body.value || tab.value !== 'log') return;
        if (!force && !autoFollow.value) return;
        body.value.scrollTop = body.value.scrollHeight;
      });
    }

    function onLogScroll() {
      if (!body.value || tab.value !== 'log') return;
      const el = body.value;
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      autoFollow.value = nearBottom;
    }

    async function submitUrls() {
      if (!urlsRaw.value.trim()) return;
      submitting.value = true;
      try {
        const r = await fetchJSON('/api/tasks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            urls: urlsRaw.value, prompt: prompt.value,
            project_id: currentProjectId.value,
            // 任务级 cookie/凭据: 仅这批任务用, 不污染项目其他任务.
            // 留空时后端会用项目级 auth_payload 兜底.
            auth_payload: cookieDraft.value || '',
          }),
        });
        urlsRaw.value = '';
        if (r.skipped && r.skipped.length) {
          alert('跳过无效 URL:\n' + r.skipped.join('\n'));
        }
        await loadTasks();
        if (r.ids && r.ids.length) selectTask(r.ids[0]);
      } catch (e) {
        alert('提交失败: ' + e.message);
      } finally {
        submitting.value = false;
      }
    }

    async function submitUrlsFromModal() {
      const had = urlsRaw.value.trim().length > 0;
      await submitUrls();
      // 提交成功 (urlsRaw 被清空) 才关弹窗
      if (had && !urlsRaw.value) submitOpen.value = false;
    }

    // 批量清空当前项目的非活动任务 (running/queued/needs_input 保留)
    // 用例: 项目保留 (含授权 / 默认 prompt / 并发设置), 只清旧任务后重新提交
    async function clearProjectTasks() {
      const projName = currentProject.value?.name || '当前项目';
      if (!confirm(`确认清空 [${projName}] 下所有非活动任务吗?\n\n` +
                   `保留: running / queued / needs_input / paused / awaiting_login\n` +
                   `删除: done / failed / stopped 等\n\n` +
                   `项目本身和授权配置不会动. 此操作不可撤销.`)) {
        return;
      }
      try {
        const pid = currentProjectId.value;
        const url = pid ? `/api/tasks?project_id=${encodeURIComponent(pid)}` : '/api/tasks';
        const r = await fetchJSON(url, { method: 'DELETE' });
        await loadTasks();
        alert(`✅ 已清空 ${r.deleted} 个任务`);
      } catch (e) {
        alert('清空失败: ' + e.message);
      }
    }

    async function setConcurrency() {
      try {
        const r = await fetchJSON('/api/concurrency', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ concurrency: concurrency.value }),
        });
        concurrency.value = r.concurrency;
      } catch (e) { alert('设置失败: ' + e.message); }
    }

    const projectConcurrency = ref(3);

    // 项目级认证数据 - 在 project 详情侧栏可编辑, 提交时由后端注入到 prompt 的 {auth} (兼容 {cookies})
    const cookieDraft = ref('');
    const cookieDirty = computed(() => {
      const saved = (currentProject.value?.auth_payload || currentProject.value?.cookies || '').trim();
      return cookieDraft.value.trim() !== saved;
    });

    async function saveCookies() {
      if (!currentProject.value) return;
      const pid = currentProject.value.id;
      try {
        await fetchJSON(`/api/projects/${pid}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ auth_payload: cookieDraft.value }),
        });
        currentProject.value.auth_payload = cookieDraft.value;
      } catch (e) { alert('保存认证数据失败: ' + e.message); }
    }

    async function setProjectConcurrency() {
      if (!currentProject.value) return;
      const pid = currentProject.value.id;
      const n = Math.max(1, Math.min(32, projectConcurrency.value | 0));
      try {
        const r = await fetchJSON(`/api/projects/${pid}/concurrency`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ concurrency: n }),
        });
        projectConcurrency.value = r.concurrency;
        currentProject.value.concurrency = r.concurrency;
        currentProject.value.concurrency_effective = r.concurrency;
      } catch (e) { alert('设置失败: ' + e.message); }
    }

    async function stop(id) {
      await fetchJSON(`/api/tasks/${id}/stop`, { method: 'POST' });
      await loadTasks();
    }
    async function retry(id) {
      await fetchJSON(`/api/tasks/${id}/retry`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fresh: true }),
      });
      await loadTasks();
      selectTask(id);
    }
    async function del(id) {
      if (!confirm('删除这个任务及其日志?')) return;
      await fetchJSON(`/api/tasks/${id}`, { method: 'DELETE' });
      if (selectedId.value === id) selectedId.value = null;
      await loadTasks();
    }
    async function logout() {
      await fetch('/logout', { method: 'POST' });
      location.href = '/login';
    }

    // ---------- 系统设置 ----------

    async function openSystemSettings() {
      try {
        const cfg = await fetchJSON('/api/settings/temp_dir');
        systemForm.temp_dir = cfg.current || '';
        systemForm.env_default = cfg.env_default || '';
      } catch (e) {
        systemForm.temp_dir = '';
        systemForm.env_default = '';
      }
      systemModal.value = true;
    }
    function closeSystemSettings() { systemModal.value = false; }

    async function saveSystemSettings() {
      systemSaving.value = true;
      try {
        const r = await fetchJSON('/api/settings/temp_dir', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ temp_dir: systemForm.temp_dir }),
        });
        systemForm.temp_dir = r.current || '';
        closeSystemSettings();
      } catch (e) {
        alert('保存失败: ' + e.message);
      } finally {
        systemSaving.value = false;
      }
    }

    // ---------- .env 文件编辑器 ----------

    async function _fetchEnv() {
      const r = await fetchJSON('/api/settings/env');
      envForm.content = r.content || '';
      envForm.path = r.path || '';
      envForm.exists = !!r.exists;
      envForm.hot_keys = r.hot_keys || [];
    }

    async function openEnvSettings() {
      envForm.last_hot_applied = [];
      envForm.last_restart_required = [];
      try {
        await _fetchEnv();
      } catch (e) {
        alert('加载 .env 失败: ' + e.message);
        return;
      }
      envModal.value = true;
    }
    function closeEnvSettings() { envModal.value = false; }

    async function reloadEnvSettings() {
      try {
        await _fetchEnv();
        envForm.last_hot_applied = [];
        envForm.last_restart_required = [];
      } catch (e) {
        alert('重新加载失败: ' + e.message);
      }
    }

    async function saveEnvSettings() {
      envSaving.value = true;
      try {
        const r = await fetchJSON('/api/settings/env', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: envForm.content }),
        });
        envForm.content = r.content || '';
        envForm.last_hot_applied = r.hot_applied || [];
        envForm.last_restart_required = r.restart_required || [];
        if (envForm.last_restart_required.length) {
          alert(
            '已保存, 但以下 key 属冷配置, 需重启服务才生效:\n\n  ' +
            envForm.last_restart_required.join(', ')
          );
        } else if (envForm.last_hot_applied.length) {
          closeEnvSettings();
        } else {
          closeEnvSettings();
        }
      } catch (e) {
        alert('保存失败: ' + e.message);
      } finally {
        envSaving.value = false;
      }
    }

    // ---------- 邮件设置 ----------

    async function openMailSettings() {
      try {
        const cfg = await fetchJSON('/api/settings/mail');
        Object.assign(mail, cfg);
        mail.password = '';  // 永远不回显, 留空 = 保留旧值
        mailToRaw.value = (cfg.to_addrs || []).join(', ');
      } catch (e) {
        // 配置首次为空时也允许打开
      }
      mailModal.value = true;
    }
    function closeMailSettings() { mailModal.value = false; }

    function _mailPayload() {
      const to = mailToRaw.value
        .split(/[,;\s]+/).map(x => x.trim()).filter(Boolean);
      return {
        enabled: mail.enabled,
        smtp_host: mail.smtp_host,
        smtp_port: mail.smtp_port || 465,
        use_ssl: !!mail.use_ssl,
        username: mail.username,
        password: mail.password,  // 空 = 服务端保留旧值
        from_addr: mail.from_addr,
        to_addrs: to,
        notify_on_finding: !!mail.notify_on_finding,
        notify_on_failure: !!mail.notify_on_failure,
      };
    }

    async function saveMail() {
      mailSaving.value = true;
      try {
        const cfg = await fetchJSON('/api/settings/mail', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(_mailPayload()),
        });
        Object.assign(mail, cfg);
        mail.password = '';
        mailToRaw.value = (cfg.to_addrs || []).join(', ');
        closeMailSettings();
      } catch (e) {
        alert('保存失败: ' + e.message);
      } finally {
        mailSaving.value = false;
      }
    }

    async function testMail() {
      mailTesting.value = true;
      try {
        // 先保存当前编辑值, 再发测试 (否则用户改了没保存就看不到效果)
        await fetchJSON('/api/settings/mail', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(_mailPayload()),
        });
        await fetchJSON('/api/settings/mail/test', { method: 'POST' });
        alert('测试邮件已发送, 请检查收件箱 (含垃圾邮件)');
      } catch (e) {
        alert('测试失败: ' + e.message);
      } finally {
        mailTesting.value = false;
      }
    }

    // ---------- 修改密码 ----------

    async function openPasswordModal() {
      pwdForm.current = '';
      pwdForm.next = '';
      pwdForm.confirm = '';
      pwdError.value = '';
      try {
        const s = await fetchJSON('/api/auth/status');
        passwordIsDefault.value = !!s.password_is_default;
      } catch (_) {
        passwordIsDefault.value = false;
      }
      passwordModal.value = true;
    }
    function closePasswordModal() { passwordModal.value = false; }

    async function submitPasswordChange() {
      pwdError.value = '';
      if (!pwdForm.current || !pwdForm.next) {
        pwdError.value = '请填写当前密码和新密码';
        return;
      }
      if (pwdForm.next !== pwdForm.confirm) {
        pwdError.value = '两次输入的新密码不一致';
        return;
      }
      if (pwdForm.next.length < 6) {
        pwdError.value = '新密码至少 6 位';
        return;
      }
      if (pwdForm.next === pwdForm.current) {
        pwdError.value = '新密码不能与旧密码相同';
        return;
      }
      pwdSaving.value = true;
      try {
        await fetchJSON('/api/auth/change-password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            current_password: pwdForm.current,
            new_password: pwdForm.next,
            confirm_password: pwdForm.confirm,
          }),
        });
        passwordModal.value = false;
        passwordIsDefault.value = false;
        alert('密码已更新, 为安全起见请重新登录');
        await fetch('/logout', { method: 'POST' });
        location.href = '/login';
      } catch (e) {
        pwdError.value = e.message || '提交失败';
      } finally {
        pwdSaving.value = false;
      }
    }

    function openAnswer(t) {
      answerModal.value = t;
      answerText.value = '';
    }
    async function submitAnswer() {
      if (!answerText.value.trim()) return;
      const id = answerModal.value.id;
      try {
        await fetchJSON(`/api/tasks/${id}/answer`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ answer: answerText.value }),
        });
        answerModal.value = null;
        await loadTasks();
        selectTask(id);
      } catch (e) { alert('提交失败: ' + e.message); }
    }

    const followupModal = ref(null);
    const followupText = ref('');

    async function resumeRerun(id) {
      if (!confirm('沿用原 session_id 重跑? 全部上下文会保留')) return;
      try {
        await fetchJSON(`/api/tasks/${id}/resume-rerun`, { method: 'POST' });
        await loadTasks();
        selectTask(id);
      } catch (e) { alert('续跑失败: ' + e.message); }
    }

    function openFollowup(t) {
      followupModal.value = t;
      followupText.value = '';
    }

    async function submitFollowup() {
      if (!followupText.value.trim()) return;
      const id = followupModal.value.id;
      const wasRunning = followupModal.value.status === 'running';
      try {
        await fetchJSON(`/api/tasks/${id}/continue`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt: followupText.value }),
        });
        followupModal.value = null;
        await loadTasks();
        selectTask(id);
      } catch (e) {
        const verb = wasRunning ? '暂停并继续' : '续跑';
        alert(verb + '失败: ' + e.message);
      }
    }

    async function pauseTask(t) {
      try {
        await fetchJSON(`/api/tasks/${t.id}/pause`, { method: 'POST' });
        await loadTasks();
        selectTask(t.id);
      } catch (e) { alert('暂停失败: ' + e.message); }
    }

    async function unpauseTask(t) {
      try {
        await fetchJSON(`/api/tasks/${t.id}/unpause`, { method: 'POST' });
        await loadTasks();
        selectTask(t.id);
      } catch (e) { alert('继续失败: ' + e.message); }
    }

    async function openLoginBrowser(t) {
      try {
        await fetchJSON(`/api/tasks/${t.id}/await-login`, { method: 'POST' });
        await loadTasks();
        selectTask(t.id);
      } catch (e) { alert('启动浏览器失败: ' + e.message); }
    }

    async function loginDone(t) {
      try {
        await fetchJSON(`/api/tasks/${t.id}/login-done`, { method: 'POST' });
        await loadTasks();
        selectTask(t.id);
      } catch (e) { alert('操作失败: ' + e.message); }
    }

    async function copySession(sid) {
      if (!sid) return;
      try {
        await navigator.clipboard.writeText(sid);
      } catch {
        const ta = document.createElement('textarea');
        ta.value = sid; document.body.appendChild(ta);
        ta.select(); document.execCommand('copy'); ta.remove();
      }
    }

    async function loadReport() {
      tab.value = 'report';
      if (!selectedId.value) return;
      reportLoading.value = true;
      try {
        const r = await fetchJSON(`/api/tasks/${selectedId.value}/report`);
        report.value = r.content || '';
      } finally { reportLoading.value = false; }
    }
    async function loadFiles() {
      tab.value = 'files';
      if (!selectedId.value) return;
      const r = await fetchJSON(`/api/tasks/${selectedId.value}/files`);
      files.value = r.files;
    }

    // ---------- 项目 CRUD ----------

    function openCreateProject() {
      newProject.name = '';
      newProject.description = '';
      newProject.default_prompt = '';
      newProject.auth_payload = '';
      createProjectModal.value = true;
    }

    async function submitNewProject() {
      if (!newProject.name.trim()) { alert('项目名不能为空'); return; }
      try {
        const r = await fetchJSON('/api/projects', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: newProject.name.trim(),
            description: newProject.description.trim(),
            default_prompt: newProject.default_prompt.trim(),
            auth_payload: newProject.auth_payload.trim(),
          }),
        });
        createProjectModal.value = false;
        await loadProjects();
        openProject(r.id);
      } catch (e) { alert('创建失败: ' + e.message); }
    }

    async function confirmDeleteProject(p) {
      const hasTasks = p.task_count > 0;
      const msg = hasTasks
        ? `项目「${p.name}」下有 ${p.task_count} 个任务，将一起删除（含日志和产物）。确定?`
        : `删除项目「${p.name}」?`;
      if (!confirm(msg)) return;
      try {
        await fetchJSON(`/api/projects/${p.id}?cascade=${hasTasks}`, { method: 'DELETE' });
        await loadProjects();
      } catch (e) { alert('删除失败: ' + e.message); }
    }

    // ---------- WS ----------

    function connectWS() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => {
        // WS 连上 (含断线重连) 立刻拉一次状态对账, 补回断开期间漏的 status 事件。
        // 没这一步, Chrome 后台 tab 节流 / 系统睡眠醒来后, 状态可能停在旧值。
        if (route.value === 'home') loadProjects();
        else if (route.value === 'reports') loadReports(reportsProjectId.value);
        else loadTasks();
        if (selectedId.value) reloadEvents();
      };
      ws.onmessage = (msg) => {
        let m;
        try { m = JSON.parse(msg.data); } catch { return; }
        if (m.type !== 'event') return;

        if (m.kind === 'status') {
          // 同时刷新项目列表(任务计数变了)和当前项目任务列表
          if (route.value === 'home') loadProjects();
          if (route.value === 'detail') loadTasks();
          if (m.task_id === selectedId.value) {
            lastEventId += 1;
            events.value.push({
              id: lastEventId, task_id: m.task_id, kind: 'status',
              payload: m.payload, ts: Date.now()/1000,
            });
            scrollLogToEnd();
          }
          return;
        }

        if (m.task_id === selectedId.value) {
          lastEventId += 1;
          events.value.push(normalizeEvent({
            id: lastEventId, task_id: m.task_id, kind: m.kind,
            payload: m.payload, ts: Date.now()/1000,
          }));
          // 实质事件 (非 thinking 非 system) 才重置计时器
          // thinking 不算 — 用户关心的是"是否还在干活", thinking 不算干活
          if (m.kind !== 'thinking' && m.kind !== 'system') {
            lastSubstantiveTs.value = Date.now();
          }
          if (events.value.length > 5000) events.value.splice(0, 1000);
          scrollLogToEnd();
        }
      };
      ws.onclose = () => { setTimeout(connectWS, 2000); };
    }

    // ---------- 路由切换响应 ----------

    watch([route, currentProjectId, reportsProjectId], async ([r, pid, rpid]) => {
      if (r === 'home') {
        await loadProjects();
      } else if (r === 'detail' && pid) {
        await loadProjectDetail(pid);
      } else if (r === 'reports') {
        if (!projects.value.length) await loadProjects();
        await loadReports(rpid);
      }
    });

    onMounted(async () => {
      window.addEventListener('hashchange', parseHash);
      parseHash();
      await loadGlobalConfig();
      if (route.value === 'home') await loadProjects();
      else if (route.value === 'reports') {
        await loadProjects();
        await loadReports(reportsProjectId.value);
      }
      else if (currentProjectId.value) await loadProjectDetail(currentProjectId.value);
      connectWS();
      pollTimer = setInterval(() => {
        if (route.value === 'home') loadProjects();
        else if (route.value === 'reports') loadReports(reportsProjectId.value);
        else loadTasks();
      }, 5000);
      // tab 重新可见 / 系统从睡眠唤醒 时立刻对账, 别等 5s 轮询
      // (Chrome 后台 tab 的 setInterval 会被节流到分钟级, 从睡眠醒来更慢)
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState !== 'visible') return;
        if (route.value === 'home') loadProjects();
        else if (route.value === 'reports') loadReports(reportsProjectId.value);
        else loadTasks();
        if (selectedId.value) reloadEvents();
        // WS 可能也已悄悄死了, 主动重连一下
        if (ws && ws.readyState !== WebSocket.OPEN && ws.readyState !== WebSocket.CONNECTING) {
          try { ws.close(); } catch {}
          connectWS();
        }
      });
    });

    return {
      route, currentProject, projects, tasks, stats,
      selectedId, selected, filteredTasks, events, visibleEvents, thinkingStatus,
      filter, tab, urlsRaw, prompt, promptPreset, PROMPT_PRESETS, onPromptPresetChange, concurrency, submitting, submitOpen,
      report, reportLoading, files, answerModal, answerText, body, logStream,
      autoFollow, showThinking, showSystem,
      createProjectModal, newProject,
      reports, reportsLoading, reportsScope,
      progressPct, goReports, goProjectReports, openReportTask,
      statusLabel, formatTime, formatClock, formatPayload, humanSize, renderMd,
      depthCellLevel, depthLevel, depthLabel, bashClass,
      contractPct, contractLevel, contractLabel, contractMissingIds, contractItemHint,
      goHome, openProject,
      selectTask, submitUrls, submitUrlsFromModal, clearProjectTasks, setConcurrency, setProjectConcurrency, projectConcurrency,
      cookieDraft, cookieDirty, saveCookies,
      stop, retry, del,
      logout, openAnswer, submitAnswer, loadReport, loadFiles,
      followupModal, followupText, resumeRerun, openFollowup, submitFollowup, copySession,
      reloadEvents, onLogScroll,
      openCreateProject, submitNewProject, confirmDeleteProject,
      mailModal, mail, mailToRaw, mailSaving, mailTesting,
      openMailSettings, closeMailSettings, saveMail, testMail,
      systemModal, systemForm, systemSaving,
      openSystemSettings, closeSystemSettings, saveSystemSettings,
      envModal, envForm, envSaving,
      openEnvSettings, closeEnvSettings, saveEnvSettings, reloadEnvSettings,
      currentModel, modelPresets, onModelChange,
      passwordModal, passwordIsDefault, pwdForm, pwdSaving, pwdError,
      openPasswordModal, closePasswordModal, submitPasswordChange,
      openLoginBrowser, loginDone, pauseTask, unpauseTask,
    };
  }
}).mount('#app');
