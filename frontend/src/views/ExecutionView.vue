<script setup lang="ts">
import { computed } from 'vue'
import { artifacts, executions } from '../store/workbench'
import { dateTime } from '../lib/display'
import CodeBlock from '../components/CodeBlock.vue'
import StatusPill from '../components/StatusPill.vue'
const code=computed(()=>artifacts.value.find(item=>item.key==='code')?.entity)
const test=computed(()=>artifacts.value.find(item=>item.key==='test')?.entity)
function log(document:Record<string,any>):string {return `${Array.isArray(document.command) ? document.command.join(' ') : document.command || ''}\n\n${document.stdout || ''}${document.stderr || ''}${document.error ? '\n'+document.error : ''}`}
</script>
<template><div class="section-stack"><CodeBlock v-if="code?.attributes?.extensions?.source" :title="code.attributes.path || '实现源码'" :source="code.attributes.extensions.source"/><div v-else class="info-box">实现代码尚未形成。运行代码阶段并审阅、提交产物后显示。</div><CodeBlock v-if="test?.attributes?.extensions?.test_source" title="验收测试源码" :source="test.attributes.extensions.test_source"/><div v-else class="info-box">测试源码尚未形成。运行测试阶段并审阅、提交产物后显示。</div><article v-for="execution in executions" :key="execution.id" class="execution-card"><div class="execution-top"><StatusPill :status="execution.document?.outcome || (execution.document?.passed ? 'passed':'failed')"/><strong>真实参考测试执行</strong><small>{{ dateTime(execution.recorded_at,true) }}</small></div><div class="execution-grid"><span><b>{{ execution.document?.tests_run ?? '—' }}</b>实际运行测试</span><span><b>{{ execution.document?.returncode ?? '—' }}</b>进程退出码</span><span><b>{{ execution.document?.duration_ms ?? '—' }} <small>ms</small></b>执行耗时</span></div><div class="hash-row">代码摘要 {{ execution.code_sha256 || '—' }}<br>测试摘要 {{ execution.test_sha256 || '—' }}</div><details><summary>查看执行日志与固定命令</summary><CodeBlock title="真实执行输出" :source="log(execution.document || {})" log/><p v-if="execution.document?.output_truncated" class="section-label">日志达到保留上限，以上为截断内容。</p></details></article><div v-if="!executions.length" class="info-box warning">尚无真实执行记录。测试阶段的参考 Agent 会调用可信执行器，此处显示实际执行结果。</div><p class="section-label">仅执行内置受信任参考代码，不会运行上传脚本或修改业务仓库。</p></div></template>
