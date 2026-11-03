export class ApiError extends Error { constructor(message: string, public status: number) { super(message) } }
export async function request<T = any>(path: string, body?: unknown): Promise<T> {
  let response: Response
  try { response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', cache: 'no-store', headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }) }
  catch { throw new ApiError(body === undefined ? '无法连接服务，请确认后端已启动。' : '连接中断，请刷新核对操作状态后再继续，不要重复提交。', 0) }
  const data = await response.json().catch(() => null)
  if (!response.ok) { const detail = data?.detail; throw new ApiError(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(item => item.msg || item.message).join('；') : '请求未能完成，请刷新核对后再操作。', response.status) }
  if (data === null) throw new ApiError('服务返回了无法读取的响应。', response.status)
  return data as T
}
