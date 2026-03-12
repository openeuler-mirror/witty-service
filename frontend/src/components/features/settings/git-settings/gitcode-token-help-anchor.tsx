import { Trans, useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";

export function GitCodeTokenHelpAnchor() {
  const { t } = useTranslation();

  return (
    <p data-testid="gitcode-token-help-anchor" className="text-xs">
      <Trans
        i18nKey={I18nKey.GITCODE$TOKEN_HELP_TEXT}
        components={[
          <a
            key="gitcode-token-help-anchor-link"
            aria-label={t(I18nKey.GIT$GITCODE_TOKEN_HELP_LINK)}
            href="https://gitcode.com/setting/token-classic/create"
            target="_blank"
            className="underline underline-offset-2"
            rel="noopener noreferrer"
          />,
          <a
            key="gitcode-token-help-anchor-link-2"
            aria-label={t(I18nKey.GIT$GITCODE_TOKEN_SEE_MORE_LINK)}
            href="https://docs.gitcode.com/docs/help/home/user_center/security_management/user_pat"
            target="_blank"
            className="underline underline-offset-2"
            rel="noopener noreferrer"
          />,
        ]}
      />
    </p>
  );
}
