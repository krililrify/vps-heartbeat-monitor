<script setup>
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
import { api } from './api.js'

const ST = { pending: '等待首次心跳', installing: '安装中', online: '在线', offline: '离线', install_failed: '安装失败' }
const STEP = { waiting: '等待连接', ssh: '正在验证 SSH', installing: '正在安装探针', starting: '正在启动服务', heartbeat: '正在等待首次心跳', done: '安装成功' }
const me = ref(false), tab = ref('nodes'), err = ref('')
const lf = reactive({ username: '', password: '' })
const nodes = ref([]), total = ref(0), counts = reactive({ total: 0, online: 0, offline: 0, installing: 0, install_failed: 0, pending: 0 })
const q = ref(''), statusF = ref(''), page = ref(1), SIZE = 20

const fmt = t => t ? new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }) : '-'
const dur = s => { if (s == null) return '-'; s = Math.round(s); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + '天' : '') + (h ? h + '时' : '') + (m ? m + '分' : '') + (s % 60) + '秒' }

// ---- 轮询：串行（上一个请求结束后才排下一个），登出/卸载时停止 ----
let timer = null, ctrl = null, running = false, stopped = true
async function poll() {
  if (stopped || running) return
  running = true; ctrl = new AbortController()
  try {
    const d = await api(`/nodes?q=${encodeURIComponent(q.value)}&status=${statusF.value}&page=${page.value}&size=${SIZE}`, { signal: ctrl.signal })
    nodes.value = d.items; total.value = d.total; Object.assign(counts, d.counts); err.value = ''
  } catch (e) { if (e.name !== 'AbortError') err.value = e.message }
  finally { running = false; if (!stopped) timer = setTimeout(poll, 5000) }
}
function start() { stopped = false; clearTimeout(timer); poll() }
function stop() { stopped = true; clearTimeout(timer); ctrl && ctrl.abort() }
function refresh() { clearTimeout(timer); if (!running) poll(); else timer = setTimeout(poll, 300) }
function onUnauth() { stop(); me.value = false }

async function login() {
  try { await api('/auth/login', { method: 'POST', body: { ...lf } }); lf.password = ''; me.value = true; loginErr.value = ''; start() }
  catch (e) { loginErr.value = e.message }
}
const loginErr = ref('')
async function logout() { stop(); try { await api('/auth/logout', { method: 'POST' }) } catch {} me.value = false }

// ---- 添加 VPS ----
const form = reactive({ name: '', ip: '', ssh_port: 22, password: '', note: '' })
const job = ref(null), addErr = ref(''), jobTimer = { t: null }
async function submitAdd() {
  addErr.value = ''; job.value = null
  try {
    const r = await api('/nodes', { method: 'POST', body: { ...form, ssh_port: Number(form.ssh_port) } })
    form.password = ''                                  // 密码不在前端保留
    trackJob(r.job_id)
  } catch (e) { addErr.value = e.message }
}
async function trackJob(id) {
  clearTimeout(jobTimer.t)
  try {
    job.value = await api('/jobs/' + id)
    if (job.value.status === 'running') jobTimer.t = setTimeout(() => trackJob(id), 2000)
    else { refresh(); if (job.value.status === 'done') { form.name = form.ip = form.note = '' } }
  } catch (e) { addErr.value = e.message }
}

// ---- 节点详情 ----
const sel = ref(null), edit = reactive({ name: '', note: '' }), actPw = ref(''), selMsg = ref('')
async function openNode(n) {
  sel.value = await api('/nodes/' + n.id); edit.name = sel.value.name; edit.note = sel.value.note; actPw.value = ''; selMsg.value = ''
}
async function saveNode() { await api('/nodes/' + sel.value.id, { method: 'PATCH', body: { ...edit } }); selMsg.value = '已保存'; refresh() }
async function act(kind) {
  if (!actPw.value) { selMsg.value = '请输入 SSH root 密码（仅用于本次操作，不会保存）'; return }
  if (kind === 'uninstall' && !confirm('确认卸载该节点上的探针？卸载后节点将停止监控。')) return
  try { const r = await api(`/nodes/${sel.value.id}/${kind}`, { method: 'POST', body: { password: actPw.value } }); actPw.value = ''; sel.value = null; tab.value = 'add'; trackJob(r.job_id) }
  catch (e) { selMsg.value = e.message }
}
async function delNode() {
  if (!confirm(`确认删除节点「${sel.value.name}」？\n（故障历史不会被删除）`)) return
  await api(`/nodes/${sel.value.id}?confirm=true`, { method: 'DELETE' }); sel.value = null; refresh()
}

