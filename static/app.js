const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const UI = {
  state: { connected: false, profile: {}, preferences: {}, snapshot: null, conda: { envs: [] }, experiments: [], benchmark_history: [] },
  view: 'dashboard',
  queueFilter: 'all',
  selectedLog: null
};

const statusLabels = {
  queued: '等待调度',
  running: '运行中',
  success: '已完成',
  failed: '执行失败',
  waiting_memory: '等待显存',
  canceled: '已取消'
};

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>'"]/g, function (ch) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[ch];
  });
}

function fmtBytes(bytes, digits) {
  const value = Number(bytes || 0);
  if (!value) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = value;
  let i = 0;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i += 1; }
  return size.toFixed(i >= 3 ? (digits == null ? 1 : digits) : 0) + ' ' + units[i];
}

function fmtMb(mb) {
  return mb == null || Number.isNaN(Number(mb)) ? '—' : Number(mb).toLocaleString() + ' MB';
}

function pct(value) {
  return Math.max(0, Math.min(100, Number(value || 0))).toFixed(1) + '%';
}

function fmtTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function showToast(message, error) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.toggle('error', Boolean(error));
  toast.classList.remove('hidden');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(function () { toast.classList.add('hidden'); }, 3500);
}

async function api(url, options) {
  const response = await fetch(url, options || {});
  const data = await response.json().catch(function () { return {}; });
  if (!response.ok || data.error) throw new Error(data.error || ('请求失败 (' + response.status + ')'));
  return data;
}

function setView(view) {
  UI.view = view;
  $$('.nav-item').forEach(function (item) { item.classList.toggle('active', item.dataset.viewTarget === view); });
  $$('.view').forEach(function (item) { item.classList.toggle('active-view', item.id === 'view-' + view); });
  const meta = {
    dashboard: ['LIVE RESOURCE MAP', '资源总览'],
    queue: ['EXPERIMENT QUEUE', '实验队列'],
    benchmarks: ['TENSOR BENCHMARK', 'GPU 测试'],
    logs: ['RUN LOGS', '运行日志']
  }[view];
  $('#view-kicker').textContent = meta[0];
  $('#view-title').textContent = meta[1];
  if (view === 'logs') renderLogs();
  if (view === 'benchmarks') renderBenchmarks();
}

function setModal(id, open) {
  $('#' + id).classList.toggle('hidden', !open);
}

function renderConnection() {
  const profile = UI.state.profile || {};
  const connected = UI.state.connected;
  $('#server-name').textContent = connected ? (profile.host || '已连接') : '未连接';
  $('#server-meta').textContent = connected ? ((profile.username || 'user') + ' · ' + (profile.home || '~')) : '配置 SSH 连接后开始监控';
  $('#connection-label').textContent = connected ? '已连接 · 自动轮询中' : '离线';
  $('#connection-dot').parentElement.classList.toggle('online', connected);
  $('#open-connection').innerHTML = connected ? '连接设置 <span>→</span>' : '连接服务器 <span>→</span>';
  $('#last-sync').textContent = UI.state.last_poll_at ? '同步于 ' + fmtTime(UI.state.last_poll_at) : '尚未同步';
  const alert = $('#global-alert');
  if (UI.state.last_error) {
    alert.textContent = UI.state.last_error;
    alert.classList.remove('hidden');
  } else {
    alert.classList.add('hidden');
  }
}

function renderMetrics() {
  const snapshot = UI.state.snapshot;
  if (!snapshot) return;
  const memory = snapshot.memory || {};
  const gpus = snapshot.gpus || [];
  const disk = snapshot.disk || {};
  $('#cpu-value').innerHTML = Number(snapshot.cpu_percent || 0).toFixed(1) + '<small>%</small>';
  $('#cpu-bar').style.width = pct(snapshot.cpu_percent);
  $('#load-value').textContent = '负载 ' + ((snapshot.load_average || [0])[0] == null ? '—' : snapshot.load_average[0]);
  $('#ram-value').innerHTML = Number(memory.used_percent || 0).toFixed(1) + '<small>%</small>';
  $('#ram-bar').style.width = pct(memory.used_percent);
  $('#ram-detail').textContent = fmtBytes(memory.used_bytes) + ' / ' + fmtBytes(memory.total_bytes);
  const totalGpu = gpus.reduce(function (sum, gpu) { return sum + Number(gpu.memory_total_mb || 0); }, 0);
  const usedGpu = gpus.reduce(function (sum, gpu) { return sum + Number(gpu.memory_used_mb || 0); }, 0);
  const gpuPercent = totalGpu ? usedGpu / totalGpu * 100 : 0;
  $('#gpu-value').innerHTML = gpuPercent.toFixed(1) + '<small>%</small>';
  $('#gpu-bar').style.width = pct(gpuPercent);
  $('#gpu-detail').textContent = gpus.length + ' 张显卡 · ' + fmtMb(usedGpu) + ' / ' + fmtMb(totalGpu);
  const diskPercent = Number(disk.used_percent || 0);
  $('#disk-value').innerHTML = diskPercent.toFixed(1) + '<small>% used</small>';
  $('#disk-bar').style.width = pct(diskPercent);
  $('#disk-detail').textContent = fmtBytes(disk.free_bytes) + ' 可用';
  $('#storage-path').textContent = disk.path || '~';
  $('#storage-free-number').textContent = disk.free_bytes ? (disk.free_bytes / 1024 ** 3).toFixed(1) : '—';
  $('#storage-free-detail').textContent = fmtBytes(disk.free_bytes);
  $('#storage-used-number').textContent = fmtBytes(disk.used_bytes);
  $('#storage-total-number').textContent = fmtBytes(disk.total_bytes);
  $('#storage-donut').style.setProperty('--percent', diskPercent + '%');
}

