import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react'
import { Link, type LinkProps } from 'react-router-dom'
import { Loader2 } from 'lucide-react'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'outline'
export type ButtonSize = 'md' | 'sm' | 'auth'

const base =
  'inline-flex shrink-0 select-none items-center justify-center gap-2 whitespace-nowrap font-medium transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-55'

const variants: Record<ButtonVariant, string> = {
  primary: 'bg-primary text-white hover:bg-primary-hover active:bg-primary-hover',
  secondary: 'bg-subtle text-ink hover:bg-line active:bg-line',
  outline: 'border border-line bg-surface text-ink hover:bg-subtle',
  ghost: 'text-muted hover:bg-subtle hover:text-ink',
  danger: 'bg-destructive text-white hover:bg-destructive/90',
}

const sizes: Record<ButtonSize, string> = {
  md: 'h-11 rounded-xl px-5 text-sm',
  sm: 'h-9 rounded-xl px-3.5 text-[0.8125rem]',
  // Sign-in screen: 48px tall with 10px corners.
  auth: 'h-12 rounded-[10px] px-6 text-[0.9375rem]',
}

export function buttonClasses(variant: ButtonVariant = 'primary', size: ButtonSize = 'md', extra = ''): string {
  return `${base} ${variants[variant]} ${sizes[size]} ${extra}`.trim()
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  icon?: ReactNode
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'primary', size = 'md', loading = false, icon, className = '', children, disabled, type = 'button', ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={buttonClasses(variant, size, className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : icon}
      {children}
    </button>
  )
})

interface ButtonLinkProps extends LinkProps {
  variant?: ButtonVariant
  size?: ButtonSize
  icon?: ReactNode
}

export function ButtonLink({ variant = 'secondary', size = 'md', icon, className = '', children, ...rest }: ButtonLinkProps) {
  return (
    <Link className={buttonClasses(variant, size, className)} {...rest}>
      {icon}
      {children}
    </Link>
  )
}

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string
  size?: 'md' | 'sm'
}

/** Icon-only button: the label becomes the aria-label and the native tooltip. */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, size = 'md', className = '', children, type = 'button', ...rest },
  ref,
) {
  const dim = size === 'md' ? 'h-11 w-11' : 'h-9 w-9'
  return (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      title={label}
      className={`inline-flex shrink-0 items-center justify-center rounded-xl text-muted transition-colors duration-150 hover:bg-subtle hover:text-ink disabled:cursor-not-allowed disabled:opacity-55 ${dim} ${className}`}
      {...rest}
    >
      {children}
    </button>
  )
})
