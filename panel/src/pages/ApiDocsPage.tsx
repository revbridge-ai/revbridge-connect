import { useMemo, useState } from 'react'
import { BookOpen, Search } from 'lucide-react'
import { useI18n } from '../i18n'
import { API_BASE, request } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { PageHeader, Section } from '../components/Layout'
import { EmptyState, ErrorState, SkeletonRows } from '../components/Feedback'
import { CodeBlock } from '../components/Copy'
import { TextInput } from '../components/Form'

// API reference built from the running API's own OpenAPI document (GET /openapi.json, public),
// so it always lists exactly the endpoints this installation serves.

interface Schema {
  $ref?: string
  type?: string
  format?: string
  enum?: unknown[]
  default?: unknown
  example?: unknown
  items?: Schema
  properties?: Record<string, Schema>
  required?: string[]
  anyOf?: Schema[]
  oneOf?: Schema[]
  allOf?: Schema[]
  description?: string
  title?: string
}

interface Parameter {
  name: string
  in: 'path' | 'query' | 'header' | 'cookie'
  required?: boolean
  description?: string
  schema?: Schema
}

interface Operation {
  tags?: string[]
  summary?: string
  description?: string
  parameters?: Parameter[]
  requestBody?: { content?: Record<string, { schema?: Schema }> }
  security?: unknown[]
}

interface OpenApi {
  info?: { version?: string }
  tags?: { name: string }[]
  paths: Record<string, Record<string, Operation | unknown>>
  components?: { schemas?: Record<string, Schema> }
}

interface Endpoint {
  key: string
  method: string
  path: string
  tag: string
  summary: string
  op: Operation
}

const METHODS = ['get', 'post', 'put', 'patch', 'delete']
const METHOD_STYLE: Record<string, string> = {
  GET: 'bg-info/10 text-info-text',
  POST: 'bg-success/10 text-success-text',
  PUT: 'bg-warning/15 text-warning-text',
  PATCH: 'bg-warning/15 text-warning-text',
  DELETE: 'bg-danger/10 text-danger-text',
}

/** Public base URL: the panel's origin + /api (or an absolute VITE_API_BASE). */
function baseUrl(): string {
  return /^https?:\/\//.test(API_BASE) ? API_BASE : `${window.location.origin}${API_BASE}`
}

function resolve(doc: OpenApi, schema: Schema | undefined, depth = 0): Schema {
  let current = schema ?? {}
  while (current.$ref && depth < 8) {
    const name = current.$ref.split('/').pop() ?? ''
    current = doc.components?.schemas?.[name] ?? {}
    depth += 1
  }
  return current
}

function typeLabel(doc: OpenApi, schema: Schema | undefined): string {
  const s = resolve(doc, schema)
  const union = s.anyOf ?? s.oneOf
  if (union) {
    const parts = union.map((item) => typeLabel(doc, item)).filter((label) => label !== 'null')
    return [...new Set(parts)].join(' | ') || 'any'
  }
  if (s.enum) return s.enum.map((value) => JSON.stringify(value)).join(' | ')
  if (s.type === 'array') return `${typeLabel(doc, s.items)}[]`
  if (s.type === 'object' || s.properties) return 'object'
  return s.format ? `${s.type ?? 'any'} (${s.format})` : (s.type ?? 'any')
}

/** Small example value for a request body: required fields only, two levels deep. */
function sample(doc: OpenApi, schema: Schema | undefined, depth = 0): unknown {
  const s = resolve(doc, schema)
  if (s.example !== undefined) return s.example
  if (s.default !== undefined && s.default !== null) return s.default
  const union = s.anyOf ?? s.oneOf
  if (union) {
    const first = union.find((item) => resolve(doc, item).type !== 'null')
    return sample(doc, first, depth)
  }
  if (s.enum?.length) return s.enum[0]
  if (s.type === 'array') return depth > 2 ? [] : [sample(doc, s.items, depth + 1)]
  if (s.type === 'integer' || s.type === 'number') return 0
  if (s.type === 'boolean') return false
  if (s.type === 'string') return s.format === 'email' ? 'name@example.com' : 'string'
  if (s.properties) {
    if (depth > 2) return {}
    const names = s.required?.length ? s.required : Object.keys(s.properties).slice(0, 4)
    return Object.fromEntries(names.filter((n) => s.properties?.[n]).map((n) => [n, sample(doc, s.properties?.[n], depth + 1)]))
  }
  return null
}