function renderGpuFleet() {
  const gpus = (UI.state.snapshot && UI.state.snapshot.gpus) || [];
  const root = $('#gpu-grid');
  if (!gpus.length) {
    root.innerHTML = '<div class="empty-state">连接服务器后，这里会显示每张 GPU 的显存、利用率和当前进程。</div>';
    return;
  }
  root.innerHTML = gpus.map(function (gpu) {
    const memoryPercent = gpu.memory_total_mb ? gpu.memory_used_mb / gpu.memory_total_mb * 100 : 0;
    const utilization = Number(gpu.utilization_gpu || 0);
    const benchmark = gpu.benchmark || {};
    const idle = gpu.scheduler_idle && !gpu.reserved_mb;
    const tempClass = Number(gpu.temperature_c || 0) > 80 ? 'hot' : '';
    const speed = benchmark.tensor_tflops ? Number(benchmark.tensor_tflops).toFixed(2) + ' TFLOPS' : '未测试';
    return '<article class="gpu-card">' +
      '<div class="gpu-card-top"><div><div class="gpu-index">GPU ' + esc(gpu.index) + '</div></div><div class="gpu-temp ' + tempClass + '">' + esc(gpu.temperature_c == null ? '—' : gpu.temperature_c) + '°C</div></div>' +
      '<div class="gpu-name" title="' + esc(gpu.name) + '">' + esc(gpu.name || 'Unknown GPU') + '</div>' +
      '<div class="gpu-id">' + esc((gpu.uuid || '').replace('GPU-', '').slice(0, 20)) + '</div>' +
      '<div class="gpu-meters"><div class="meter-row"><span>显存</span><div class="meter-track"><div class="meter-fill memory" style="width:' + pct(memoryPercent) + '"></div></div><span class="meter-number">' + memoryPercent.toFixed(1) + '%</span></div>' +
      '<div class="meter-row"><span>利用率</span><div class="meter-track"><div class="meter-fill util" style="width:' + pct(utilization) + '"></div></div><span class="meter-number">' + utilization.toFixed(1) + '%</span></div></div>' +
      '<div class="gpu-foot"><span class="gpu-status ' + (idle ? 'idle' : 'busy') + '"><i></i>' + (idle ? '空闲可调度' : (gpu.reserved_mb ? '队列已占用' : (gpu.process_count || 0) + ' 个进程')) + '</span><span><strong>' + esc(speed) + '</strong></span><button class="gpu-test-mini" data-benchmark="' + esc(gpu.index) + '">测试 ϟ</button></div></article>';
  }).join('');
  $$('[data-benchmark]').forEach(function (button) {
    button.addEventListener('click', function () { runBenchmark(Number(button.dataset.benchmark)); });
  });
}

function renderActivity() {
  const items = (UI.state.experiments || []).slice().sort(function (a, b) {
    return Number(b.created_seq || 0) - Number(a.created_seq || 0);
  }).slice(0, 5);
  const root = $('#activity-list');
  if (!items.length) {
    root.innerHTML = '<div class="empty-state compact">还没有实验任务</div>';
    return;
  }
  root.innerHTML = items.map(function (item) {
    const meta = item.assigned_gpu ? ('GPU ' + item.assigned_gpu.index + ' · ' + (item.conda_env || '默认 Python')) : (item.script_name + ' · 优先级 ' + item.priority);
    return '<div class="activity-item"><i class="activity-dot ' + esc(item.status) + '"></i><div><div class="activity-name">' + esc(item.name) + '</div><div class="activity-meta">' + esc(meta) + '</div></div><span class="activity-status">' + esc(statusLabels[item.status] || item.status) + '</span></div>';
  }).join('');
}

