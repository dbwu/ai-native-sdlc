<script lang="ts">
export type ArchitectureNode = {
  key: string
  name: string
  kind: 'domain' | 'module'
  parent_key: string | null
  description: string
  entity_id?: string | null
  entity_version?: number | null
}
</script>

<script setup lang="ts">
const props = defineProps<{
  childrenByParent: Map<string | null, ArchitectureNode[]>
  nodeVersions: Record<string, number>
  projectId: string
  parentKey: string | null
  parentKind?: ArchitectureNode['kind']
}>()
</script>

<template>
  <ul class="architecture-tree" :class="{ nested: props.parentKey }">
    <li v-for="node in childrenByParent.get(parentKey)" :key="node.key">
      <details>
        <summary>
          <strong>{{ node.name }}</strong>
          <span>{{ node.kind === 'domain' ? '领域' : parentKind === 'module' ? '子模块' : '模块' }}</span>
        </summary>
        <div class="architecture-node">
          <p v-if="node.description">{{ node.description }}</p>
          <small v-if="nodeVersions[node.key]">基线中的知识版本 v{{ nodeVersions[node.key] }}</small>
          <RouterLink v-if="node.entity_id" :to="{
            path: `/projects/${projectId}/knowledge`,
            query: { entity: node.entity_id, version: nodeVersions[node.key] ? String(nodeVersions[node.key]) : undefined },
          }">查看知识记录</RouterLink>
        </div>
        <ProjectArchitectureTree v-if="childrenByParent.get(node.key)?.length" :children-by-parent="childrenByParent"
          :node-versions="nodeVersions" :project-id="projectId" :parent-key="node.key" :parent-kind="node.kind"/>
      </details>
    </li>
  </ul>
</template>

<style scoped>
.architecture-tree { list-style: none; margin: 7px 0; padding: 0; }
.architecture-tree.nested { border-left: 1px solid var(--line); margin-inline-start: 4px; padding-inline-start: 10px; }
.architecture-tree li + li { margin-top: 5px; }
.architecture-tree summary { cursor: pointer; padding: 5px 7px; border-radius: 5px; overflow-wrap: anywhere; }
.architecture-tree summary:hover { background: var(--blue-soft); }
.architecture-tree summary span { color: var(--muted); font-size: 11px; margin-inline-start: 7px; }
.architecture-node { padding: 0 8px 4px 16px; font-size: 12px; overflow-wrap: anywhere; }
.architecture-node small, .architecture-node a { display: block; }
.architecture-node p { white-space: pre-wrap; }
</style>
