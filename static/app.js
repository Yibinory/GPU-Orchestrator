const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const UI = {
  data: null,
  view: 'dashboard',
  queueFilter: 'all',
  selectedLog: null,
  editingExperimentId: null,
  editingServerId: null,
  sshHosts: []
};

const statusLabels = {
  queued: '等待调度',
  running: '运行中',
  paused: '暂停中',
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

function taskNo(item) {
  const number = Number((item || {}).task_no || 0);
  return number > 0 ? '#' + String(number).padStart(4, '0') : '#' + String((item || {}).id || '').slice(0, 8);
}

function serverName(serverId) {
  const record = (UI.data.servers || []).find(function (item) { return item.id === serverId; });
  return record ? (record.name || serverId) : (serverId || '默认服务器');
}

function showToast(message, error) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.toggle('error', Boolean(error));
  toast.classList.remove('hidden');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(function () { toast.classList.add('hidden'); }, 3800);
}

async function api(url, options) {
  const response = await fetch(url, options || {});
  const data = await response.json().catch(function () { return {}; });
  if (!response.ok || data.error) throw new Error(data.error || ('请求失败 (' + response.status + ')'));
  if (data.warning) showToast(data.warning, true);
  return data;
}

async function postJson(url, payload) {
  return api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}) });
}

function setView(view) {
  UI.view = view;
  $$('.nav-item').forEach(function (item) { item.classList.toggle('active', item.dataset.viewTarget === view); });
  $$('.view').forEach(function (item) { item.classList.toggle('active-view', item.id === 'view-' + view); });
  const meta = {
    dashboard: ['LIVE RESOURCE MAP', '资源总览'],
    queue: ['EXPERIMENT QUEUE', '实验队列'],
    benchmarks: ['TENSOR BENCHMARK', 'GPU 测试'],
    logs: ['RUN LOGS', '运行日志'],
    servers: ['SERVERS', '服务器管理']
  }[view];
  $('#view-kicker').textContent = meta[0];
  $('#view-title').textContent = meta[1];
  if (view === 'logs') renderLogs();
  if (view === 'benchmarks') renderBenchmarks();
}

function setModal(id, open) {
  $('#' + id).classList.toggle('hidden', !open);
}

function activeState() {
  if (!UI.data) return null;
  return (UI.data.server_states || {})[UI.data.active_server_id] || UI.data;
}

/* ---------- rendering ---------- */

function renderConnection() {
  const data = UI.data;
  const record = (data.servers || []).find(function (item) { return item.id === data.active_server_id; }) || {};
  const connected = data.connected;
  $('#server-name').textContent = record.name || record.host || '未连接';
  $('#server-meta').textContent = record.host
    ? (record.username || 'user') + ' · ' + record.host + ':' + (record.port || 22)
    : '配置 SSH 连接后开始监控';
  $('#connection-label').textContent = !record.enabled ? '监控已停用' : (connected ? '已连接 · 自动轮询中' : '离线');
  $('#connection-state').classList.toggle('online', Boolean(connected) && record.enabled !== false);
  $('#open-connection').innerHTML = connected ? '连接设置 <span>→</span>' : '连接服务器 <span>→</span>';
  $('#last-sync').textContent = data.last_poll_at ? '同步于 ' + fmtTime(data.last_poll_at) : '尚未同步';

  const notices = [];
  if (data.last_error) notices.push(data.last_error);
  const guard = data.disk_guard || {};
  if (guard.blocked && guard.message) notices.push(guard.message);
  if (data.scheduler_paused && data.scheduler_pause_reason) notices.push(data.scheduler_pause_reason);
  const alert = $('#global-alert');
  if (notices.length) {
    alert.textContent = notices.join(' · ');
    alert.classList.remove('hidden');
  } else {
    alert.classList.add('hidden');
  }
}

function renderServerList() {
  const root = $('#server-list');
  const servers = UI.data.servers || [];
  root.innerHTML = servers.map(function (record) {
    const state = (UI.data.server_states || {})[record.id] || {};
    const dotClass = !record.enabled ? 'disabled' : (record.connected ? 'online' : 'offline');
    return '<button class="server-item ' + (record.id === UI.data.active_server_id ? 'active' : '') + '" data-switch-server="' + esc(record.id) + '" title="切换到该服务器">' +
      '<i class="server-dot ' + dotClass + '"></i>' +
      '<span class="server-item-body"><strong>' + esc(record.name || record.host || record.id) + '</strong>' +
      '<small>' + esc(record.host ? record.host + ':' + record.port : '未配置') + '</small></span>' +
      '<em>' + (record.id === UI.data.active_server_id ? '当前' : '切换') + '</em></button>';
  }).join('');
  $$('[data-switch-server]').forEach(function (button) {
    button.addEventListener('click', async function () {
      if (button.dataset.switchServer === UI.data.active_server_id) return;
      try {
        UI.data = await postJson('/api/servers/' + encodeURIComponent(button.dataset.switchServer) + '/active');
        renderAll();
      } catch (error) { showToast(error.message, true); }
    });
  });
}