function renderQueue() {
  const items = UI.state.experiments || [];
  const counts = items.reduce(function (acc, item) {
    acc[item.status] = (acc[item.status] || 0) + 1;
    return acc;
  }, {});
  $('#queue-count').textContent = items.filter(function (item) {
    return ['queued', 'running', 'waiting_memory'].includes(item.status);
  }).length;
  $('#summary-queued').textContent = (counts.queued || 0) + (counts.waiting_memory || 0);
  $('#summary-running').textContent = counts.running || 0;
  $('#summary-success').textContent = counts.success || 0;
  $('#summary-failed').textContent = (counts.failed || 0) + (counts.canceled || 0);
  const visible = items.filter(function (item) {
    if (UI.queueFilter === 'all') return true;
    if (UI.queueFilter === 'queued') return ['queued', 'waiting_memory'].includes(item.status);
    if (UI.queueFilter === 'failed') return ['failed', 'canceled'].includes(item.status);
    return item.status === UI.queueFilter;
  }).sort(function (a, b) {
    return Number(a.priority || 50) - Number(b.priority || 50) || Number(a.created_seq || 0) - Number(b.created_seq || 0);
  });
  const root = $('#queue-table');
  if (!visible.length) {
    root.innerHTML = '<tr><td colspan="7"><div class="empty-state">这个筛选下还没有实验</div></td></tr>';
    return;
  }
  root.innerHTML = visible.map(function (item) {
    const action = item.status === 'running'
      ? '<button class="table-action danger" data-cancel="' + esc(item.id) + '">停止</button>'
      : ['failed', 'canceled', 'waiting_memory'].includes(item.status)
        ? '<button class="table-action" data-retry="' + esc(item.id) + '">重试</button>' : '';
    const gpu = item.assigned_gpu ? ('GPU ' + item.assigned_gpu.index + ' · ' + esc(item.assigned_gpu.name || '')) : '待分配';
    const memory = item.peak_memory_mb ? Number(item.peak_memory_mb).toLocaleString() + ' MB' : '自动';
    return '<tr><td><div class="experiment-cell"><div class="experiment-avatar">' + esc((item.name || 'E').slice(0, 1).toUpperCase()) + '</div><div class="experiment-title"><strong title="' + esc(item.name) + '">' + esc(item.name) + '</strong><small>' + esc(item.script_name) + ' · ' + esc(fmtTime(item.created_at)) + '</small></div></div></td>' +
      '<td><span class="status-pill ' + esc(item.status) + '">' + esc(statusLabels[item.status] || item.status) + '</span>' + (item.failure_reason ? '<div class="failure-hint" title="' + esc(item.failure_reason) + '">' + esc(item.failure_reason) + '</div>' : '') + '</td>' +
      '<td><span class="priority">P' + esc(item.priority) + '</span></td><td><span class="strategy">' + (item.execution_level === 'emergency' ? '紧急 · 有显存即跑' : '默认 · GPU 空闲') + '</span></td><td><span class="gpu-target">' + gpu + '</span></td><td><span class="gpu-target">' + memory + '</span></td><td><div class="action-group"><button class="table-action" data-log="' + esc(item.id) + '">日志</button>' + action + '</div></td></tr>';
  }).join('');
  $$('[data-log]').forEach(function (button) {
    button.addEventListener('click', function () {
      UI.selectedLog = button.dataset.log; setView('logs'); renderLogs(); loadLog();
    });
  });
  $$('[data-retry]').forEach(function (button) { button.addEventListener('click', function () { updateExperiment(button.dataset.retry, 'retry'); }); });
  $$('[data-cancel]').forEach(function (button) { button.addEventListener('click', function () { updateExperiment(button.dataset.cancel, 'cancel'); }); });
}

