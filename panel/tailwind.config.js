// Colors come from CSS variables (src/index.css), so light/dark theming lives in one place.
const token = (name) => `rgb(var(--c-${name}) / <alpha-value>)`

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        canvas: token('canvas'),
        surface: token('surface'),
        subtle: token('subtle'),
        ink: token('ink'),
        muted: token('muted'),
        line: token('line'),
        field: token('field'),
        primary: token('primary'),
        'primary-hover': token('primary-hover'),
        'primary-text': token('primary-text'),
        accent: token('accent'),
        destructive: token('destructive'),
        success: token('success'),
        'success-text': token('success-text'),
        warning: token('warning'),
        'warning-text': token('warning-text'),
        danger: token('danger'),
        'danger-text': token('danger-text'),
        info: token('info'),
        'info-text': token('info-text'),
      },
      fontFamily: {
        sans: ['"Inter Variable"', 'Inter', 'ui-sans-serif', 'system-ui', '-apple-system', '"Segoe UI"', 'Roboto', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', '"Liberation Mono"', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
      borderRadius: {
        sm: '6px',
        md: '8px',
        lg: '10px',
        xl: '12px',
      },
      maxWidth: {
        workspace: '1200px',
      },
      boxShadow: {
        pop: '0 12px 32px -8px rgb(15 23 42 / 0.25), 0 2px 6px -2px rgb(15 23 42 / 0.12)',
      },
    },
  },
  plugins: [],
}