function renderFleet() {
  const root = $('#fleet-grid');
  const servers = UI.data.servers || [];
  if (!servers.length) {
    root.innerHTML = '<div class="empty-state">还没有服务器，到「服务器管理」添加。</div>';
    return;
  }
  root.innerHTML = servers.map(function (record) {
    const state = (UI.data.server_states || {})[record.id] || {};
    const snapshot = state.snapshot || {};
    const gpus = snapshot.gpus || [];
    const totalGpu = gpus.reduce(function (sum, gpu) { return sum + Number(gpu.memory_total_mb || 0); }, 0);
    const usedGpu = gpus.reduce(function (sum, gpu) { return sum + Number(gpu.memory_used_mb || 0); }, 0);
    const gpuPercent = totalGpu ? (usedGpu / totalGpu * 100).toFixed(0) : '—';
    const status = !record.enabled ? '已停用' : (record.connected ? '已连接' : '离线');
    const badges = [];
    if (record.scheduler_paused) badges.push('<span class="badge warn">调度已暂停</span>');
    if (record.disk_blocked) badges.push('<span class="badge danger">磁盘保护</span>');
    if (record.auto_connect) badges.push('<span class="badge">自动连接</span>');
    return '<article class="fleet-card ' + (record.id === UI.data.active_server_id ? 'active' : '') + '">' +
      '<div class="fleet-head"><div><div class="fleet-name">' + esc(record.name || record.host || record.id) + '</div>' +
      '<div class="fleet-host">' + esc(record.host ? record.username + '@' + record.host + ':' + record.port : '未配置') + '</div></div>' +
      '<span class="status-pill ' + (record.connected ? 'running' : (!record.enabled ? 'canceled' : 'failed')) + '">' + status + '</span></div>' +
      '<div class="fleet-stats">' +
      '<div><span>CPU</span><strong>' + (record.connected ? Number(snapshot.cpu_percent || 0).toFixed(0) + '%' : '—') + '</strong></div>' +
      '<div><span>内存</span><strong>' + (record.connected ? Number((snapshot.memory || {}).used_percent || 0).toFixed(0) + '%' : '—') + '</strong></div>' +
      '<div><span>显存</span><strong>' + (record.connected ? gpuPercent + '%' : '—') + '</strong></div>' +
      '<div><span>磁盘可用</span><strong>' + (record.connected ? fmtBytes((snapshot.disk || {}).free_bytes) : '—') + '</strong></div></div>' +
      '<div class="fleet-foot">' + badges.join('') + '<span class="fleet-poll">' + esc(fmtTime(state.last_poll_at)) + '</span></div>' +
      (state.last_error ? '<div class="fleet-error" title="' + esc(state.last_error) + '">' + esc(state.last_error) + '</div>' : '') +
      '</article>';
  }).join('');
}

function renderMetrics() {
  const snapshot = activeState() && activeState().snapshot;
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
  const gpus = (activeState() && activeState().snapshot && activeState().snapshot.gpus) || [];
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
  const items = (UI.data.experiments || []).slice().sort(function (a, b) {
    return Number(b.created_seq || 0) - Number(a.created_seq || 0);
  }).slice(0, 5);
  const root = $('#activity-list');
  if (!items.length) {
    root.innerHTML = '<div class="empty-state compact">还没有实验任务</div>';
    return;
  }
  root.innerHTML = items.map(function (item) {
    const meta = item.assigned_gpu
      ? serverName(item.server_id) + ' · GPU ' + item.assigned_gpu.index
      : serverName(item.server_id) + ' · ' + (item.script_name || '');
    return '<div class="activity-item"><i class="activity-dot ' + esc(item.status) + '"></i><div><div class="activity-name">' + esc(taskNo(item)) + ' ' + esc(item.name) + '</div><div class="activity-meta">' + esc(meta) + '</div></div><span class="activity-status">' + esc(statusLabels[item.status] || item.status) + '</span></div>';
  }).join('');
}

