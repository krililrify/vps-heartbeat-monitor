<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { api } from './api.js'

const id = location.hash.split('/')[2]
const el = ref(null), state = ref('connecting'), msg = ref('正在连接…'), pw = ref('')
let term, fit, ws, ro

onMounted(async () => {
  document.title = '终端 #' + id
  try { document.title = (await api('/nodes/' + id)).name + ' · 终端' } catch {}
  term = new Terminal({ cursorBlink: true, fontSize: 14, scrollback: 5000, fontFamily: 'Menlo, Consolas, "Courier New", monospace', theme: { background: '#ffffff', foreground: '#1f2937', cursor: '#2563eb', selectionBackground: '#bfdbfe',
    black: '#1f2937', red: '#dc2626', green: '#16a34a', yellow: '#b45309', blue: '#2563eb', magenta: '#9333ea', cyan: '#0e7490', white: '#6b7280',
    brightBlack: '#6b7280', brightRed: '#ef4444', brightGreen: '#22c55e', brightYellow: '#d97706', brightBlue: '#3b82f6', brightMagenta: '#a855f7', brightCyan: '#06b6d4', brightWhite: '#374151' } })
  fit = new FitAddon(); term.loadAddon(fit); term.open(el.value); fit.fit()
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/nodes/${id}/terminal`)
  ws.binaryType = 'arraybuffer'
  ws.onopen = () => ws.send(JSON.stringify({ type: 'init', cols: term.cols, rows: term.rows }))
  ws.onmessage = e => {
    if (typeof e.data !== 'string') return term.write(new Uint8Array(e.data))
    const m = JSON.parse(e.data)
    if (m.type === 'need_password') state.value = 'password'
    else if (m.type === 'status') msg.value = m.msg
    else if (m.type === 'ready') { state.value = 'ready'; term.focus() }
    else if (m.type === 'error') { state.value = 'error'; msg.value = m.msg }
  }
  ws.onclose = () => {
    if (state.value === 'ready') { state.value = 'closed'; msg.value = '连接已断开（可刷新页面重新连接）' }
    else if (state.value === 'connecting') { state.value = 'error'; msg.value = '连接失败：请确认已登录且节点在线' }
  }
  const enc = new TextEncoder()
  term.onData(d => { if (ws.readyState === 1 && state.value === 'ready') ws.send(enc.encode(d)) })
  term.onResize(({ cols, rows }) => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'resize', cols, rows })) })
  ro = new ResizeObserver(() => fit.fit()); ro.observe(el.value)
})
function sendPw() { ws.send(JSON.stringify({ type: 'password', password: pw.value })); pw.value = ''; state.value = 'connecting'; msg.value = '正在连接…' }
onBeforeUnmount(() => { ro && ro.disconnect(); ws && ws.close(); term && term.dispose() })
</script>

<template>
  <div class="t-page">
    <div ref="el" class="t-term"></div>
    <div v-if="state !== 'ready'" class="t-mask">
      <form v-if="state === 'password'" class="t-box" @submit.prevent="sendPw">
        <b>该节点未保存密码</b><p>输入 root 密码（仅用于本次连接，不会保存）</p>
        <input v-model="pw" type="password" autocomplete="new-password" autofocus /><button>连接</button>
      </form>
      <div v-else class="t-box"><b :style="{ color: state === 'connecting' ? '#1f2937' : '#dc2626' }">{{ msg }}</b><p v-if="state === 'error' || state === 'closed'"><a href="/">返回面板</a></p></div>
    </div>
  </div>
</template>

<style>
html,body{margin:0;height:100%;background:#fff}
.t-page{position:fixed;inset:0;background:#fff}.t-term{position:absolute;inset:8px}
.t-mask{position:absolute;inset:0;display:grid;place-items:center;background:#ffffffdd;color:#1f2937;font:14px -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.t-box{background:#fff;border:1px solid #e5e7eb;box-shadow:0 4px 18px #0001;border-radius:10px;padding:20px 24px;min-width:280px;text-align:center}.t-box a{color:#2563eb}
.t-box input{display:block;width:100%;box-sizing:border-box;margin:8px 0;padding:8px;border-radius:6px;border:1px solid #d1d5db;background:#fff;color:#1f2937}
.t-box button{padding:6px 18px;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}
</style>
