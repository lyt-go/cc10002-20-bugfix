/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

export interface ExportManifest {
  module: string
  total: number
  fingerprint: string
  generated_at: string
  downloaded_at: string
  download_count: number
  files: Array<{ filename: string; content_type: string; record_count: number; size_bytes: number }>
  items: Array<Record<string, unknown>>
}

/**
 * 调起清单导出：后端对相同内容只生成一份结果，这里把返回的清单落成本地文件，
 * 并把文件清单、下载时间、累计下载次数回传给页面展示，避免「下载只剩空清单」。
 */
export async function downloadExport(path: string): Promise<ExportManifest> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`导出失败：接口返回 ${response.status}`)
  }
  const payload = (await response.json()) as ExportManifest
  if (!payload.files || payload.files.length === 0) {
    throw new Error('导出结果缺少文件清单，请联系管理员核对')
  }
  const file = payload.files[0]
  const blob = new Blob([JSON.stringify(payload.items, null, 2)], {
    type: file.content_type || 'application/json',
  })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = file.filename
  anchor.click()
  URL.revokeObjectURL(url)
  return payload
}
