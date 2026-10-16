import { createApp } from 'vue'
import App from './App.vue'
import Terminal from './Terminal.vue'
// 终端在独立标签页打开：/#/term/<节点ID>
createApp(location.hash.startsWith('#/term/') ? Terminal : App).mount('#app')