function renderBenchmarks() {
  const gpus = (UI.state.snapshot && UI.state.snapshot.gpus) || [];
  const root = $('#benchmark-grid');
  if (!gpus.length) {
    root.innerHTML = '<div class="empty-state">连接服务器后可选择显卡开始测试。</div>';
    return;
  }
  const envs = (UI.state.conda && UI.state.conda.envs) || [];
  const defaultEnv = (UI.state.preferences && UI.state.preferences.conda_env) || '';
  root.innerHTML = gpus.map(function (gpu) {
    const benchmark = gpu.benchmark || {};
    const options = envs.map(function (env) {
      return '<option value="' + esc(env) + '"' + (env === defaultEnv ? ' selected' : '') + '>' + esc(env) + '</option>';
    }).join('');
    return '<article class="benchmark-card"><div class="gpu-card-top"><div><div class="gpu-name">GPU ' + esc(gpu.index) + ' · ' + esc(gpu.name) + '</div><div class="gpu-id">' + fmtMb(gpu.memory_total_mb) + ' 显存 · ' + esc(gpu.uuid || '') + '</div></div><div class="gpu-temp">' + esc(gpu.temperature_c == null ? '—' : gpu.temperature_c) + '°C</div></div>' +
      '<div class="benchmark-score"><div class="score-box"><span>Tensor FP16</span><strong>' + (benchmark.tensor_tflops ? Number(benchmark.tensor_tflops).toFixed(2) : '—') + '<small> TFLOPS</small></strong></div><div class="score-box"><span>FP32</span><strong>' + (benchmark.fp32_tflops ? Number(benchmark.fp32_tflops).toFixed(2) : '—') + '<small> TFLOPS</small></strong></div></div>' +
      '<div class="benchmark-actions"><select class="benchmark-env" data-bench-env="' + esc(gpu.index) + '"><option value="">默认 Python</option>' + options + '</select><button class="test-button" data-benchmark="' + esc(gpu.index) + '">运行测试 ϟ</button></div></article>';
  }).join('');
  $$('[data-benchmark]').forEach(function (button) {
    button.addEventListener('click', function () { runBenchmark(Number(button.dataset.benchmark)); });
  });
}

function renderHistory() {
  const history = (UI.state.benchmark_history || []).slice().reverse();
  const root = $('#benchmark-history');
  if (!history.length) {
    root.innerHTML = '<tr><td colspan="6"><div class="empty-state">暂无测试记录</div></td></tr>';
    return;
  }
  root.innerHTML = history.slice(0, 12).map(function (item) {
    const status = item.ok ? '成功' : (item.error || '失败');
    return '<tr><td>' + esc(fmtTime(item.created_at)) + '</td><td>GPU ' + esc(item.gpu_index) + ' · ' + esc(item.gpu_name || '—') + '</td><td><span class="priority">' + (item.tensor_tflops ? Number(item.tensor_tflops).toFixed(2) : '—') + ' TFLOPS</span></td><td>' + (item.fp32_tflops ? Number(item.fp32_tflops).toFixed(2) + ' TFLOPS' : '—') + '</td><td>' + esc(item.env || '默认 Python') + '</td><td><span class="status-pill ' + (item.ok ? 'success' : 'failed') + '">' + esc(status) + '</span></td></tr>';
  }).join('');
}

function renderLogs() {
  const items = (UI.state.experiments || []).slice().sort(function (a, b) {
    return Number(b.created_seq || 0) - Number(a.created_seq || 0);
  });
  const root = $('#log-list');
  if (!items.length) {
    root.innerHTML = '<div class="empty-state compact">暂无实验</div>';
    return;
  }
  if (!UI.selectedLog || !items.some(function (item) { return item.id === UI.selectedLog; })) UI.selectedLog = items[0].id;
  root.innerHTML = items.map(function (item) {
    return '<button class="log-item ' + (item.id === UI.selectedLog ? 'active' : '') + '" data-log-select="' + esc(item.id) + '"><strong>' + esc(item.name) + '</strong><small>' + esc(item.script_name) + ' · ' + esc(fmtTime(item.created_at)) + '</small><span class="status-pill ' + esc(item.status) + '">' + esc(statusLabels[item.status] || item.status) + '</span></button>';
  }).join('');
  $$('[data-log-select]').forEach(function (button) {
    button.addEventListener('click', function () { UI.selectedLog = button.dataset.logSelect; renderLogs(); loadLog(); });
  });
  loadLog();
}

async function loadLog() {
  if (!UI.selectedLog) return;
  const item = (UI.state.experiments || []).find(function (exp) { return exp.id === UI.selectedLog; });
  if (!item) return;
  $('#log-title').textContent = item.name;
  try {
    const data = await api('/api/experiments/' + encodeURIComponent(UI.selectedLog) + '/log');
    $('#log-viewer').textContent = data.log || '暂无输出';
    $('#log-viewer').scrollTop = $('#log-viewer').scrollHeight;
  } catch (error) {
    $('#log-viewer').textContent = error.message;
  }
}

function populateConnection() {
  const profile = UI.state.profile || {};
  $('#connect-host').value = profile.host || '';
  $('#connect-port').value = profile.port || 22;
  $('#connect-username').value = profile.username || '';
  $('#connect-interval').value = profile.poll_interval || 5;
}

