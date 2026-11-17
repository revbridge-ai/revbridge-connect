import { useI18n } from '../i18n'
import { PageHeader } from '../components/Layout'
import { ButtonLink } from '../components/Button'

export function NotFoundPage() {
  const { t } = useI18n()
  return (
    <>
      <PageHeader title={t('nf.title')} description={t('nf.body')} />
      <ButtonLink to="/" variant="primary">
        {t('nf.action')}
      </ButtonLink>
    </>
  )
}
