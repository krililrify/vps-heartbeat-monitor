<script setup>
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
import { api } from './api.js'

const ST = { pending: '等待首次心跳', installing: '安装中', online: '在线', offline: '离线', install_failed: '安装失败' }
const STEP = { waiting: '等待连接', ssh: '正在验证 SSH', installing: '正在安装探针', starting: '正在启动服务', heartbeat: '正在等待首次心跳', done: '安装成功' }
const me = ref(false), tab = ref('nodes'), err = ref('')
const lf = reactive({ username: '', password: '' }), loginErr = ref('')
const nodes = ref([]), total = ref(0), counts = reactive({ total: 0, online: 0, offline: 0, installing: 0, install_failed: 0, pending: 0 })
const q = ref(''), statusF = ref(''), vendorF = ref(''), page = ref(1), SIZE = 20
const vendors = ref([]), pwMap = reactive({}), showPw = ref(true)

const fmt = t => t ? new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }) : '-'
const dur = s => { if (s == null) return '-'; s = Math.round(s); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + '天' : '') + (h ? h + '时' : '') + (m ? m + '分' : '') + (s % 60) + '秒' }

// ---- 提示 / 复制 ----
const toastMsg = ref(''), toastBad = ref(false); let toastT = null
function toast(m, bad = false) { toastMsg.value = m; toastBad.value = bad; clearTimeout(toastT); toastT = setTimeout(() => toastMsg.value = '', 1800) }
async function copy(text, label) {
  if (text == null || text === '') return false
  let ok = false
  try { await navigator.clipboard.writeText(text); ok = true } catch {
    const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select()
    try { ok = document.execCommand('copy') } catch {}
    t.remove()
  }
  if (label) toast(ok ? label + '已复制' : '复制失败，请手动复制', !ok)
  return ok
}

// ---- 轮询：串行（上一个请求结束后才排下一个），登出/卸载时停止 ----
let timer = null, ctrl = null, running = false, stopped = true
async function poll() {
  if (stopped || running) return
  running = true; ctrl = new AbortController()
  try {
    const d = await api(`/nodes?q=${encodeURIComponent(q.value)}&status=${statusF.value}&vendor=${encodeURIComponent(vendorF.value)}&page=${page.value}&size=${SIZE}`, { signal: ctrl.signal })
    nodes.value = d.items; total.value = d.total; Object.assign(counts, d.counts); err.value = ''
    loadPasswords(d.items)
  } catch (e) { if (e.name !== 'AbortError') err.value = e.message }
  finally { running = false; if (!stopped) timer = setTimeout(poll, 5000) }
}
// 密码不在每 5 秒的轮询里返回；只对尚未取得的节点按需批量获取
async function loadPasswords(items) {
  const ids = items.filter(n => n.has_password && !(n.id in pwMap)).map(n => n.id)
  if (!ids.length) return
  try { Object.assign(pwMap, await api('/passwords?ids=' + ids.join(','))) } catch {}
}
async function loadVendors() { try { vendors.value = (await api('/vendors')).items } catch {} }
function start() { stopped = false; clearTimeout(timer); loadVendors(); poll() }
function stop() { stopped = true; clearTimeout(timer); ctrl && ctrl.abort() }
function refresh() { clearTimeout(timer); if (!running) poll(); else timer = setTimeout(poll, 300) }
function onUnauth() { stop(); me.value = false }

async function login() {
  try { await api('/auth/login', { method: 'POST', body: { ...lf } }); lf.password = ''; me.value = true; loginErr.value = ''; start() }
  catch (e) { loginErr.value = e.message }
}
async function logout() { stop(); try { await api('/auth/logout', { method: 'POST' }) } catch {} me.value = false; Object.keys(pwMap).forEach(k => delete pwMap[k]) }