function bodySchema(op: Operation): Schema | undefined {
  return op.requestBody?.content?.['application/json']?.schema
}

function curl(doc: OpenApi, e: Endpoint): string {
  const url = `${baseUrl()}${e.path.replace(/\{([^}]+)\}/g, '<$1>')}`
  const lines = [`curl -X ${e.method} "${url}" \\`]
  const needsAuth = (e.op.security?.length ?? 0) > 0
  if (needsAuth) lines.push(`  -H "Authorization: Bearer $CONNECT_API_KEY" \\`)
  const body = bodySchema(e.op)
  if (body) {
    lines.push(`  -H "Content-Type: application/json" \\`)
    lines.push(`  -d '${JSON.stringify(sample(doc, body), null, 2).replace(/\n/g, '\n  ')}'`)
  } else {
    lines[lines.length - 1] = lines[lines.length - 1].replace(/ \\$/, '')
  }
  return lines.join('\n')
}

/** The API titles often start with an emoji and the descriptions are Markdown: show them as plain text. */
function plain(text: string | undefined): string {
  return (text ?? '')
    .replace(/^[^\p{L}\p{N}/{]+/u, '')
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
    .trim()
}

function collect(doc: OpenApi): { tag: string; items: Endpoint[] }[] {
  const items: Endpoint[] = []
  for (const [path, methods] of Object.entries(doc.paths)) {
    for (const method of METHODS) {
      const op = methods[method] as Operation | undefined
      if (!op || typeof op !== 'object') continue
      items.push({
        key: `${method} ${path}`,
        method: method.toUpperCase(),
        path,
        tag: op.tags?.[0] ?? 'other',
        summary: plain(op.summary),
        op,
      })
    }
  }
  const order = (doc.tags ?? []).map((t) => t.name)
  const tags = [...new Set([...order, ...items.map((i) => i.tag)])]
  return tags.map((tag) => ({ tag, items: items.filter((i) => i.tag === tag) })).filter((g) => g.items.length > 0)
}

function EndpointRow({ doc, e }: { doc: OpenApi; e: Endpoint }) {
  const { t } = useI18n()
  const params = e.op.parameters ?? []
  const body = resolve(doc, bodySchema(e.op))
  const fields = Object.entries(body.properties ?? {})
  const needsAuth = (e.op.security?.length ?? 0) > 0
  const description = plain(e.op.description)

  return (
    <details className="group border-t border-line/70 first:border-t-0">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 hover:bg-subtle/60">
        <span className={`inline-flex w-16 justify-center rounded-md px-2 py-0.5 font-mono text-2xs font-semibold ${METHOD_STYLE[e.method] ?? 'bg-subtle text-muted'}`}>
          {e.method}
        </span>
        <code className="break-all font-mono text-[0.8125rem] text-ink">{e.path}</code>
        <span className="min-w-0 text-[0.8125rem] text-muted">{e.summary}</span>
      </summary>
      <div className="flex flex-col gap-4 px-4 pb-5 pt-1">
        <p className="text-xs text-muted">{needsAuth ? t('docs.needsAuth') : t('docs.public')}</p>
        {description && (
          <p className="max-h-56 max-w-3xl overflow-y-auto whitespace-pre-line text-[0.8125rem] leading-relaxed text-muted">{description}</p>
        )}
        {params.length > 0 && (
          <div>
            <h4 className="mb-1.5 text-xs font-medium text-ink">{t('docs.params')}</h4>
            <ul className="flex flex-col gap-1">
              {params.map((p) => (
                <li key={`${p.in}-${p.name}`} className="text-[0.8125rem] text-muted">
                  <code className="font-mono text-ink">{p.name}</code> · {p.in} · {typeLabel(doc, p.schema)}
                  {p.required ? ` · ${t('docs.required')}` : ''}
                  {p.description ? ` · ${plain(p.description)}` : ''}
                </li>
              ))}
            </ul>
          </div>
        )}
        {fields.length > 0 && (
          <div>
            <h4 className="mb-1.5 text-xs font-medium text-ink">{t('docs.body')}</h4>
            <ul className="flex flex-col gap-1">
              {fields.map(([name, schema]) => (
                <li key={name} className="text-[0.8125rem] text-muted">
                  <code className="font-mono text-ink">{name}</code> · {typeLabel(doc, schema)}
                  {body.required?.includes(name) ? ` · ${t('docs.required')}` : ''}
                  {resolve(doc, schema).description ? ` · ${plain(resolve(doc, schema).description)}` : ''}
                </li>
              ))}
            </ul>
          </div>
        )}
        <CodeBlock code={curl(doc, e)} label={t('docs.example')} />
      </div>
    </details>
  )
}

export function ApiDocsPage() {
  const { t } = useI18n()
  const [query, setQuery] = useState('')
  const spec = useAsync((signal) => request<OpenApi>('/openapi.json', { auth: false, signal }), [])

  const groups = useMemo(() => (spec.data ? collect(spec.data) : []), [spec.data])
  const needle = query.trim().toLowerCase()
  const shown = useMemo(
    () =>
      groups
        .map((g) => ({
          ...g,
          items: needle ? g.items.filter((i) => `${i.method} ${i.path} ${i.summary} ${i.tag}`.toLowerCase().includes(needle)) : g.items,
        }))
        .filter((g) => g.items.length > 0),
    [groups, needle],
  )
  const total = groups.reduce((n, g) => n + g.items.length, 0)

  return (
    <>
      <PageHeader title={t('docs.title')} description={t('docs.intro', { n: total })} />
      <div className="flex flex-col gap-8">
        <Section id="docs-start" title={t('docs.start.title')} description={t('docs.start.body')}>
          <CodeBlock
            label={t('docs.example')}
            code={`curl ${baseUrl()}/api-keys/me \\\n  -H "Authorization: Bearer $CONNECT_API_KEY"`}
          />
          <p className="text-[0.8125rem] text-muted">
            <a className="font-medium text-primary-text underline-offset-2 hover:underline" href={`${baseUrl()}/openapi.json`} target="_blank" rel="noreferrer">
              {t('docs.raw')}
            </a>
          </p>
        </Section>

        {spec.loading && <SkeletonRows rows={6} cols={2} />}
        {spec.error != null && <ErrorState error={spec.error} onRetry={spec.reload} what={t('docs.what')} />}

        {spec.data && (
          <>
            <div className="relative max-w-md">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" strokeWidth={1.5} aria-hidden="true" />
              <TextInput
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t('docs.search')}
                aria-label={t('docs.search')}
                className="pl-9"
              />
            </div>
            {shown.length === 0 && <EmptyState icon={<BookOpen className="h-5 w-5" strokeWidth={1.5} />} title={t('docs.empty')} compact />}
            {shown.map((g) => (
              <Section key={g.tag} id={`docs-${g.tag}`} title={g.tag}>
                <div className="overflow-hidden rounded-xl border border-line">
                  {g.items.map((e) => (
                    <EndpointRow key={e.key} doc={spec.data as OpenApi} e={e} />
                  ))}
                </div>
              </Section>
            ))}
          </>
        )}
      </div>
    </>
  )
}