function populateExperiment() {
  const pref = UI.state.preferences || {};
  $('#experiment-workdir').value = pref.workdir || (UI.state.profile && UI.state.profile.home) || '';
  $('#experiment-memory').value = pref.peak_memory_mb || '';
  $('#experiment-conda').innerHTML = '<option value="">使用远程默认 Python</option>' +
    ((UI.state.conda && UI.state.conda.envs) || []).map(function (env) {
      return '<option value="' + esc(env) + '"' + (env === pref.conda_env ? ' selected' : '') + '>' + esc(env) + '</option>';
    }).join('');
  const level = $('[name="execution_level"]', $('#experiment-form'));
  if (level) level.value = pref.execution_level || 'idle_only';
}

async function refresh() {
  try {
    UI.state = await api('/api/state');
    renderAll();
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderAll() {
  renderConnection();
  renderMetrics();
  renderGpuFleet();
  renderActivity();
  renderQueue();
  renderBenchmarks();
  renderHistory();
  if (UI.view === 'logs') renderLogs();
}

async function updateExperiment(id, action) {
  try {
    UI.state = await api('/api/experiments/' + encodeURIComponent(id) + '/' + action, { method: 'POST' });
    renderAll();
    showToast(action === 'retry' ? '实验已重新加入队列' : '已发送停止请求');
  } catch (error) {
    showToast(error.message, true);
  }
}

async function runBenchmark(index) {
  const buttons = $$('[data-benchmark="' + index + '"]');
  const button = buttons.find(function (item) { return item.tagName === 'BUTTON'; });
  if (button) { button.disabled = true; button.textContent = '测试中…'; }
  const envSelect = $('[data-bench-env="' + index + '"]');
  const env = envSelect ? envSelect.value : ((UI.state.preferences && UI.state.preferences.conda_env) || '');
  try {
    const data = await api('/api/benchmark', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gpu_index: index, seconds: 5, conda_env: env })
    });
    UI.state = data.state;
    renderAll();
    showToast(data.result.ok ? ('GPU ' + index + ' 测试完成：' + Number(data.result.tensor_tflops || 0).toFixed(2) + ' TFLOPS') : data.result.error, !data.result.ok);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    if (button) { button.disabled = false; button.textContent = '运行测试 ϟ'; }
  }
}

document.addEventListener('click', function (event) {
  const target = event.target.closest('[data-view-target]');
  if (target) setView(target.dataset.viewTarget);
  const closer = event.target.closest('[data-close-modal]');
  if (closer) setModal(closer.dataset.closeModal, false);
});

$('#open-connection').addEventListener('click', function () { populateConnection(); setModal('connection-modal', true); });
$('#refresh-button').addEventListener('click', async function () {
  try { UI.state = await api('/api/poll', { method: 'POST' }); renderAll(); showToast('状态已刷新'); }
  catch (error) { showToast(error.message, true); }
});
$('#open-experiment').addEventListener('click', function () { populateExperiment(); setModal('experiment-modal', true); });
$$('.filter-tab').forEach(function (button) {
  button.addEventListener('click', function () {
    UI.queueFilter = button.dataset.filter;
    $$('.filter-tab').forEach(function (item) { item.classList.toggle('active', item === button); });
    renderQueue();
  });
});
$('#reload-log').addEventListener('click', loadLog);

$('#connection-form').addEventListener('submit', async function (event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const submit = $('#connect-submit');
  submit.disabled = true;
  submit.textContent = '连接中…';
  try {
    UI.state = await api('/api/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        host: form.get('host'),
        port: form.get('port'),
        username: form.get('username'),
        password: form.get('password'),
        poll_interval: form.get('poll_interval'),
        save_password: $('#save-password').checked
      })
    });
    setModal('connection-modal', false);
    renderAll();
    showToast('服务器连接成功');
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
    submit.innerHTML = '开始连接 <span>→</span>';
  }
});

$('#experiment-form').addEventListener('submit', async function (event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const submit = $('#experiment-submit');
  submit.disabled = true;
  submit.textContent = '提交中…';
  try {
    UI.state = await api('/api/experiments', { method: 'POST', body: form });
    setModal('experiment-modal', false);
    event.currentTarget.reset();
    renderAll();
    showToast('实验已加入队列');
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
    submit.innerHTML = '加入队列 <span>→</span>';
  }
});

refresh();
setInterval(refresh, 5000);
setInterval(function () { if (UI.view === 'logs' && UI.selectedLog) loadLog(); }, 5000);