// ---- 故障历史 ----
const evs = ref([]), evTotal = ref(0), evPage = ref(1), ev = reactive({ node: '', start: '', end: '', clearBefore: '' }), picked = ref([])
const ts = v => v ? new Date(v).getTime() / 1000 : ''
async function loadEvents() {
  const p = new URLSearchParams({ page: evPage.value, size: SIZE })
  if (ev.node) p.set('node_id', ev.node); if (ev.start) p.set('start', ts(ev.start)); if (ev.end) p.set('end', ts(ev.end))
  const d = await api('/events?' + p); evs.value = d.items; evTotal.value = d.total; picked.value = []
}
async function delPicked() {
  if (!picked.value.length || !confirm(`确认删除选中的 ${picked.value.length} 条故障历史？此操作不可恢复。`)) return
  await api('/events/delete', { method: 'POST', body: { ids: picked.value, confirm: true } }); loadEvents()
}
async function clearOld() {
  if (!ev.clearBefore) return alert('请先选择清理截止时间')
  if (!confirm(`将清理 ${ev.clearBefore} 之前的所有已结束故障历史，不可恢复。继续？`)) return
  if (prompt('请输入「清理历史」以确认') !== '清理历史') return
  const r = await api('/events/clear', { method: 'POST', body: { before: ts(ev.clearBefore), confirm_text: '清理历史' } }); alert(`已清理 ${r.deleted} 条`); loadEvents()
}
const evState = { open: '故障中', recovered: '已恢复', node_deleted: '节点已删除' }

onMounted(async () => {
  window.addEventListener('hbm-unauth', onUnauth)
  try { await api('/auth/me'); me.value = true; start() } catch {}
})
onBeforeUnmount(() => { stop(); clearTimeout(jobTimer.t); window.removeEventListener('hbm-unauth', onUnauth) })
</script>

