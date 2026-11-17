import { forwardRef, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from 'react'
import { ChevronDown } from 'lucide-react'
import { useI18n } from '../i18n'

/** Hint and error ids linked to the field through aria-describedby. */
export function describedBy(id: string, hint?: ReactNode, error?: ReactNode): string | undefined {
  const ids = [hint ? `${id}-hint` : '', error ? `${id}-error` : ''].filter(Boolean)
  return ids.length ? ids.join(' ') : undefined
}

interface FieldProps {
  id: string
  label: ReactNode
  hint?: ReactNode
  error?: ReactNode
  optional?: boolean
  className?: string
  children: ReactNode
}

export function Field({ id, label, hint, error, optional, className = '', children }: FieldProps) {
  const { t } = useI18n()
  return (
    <div className={`flex flex-col gap-1.5 ${className}`}>
      <label htmlFor={id} className="text-[0.8125rem] font-medium text-ink">
        {label}
        {optional && <span className="ml-1 font-normal text-muted">({t('common.optional')})</span>}
      </label>
      {children}
      {hint && !error && (
        <p id={`${id}-hint`} className="text-xs leading-relaxed text-muted">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="text-xs font-medium leading-relaxed text-danger-text">
          {error}
        </p>
      )}
    </div>
  )
}

const control =
  'w-full rounded-xl border bg-surface text-sm text-ink transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-60'

interface TextInputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean
  size?: never
}

export const TextInput = forwardRef<HTMLInputElement, TextInputProps>(function TextInput(
  { invalid, className = '', ...rest },
  ref,
) {
  return (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={`${control} h-11 px-3.5 ${invalid ? 'border-danger-text' : 'border-field'} ${className}`}
      {...rest}
    />
  )
})

interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  wrapperClassName?: string
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { className = '', wrapperClassName = '', children, ...rest },
  ref,
) {
  return (
    <div className={`relative ${wrapperClassName}`}>
      <select
        ref={ref}
        className={`${control} h-11 appearance-none border-field pl-3.5 pr-10 ${className}`}
        {...rest}
      >
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute right-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted"
        strokeWidth={1.75}
        aria-hidden="true"
      />
    </div>
  )
})

interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label: ReactNode
  description?: ReactNode
}

export function Checkbox({ label, description, id, className = '', ...rest }: CheckboxProps) {
  return (
    <label htmlFor={id} className={`flex cursor-pointer items-start gap-2.5 ${className}`}>
      <input
        id={id}
        type="checkbox"
        className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded border-field accent-primary"
        {...rest}
      />
      <span className="flex flex-col">
        <span className="text-sm text-ink">{label}</span>
        {description && <span className="text-xs text-muted">{description}</span>}
      </span>
    </label>
  )
}

interface SwitchProps {
  checked: boolean
  onChange: (checked: boolean) => void
  label: string
  /** Shows the label next to the switch; otherwise it is only for screen readers. */
  showLabel?: boolean
  disabled?: boolean
  busy?: boolean
  id?: string
}

export function Switch({ checked, onChange, label, showLabel = false, disabled, busy, id }: SwitchProps) {
  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={showLabel ? undefined : label}
      aria-busy={busy || undefined}
      disabled={disabled || busy}
      onClick={() => onChange(!checked)}
      className="group inline-flex min-h-[2.25rem] items-center gap-2.5 rounded-lg disabled:cursor-not-allowed disabled:opacity-60"
    >
      <span
        aria-hidden="true"
        className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors duration-150 ${
          checked ? 'border-primary bg-primary' : 'border-field bg-subtle'
        }`}
      >
        <span
          className={`inline-block h-3.5 w-3.5 rounded-full shadow-sm transition-transform duration-150 ${
            checked ? 'translate-x-[1.125rem] bg-white' : 'translate-x-[0.1875rem] bg-muted'
          }`}
        />
      </span>
      {showLabel && <span className="text-sm text-ink">{label}</span>}
    </button>
  )
}
