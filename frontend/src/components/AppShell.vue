<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { state, refresh, openWorkflowForm, executions, notify } from '../store/workbench'
import ProjectSwitcher from './ProjectSwitcher.vue'
import WorkflowList from './WorkflowList.vue'
const route = useRoute(), router = useRouter()
const navigation = [['overview', '▦', '验证工作区'], ['review', '▤', '变更审阅'], ['execution', '⌘', '代码与测试'], ['trace', '◇', '知识追溯'], ['history', '◷', '操作记录']]
const projectNavigation = [['knowledge', '◇', '项目知识'], ['proposals', '▤', '项目提案'], ['design', '✧', '需求设计'], ['engineering', '⌘', '工程工作区']]
const projectTool = computed(() => /\/projects\/[^/]+\/(design|engineering)$/.test(route.path))
const completed = computed(() => state.snapshot?.workflow.state?.completed_stages?.length || 0)
const latest = computed(() => executions.value[0]?.document)
const pending = computed(() => state.snapshot?.proposal && ['pending', 'validated'].includes(state.snapshot.proposal.status))
function path(page: string) { return state.flowId ? `/workflows/${state.flowId}/${page}` : '/projects' }
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
    <aside class="sidebar" aria-label="工作台导航">
      <RouterLink class="brand" to="/projects"><span class="brand-icon">知</span><span>工程知识工作台<small>ENGINEERING KNOWLEDGE</small></span></RouterLink>
      <div class="workspace-label">本地验证空间 <span class="live-dot"></span></div>
      <nav>
        <template v-if="state.projectId"><RouterLink v-for="[page, icon, label] in projectNavigation" :key="page" class="nav-item" :class="{ active: route.path.includes('/' + page) }" :title="label" :to="`/projects/${state.projectId}/${page}`"><span class="nav-icon">{{ icon }}</span><span>{{ label }}</span></RouterLink></template>
        <p v-if="!state.flowId" class="section-label">选择验证流程后查看四阶段内容</p>
        <RouterLink v-for="[page, icon, label] in navigation" :key="page" :to="path(page)" class="nav-item" :aria-disabled="!state.flowId" @click="!state.flowId && $event.preventDefault()" :class="{ active: route.path.endsWith('/' + page) || (page === 'overview' && route.path === '/projects') }" :title="label"><span class="nav-icon">{{ icon }}</span><span>{{ label }}</span><span v-if="page === 'review' && pending" class="nav-count">1</span></RouterLink>
      </nav>
      <div class="sidebar-bottom"><div class="mode-badge"><span class="mode-dot"></span>知识协作平台</div><p>逐阶段运行 · 明确审阅<br>所有确认均由你操作</p><a href="/docs" target="_blank" rel="noopener">接口文档 ↗</a></div>
    </aside>
    <div class="app-body">
      <header class="topbar"><ProjectSwitcher/><div class="topbar-actions"><span class="connection" :class="state.connected ? 'ok' : state.initialized ? 'error' : ''"><span class="connection-dot"></span>{{ state.loading ? '读取中' : state.connected ? '服务已连接' : '未连接服务' }}</span><button class="button secondary compact" :disabled="state.busy || state.loading" @click="reload">↻ <span>刷新</span></button><button v-if="!projectTool" class="button primary compact" :disabled="state.busy" @click="openWorkflowForm">＋ 新建验证流程</button></div></header>
      <main id="main" tabindex="-1">
        <div class="page-heading"><div><p class="eyebrow">SDLC 验证工作区</p><h1>让每一步研发都有据可查</h1><p class="page-description">从需求到设计、工程与测试，在同一工作区审阅产物、确认变更和追溯依据。</p></div><span class="version-badge">本地工作台</span></div>
        <div class="reference-notice"><span class="notice-symbol">i</span><p><strong>知识、设计与工程验证。</strong>需求设计支持可配置模型服务，生成结果须明确审阅确认；工程快照与隔离执行分别记录。原有订单和选课教学流程继续使用注册适配器。</p><span class="future-mode">模型服务 · 按配置启用</span></div>
        <div v-if="state.error" class="error-banner" role="alert">{{ state.error }}</div>
        <nav v-if="state.projectId" class="inline-actions project-shortcuts" aria-label="项目入口"><RouterLink v-for="[page, , label] in projectNavigation" :key="page" class="button secondary" :to="`/projects/${state.projectId}/${page}`">{{ label }}</RouterLink><RouterLink v-if="state.flowId" class="button secondary" :to="`/workflows/${state.flowId}/overview`">当前验证流程</RouterLink></nav>
        <template v-if="!projectTool">
          <section class="metrics" aria-label="验证概览"><article class="metric-card"><div><p class="metric-title">当前项目验证流程</p><p class="metric-value">{{ state.flows.length }}<small>个</small></p></div><span class="metric-icon">▦</span></article><article class="metric-card"><div><p class="metric-title">当前流程已放行阶段</p><p class="metric-value">{{ completed }}<small>/ 4</small></p></div><span class="metric-icon green">✓</span></article><article class="metric-card"><div><p class="metric-title">当前待审提案</p><p class="metric-value">{{ pending ? 1 : 0 }}<small>份</small></p></div><span class="metric-icon amber">▤</span></article><article class="metric-card"><div><p class="metric-title">最近真实测试</p><p class="metric-value" style="font-size:19px">{{ latest ? latest.passed ? '通过' : '未通过' : '未执行' }}</p></div><span class="metric-icon purple">⌘</span></article></section>
          <div class="workspace-grid"><WorkflowList/><section class="flow-workspace" aria-label="当前流程工作区"><RouterView/></section></div>
        </template>
        <section v-else class="project-tool-workspace" aria-label="项目设计与工程工作区"><RouterView/></section>
        <footer class="page-footer"><span>知识有版本 · 变更有依据 · 确认可追溯</span><span>审核署名用于本地记录，不等同于登录认证</span></footer>
      </main>
    </div>
  </div>
</template>