function dependencyLabels(item) {
  const ids = item.depends_on || [];
  if (!ids.length) return '无';
  return ids.map(function (id) {
    const dep = (UI.data.experiments || []).find(function (candidate) { return candidate.id === id; });
    return dep ? taskNo(dep) : '#' + String(id).slice(0, 8);
  }).join('、');
}

function queueActions(item) {
  const id = esc(item.id);
  const buttons = ['<button class="table-action" data-log="' + id + '">日志</button>'];
  const status = item.status;
  const busyProcess = status === 'running' || item.paused_process;
  if (status === 'paused') {
    buttons.push('<button class="table-action" data-action="resume" data-id="' + id + '">恢复</button>');
    buttons.push('<button class="table-action" data-action="requeue" data-id="' + id + '">重新等待</button>');
  } else if (status === 'queued' || status === 'waiting_memory') {
    buttons.push('<button class="table-action" data-action="pause" data-id="' + id + '">暂停</button>');
  }
  if (status === 'running' || status === 'paused' || status === 'queued' || status === 'waiting_memory') {
    buttons.push('<button class="table-action danger" data-action="cancel" data-id="' + id + '">中断</button>');
  } else {
    buttons.push('<button class="table-action" data-action="retry" data-id="' + id + '">重试</button>');
  }
  if (!busyProcess) {
    buttons.push('<button class="table-action" data-action="edit" data-id="' + id + '">编辑</button>');
    buttons.push('<button class="table-action danger" data-action="delete" data-id="' + id + '">删除</button>');
  }
  return buttons.join('');
}

function renderQueue() {
  const items = UI.data.experiments || [];
  const counts = items.reduce(function (acc, item) {
    acc[item.status] = (acc[item.status] || 0) + 1;
    return acc;
  }, {});
  $('#queue-count').textContent = items.filter(function (item) {
    return ['queued', 'running', 'waiting_memory', 'paused'].includes(item.status);
  }).length;
  $('#summary-queued').textContent = (counts.queued || 0) + (counts.waiting_memory || 0);
  $('#summary-running').textContent = counts.running || 0;
  $('#summary-paused').textContent = counts.paused || 0;
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
    root.innerHTML = '<tr><td colspan="8"><div class="empty-state">这个筛选下还没有实验</div></td></tr>';
    return;
  }
  root.innerHTML = visible.map(function (item) {
    const gpu = item.assigned_gpu ? ('GPU ' + item.assigned_gpu.index + ' · ' + esc(item.assigned_gpu.name || '')) : '待分配';
    const memory = item.peak_memory_mb ? Number(item.peak_memory_mb).toLocaleString() + ' MB' : '自动';
    const hint = item.failure_reason || item.validation_error || item.dependency_reason || item.pause_reason || '';
    return '<tr><td><div class="experiment-cell"><div class="experiment-avatar">' + esc(taskNo(item)) + '</div><div class="experiment-title"><strong title="' + esc(item.name) + '">' + esc(item.name) + '</strong><small>' + esc(item.script_name) + ' · ' + esc(serverName(item.server_id)) + ' · ' + esc(fmtTime(item.created_at)) + '</small></div></div></td>' +
      '<td><span class="status-pill ' + esc(item.status) + '">' + esc(statusLabels[item.status] || item.status) + '</span>' + (hint ? '<div class="failure-hint" title="' + esc(hint) + '">' + esc(hint) + '</div>' : '') + '</td>' +
      '<td><span class="priority">P' + esc(item.priority) + '</span></td>' +
      '<td><span class="strategy">' + (item.execution_level === 'emergency' ? '紧急 · 有显存即跑' : item.execution_level === 'low_interference' ? '低干扰 · 低峰忙卡' : '默认 · GPU 空闲') + '</span></td>' +
      '<td><span class="gpu-target">' + gpu + '</span></td>' +
      '<td><span class="gpu-target">' + memory + '</span></td>' +
      '<td><span class="gpu-target">' + esc(dependencyLabels(item)) + '</span></td>' +
      '<td><div class="action-group">' + queueActions(item) + '</div></td></tr>';
  }).join('');
  $$('[data-log]').forEach(function (button) {
    button.addEventListener('click', function () {
      UI.selectedLog = button.dataset.log; setView('logs'); renderLogs(); loadLog();
    });
  });
  $$('[data-action]').forEach(function (button) {
    button.addEventListener('click', function () { runQueueAction(button.dataset.action, button.dataset.id); });
  });
}