<template>
  <div v-if="!me" class="login">
    <form @submit.prevent="login" class="card">
      <h1>VPS 心跳监控</h1>
      <input v-model="lf.username" placeholder="用户名" autocomplete="username" required />
      <input v-model="lf.password" type="password" placeholder="密码" autocomplete="current-password" required />
      <button class="primary">登录</button>
      <p class="bad">{{ loginErr }}</p>
    </form>
  </div>

  <div v-else>
    <header>
      <b>VPS 心跳监控</b>
      <nav>
        <button :class="{ on: tab === 'nodes' }" @click="tab = 'nodes'">节点</button>
        <button :class="{ on: tab === 'add' }" @click="tab = 'add'">添加 VPS</button>
        <button :class="{ on: tab === 'history' }" @click="tab = 'history'; loadEvents()">故障历史</button>
      </nav>
      <button @click="logout">退出登录</button>
    </header>
    <main>
      <p v-if="err" class="bad">状态刷新失败：{{ err }}</p>

      <section v-show="tab === 'nodes'">
        <div class="cards">
          <div class="stat">VPS 总数<b>{{ counts.total }}</b></div>
          <div class="stat ok">在线<b>{{ counts.online }}</b></div>
          <div class="stat off">离线<b>{{ counts.offline }}</b></div>
          <div class="stat">安装中<b>{{ counts.installing }}</b></div>
          <div class="stat warn">安装失败<b>{{ counts.install_failed }}</b></div>
        </div>
        <div class="bar">
          <input v-model="q" placeholder="搜索名称或 IP" @input="page = 1; refresh()" />
          <select v-model="statusF" @change="page = 1; refresh()">
            <option value="">全部状态</option><option v-for="(l, k) in ST" :key="k" :value="k">{{ l }}</option>
          </select>
          <button @click="refresh">手动刷新</button><span class="muted">每 5 秒自动刷新</span>
        </div>
        <table>
          <thead><tr><th>名称</th><th>IP</th><th>状态</th><th>最近心跳</th><th>状态变化</th><th>离线时长</th><th>备注</th><th>添加时间</th></tr></thead>
          <tbody>
            <tr v-for="n in nodes" :key="n.id" :class="n.status" @click="openNode(n)">
              <td>{{ n.name }}</td><td>{{ n.ip }}</td><td><span class="tag" :class="n.status">{{ ST[n.status] }}</span></td>
              <td>{{ fmt(n.last_heartbeat) }}</td><td>{{ fmt(n.status_changed) }}</td><td>{{ dur(n.offline_seconds) }}</td><td>{{ n.note }}</td><td>{{ fmt(n.created_at) }}</td>
            </tr>
            <tr v-if="!nodes.length"><td colspan="8" class="muted">暂无节点</td></tr>
          </tbody>
        </table>
        <div class="bar"><button :disabled="page <= 1" @click="page--; refresh()">上一页</button> 第 {{ page }} 页 / 共 {{ Math.max(1, Math.ceil(total / SIZE)) }} 页
          <button :disabled="page * SIZE >= total" @click="page++; refresh()">下一页</button></div>
      </section>

      <section v-show="tab === 'add'">
        <form class="card" @submit.prevent="submitAdd">
          <h2>添加 VPS（SSH 用户固定为 root）</h2>
          <label>节点名称 *<input v-model="form.name" maxlength="64" required /></label>
          <label>VPS IP 地址 *<input v-model="form.ip" required placeholder="仅支持公网 IP" /></label>
          <label>SSH 端口<input v-model="form.ssh_port" type="number" min="1" max="65535" /></label>
          <label>SSH root 密码 *（仅用于本次安装，不会保存）<input v-model="form.password" type="password" autocomplete="new-password" required /></label>
          <label>备注<input v-model="form.note" maxlength="200" /></label>
          <button class="primary">连接并安装</button>
          <p class="bad">{{ addErr }}</p>
        </form>
        <div v-if="job" class="card">
          <b>安装进度：</b>{{ job.status === 'failed' ? '安装失败' : STEP[job.step] }}
          <p v-if="job.status === 'failed'" class="bad">{{ job.error }}</p>
          <p v-if="job.status === 'done'" class="okc">探针已上线并收到首次心跳。</p>
        </div>
      </section>

      <section v-show="tab === 'history'">
        <div class="bar">
          <input v-model="ev.node" placeholder="节点 ID" size="8" />
          <input v-model="ev.start" type="datetime-local" /> 至 <input v-model="ev.end" type="datetime-local" />
          <button @click="evPage = 1; loadEvents()">查询</button>
          <button class="danger" @click="delPicked">删除选中</button>
        </div>
        <table>
          <thead><tr><th></th><th>事件</th><th>节点</th><th>IP</th><th>判定离线</th><th>最后心跳</th><th>恢复时间</th><th>持续时长</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="e in evs" :key="e.id">
              <td><input type="checkbox" v-model="picked" :value="e.id" :disabled="e.status === 'open'" /></td>
              <td>#{{ e.id }}</td><td>{{ e.node_name }} (ID {{ e.node_id }})</td><td>{{ e.node_ip }}</td><td>{{ fmt(e.offline_at) }}</td>
              <td>{{ fmt(e.last_heartbeat) }}</td><td>{{ fmt(e.recovered_at) }}</td><td>{{ dur(e.duration) }}</td><td>{{ evState[e.status] }}</td>
            </tr>
            <tr v-if="!evs.length"><td colspan="9" class="muted">暂无记录</td></tr>
          </tbody>
        </table>
        <div class="bar"><button :disabled="evPage <= 1" @click="evPage--; loadEvents()">上一页</button> 第 {{ evPage }} 页，共 {{ evTotal }} 条
          <button :disabled="evPage * SIZE >= evTotal" @click="evPage++; loadEvents()">下一页</button></div>
        <div class="bar"><b>清理历史：</b><input v-model="ev.clearBefore" type="datetime-local" /><button class="danger" @click="clearOld">清理此时间之前的记录</button>
          <span class="muted">默认永久保留；故障中的事件不会被清理</span></div>
      </section>
    </main>

    <div v-if="sel" class="modal" @click.self="sel = null">
      <div class="card">
        <h2>{{ sel.name }} <span class="tag" :class="sel.status">{{ ST[sel.status] }}</span></h2>
        <p>IP {{ sel.ip }}:{{ sel.ssh_port }} · 最近心跳 {{ fmt(sel.last_heartbeat) }} · 添加于 {{ fmt(sel.created_at) }}</p>
        <p v-if="sel.install_error" class="bad">{{ sel.install_error }}</p>
        <label>名称<input v-model="edit.name" maxlength="64" /></label>
        <label>备注<input v-model="edit.note" maxlength="200" /></label>
        <button class="primary" @click="saveNode">保存</button>
        <hr />
        <label>SSH root 密码（重新安装/卸载时需要，不会保存）<input v-model="actPw" type="password" autocomplete="new-password" /></label>
        <button @click="act('reinstall')">重新安装探针</button> <button @click="act('uninstall')">卸载探针</button>
        <button class="danger" @click="delNode">删除节点</button>
        <p class="bad">{{ selMsg }}</p>
        <b>最近故障</b>
        <ul><li v-for="e in sel.events" :key="e.id">{{ fmt(e.offline_at) }} · {{ evState[e.status] }} · {{ dur(e.duration) }}</li><li v-if="!sel.events.length" class="muted">无</li></ul>
        <button @click="sel = null">关闭</button>
      </div>
    </div>
  </div>
