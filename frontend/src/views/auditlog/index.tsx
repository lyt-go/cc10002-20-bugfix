import { useCallback, useEffect, useMemo, useState } from 'react'

import { downloadExport, request } from '@/api/client'

const ENDPOINT = '/api/auditlog'
const columns = [
  "记录编号",
  "操作表名",
  "变更类型",
  "变更前值",
  "变更后值",
  "操作人",
  "操作时间",
  "变更原因"
]
const statuses = [
  "草稿",
  "已记录",
  "已复核"
]
// 审计单向状态机：只有当前状态允许的下一步动作可点，已复核为终态。
const NEXT_ACTION: Record<string, string> = {
  "草稿": "确认记录",
  "已记录": "提交复核",
}

interface AuditRow {
  [key: string]: string | number | null | undefined
  id: number
  status: string
  version: number
  transition_id?: string
  before_state?: string
  after_state?: string
  operator?: string
  reason?: string
  source_version?: number
  recorded_at?: string
}

interface ActionResponse {
  ok: boolean
  message: string
  entry?: AuditRow
}

interface ExportSnapshot {
  total: number
  downloaded_at: string
  download_count: number
  files: Array<{ filename: string; record_count: number }>
}

export default function AuditlogView() {
  const [rows, setRows] = useState<AuditRow[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [statusFilter, setStatusFilter] = useState('')
  const [stats, setStats] = useState<Record<string, number>>({ total: 0, reviewed: 0 })
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
        throw new Error('变更记录列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)

      const [all, reviewed] = await Promise.all([
        request(`${ENDPOINT}?${new URLSearchParams({ size: '1' })}`).then((res) => res.json()),
        request(`${ENDPOINT}?${new URLSearchParams({ status: '已复核', size: '1' })}`).then((res) => res.json()),
      ])
      setStats({ total: Number(all.total ?? 0), reviewed: Number(reviewed.total ?? 0) })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '变更审计列表读取失败')
    }
  }, [buildQuery, statusFilter])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: AuditRow) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = (await response.json()) as ActionResponse
      // 业务拒绝（前后值相同、重复操作、非法跳转）后端以 ok:false 返回，必须提示而不是当成功。
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '变更审计动作未生效，请稍后重试')
      }
      setInfoMessage(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '变更审计操作失败')
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
      setErrorMessage(error instanceof Error ? error.message : '变更审计导出失败')
    }
  }

  const statCards = [
    { label: '变更记录总数', value: stats.total },
    { label: '已复核记录', value: stats.reviewed },
  ]

  return (
    <section className="page" data-module="auditlog">
      <header className="page-head">
        <div>
          <h2>变更审计管理</h2>
          <p className="page-desc">记录所有表结构、数据和质量规则的变更历史，支持回溯和合规审计。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('变更记录登记入口尚未接入审批流')}>
            登记变更记录
          </button>
          <button className="btn" type="button" onClick={() => void exportList()}>
            导出变更审计清单
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
            <th>流转编号</th>
            <th>事务版本</th>
            <th>记录时间</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row.status}</td>
              <td>{row.transition_id ?? '—'}</td>
              <td>{row.source_version !== undefined ? `v${row.source_version}` : '—'}</td>
              <td>{row.recorded_at ?? row['操作时间'] ?? '—'}</td>
              <td className="row-actions">
                {NEXT_ACTION[row.status] ? (
                  <button
                    className="link"
                    type="button"
                    onClick={() => void runAction(NEXT_ACTION[row.status], row)}
                  >
                    {NEXT_ACTION[row.status]}
                  </button>
                ) : (
                  <span>终态，不可再流转</span>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 5}>暂无变更审计数据，可先登记变更记录</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条变更审计记录</span>
        {infoMessage && <span className="info-text">{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