// ---- 添加 VPS ----
const form = reactive({ name: '', ip: '', ssh_port: 22, vendor: '', password: '', save_password: true, note: '' })
const job = ref(null), addErr = ref(''), jobTimer = { t: null }
async function submitAdd() {
  addErr.value = ''; job.value = null
  try {
    const r = await api('/nodes', { method: 'POST', body: { ...form, ssh_port: Number(form.ssh_port) } })
    form.password = ''; loadVendors(); trackJob(r.job_id)
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

// ---- 管理窗口（点齿轮）----
const sel = ref(null), edit = reactive({ name: '', vendor: '', note: '', password: '' }), selMsg = ref(''), manualCmd = ref('')
async function openNode(n) {
  sel.value = await api('/nodes/' + n.id); selMsg.value = ''; manualCmd.value = ''
  Object.assign(edit, { name: sel.value.name, vendor: sel.value.vendor, note: sel.value.note, password: pwMap[n.id] || '' })
}
async function saveNode() {
  try {
    const body = { name: edit.name, vendor: edit.vendor, note: edit.note }
    if (edit.password !== (pwMap[sel.value.id] || '')) body.password = edit.password
    await api('/nodes/' + sel.value.id, { method: 'PATCH', body })
    if ('password' in body) { if (body.password) pwMap[sel.value.id] = body.password; else delete pwMap[sel.value.id] }
    selMsg.value = '已保存'; loadVendors(); refresh()
  } catch (e) { selMsg.value = e.message }
}
async function act(kind) {
  if (kind === 'uninstall' && !confirm('确认卸载该节点上的探针？卸载后节点将停止监控。')) return
  try {
    const r = await api(`/nodes/${sel.value.id}/${kind}`, { method: 'POST', body: { password: edit.password || null } })
    sel.value = null; tab.value = 'add'; trackJob(r.job_id)
  } catch (e) { selMsg.value = e.message }
}
// 手动安装：生成命令并自动复制；命令可在 VPS 上重复执行
async function manualInstall() {
  if (sel.value.status === 'online' && !confirm('该节点当前在线。生成手动安装命令会让它现有探针的 Token 失效，直到你在 VPS 上执行新命令。继续？')) return
  try {
    const r = await api(`/nodes/${sel.value.id}/manual-install`, { method: 'POST' })
    manualCmd.value = r.command; selMsg.value = ''
    if (!(await copy(r.command, '安装命令'))) selMsg.value = '请点击下方「复制命令」按钮复制'
    refresh()
  } catch (e) { selMsg.value = e.message }
}
async function delNode() {
  if (!confirm(`确认删除节点「${sel.value.name}」？\n（故障历史不会被删除）`)) return
  await api(`/nodes/${sel.value.id}?confirm=true`, { method: 'DELETE' }); delete pwMap[sel.value.id]; sel.value = null; refresh()
}
const openTerm = n => window.open('/#/term/' + n.id, '_blank', 'noopener')

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
onBeforeUnmount(() => { stop(); clearTimeout(jobTimer.t); clearTimeout(toastT); window.removeEventListener('hbm-unauth', onUnauth) })
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
          <select v-model="vendorF" @change="page = 1; refresh()"><option value="">全部厂商</option><option v-for="v in vendors" :key="v" :value="v">{{ v }}</option></select>
          <button @click="refresh">手动刷新</button>
          <button @click="showPw = !showPw">{{ showPw ? '隐藏密码' : '显示密码' }}</button>
          <span class="muted">每 5 秒自动刷新 · 点击 IP / 密码可复制</span>
        </div>
        <div class="wrap">
          <table>
            <thead><tr><th>管理</th><th>名称</th><th>IP</th><th>厂商</th><th>状态</th><th>最近心跳</th><th>状态变化</th><th>离线时长</th><th>备注</th><th>添加时间</th><th>root 密码</th><th>终端</th></tr></thead>
            <tbody>
              <tr v-for="n in nodes" :key="n.id" :class="n.status">
                <td><button class="icon" title="管理" @click="openNode(n)">
                  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button></td>
                <td class="nm">{{ n.name }}</td>
                <td><span class="copy" title="点击复制" @click="copy(n.ip, 'IP ')">{{ n.ip }}</span></td>
                <td>{{ n.vendor || '-' }}</td>
                <td><span class="tag" :class="n.status" :title="n.install_error || ''">{{ ST[n.status] }}</span></td>
                <td>{{ fmt(n.last_heartbeat) }}</td><td>{{ fmt(n.status_changed) }}</td><td>{{ dur(n.offline_seconds) }}</td>
                <td class="note" :title="n.note">{{ n.note }}</td><td>{{ fmt(n.created_at) }}</td>
                <td>
                  <span v-if="n.has_password" class="copy mono" title="点击复制" @click="copy(pwMap[n.id], '密码')">{{ pwMap[n.id] == null ? '…' : showPw ? pwMap[n.id] : '••••••••' }}</span>
                  <span v-else class="muted">未保存</span>
                </td>
                <td><button class="small" :disabled="n.status !== 'online'" :title="n.status === 'online' ? '新标签页打开 SSH 终端' : '节点在线时才能连接'" @click="openTerm(n)">终端</button></td>
              </tr>
              <tr v-if="!nodes.length"><td colspan="12" class="muted">暂无节点</td></tr>
            </tbody>
          </table>
        </div>
        <div class="bar"><button :disabled="page <= 1" @click="page--; refresh()">上一页</button> 第 {{ page }} 页 / 共 {{ Math.max(1, Math.ceil(total / SIZE)) }} 页
          <button :disabled="page * SIZE >= total" @click="page++; refresh()">下一页</button></div>
      </section>

      <section v-show="tab === 'add'">
        <form class="card narrow" @submit.prevent="submitAdd">
          <h2>添加 VPS（SSH 用户固定为 root）</h2>
          <label>节点名称 *<input v-model="form.name" maxlength="64" required /></label>
          <label>VPS IP 地址 *<input v-model="form.ip" required placeholder="仅支持公网 IP" /></label>
          <label>SSH 端口<input v-model="form.ssh_port" type="number" min="1" max="65535" /></label>
          <label>厂商<input v-model="form.vendor" maxlength="32" list="vendor-list" placeholder="如 阿里云、Linode" /></label>
          <datalist id="vendor-list"><option v-for="v in vendors" :key="v" :value="v" /></datalist>
          <div v-if="vendors.length" class="chips"><span v-for="v in vendors" :key="v" class="chip" :class="{ on: form.vendor === v }" @click="form.vendor = v">{{ v }}</span></div>
          <label>SSH root 密码 *<input v-model="form.password" type="text" autocomplete="off" required /></label>
          <label class="chk"><input v-model="form.save_password" type="checkbox" /> 保存密码（加密存储，列表中可点击复制，终端自动登录）</label>
          <label>备注<input v-model="form.note" maxlength="200" /></label>
          <button class="primary">连接并安装</button>
          <p class="bad">{{ addErr }}</p>
        </form>
        <div v-if="job" class="card narrow">
          <b>安装进度：</b>{{ job.status === 'failed' ? '安装失败' : STEP[job.step] }}
          <p v-if="job.status === 'failed'" class="bad">{{ job.error }}</p>
          <p v-if="job.status === 'done'" class="okc">任务完成。</p>
        </div>
      </section>

      <section v-show="tab === 'history'">
        <div class="bar">
          <input v-model="ev.node" placeholder="节点 ID" size="8" />
          <input v-model="ev.start" type="datetime-local" /> 至 <input v-model="ev.end" type="datetime-local" />
          <button @click="evPage = 1; loadEvents()">查询</button>
          <button class="danger" @click="delPicked">删除选中</button>
        </div>
        <div class="wrap">
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
        </div>
        <div class="bar"><button :disabled="evPage <= 1" @click="evPage--; loadEvents()">上一页</button> 第 {{ evPage }} 页，共 {{ evTotal }} 条
          <button :disabled="evPage * SIZE >= evTotal" @click="evPage++; loadEvents()">下一页</button></div>
        <div class="bar"><b>清理历史：</b><input v-model="ev.clearBefore" type="datetime-local" /><button class="danger" @click="clearOld">清理此时间之前的记录</button>
          <span class="muted">默认永久保留；故障中的事件不会被清理</span></div>
      </section>
    </main>

    <div v-if="sel" class="modal" @click.self="sel = null">
      <div class="card">
        <h2>{{ sel.name }} <span class="tag" :class="sel.status">{{ ST[sel.status] }}</span></h2>
        <p class="muted">IP {{ sel.ip }}:{{ sel.ssh_port }} · 最近心跳 {{ fmt(sel.last_heartbeat) }} · 添加于 {{ fmt(sel.created_at) }}</p>
        <p v-if="sel.install_error" class="bad">{{ sel.install_error }}</p>
        <label>名称<input v-model="edit.name" maxlength="64" /></label>
        <label>厂商<input v-model="edit.vendor" maxlength="32" list="vendor-list2" /></label>
        <datalist id="vendor-list2"><option v-for="v in vendors" :key="v" :value="v" /></datalist>
        <label>备注<input v-model="edit.note" maxlength="200" /></label>
        <label>root 密码（加密保存；清空并保存即删除）<input v-model="edit.password" type="text" autocomplete="off" /></label>
        <button class="primary" @click="saveNode">保存</button>
        <hr />
        <button @click="act('reinstall')">重新安装探针</button> <button @click="manualInstall">手动安装</button> <button @click="act('uninstall')">卸载探针</button>
        <button class="danger" @click="delNode">删除节点</button>
        <p v-if="sel.status === 'install_failed'" class="hint">自动安装失败：{{ sel.install_error }}<br />可点「手动安装」，把复制的命令粘贴到该 VPS 的 SSH 里执行（可重复执行，不会冲突）。</p>
        <div v-if="manualCmd" class="cmd">
          <textarea readonly rows="3" :value="manualCmd" @focus="$event.target.select()"></textarea>
          <button class="primary" @click="copy(manualCmd, '命令')">复制命令</button>
          <span class="muted"> 用 root 在 VPS 上执行，成功后约 30 秒显示在线</span>
        </div>
        <p :class="selMsg === '已保存' ? 'okc' : 'bad'">{{ selMsg }}</p>
        <b>最近故障</b>
        <ul><li v-for="e in sel.events" :key="e.id">{{ fmt(e.offline_at) }} · {{ evState[e.status] }} · {{ dur(e.duration) }}</li><li v-if="!sel.events.length" class="muted">无</li></ul>
        <button @click="sel = null">关闭</button>
      </div>
    </div>
    <div v-if="toastMsg" class="toast" :class="{ bad: toastBad }">{{ toastMsg }}</div>
  </div>
</template>

<style>
body{margin:0;font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f7f9fc;color:#1f2937}
header{display:flex;gap:16px;align-items:center;padding:10px 24px;background:#fff;border-bottom:1px solid #e5e7eb}header nav{flex:1}
main{max-width:1500px;margin:16px auto;padding:0 16px}
button,input,select,textarea{font:inherit;padding:6px 10px;border:1px solid #d1d5db;border-radius:6px;background:#fff}
button{cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}
button.on,button.primary{background:#2563eb;color:#fff;border-color:#2563eb}button.danger{color:#dc2626;border-color:#fca5a5}
button.icon{padding:4px 6px;line-height:0;color:#4b5563}button.small{padding:2px 10px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin:12px 0}.card label{display:block;margin:8px 0}.card input{display:block;width:100%;box-sizing:border-box}
.card.narrow{max-width:560px}.card label.chk{display:flex;gap:8px;align-items:center}.card label.chk input{width:auto}
.login{display:grid;place-items:center;height:100vh}.login .card{width:320px}.login input,.login button{width:100%;margin:6px 0;box-sizing:border-box}
.cards{display:flex;gap:12px}.stat{flex:1;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px}.stat b{display:block;font-size:28px}
.stat.off{border-color:#fca5a5;background:#fef2f2;color:#b91c1c}.stat.ok b{color:#16a34a}.stat.warn b{color:#d97706}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0}
.wrap{overflow-x:auto;border-radius:8px}
table{width:100%;border-collapse:collapse;background:#fff;white-space:nowrap}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #f0f2f5}th{background:#f9fafb}tr.offline{background:#fef2f2}
td.nm{font-weight:600}td.note{max-width:160px;overflow:hidden;text-overflow:ellipsis}
.copy{cursor:pointer;border-bottom:1px dashed #9ca3af}.copy:hover{color:#2563eb;border-color:#2563eb}.mono{font-family:Menlo,Consolas,monospace}
.tag{padding:2px 8px;border-radius:10px;font-size:12px;background:#e5e7eb}.tag.online{background:#dcfce7;color:#166534}.tag.offline{background:#dc2626;color:#fff}.tag.install_failed{background:#fef3c7;color:#92400e}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:-2px 0 8px}.chip{padding:2px 10px;border:1px solid #d1d5db;border-radius:12px;cursor:pointer;font-size:12px}.chip.on,.chip:hover{border-color:#2563eb;color:#2563eb}
.hint{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:6px;padding:8px 10px}
.cmd textarea{display:block;width:100%;box-sizing:border-box;margin-bottom:6px;font-family:Menlo,Consolas,monospace;font-size:12px}
.muted{color:#9ca3af}.bad{color:#dc2626}.okc{color:#16a34a}
.modal{position:fixed;inset:0;background:#0003;display:grid;place-items:center}.modal .card{width:600px;max-height:85vh;overflow:auto}
.toast{position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:#fff;color:#166534;border:1px solid #bbf7d0;box-shadow:0 4px 14px #0002;padding:8px 18px;border-radius:20px}.toast.bad{color:#b91c1c;border-color:#fecaca;background:#fef2f2}
</style>
