export type RecordData = Record<string, any>
export interface Action { id: string; label: string; enabled: boolean; reason: string }
export interface Artifact { key: string; entity: RecordData | null }
export interface Snapshot { workflow: RecordData; task: RecordData | null; proposal: RecordData | null; proposal_kind: 'bootstrap' | 'task' | 'gate' | null; artifacts: Artifact[]; relationships: RecordData[]; executions: RecordData[]; history: RecordData[]; actions: Action[]; mode: string }
export interface Toast { id: number; message: string; level: string }
