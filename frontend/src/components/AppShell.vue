<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { state, refresh, executions, notify } from '../store/workbench'
import ProjectSwitcher from './ProjectSwitcher.vue'
import WorkflowList from './WorkflowList.vue'
const route = useRoute(), router = useRouter()
const projectNavigation = [['knowledge', '◇', '项目知识'], ['proposals', '▤', '项目提案'], ['design', '✧', '需求设计'], ['engineering', '⌘', '工程工作区']]
const projectTool = computed(() => typeof route.params.projectId === 'string')
const validationArea = computed(() => route.path === '/projects' || route.path.startsWith('/workflows/'))
const landing = computed(() => route.path === '/projects')
const completed = computed(() => state.snapshot?.workflow.state?.completed_stages?.length || 0)
const latest = computed(() => executions.value[0]?.document)
const pending = computed(() => state.snapshot?.proposal && ['pending', 'validated'].includes(state.snapshot.proposal.status))
function path(page: string) { return state.flowId ? `/workflows/${state.flowId}/${page}` : '/projects' }
function projectPath(page: string) { return `/projects/${state.projectId}/${page}` }
function inProjectSection(page: string) {
  const target = projectPath(page)
  return route.path === target || route.path.startsWith(target + '/')
}
function focusMain() { document.getElementById('main')?.focus() }
async function reload() {
  if (state.busy) return
  await refresh()
  const id = String(route.params.id || '')
  if (id && !state.flows.some(flow => flow.id === id)) await router.push(state.flowId ? path('overview') : '/projects')
  if (state.error) notify(state.error, 'error')
}
</script>

<template>
  <a class="skip-link" href="#main" @click.prevent="focusMain">跳到主要内容</a>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-icon">知</span><span>工程知识工作台<small>ENGINEERING KNOWLEDGE</small></span></div>
      <div class="workspace-label">本地验证空间 <span class="live-dot"></span></div>
      <nav class="workbench-nav" aria-label="工作台导航">
        <RouterLink v-for="[page, icon, label] in (state.projectId ? projectNavigation : [])" :key="page"
          :to="projectPath(page)" class="nav-item" :class="{ active: inProjectSection(page) }"
          :aria-current="inProjectSection(page) ? 'location' : undefined" :title="label">
          <span class="nav-icon" aria-hidden="true">{{ icon }}</span><span>{{ label }}</span>
        </RouterLink>
        <RouterLink to="/projects" class="nav-item" :class="{ active: validationArea }"
          :aria-current="validationArea ? 'location' : undefined" title="验证流程">
          <span class="nav-icon" aria-hidden="true">▦</span><span>验证流程</span>
        </RouterLink>
      </nav>
      <div class="sidebar-bottom"><div class="mode-badge"><span class="mode-dot"></span>知识协作平台</div><p>逐阶段运行 · 明确审阅<br>所有确认均由你操作</p><a href="/docs" target="_blank" rel="noopener">接口文档 ↗</a></div>
    </aside>
    <div class="app-body">
      <header class="topbar"><ProjectSwitcher/><div class="topbar-actions"><span class="connection" :class="state.connected ? 'ok' : state.initialized ? 'error' : ''"><span class="connection-dot"></span>{{ state.loading ? '读取中' : state.connected ? '服务已连接' : '未连接服务' }}</span><button class="button secondary compact" :disabled="state.busy || state.loading" aria-label="刷新" @click="reload">↻ <span>刷新</span></button></div></header>
      <main id="main" tabindex="-1">
        <div v-if="landing" class="page-heading"><div><p class="eyebrow">SDLC 验证工作区</p><h1>验证流程</h1><p class="page-description">选择一个流程，查看阶段产物、审阅变更、运行测试并追溯依据。</p></div><span class="version-badge">本地工作台</span></div>
        <div v-if="state.error" class="error-banner" role="alert">{{ state.error }}</div>
        <template v-if="!projectTool">
          <section class="metrics" aria-label="验证概览"><article class="metric-card"><div><p class="metric-title">当前项目验证流程</p><p class="metric-value">{{ state.flows.length }}<small>个</small></p></div><span class="metric-icon">▦</span></article><article class="metric-card"><div><p class="metric-title">当前流程已放行阶段</p><p class="metric-value">{{ completed }}<small>/ 4</small></p></div><span class="metric-icon green">✓</span></article><article class="metric-card"><div><p class="metric-title">当前待审提案</p><p class="metric-value">{{ pending ? 1 : 0 }}<small>份</small></p></div><span class="metric-icon amber">▤</span></article><article class="metric-card"><div><p class="metric-title">最近真实测试</p><p class="metric-value" style="font-size:19px">{{ latest ? latest.passed ? '通过' : '未通过' : '未执行' }}</p></div><span class="metric-icon purple">⌘</span></article></section>
          <div class="workspace-grid"><WorkflowList/><section class="flow-workspace" aria-label="当前流程工作区"><RouterView/></section></div>
        </template>
        <section v-else class="project-tool-workspace" aria-label="项目内容"><RouterView/></section>
        <footer class="page-footer"><span>知识有版本 · 变更有依据 · 确认可追溯</span><span>审核署名用于本地记录，不等同于登录认证</span></footer>
      </main>
    </div>
  </div>
</template>

<style scoped>
@media (max-width: 700px) {
  .app-shell { display: block; }
  .sidebar { display: block; position: static; width: 100%; padding: 6px 10px; }
  .sidebar .brand { display: none; }
  .sidebar .workbench-nav { display: flex; gap: 2px; margin: 0; overflow-x: auto; }
  .workbench-nav .nav-item { flex: 0 0 auto; width: auto; margin: 0; padding: 9px 10px; font-size: 12px; white-space: nowrap; }
  .workbench-nav .nav-item.active { box-shadow: inset 0 -2px #6db5da; }
  .workbench-nav .nav-icon { display: none; }
}
</style>
