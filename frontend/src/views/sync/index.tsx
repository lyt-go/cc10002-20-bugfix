import { useCallback, useEffect, useMemo, useState } from 'react'

import { downloadExport, request } from '@/api/client'

const ENDPOINT = '/api/sync'
const columns = [
  "通道编号",
  "来源表名",
  "目标表名",
  "同步方式",
  "延迟秒数",
  "并发度",
  "断点续传",
  "最后同步时间"
]
const statuses = [
  "草稿",
  "通道审批中",
  "已启用",
  "已停用"
]
// 单向状态机：每个状态只亮出允许的下一步动作，已停用为终态、不可重新启用。
const NEXT_ACTION: Record<string, { action: string; label: string; needReason: boolean }> = {
  "草稿": { action: "提交通道审批", label: "提交通道审批", needReason: false },
  "通道审批中": { action: "启用通道", label: "启用通道", needReason: true },
  "已启用": { action: "停用通道", label: "停用通道", needReason: true },
}
const OPERATOR = '值班管理员'

interface SyncRow {
  [key: string]: string | number | null | undefined
  id: number
  status: string
  version: number
}

interface AuditSnapshot {
  transition_id: string
  before_state: string
  after_state: string
  operator: string
  reason: string
  source_version: number
  recorded_at: string
}

interface ActionResponse {
  ok: boolean
  conflict?: boolean
  message: string
  entry?: SyncRow
  audit?: AuditSnapshot
}

interface ExportSnapshot {
  total: number
  downloaded_at: string
  download_count: number
  files: Array<{ filename: string; record_count: number }>
}

export default function SyncView() {
  const [rows, setRows] = useState<SyncRow[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [statusFilter, setStatusFilter] = useState('')
  const [stats, setStats] = useState<Record<string, number>>({ total: 0, enabled: 0, approving: 0 })
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const buildQuery = useCallback((extra: Record<string, string> = {}) => {
    const merged = { ...filters, ...extra }
    return new URLSearchParams(Object.entries(merged).filter(([, v]) => v !== '')).toString()
  }, [filters])

  const reload = useCallback(async () => {
    setErrorMessage('')
    try {
      const query = buildQuery(statusFilter ? { status: statusFilter } : {})
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('同步通道列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)

      // 统计卡片与列表同源，分别按状态取总数，避免用当前页估算。
      const counts = await Promise.all(statuses.map((status) =>
        request(`${ENDPOINT}?${new URLSearchParams({ status, size: '1' })}`)
          .then((res) => res.json())
          .then((page) => Number(page.total ?? 0))
      ))
      setStats({
        total: counts.reduce((sum, count) => sum + count, 0),
        approving: counts[1],
        enabled: counts[2],
      })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '同步通道列表读取失败')
    }
  }, [buildQuery, statusFilter])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: SyncRow, needReason: boolean) {
    setErrorMessage('')
    setInfoMessage('')
    let reason = action
    if (needReason) {
      const input = window.prompt(`请填写「${action}」的变更原因（将写入变更审计）`, action)
      if (input === null) {
        return
      }
      reason = input.trim() || action
    }
    try {
      // expected_version 带上当前行版本，后端据此做并发仲裁，只接受一次有效启停。
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({
          values: { action, operator: OPERATOR, reason, expected_version: row.version },
        }),
      })
      const payload = (await response.json()) as ActionResponse
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '同步通道动作未生效，请稍后重试')
      }
      if (payload.audit) {
        const audit = payload.audit
        setInfoMessage(
          `${payload.message}；审计 ${audit.transition_id}：${audit.before_state} → ` +
          `${audit.after_state}（操作人：${audit.operator}，版本 v${audit.source_version}）`
        )
      } else {
        setInfoMessage(payload.message)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '同步通道操作失败')
    }
  }

  async function exportList() {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const payload = (await downloadExport(
        `${ENDPOINT}/export?${buildQuery(statusFilter ? { status: statusFilter } : {})}`
      )) as ExportSnapshot
      const file = payload.files[0]
      setInfoMessage(
        `已导出 ${file.filename}，共 ${payload.total} 条；下载时间 ${payload.downloaded_at}，` +
        `该内容第 ${payload.download_count} 次下载（相同内容只生成一份结果）`
      )
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '同步通道导出失败')
    }
  }

  const statCards = [
    { label: '同步通道总数', value: stats.total },
    { label: '已启用通道', value: stats.enabled },
    { label: '通道审批中', value: stats.approving },
  ]

  return (
    <section className="page" data-module="sync">
      <header className="page-head">
        <div>
          <h2>同步通道管理</h2>
          <p className="page-desc">管理跨系统或跨环境的数据同步通道，配置同步方式和断点续传策略。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('同步通道登记入口尚未接入审批流')}>
            登记同步通道
          </button>
          <button className="btn" type="button" onClick={() => void exportList()}>
            导出同步通道清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {statCards.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>状态</span>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="">全部状态</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => { setFilters({}); setStatusFilter('') }}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>版本</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const next = NEXT_ACTION[row.status]
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td>{row.status}</td>
                <td>v{row.version}</td>
                <td className="row-actions">
                  {next ? (
                    <button
                      className="link"
                      type="button"
                      onClick={() => void runAction(next.action, row, next.needReason)}
                    >
                      {next.label}
                    </button>
                  ) : (
                    <span>终态，不可再流转</span>
                  )}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无同步通道数据，可先登记同步通道</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条同步通道记录</span>
        {infoMessage && <span className="info-text">{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
