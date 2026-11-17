import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/pt'
import { API_BASE } from '../lib/api'
import { CodeBlock } from '../components/Copy'
import { Section } from '../components/Layout'

/** Public send URL: the panel's origin + /api (or an absolute VITE_API_BASE). */
export function sendUrl(): string {
  const base = /^https?:\/\//.test(API_BASE) ? API_BASE : `${window.location.origin}${API_BASE}`
  return `${base}/send`
}

/** curl command for the first send. The sample values come from the dictionaries (send.sample.*). */
export function curlExample(domain: string, t: (key: MessageKey) => string): string {
  // One field per line with compact values: it fits on screen and stays valid JSON
  // (every value goes through JSON.stringify, which takes care of escaping).
  const fields: [string, unknown][] = [
    ['from', { address: `${t('send.sample.sender')}@${domain}`, name: domain }],
    ['personalizations', [{ to: [{ address: t('send.sample.recipient') }] }]],
    ['subject', t('send.sample.subject')],
    ['content', [{ type: 'text/html', value: `<p>${t('send.sample.body')}</p>` }]],
  ]
  const body = fields
    .map(([key, value]) => `    ${JSON.stringify(key)}: ${JSON.stringify(value).replace(/([,:])(?=["{[])/g, '$1 ')}`)
    .join(',\n')
  return [
    `curl -X POST ${sendUrl()} \\`,
    `  -H "Authorization: Bearer $CONNECT_API_KEY" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{`,
    body,
    `  }'`,
  ].join('\n')
}

export function SendExample({ domain }: { domain: string }) {
  const { t } = useI18n()
  return (
    <Section id="send" title={t('send.title')} description={t('send.body', { domain })}>
      <CodeBlock code={curlExample(domain, t)} label={t('send.example')} />
      <p className="text-[0.8125rem] leading-relaxed text-muted">{t('send.response')}</p>
    </Section>
  )
}