</template>

<style>
body{margin:0;font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f7fa;color:#1f2937}
header{display:flex;gap:16px;align-items:center;padding:10px 24px;background:#fff;border-bottom:1px solid #e5e7eb}header nav{flex:1}
main{max-width:1200px;margin:16px auto;padding:0 16px}
button,input,select{font:inherit;padding:6px 10px;border:1px solid #d1d5db;border-radius:6px;background:#fff}
button{cursor:pointer}button.on,button.primary{background:#2563eb;color:#fff;border-color:#2563eb}button.danger{color:#dc2626;border-color:#fca5a5}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin:12px 0}.card label{display:block;margin:8px 0}.card input{display:block;width:100%;box-sizing:border-box}
.login{display:grid;place-items:center;height:100vh}.login .card{width:320px}.login input,.login button{width:100%;margin:6px 0;box-sizing:border-box}
.cards{display:flex;gap:12px}.stat{flex:1;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px}.stat b{display:block;font-size:28px}
.stat.off{border-color:#fca5a5;background:#fef2f2;color:#b91c1c}.stat.ok b{color:#16a34a}.stat.warn b{color:#d97706}
.bar{display:flex;gap:8px;align-items:center;margin:12px 0}table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #f0f2f5}th{background:#f9fafb}tbody tr{cursor:pointer}tr.offline{background:#fef2f2}
.tag{padding:2px 8px;border-radius:10px;font-size:12px;background:#e5e7eb}.tag.online{background:#dcfce7;color:#166534}.tag.offline{background:#dc2626;color:#fff}.tag.install_failed{background:#fef3c7;color:#92400e}
.muted{color:#9ca3af}.bad{color:#dc2626}.okc{color:#16a34a}.modal{position:fixed;inset:0;background:#0006;display:grid;place-items:center}.modal .card{width:560px;max-height:85vh;overflow:auto}
</style>