async function runQueueAction(action, expId) {
  const item = (UI.data.experiments || []).find(function (candidate) { return candidate.id === expId; }) || {};
  if (action === 'edit') { openExperimentModal(item); return; }
  if (action === 'delete') {
    const confirmed = window.confirm('确定删除 ' + taskNo(item) + ' ' + (item.name || '') + ' 吗？\n\n本地任务记录和脚本缓存会删除，服务器上的历史日志会保留。');
    if (!confirmed) return;
  }
  const messages = {
    pause: '任务已暂停', resume: '任务已恢复', requeue: '任务已重新进入等待队列',
    retry: '实验已重新加入队列', cancel: '已发送中断请求', delete: '实验任务已删除'
  };
  const paths = {
    pause: 'pause', resume: 'resume', requeue: 'requeue',
    retry: 'retry', cancel: 'cancel', delete: 'delete'
  };
  try {
    UI.data = await postJson('/api/experiments/' + encodeURIComponent(expId) + '/' + paths[action]);
    renderAll();
    showToast(messages[action] || '操作完成');
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderBenchmarks() {
  const state = activeState() || {};
  const gpus = (state.snapshot && state.snapshot.gpus) || [];
  const root = $('#benchmark-grid');
  if (!gpus.length) {
    root.innerHTML = '<div class="empty-state">连接服务器后可选择显卡开始测试。</div>';
    return;
  }
  const envs = (state.conda && state.conda.envs) || [];
  const defaultEnv = (state.preferences && state.preferences.conda_env) || '';
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
  const history = (UI.data.benchmark_history || []).slice().reverse();
  const root = $('#benchmark-history');
  if (!history.length) {
    root.innerHTML = '<tr><td colspan="7"><div class="empty-state">暂无测试记录</div></td></tr>';
    return;
  }
  root.innerHTML = history.slice(0, 12).map(function (item) {
    const status = item.ok ? '成功' : (item.error || '失败');
    return '<tr><td>' + esc(fmtTime(item.created_at)) + '</td><td>' + esc(serverName(item.server_id)) + '</td><td>GPU ' + esc(item.gpu_index) + ' · ' + esc(item.gpu_name || '—') + '</td><td><span class="priority">' + (item.tensor_tflops ? Number(item.tensor_tflops).toFixed(2) : '—') + ' TFLOPS</span></td><td>' + (item.fp32_tflops ? Number(item.fp32_tflops).toFixed(2) + ' TFLOPS' : '—') + '</td><td>' + esc(item.env || '默认 Python') + '</td><td><span class="status-pill ' + (item.ok ? 'success' : 'failed') + '">' + esc(status) + '</span></td></tr>';
  }).join('');
}

function renderLogs() {
  const items = (UI.data.experiments || []).slice().sort(function (a, b) {
    return Number(b.created_seq || 0) - Number(a.created_seq || 0);
  });
  const root = $('#log-list');
  if (!items.length) {
    root.innerHTML = '<div class="empty-state compact">暂无实验</div>';
    return;
  }
  if (!UI.selectedLog || !items.some(function (item) { return item.id === UI.selectedLog; })) UI.selectedLog = items[0].id;
  root.innerHTML = items.map(function (item) {
    return '<button class="log-item ' + (item.id === UI.selectedLog ? 'active' : '') + '" data-log-select="' + esc(item.id) + '"><strong>' + esc(taskNo(item)) + ' ' + esc(item.name) + '</strong><small>' + esc(serverName(item.server_id)) + ' · ' + esc(item.script_name) + '</small><span class="status-pill ' + esc(item.status) + '">' + esc(statusLabels[item.status] || item.status) + '</span></button>';
  }).join('');
  $$('[data-log-select]').forEach(function (button) {
    button.addEventListener('click', function () { UI.selectedLog = button.dataset.logSelect; renderLogs(); loadLog(); });
  });
  loadLog();
}

async function loadLog(full) {
  if (!UI.selectedLog) return;
  const item = (UI.data.experiments || []).find(function (exp) { return exp.id === UI.selectedLog; });
  if (!item) return;
  $('#log-title').textContent = taskNo(item) + ' ' + item.name;
  try {
    const data = await api('/api/experiments/' + encodeURIComponent(UI.selectedLog) + '/log' + (full ? '?full=1' : ''));
    $('#log-viewer').textContent = data.log || '暂无输出';
    $('#log-viewer').scrollTop = $('#log-viewer').scrollHeight;
  } catch (error) {
    $('#log-viewer').textContent = error.message;
  }
}

function renderServers() {
  const root = $('#server-cards');
  $('#gpu-history-samples').value = (UI.data.global_settings || {}).gpu_history_samples || 5;
  const servers = UI.data.servers || [];
  if (!servers.length) {
    root.innerHTML = '<div class="empty-state">还没有服务器记录。</div>';
    return;
  }
  root.innerHTML = servers.map(function (record) {
    const isDefault = record.id === 'default';
    const status = !record.enabled ? '已停用' : (record.connected ? '已连接' : '离线');
    const badges = [];
    if (record.auto_connect) badges.push('<span class="badge">自动连接</span>');
    if (record.scheduler_paused) badges.push('<span class="badge warn">调度已暂停</span>');
    if (record.disk_blocked) badges.push('<span class="badge danger">磁盘保护</span>');
    return '<article class="server-record">' +
      '<div class="fleet-head"><div><div class="fleet-name">' + esc(record.name || record.host || record.id) + '</div>' +
      '<div class="fleet-host">' + esc(record.host ? record.username + '@' + record.host + ':' + record.port : '未配置') + '</div></div>' +
      '<span class="status-pill ' + (record.connected ? 'running' : (!record.enabled ? 'canceled' : 'failed')) + '">' + status + '</span></div>' +
      '<div class="server-record-meta">最近同步 ' + esc(fmtTime(record.last_poll_at)) + '</div>' +
      (record.last_error ? '<div class="fleet-error" title="' + esc(record.last_error) + '">' + esc(record.last_error) + '</div>' : '') +
      '<div class="server-record-actions">' +
      (record.id === UI.data.active_server_id ? '<span class="badge">当前服务器</span>' : '<button class="table-action" data-server-action="active" data-server="' + esc(record.id) + '">设为当前</button>') +
      (record.connected
        ? '<button class="table-action" data-server-action="disconnect" data-server="' + esc(record.id) + '">断开</button>'
        : '<button class="table-action" data-server-action="connect" data-server="' + esc(record.id) + '">连接</button>') +
      '<button class="table-action" data-server-action="' + (record.enabled ? 'disable' : 'enable') + '" data-server="' + esc(record.id) + '">' + (record.enabled ? '停用监控' : '启用监控') + '</button>' +
      '<button class="table-action" data-server-action="scheduler" data-server="' + esc(record.id) + '">' + (record.scheduler_paused ? '恢复调度' : '暂停调度') + '</button>' +
      '<button class="table-action" data-server-action="edit" data-server="' + esc(record.id) + '">编辑</button>' +
      (isDefault ? '' : '<button class="table-action danger" data-server-action="delete" data-server="' + esc(record.id) + '">删除</button>') +
      '</div></article>';
  }).join('');
  $$('[data-server-action]').forEach(function (button) {
    button.addEventListener('click', function () { runServerAction(button.dataset.serverAction, button.dataset.server); });
  });
}

async function runServerAction(action, serverId) {
  const record = (UI.data.servers || []).find(function (item) { return item.id === serverId; }) || {};
  if (action === 'connect') { openConnectionModal(serverId); return; }
  if (action === 'edit') { openServerModal(serverId); return; }
  if (action === 'delete') {
    const confirmed = window.confirm('确定删除服务器「' + (record.name || serverId) + '」吗？\n\n仅删除本机连接记录，服务器上的文件不会受影响。');
    if (!confirmed) return;
  }
  try {
    if (action === 'active') {
      UI.data = await postJson('/api/servers/' + encodeURIComponent(serverId) + '/active');
    } else if (action === 'disconnect') {
      UI.data = await postJson('/api/disconnect', { server_id: serverId });
    } else if (action === 'enable' || action === 'disable') {
      UI.data = await postJson('/api/servers/' + encodeURIComponent(serverId) + '/enabled', { enabled: action === 'enable' });
    } else if (action === 'scheduler') {
      const paused = !record.scheduler_paused;
      UI.data = await postJson('/api/servers/' + encodeURIComponent(serverId) + '/scheduler', { paused: paused });
      showToast(paused ? '已暂停该服务器的任务调度' : '已恢复该服务器的任务调度');
      renderAll();
      return;
    } else if (action === 'delete') {
      UI.data = await postJson('/api/servers/' + encodeURIComponent(serverId) + '/delete');
    }
    renderAll();
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderAll() {
  renderConnection();
  renderServerList();
  renderFleet();
  renderMetrics();
  renderGpuFleet();
  renderActivity();
  renderQueue();
  renderBenchmarks();
  renderHistory();
  renderServers();
  if (UI.view === 'logs') renderLogs();
}

/* ---------- benchmark ---------- */

async function runBenchmark(index) {
  const buttons = $$('[data-benchmark="' + index + '"]');
  const button = buttons.find(function (item) { return item.tagName === 'BUTTON'; });
  if (button) { button.disabled = true; button.textContent = '测试中…'; }
  const envSelect = $('[data-bench-env="' + index + '"]');
  const env = envSelect ? envSelect.value : ((activeState().preferences && activeState().preferences.conda_env) || '');
  try {
    const data = await postJson('/api/benchmark', { server_id: UI.data.active_server_id, gpu_index: index, seconds: 5, conda_env: env });
    UI.data = data.state;
    renderAll();
    showToast(data.result.ok ? ('GPU ' + index + ' 测试完成：' + Number(data.result.tensor_tflops || 0).toFixed(2) + ' TFLOPS') : data.result.error, !data.result.ok);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    if (button) { button.disabled = false; button.textContent = '运行测试 ϟ'; }
  }
}

/* ---------- modals ---------- */

function openConnectionModal(serverId) {
  const target = serverId || UI.data.active_server_id;
  const record = (UI.data.servers || []).find(function (item) { return item.id === target; }) || {};
  const state = (UI.data.server_states || {})[target] || {};
  const profile = state.profile || record || {};
  $('#connection-modal-title').textContent = '连接 ' + (record.name || profile.host || 'Linux 服务器');
  $('#connect-server-id').value = target;
  $('#connect-host').value = profile.host || '';
  $('#connect-port').value = profile.port || 22;
  $('#connect-username').value = profile.username || '';
  $('#connect-interval').value = profile.poll_interval || 5;
  $('#connect-password').value = '';
  $('#connect-identity').value = profile.identity_file || '';
  $('#connect-sshconfig').value = '';
  loadSshConfigOptions();
  setModal('connection-modal', true);
}

function openServerModal(serverId) {
  UI.editingServerId = serverId || null;
  const record = serverId ? (UI.data.servers || []).find(function (item) { return item.id === serverId; }) || {} : {};
  const profile = record.profile || record || {};
  $('#server-modal-kicker').textContent = serverId ? 'EDIT SERVER' : 'NEW SERVER';
  $('#server-modal-title').textContent = serverId ? '编辑服务器' : '新增服务器';
  $('#server-form-id').value = serverId || '';
  $('#server-form-name').value = record.name || '';
  $('#server-form-host').value = profile.host || '';
  $('#server-form-port').value = profile.port || 22;
  $('#server-form-username').value = profile.username || '';
  $('#server-form-interval').value = profile.poll_interval || 5;
  $('#server-form-disk').value = record.disk_alert_gb != null ? record.disk_alert_gb : 5;
  $('#server-form-password').value = '';
  $('#server-form-identity').value = profile.identity_file || '';
  $('#server-form-sshconfig').value = '';
  loadSshConfigOptions();
  $('#server-form-auto').checked = Boolean(record.auto_connect);
  $('#server-form-save').checked = true;
  $('#server-form-connect').checked = !serverId;
  setModal('server-modal', true);
}

function condaEnvsFor(serverId) {
  const state = (UI.data.server_states || {})[serverId];
  return (state && state.conda && state.conda.envs) || [];
}

function preferencesFor(serverId) {
  const state = (UI.data.server_states || {})[serverId];
  return (state && state.preferences) || (UI.data.preferences || {});
}

function renderDependsOptions(selectedServerId, editingId, selectedIds) {
  const root = $('#experiment-depends');
  const candidates = (UI.data.experiments || []).filter(function (item) {
    return item.id !== editingId;
  }).sort(function (a, b) { return Number(b.created_seq || 0) - Number(a.created_seq || 0); });
  if (!candidates.length) {
    root.innerHTML = '<div class="empty-state compact">还没有其他任务可选</div>';
    return;
  }
  root.innerHTML = candidates.map(function (item) {
    const checked = (selectedIds || []).includes(item.id) ? ' checked' : '';
    const sameServer = !selectedServerId || item.server_id === selectedServerId;
    return '<label class="check-list-item"><input type="checkbox" name="depends_on" value="' + esc(item.id) + '"' + checked + ' /> <span><strong>' + esc(taskNo(item)) + ' ' + esc(item.name) + '</strong><small>' + esc(serverName(item.server_id)) + ' · ' + esc(statusLabels[item.status] || item.status) + (sameServer ? '' : ' · 跨服务器依赖') + '</small></span></label>';
  }).join('');
}

function openExperimentModal(item) {
  const editing = Boolean(item && item.id);
  UI.editingExperimentId = editing ? item.id : null;
  $('#experiment-modal-kicker').textContent = editing ? 'EDIT EXPERIMENT' : 'NEW EXPERIMENT';
  $('#experiment-modal-title').textContent = editing ? '编辑实验任务' : '提交一个实验';
  $('#experiment-modal-intro').textContent = editing
    ? '修改配置后重新进入等待队列；运行中的任务需要先暂停或停止。'
    : '只需要选择脚本、工作目录和运行环境；相同配置会自动成为下次默认值。';
  const serverSelect = $('#experiment-server');
  serverSelect.disabled = editing;
  serverSelect.innerHTML = (UI.data.servers || []).map(function (record) {
    return '<option value="' + esc(record.id) + '">' + esc(record.name || record.host || record.id) + '</option>';
  }).join('');
  const targetServer = editing ? (item.server_id || UI.data.active_server_id) : UI.data.active_server_id;
  serverSelect.value = targetServer;
  $('#experiment-name').value = editing ? (item.name || '') : '';
  const scriptInput = $('#experiment-script');
  scriptInput.value = '';
  scriptInput.required = !editing;
  $('#experiment-script-hint').textContent = editing ? '不选择文件则保留原脚本' : '只接受 .sh 文件';
  const prefs = preferencesFor(targetServer);
  $('#experiment-workdir').value = editing ? (item.workdir || '') : (prefs.workdir || '');
  $('#experiment-memory').value = editing ? (item.peak_memory_mb || '') : (prefs.peak_memory_mb || '');
  $('#experiment-max-util').value = editing
    ? (item.max_gpu_utilization != null ? item.max_gpu_utilization : 30)
    : (prefs.max_gpu_utilization != null ? prefs.max_gpu_utilization : 30);
  renderCondaOptions(targetServer, editing ? item.conda_env : prefs.conda_env);
  $('[name="priority"]', $('#experiment-form')).value = editing ? (item.priority || 50) : 50;
  $('[name="execution_level"]', $('#experiment-form')).value = editing ? (item.execution_level || 'idle_only') : (prefs.execution_level || 'idle_only');
  $('[name="auto_retry_oom"]', $('#experiment-form')).checked = editing ? Boolean(item.auto_retry_oom) : true;
  renderDependsOptions(targetServer, editing ? item.id : null, editing ? item.depends_on : []);
  setModal('experiment-modal', true);
}

function renderCondaOptions(serverId, selected) {
  const envs = condaEnvsFor(serverId);
  $('#experiment-conda').innerHTML = '<option value="">使用远程默认 Python</option>' + envs.map(function (env) {
    return '<option value="' + esc(env) + '"' + (env === selected ? ' selected' : '') + '>' + esc(env) + '</option>';
  }).join('');
}

async function loadSshConfigOptions() {
  let data;
  try {
    data = await api('/api/ssh-config');
  } catch (error) {
    data = { hosts: [], message: error.message };
  }
  UI.sshHosts = data.hosts || [];
  ['#connect-sshconfig', '#server-form-sshconfig'].forEach(function (selector) {
    const select = $(selector);
    if (!select) return;
    if (!UI.sshHosts.length) {
      select.innerHTML = '<option value="">' + esc(data.message || '未找到可导入的主机') + '</option>';
      select.disabled = true;
      return;
    }
    select.disabled = false;
    select.innerHTML = '<option value="">— 选择 ~/.ssh/config 中的主机 —</option>' + UI.sshHosts.map(function (host) {
      const target = (host.user ? host.user + '@' : '') + host.hostname + ':' + host.port;
      return '<option value="' + esc(host.alias) + '">' + esc(host.alias) + ' — ' + esc(target) + (host.identity_file ? ' 🔑' : '') + '</option>';
    }).join('');
  });
}

function fillFromSshHost(host, prefix) {
  if (!host) return;
  const set = function (id, value) {
    const el = $('#' + id);
    if (el) el.value = value;
  };
  if (prefix === 'server-form') set('server-form-name', host.alias);
  set(prefix + '-host', host.hostname || host.alias);
  set(prefix + '-port', host.port || 22);
  set(prefix + '-username', host.user || '');
  set(prefix + '-identity', host.identity_file || '');
}

/* ---------- data refresh ---------- */

async function refresh() {
  try {
    UI.data = await api('/api/state');
    renderAll();
  } catch (error) {
    showToast(error.message, true);
  }
}

/* ---------- events ---------- */

document.addEventListener('click', function (event) {
  const target = event.target.closest('[data-view-target]');
  if (target) setView(target.dataset.viewTarget);
  const closer = event.target.closest('[data-close-modal]');
  if (closer) setModal(closer.dataset.closeModal, false);
});

$('#open-connection').addEventListener('click', function () { openConnectionModal(UI.data.active_server_id); });
$('#refresh-button').addEventListener('click', async function () {
  try {
    UI.data = await postJson('/api/poll', { server_id: UI.data.active_server_id });
    renderAll();
    showToast('状态已刷新');
  } catch (error) { showToast(error.message, true); }
});
$('#open-experiment').addEventListener('click', function () { openExperimentModal(null); });
$('#open-server').addEventListener('click', function () { openServerModal(null); });
$$('.filter-tab').forEach(function (button) {
  button.addEventListener('click', function () {
    UI.queueFilter = button.dataset.filter;
    $$('.filter-tab').forEach(function (item) { item.classList.toggle('active', item === button); });
    renderQueue();
  });
});
$('#reload-log').addEventListener('click', function () { loadLog(); });
$('#load-full-log').addEventListener('click', function () { loadLog(true); });
$('#save-global-settings').addEventListener('click', async function () {
  try {
    UI.data = await postJson('/api/preferences', { gpu_history_samples: $('#gpu-history-samples').value });
    renderAll();
    showToast('全局设置已保存');
  } catch (error) { showToast(error.message, true); }
});
$('#pause-all-button').addEventListener('click', async function () {
  if (!window.confirm('确定暂停全部服务器的所有任务吗？运行中的进程会被挂起。')) return;
  try { UI.data = await postJson('/api/experiments/pause_all'); renderAll(); showToast('已暂停全部任务'); }
  catch (error) { showToast(error.message, true); }
});
$('#resume-all-button').addEventListener('click', async function () {
  try { UI.data = await postJson('/api/experiments/resume_all'); renderAll(); showToast('已恢复全部任务并重新调度'); }
  catch (error) { showToast(error.message, true); }
});

$('#experiment-server').addEventListener('change', function () {
  const serverId = $('#experiment-server').value;
  renderCondaOptions(serverId, preferencesFor(serverId).conda_env || '');
  renderDependsOptions(serverId, UI.editingExperimentId, []);
});

$('#connect-sshconfig').addEventListener('change', function () {
  const alias = $('#connect-sshconfig').value;
  fillFromSshHost(UI.sshHosts.find(function (host) { return host.alias === alias; }), 'connect');
});

$('#server-form-sshconfig').addEventListener('change', function () {
  const alias = $('#server-form-sshconfig').value;
  fillFromSshHost(UI.sshHosts.find(function (host) { return host.alias === alias; }), 'server-form');
});

$('#connection-form').addEventListener('submit', async function (event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const submit = $('#connect-submit');
  submit.disabled = true;
  submit.textContent = '连接中…';
  try {
    UI.data = await postJson('/api/connect', {
      server_id: form.get('server_id'),
      host: form.get('host'),
      port: form.get('port'),
      username: form.get('username'),
      password: form.get('password'),
      identity_file: form.get('identity_file'),
      poll_interval: form.get('poll_interval'),
      save_password: $('#save-password').checked
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

$('#server-form').addEventListener('submit', async function (event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const submit = $('#server-submit');
  submit.disabled = true;
  submit.textContent = '保存中…';
  try {
    UI.data = await postJson('/api/servers', {
      id: form.get('id') || null,
      name: form.get('name'),
      host: form.get('host'),
      port: form.get('port'),
      username: form.get('username'),
      password: form.get('password'),
      identity_file: form.get('identity_file'),
      disk_alert_gb: form.get('disk_alert_gb'),
      poll_interval: form.get('poll_interval'),
      auto_connect: $('#server-form-auto').checked,
      save_password: $('#server-form-save').checked,
      connect_now: $('#server-form-connect').checked
    });
    setModal('server-modal', false);
    renderAll();
    showToast('服务器已保存');
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
    submit.innerHTML = '保存服务器 <span>→</span>';
  }
});

$('#experiment-form').addEventListener('submit', async function (event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const submit = $('#experiment-submit');
  submit.disabled = true;
  submit.textContent = '提交中…';
  const editing = Boolean(UI.editingExperimentId);
  try {
    UI.data = await api(editing
      ? '/api/experiments/' + encodeURIComponent(UI.editingExperimentId) + '/update'
      : '/api/experiments', { method: 'POST', body: form });
    setModal('experiment-modal', false);
    event.currentTarget.reset();
    renderAll();
    showToast(editing ? '实验配置已更新并重新进入等待队列' : '实验已加入队列');
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
    submit.innerHTML = (editing ? '保存并重新等待 <span>→</span>' : '加入队列 <span>→</span>');
  }
});

refresh();
setInterval(refresh, 4000);
setInterval(function () { if (UI.view === 'logs' && UI.selectedLog) loadLog(); }, 4000);
