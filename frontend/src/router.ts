import { createRouter, createWebHashHistory } from 'vue-router'
import ProjectsView from './views/ProjectsView.vue'
import WorkflowLayout from './views/WorkflowLayout.vue'
import OverviewView from './views/OverviewView.vue'
import ReviewView from './views/ReviewView.vue'
import ExecutionView from './views/ExecutionView.vue'
import TraceView from './views/TraceView.vue'
import HistoryView from './views/HistoryView.vue'
import { state } from './store/workbench'
import ProjectKnowledgeView from './views/ProjectKnowledgeView.vue'
import ProjectProposalsView from './views/ProjectProposalsView.vue'
import DesignStudioView from './views/DesignStudioView.vue'
import WorkspaceView from './views/WorkspaceView.vue'

export const router = createRouter({ history:createWebHashHistory('/workbench/'), routes:[
  { path:'/', redirect:'/projects' }, { path:'/projects', component:ProjectsView },
  { path:'/projects/:projectId/knowledge',component:ProjectKnowledgeView },
  { path:'/projects/:projectId/proposals/:proposalId?',component:ProjectProposalsView },
  { path:'/projects/:projectId/design',component:DesignStudioView },
  { path:'/projects/:projectId/engineering',component:WorkspaceView },
  { path:'/workflows/:id', component:WorkflowLayout, children:[
    { path:'', redirect:to=>`/workflows/${to.params.id}/overview` },
    { path:'overview', component:OverviewView }, { path:'review', component:ReviewView },
    { path:'execution', component:ExecutionView }, { path:'trace', component:TraceView }, { path:'history', component:HistoryView },
  ] }, { path:'/:pathMatch(.*)*', redirect:'/projects' },
] })

// 写入完成前保留当前流程，避免返回结果覆盖另一个页面。
router.beforeEach(() => !state.busy)
