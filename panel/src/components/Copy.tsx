import { useEffect, useRef, useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { useI18n } from '../i18n'

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // fall through to the legacy method below
  }
  // Plain HTTP without TLS (e.g. the server's bare IP): no Clipboard API, so use select + execCommand.
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  document.body.removeChild(area)
  return ok
}

interface CopyButtonProps {
  value: string
  /** What is being copied (e.g. "name"); the button label becomes "Copy name". */
  what?: string
  showLabel?: boolean
  size?: 'sm' | 'md'
  onCopied?: () => void
  className?: string
}

export function CopyButton({ value, what, showLabel = false, size = 'sm', onCopied, className = '' }: CopyButtonProps) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const timer = useRef<number>()

  useEffect(() => () => window.clearTimeout(timer.current), [])

  const label = what ? t('common.copyWhat', { what }) : t('common.copy')
  const handle = async () => {
    if (await copyText(value)) {
      setCopied(true)
      onCopied?.()
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => setCopied(false), 1800)
    }
  }

  const dim = size === 'md' ? 'h-11 min-w-[2.75rem] px-3' : 'h-8 min-w-[2rem] px-2'
  return (
    <button
      type="button"
      onClick={handle}
      aria-label={showLabel ? undefined : label}
      title={label}
      className={`inline-flex shrink-0 items-center justify-center gap-1.5 rounded-lg text-[0.8125rem] font-medium text-muted transition-colors duration-150 hover:bg-subtle hover:text-ink ${dim} ${className}`}
    >
      {copied ? (
        <Check className="h-4 w-4 text-success-text" strokeWidth={2} aria-hidden="true" />
      ) : (
        <Copy className="h-4 w-4" strokeWidth={1.75} aria-hidden="true" />
      )}
      {showLabel && <span>{copied ? t('common.copied') : t('common.copy')}</span>}
      <span className="sr-only" aria-live="polite">
        {copied ? t('common.copied') : ''}
      </span>
    </button>
  )
}

/** Code block with a copy button. The text shown is exactly what goes to the clipboard. */
export function CodeBlock({ code, label, className = '' }: { code: string; label: string; className?: string }) {
  return (
    <div className={`relative rounded-xl border border-line bg-subtle/70 ${className}`}>
      <div className="absolute right-2 top-2">
        <CopyButton value={code} what={label} />
      </div>
      <pre className="code overflow-x-auto p-4 pr-12 text-ink" aria-label={label} tabIndex={0}>
        <code>{code}</code>
      </pre>
    </div>
  )
}
